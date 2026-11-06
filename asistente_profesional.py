"""Lo que le contesta el bot al PROFESIONAL cuando le escribe desde su
WhatsApp de avisos (`tenants.aviso_telefono`): su agenda de hoy, mañana o la
semana con nombres, pagos para revisar, quién le debe, los mensajes sin leer,
los datos de un paciente y el link de su página.

Es SU asistente: le muestra todo lo que necesita. Lo que se cuida es quién
pregunta y de qué cuenta:
- Sólo contesta si el WhatsApp está VINCULADO: el profesional manda desde ese
  número el código que ve en su panel (`VINCULAR 123456`). Así nadie que no
  sea él (un número mal cargado, alguien que cambió de celular) ve datos de
  pacientes. Si después cambia el número en el panel, se desvincula solo.
- Cada respuesta sale de UNA cuenta (`tenant["id"]` en cada consulta); con el
  mismo número en varias cuentas, contesta por cada una vinculada, por
  separado y con el nombre arriba.

Acciones (aceptar o rechazar un comprobante, bloquear días, pausar o
reactivar la página, mandarle un mensaje a un paciente): nunca se hacen al
toque. Se guardan en `acciones_profesional` (una por número, vence en 10
minutos) y se ejecutan recién con «Sí»; antes se revisa de nuevo que el
número siga vinculado a esa cuenta. Con el número en varias cuentas, las
acciones van por el panel (no se adivina de cuál). LIBERAR / ME LO TOMO
(`cancelaciones.respuesta_del_profesional`) usan el mismo candado.

Reglas locales, sin servicios externos, como `conversation.py`. Si el texto no
es un pedido del profesional, devuelve None y el mensaje sigue como el de
cualquier paciente (así el profesional puede probar su propio bot reservando).
"""
import hashlib
import hmac
import logging
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import db

logger = logging.getLogger("asistente_profesional")

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

# Sobre el texto ya normalizado (minúsculas, sin tildes ni signos). «Ver hoy»,
# «Pagos» y «Mensajes» son también los botones del resumen de la mañana.
_HOY = re.compile(r"^(ver )?(que tengo )?(para )?hoy$|^agenda( de)? hoy$|^mi agenda$|^agenda$|^sesiones( de)? hoy$")
_MANANA = re.compile(r"^(ver )?(que tengo )?(para )?manana$|^agenda( de)? manana$|^sesiones( de)? manana$")
_SEMANA = re.compile(r"^(ver )?(esta |la )?semana$|^agenda( de la)? semana$|^que tengo (esta|la) semana$")
_PAGOS = re.compile(r"^(ver )?pagos?( para revisar| pendientes)?$|^comprobantes?$|^cobros?( de la web| web)?$")
_DEUDA = re.compile(r"^(quien|quienes) (me )?debe[n]?$|^deudas?$|^me deben$|^pendientes? de cobro$|^que me deben$")
_MENSAJES = re.compile(r"^(ver )?mensajes?( sin leer| nuevos)?$|^bandeja$")
_LIBRES = re.compile(r"^(horarios? )?libres?$|^que tengo libre$|^huecos$")
_ESPERA = re.compile(r"^(ver )?(la |mi )?lista de espera$|^(quien|quienes) (esta|estan|hay) en (la lista de )?espera$|^(en )?espera$")
_LINK = re.compile(r"^(mi )?(link|pagina|enlace)( de turnos| de reservas)?$")
_AYUDA = re.compile(r"^(ayuda|menu|opciones|comandos|hola|holis|buenas|buen dia|que podes hacer)$")
_SALDO = re.compile(r"^(?:cuanto|que) (?:me )?debe (?P<a>[a-zñ ]{2,40})$|^(?:saldo|deuda|cuenta) de (?P<b>[a-zñ ]{2,40})$")
_MES = re.compile(r"^(?:como (?:vengo|voy|me fue|me va)|resumen|balance|numeros)(?: (?:este|el|del|de este|en el))? (?P<cual>mes pasado|mes)$"
                  r"|^(?:el |este )?(?P<cual2>mes pasado|mes)$|^como (?:vengo|voy)$")
_MESES_NOMBRE = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
                 "noviembre", "diciembre"]
_PACIENTE = re.compile(r"^(paciente|buscar|busca|ficha|datos de|telefono de|cuando viene) ([a-zñ ]{2,40})$")
_VINCULAR = re.compile(r"^vincular (\d{6})$")

# Botones que van debajo de cada respuesta (ids «prof:…», ver app._interactivo).
BOTONES = [("prof:hoy", "Hoy"), ("prof:mañana", "Mañana"), ("prof:pagos", "Pagos")]


def _normal(texto: str) -> str:
    import conversation
    return conversation._normal(texto)


def _diez(numero: str | None) -> str:
    return "".join(ch for ch in numero or "" if ch.isdigit())[-10:]


def codigo_vinculo(tenant: dict) -> str | None:
    """Seis dígitos que dependen de la cuenta y del número cargado: si cambia
    el número, cambia el código. No se guarda: se recalcula."""
    numero = _diez(tenant.get("aviso_telefono"))
    if len(numero) < 10 or not tenant.get("id"):
        return None
    firma = hmac.new(config.FLASK_SECRET_KEY.encode(), f"vincular:{tenant['id']}:{numero}".encode(), hashlib.sha256)
    return f"{int(firma.hexdigest(), 16) % 1_000_000:06d}"


def vinculado(tenant: dict) -> bool:
    numero = _diez(tenant.get("aviso_telefono"))
    return len(numero) == 10 and _diez(tenant.get("aviso_verificado")) == numero


def _tz(tenant: dict) -> ZoneInfo:
    return ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")


def _pesos(v) -> str:
    return f"$ {int(v or 0):,}".replace(",", ".")


def _panel() -> str:
    return f"https://{config.DOMINIO_PRINCIPAL}/agenda" if config.DOMINIO_PRINCIPAL else "el panel"


def _cuando(inicio: str, tz: ZoneInfo, con_dia: bool = True) -> str:
    dt = datetime.fromisoformat(inicio).astimezone(tz)
    return (f"{DIAS[dt.weekday()][:3]} {dt.day:02d}/{dt.month:02d} " if con_dia else "") + f"{dt:%H:%M}"


def _tel(numero: str | None) -> str:
    return f"+{numero}" if numero else "sin teléfono"


def menu(tenant: dict) -> str:
    return (f"Hola, *{tenant['nombre']}*. Soy tu asistente de {config.PLATAFORMA_NOMBRE}. Preguntame:\n\n"
            "• *hoy* / *mañana* / *semana* — tus sesiones\n"
            "• *libres* — tus próximos horarios libres\n"
            "• *pagos* — comprobantes y devoluciones para revisar\n"
            "• *deudas* — quién te debe y cuánto\n"
            "• *mensajes* — lo que te escribieron los pacientes\n"
            "• *paciente Juan* — teléfono, próximas sesiones y deuda\n"
            "• *¿cuánto me debe Juan?* — su deuda sesión por sesión\n"
            "• *¿cómo vengo este mes?* / *mes pasado* — sesiones, cobros y cancelaciones\n"
            "• *pendientes* — lo que te pregunté y falta confirmar\n"
            "• *lista de espera* — quién espera un horario\n"
            "• *link* — tu página de turnos para compartir\n\n"
            "Y puedo hacer (siempre te pregunto antes):\n"
            "• *aceptar comprobante de Juana* / *rechazar comprobante de Juana*\n"
            "• *bloquear mañana* / *bloquear el viernes* / *bloquear del 20/12 al 5/1*\n"
            "• *pausar hasta el lunes* / *pausar* / *reactivar*\n"
            "• *avisale a Juan que llego 10 minutos tarde*\n"
            "• *contestale: …* (al último mensaje que te llegó) / *contestale a Juana: …*\n"
            "• *sumá a Juana el jueves a las 10* / *sumá a Juana todos los jueves a las 10*\n"
            "• *sumá a Martín Gómez 1155667788 el viernes a las 9* (paciente nuevo)\n"
            "• *ofrecele a Juana el jueves 10* (o *ofrecele un horario a Juana*: el próximo libre)\n"
            "• *renová a Juana* / *cortá la serie de Juana desde diciembre*\n"
            "• *pasá a Juana de los jueves 17 a los martes 18* (toda la serie)\n"
            "• *cambiá el valor de Juana a 30000* (sus próximas sesiones sin pagar)\n"
            "• *pasá a Juana del jueves al viernes 10:00*\n"
            "• *cancelá la sesión de Juana del jueves*\n"
            "• *marcá pagada la de hoy de Tomás*\n"
            "• *recordale a Juana lo que debe*\n"
            "• *agregá a Juana a la lista de espera* / *sacá a Juana de la lista de espera*\n\n"
            "También te aviso por acá de reservas, cancelaciones y pagos.")


def sin_vincular() -> str:
    """Lo único que ve un número de avisos sin vincular: ni el nombre de la cuenta."""
    return ("Para consultar tu agenda por acá, primero vinculá este WhatsApp: entrá a tu panel, en "
            "«Tu WhatsApp para avisos» está el código, y mandalo a este chat (por ejemplo *VINCULAR 123456*).")


def _linea_sesion(t: dict, tz: ZoneInfo, con_dia: bool = False) -> str:
    extras = []
    if t.get("modalidad") == "virtual":
        extras.append("virtual")
    elif t.get("sede"):
        extras.append(t["sede"])
    if t.get("tipo"):
        extras.append(t["tipo"])
    if t.get("pago_web") in ("esperando", "comprobante"):
        extras.append("pago web sin confirmar")
    elif t.get("pago") == "pagado" or t.get("pago_web") == "aprobado":
        extras.append("pagada")
    if t.get("confirmado_at"):
        extras.append("confirmó")
    nombre = f" {t['paciente_nombre']}" if t.get("paciente_nombre") else ""
    return "• " + _cuando(t["inicio"], tz, con_dia) + nombre + (f" ({', '.join(extras)})" if extras else "")


def _sesiones(tenant: dict, desde: datetime, hasta: datetime, titulo: str, con_dia: bool = False) -> str:
    tz = _tz(tenant)
    todos = db.agenda_semana(tenant["id"], desde, hasta)
    turnos = [t for t in todos if t.get("estado") == "reservado"]
    libres = [t for t in todos if t.get("estado") == "libre" and not t.get("hueco")]
    if not turnos:
        texto = f"{titulo}: no tenés sesiones."
    else:
        texto = (f"{titulo}: {len(turnos)} sesi{'ón' if len(turnos) == 1 else 'ones'}.\n\n"
                 + "\n".join(_linea_sesion(t, tz, con_dia) for t in turnos))
    if libres:
        texto += f"\n\nHorarios libres: {len(libres)} ({', '.join(_cuando(t['inicio'], tz, con_dia) for t in libres[:6])})."
    return texto


def _dia(tenant: dict, offset: int, titulo: str) -> str:
    tz = _tz(tenant)
    hoy = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=offset)
    return _sesiones(tenant, hoy, hoy + timedelta(days=1), titulo)


def _semana(tenant: dict) -> str:
    tz = _tz(tenant)
    ahora = datetime.now(tz)
    fin = (ahora.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=7 - ahora.weekday()))
    return _sesiones(tenant, ahora, fin, "Lo que queda de la semana", con_dia=True)


def _libres(tenant: dict) -> str:
    tz = _tz(tenant)
    libres = db.turnos_libres(tenant["id"], datetime.now(timezone.utc), limite=8)
    if not libres:
        return "No tenés horarios libres cargados para adelante. Los generás desde el panel (o con tu horario semanal)."
    texto = "Tus próximos horarios libres:\n\n" + "\n".join(_linea_sesion(t, tz, con_dia=True) for t in libres)
    return texto + ("\n\nTu página no está mostrando horarios: está en pausa." if tenant.get("pausado") else "")


def _de_quien(tenant: dict, pago: dict, tz: ZoneInfo) -> str:
    turnos = [t for t in (db.turno_por_id(tenant["id"], tid) for tid in pago.get("turnos") or []) if t]
    quien = next((t.get("paciente_nombre") for t in turnos if t.get("paciente_nombre")), None) or pago.get("paciente_nombre") or "Sin nombre"
    cuando = ", ".join(_cuando(t["inicio"], tz) for t in sorted(turnos, key=lambda t: t["inicio"]))
    return quien + (f" ({cuando})" if cuando else "")


def _pagos(tenant: dict) -> str:
    tz = _tz(tenant)
    pendientes = db.pagos_para_revisar(tenant["id"])
    partes = []
    for p in pendientes:
        if p.get("estado") == "comprobante":
            partes.append(f"• Comprobante de {_de_quien(tenant, p, tz)}: {_pesos(p.get('monto'))}, para validar")
        elif p.get("estado") == "aprobado_tarde":
            partes.append(f"• Pago de {_de_quien(tenant, p, tz)} que llegó tarde (el horario ya se había liberado)")
        if (p.get("a_devolver") or 0) > 0:
            partes.append(f"• Devolverle {_pesos(p['a_devolver'])} a {_de_quien(tenant, p, tz)}")
    for p in db.pagos_esperando(tenant["id"]):
        partes.append(f"• {_de_quien(tenant, p, tz)}: reservó y todavía no pagó")
    if not partes:
        return "No tenés pagos para revisar."
    return "Pagos:\n\n" + "\n".join(partes) + f"\n\nSe resuelven en {_panel()}, en «Pagos para revisar»."


def _deudas(tenant: dict) -> str:
    sin_pagar = db.pendientes_de_pago(tenant["id"], datetime.now(timezone.utc), limite=500)
    if not sin_pagar:
        return "No te deben sesiones: todas las pasadas están cobradas o marcadas «no cobra»."
    por_paciente: dict[tuple, dict] = {}
    for t in sin_pagar:
        d = por_paciente.setdefault((t.get("paciente_telefono"), t.get("ficha") or ""),
                                    {"nombre": t.get("paciente_nombre") or "Sin nombre", "tel": t.get("paciente_telefono"),
                                     "sesiones": 0, "total": 0, "sin_valor": 0})
        d["sesiones"] += 1
        d["total"] += t.get("valor") or 0
        d["sin_valor"] += 0 if t.get("valor") else 1
    total = sum(d["total"] for d in por_paciente.values())
    lineas = []
    for d in sorted(por_paciente.values(), key=lambda d: -d["total"])[:15]:
        detalle = f"{d['sesiones']} sesi{'ón' if d['sesiones'] == 1 else 'ones'}" + (f", {d['sin_valor']} sin valor" if d["sin_valor"] else "")
        lineas.append(f"• {d['nombre']}: {_pesos(d['total'])} ({detalle}) — {_tel(d['tel'])}")
    otros = len(por_paciente) - len(lineas)
    return (f"Te deben {_pesos(total)} de {len(sin_pagar)} sesi{'ón' if len(sin_pagar) == 1 else 'ones'} pasadas:\n\n"
            + "\n".join(lineas) + (f"\n• y {otros} más" if otros > 0 else "")
            + f"\n\nEl botón para recordarles está en {_panel()} → «Quién debe».")


def _saldo(tenant: dict, buscado: str) -> str:
    """Lo que debe UN paciente, sesión por sesión."""
    tz = _tz(tenant)
    encontrado = _paciente_unico(tenant, buscado)
    if isinstance(encontrado, str):
        return encontrado
    sesiones, nombre = encontrado
    clave = db.clave_de(sesiones[0])
    ahora = datetime.now(timezone.utc)
    deuda = sorted((t for t in db.pendientes_de_pago(tenant["id"], ahora, limite=500) if db.clave_de(t) == clave),
                   key=lambda t: t["inicio"])
    proximas = [t for t in sesiones if datetime.fromisoformat(t["inicio"]) > ahora and (t.get("pago") or "pendiente") == "pendiente"]
    pagadas = [t for t in sesiones if t.get("pago") == "pagado"]
    if not deuda:
        texto = f"{nombre} no te debe nada: sus sesiones pasadas están cobradas o marcadas «no cobra»."
    else:
        total = sum(t.get("valor") or 0 for t in deuda)
        sin_valor = sum(1 for t in deuda if not t.get("valor"))
        lineas = [f"• {_cuando(t['inicio'], tz)} — {_pesos(t['valor']) if t.get('valor') else 'sin valor'}"
                  + (" (faltó)" if t.get("asistencia") == "no_vino" else "") for t in deuda[:20]]
        texto = (f"{nombre} te debe {_pesos(total)} de {len(deuda)} sesi{'ón' if len(deuda) == 1 else 'ones'}"
                 + (f" ({sin_valor} sin valor cargado)" if sin_valor else "") + ":\n\n" + "\n".join(lineas)
                 + (f"\n• y {len(deuda) - 20} más" if len(deuda) > 20 else "")
                 + f"\n\nPara recordárselo: *recordale a {nombre.split()[0]} lo que debe*.")
    if pagadas:
        texto += f"\nÚltima pagada: {_cuando(pagadas[-1]['inicio'], tz)}."
    if proximas:
        a_cobrar = sum(t.get("valor") or 0 for t in proximas)
        texto += (f"\nPróximas sin pagar: {len(proximas)}" + (f" ({_pesos(a_cobrar)})" if a_cobrar else "") + ".")
    return texto


def _mes(tenant: dict, offset: int = 0) -> str:
    """Cómo viene el mes (o cómo fue el pasado): sesiones, asistencia, cobros y cancelaciones."""
    tz = _tz(tenant)
    ahora = datetime.now(tz)
    inicio = ahora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if offset:
        inicio = (inicio - timedelta(days=1)).replace(day=1)
    fin = (inicio + timedelta(days=32)).replace(day=1)
    turnos = [t for t in db.agenda_semana(tenant["id"], inicio, fin) if t.get("estado") == "reservado"]
    dadas = [t for t in turnos if datetime.fromisoformat(t["inicio"]) < ahora]
    por_venir = [t for t in turnos if datetime.fromisoformat(t["inicio"]) >= ahora]
    vinieron = sum(1 for t in dadas if t.get("asistencia") == "vino")
    faltaron = sum(1 for t in dadas if t.get("asistencia") == "no_vino")
    cobrado = sum(t.get("valor") or 0 for t in turnos if t.get("pago") == "pagado")
    deben = [t for t in dadas if (t.get("pago") or "pendiente") == "pendiente"]
    a_cobrar = sum(t.get("valor") or 0 for t in por_venir if (t.get("pago") or "pendiente") == "pendiente")
    cancel = [c for c in db.cancelaciones_desde(tenant["id"], inicio.astimezone(timezone.utc))
              if datetime.fromisoformat(str(c["created_at"])) < fin]
    de_pacientes = sum(1 for c in cancel if c.get("origen") != "profesional")
    pacientes = {db.clave_de(t) for t in turnos if t.get("paciente_telefono")}
    titulo = _MESES_NOMBRE[inicio.month - 1].capitalize() + ("" if offset else " (hasta hoy)")
    sin_marcar = len(dadas) - vinieron - faltaron
    lineas = [f"• Sesiones: {len(dadas)} dadas" + (f", {len(por_venir)} por venir" if por_venir else ""),
              f"• Asistencia: {vinieron} vinieron, {faltaron} faltaron" + (f", {sin_marcar} sin marcar" if sin_marcar else ""),
              f"• Cobrado: {_pesos(cobrado)}",
              f"• Te deben de este mes: {_pesos(sum(t.get('valor') or 0 for t in deben))} ({len(deben)} sesi{'ón' if len(deben) == 1 else 'ones'})"
              if deben else "• Te deben de este mes: nada"]
    if por_venir:
        lineas.append(f"• Por cobrar de lo que falta: {_pesos(a_cobrar)}")
    lineas.append(f"• Cancelaciones: {len(cancel)}" + (f" ({de_pacientes} de pacientes, {len(cancel) - de_pacientes} tuyas)" if cancel else ""))
    lineas.append(f"• Pacientes distintos: {len(pacientes)}")
    return f"*{titulo}*\n" + "\n".join(lineas) + ("" if offset else "\n\nPara el anterior: *mes pasado*.")


def _nombre_del_telefono(tenant: dict, telefono: str) -> str:
    try:
        return next((f.get("paciente_nombre") for f in db.fichas_del_telefono(tenant["id"], telefono) if f.get("paciente_nombre")),
                    None) or _tel(telefono)
    except Exception:
        return _tel(telefono)


def _mensajes(tenant: dict) -> str:
    tz = _tz(tenant)
    nuevos = [m for m in db.mensajes_pacientes(tenant["id"]) if not m.get("leido")]
    if not nuevos:
        return "No tenés mensajes de pacientes sin leer."
    lineas = []
    for m in nuevos[:6]:
        cuando = datetime.fromisoformat(str(m["creado"])).astimezone(tz).strftime("%d/%m %H:%M")
        texto = (m.get("texto") or f"[{m.get('tipo') or 'archivo'}]")[:160]
        lineas.append(f"• {_nombre_del_telefono(tenant, m['telefono'])} ({cuando}, {_tel(m['telefono'])}): «{texto}»")
    mas = f"\n• y {len(nuevos) - 6} más" if len(nuevos) > 6 else ""
    return (f"Mensajes sin leer ({len(nuevos)}):\n\n" + "\n".join(lineas) + mas
            + f"\n\nLos contestás desde {_panel()}, en «Mensajes», o escribiéndoles directo.")


def _paciente(tenant: dict, buscado: str) -> str:
    tz = _tz(tenant)
    ahora = datetime.now(timezone.utc)
    palabras = _normal(buscado).split()
    turnos = [t for t in db.turnos_de_paciente_por_nombre(tenant["id"], buscado)
              if all(p in _normal(t.get("paciente_nombre") or "") for p in palabras)]
    if not turnos:
        return f"No encontré a ningún paciente que se llame «{buscado}». Probá con el apellido o parte del nombre."
    fichas: dict[tuple, list[dict]] = {}
    for t in turnos:
        fichas.setdefault((t.get("paciente_telefono"), t.get("ficha") or ""), []).append(t)
    bloques = []
    for sesiones in list(fichas.values())[:3]:
        sesiones.sort(key=lambda t: t["inicio"])
        primero = sesiones[-1]
        futuras = [t for t in sesiones if datetime.fromisoformat(t["inicio"]) > ahora]
        pasadas = [t for t in sesiones if datetime.fromisoformat(t["inicio"]) <= ahora]
        debe = [t for t in pasadas if t.get("pago") == "pendiente"]
        lineas = [f"*{primero.get('paciente_nombre') or 'Sin nombre'}* — {_tel(primero.get('paciente_telefono'))}"]
        lineas.append("Próximas: " + (", ".join(_cuando(t["inicio"], tz) for t in futuras[:4]) if futuras else "ninguna"))
        if pasadas:
            lineas.append(f"Última: {_cuando(pasadas[-1]['inicio'], tz)} ({len(pasadas)} en total)")
        if debe:
            lineas.append(f"Debe {_pesos(sum(t.get('valor') or 0 for t in debe))} de {len(debe)} sesi{'ón' if len(debe) == 1 else 'ones'}")
        bloques.append("\n".join(lineas))
    otros = len(fichas) - len(bloques)
    return "\n\n".join(bloques) + (f"\n\nHay {otros} más con ese nombre: buscá con el apellido." if otros > 0 else "")


def _espera(tenant: dict) -> str:
    tz = _tz(tenant)
    lista = db.lista_de_espera(tenant["id"])
    if not lista:
        return "No tenés a nadie en lista de espera. Para anotar a alguien: *agregá a Juana a la lista de espera*."
    hoy = datetime.now(tz).date().isoformat()
    lineas = []
    for p in lista:
        extra = [f"prefiere {p['preferencia']}"] if p.get("preferencia") else []
        if p.get("created_at"):
            extra.append("desde el " + datetime.fromisoformat(str(p["created_at"])).astimezone(tz).strftime("%d/%m"))
        if p.get("origen") == "web":
            extra.append("se anotó por la web")
        if p.get("ofrecido_en") == hoy:
            extra.append("hoy ya le ofrecimos un horario")
        lineas.append(f"• {p.get('nombre')} ({_tel(p.get('telefono'))})" + (f" — {', '.join(extra)}" if extra else ""))
    return (f"Lista de espera ({len(lista)}):\n" + "\n".join(lineas)
            + "\n\nPara sacar a alguien: *sacá a Juana de la lista de espera*.")


def _link(tenant: dict) -> str:
    if not tenant.get("slug") or not config.DOMINIO_PRINCIPAL:
        return "Todavía no tenés página de turnos. Pedila a administración."
    return f"Tu página de turnos: https://{config.DOMINIO_PRINCIPAL}/{tenant['slug']}\n\nReenviá este mensaje a quien quieras."


def _pedido(texto: str):
    """La función que contesta este texto, o None si no es un pedido del profesional."""
    n = _normal(texto)
    reglas = ((_AYUDA, menu), (_HOY, lambda t: _dia(t, 0, "Hoy")), (_MANANA, lambda t: _dia(t, 1, "Mañana")),
              (_SEMANA, _semana), (_LIBRES, _libres), (_PAGOS, _pagos), (_DEUDA, _deudas),
              (_MENSAJES, _mensajes), (_ESPERA, _espera), (_LINK, _link))
    for regla, funcion in reglas:
        if regla.search(n):
            return funcion
    m = _SALDO.search(n)
    if m:
        return lambda t: _saldo(t, (m.group("a") or m.group("b")).strip())
    m = _MES.search(n)
    if m:
        pasado = "pasado" in (m.group("cual") or m.group("cual2") or "")
        return lambda t: _mes(t, -1 if pasado else 0)
    m = _PACIENTE.search(n)
    if m:
        return lambda t: _paciente(t, m.group(2).strip())
    return None


def responder(tenant: dict, texto: str) -> str | None:
    """Para UNA cuenta. None si no es un pedido del profesional (el mensaje
    sigue su camino). Sin vincular, nunca devuelve datos."""
    funcion = _pedido(texto)
    if funcion is None:
        return None
    if not vinculado(tenant):
        return sin_vincular()
    try:
        return funcion(tenant)
    except Exception:
        logger.exception("Falló el asistente del profesional (%s)", tenant.get("id"))
        return "Uh, no pude buscar eso. Probá de nuevo en un rato o mirá el panel."


def _vincular(tenants: list[dict], codigo: str, telefono: str) -> str:
    """Vincula las cuentas de este número cuyo código coincide. Compara
    contra el número que escribe, no contra el cargado: si no son el mismo,
    no hay nada que vincular."""
    hechas = []
    for t in tenants:
        esperado = codigo_vinculo(t)
        if esperado and hmac.compare_digest(esperado, codigo) and _diez(telefono) == _diez(t.get("aviso_telefono")):
            db.vincular_aviso(t["id"], t["aviso_telefono"])
            hechas.append(t)
    if not hechas:
        return "Ese código no corresponde a este WhatsApp. Copialo de tu panel, en «Tu WhatsApp para avisos»."
    return (f"Listo: este WhatsApp quedó vinculado a *{', '.join(t['nombre'] for t in hechas)}*. "
            "Desde ahora te contesto con todos los datos.\n\n" + menu(hechas[0]))


def responder_a(tenants: list[dict], texto: str, telefono: str = ""):
    """Con el mismo WhatsApp de avisos en más de una cuenta, contesta por
    cada una vinculada (con el nombre arriba), nunca mezclando datos. Devuelve
    texto con botones (`conversation.Respuesta`) o None."""
    import conversation
    m = _VINCULAR.search(_normal(texto))
    if m:
        return _vincular(tenants, m.group(1), telefono)
    propias = [t for t in tenants if vinculado(t)]
    if _pedido(texto) is None:
        if not _preparar_es_accion(texto) and not _es_respuesta(_normal(texto)) and not _PENDIENTES.search(_normal(texto)):
            return None
        if not propias:
            return sin_vincular() if _preparar_es_accion(texto) else None
        return _accion(propias, texto, telefono)
    if not propias:
        return sin_vincular()
    respuestas = [r if len(propias) == 1 else f"*{t['nombre']}*\n{r}" for t in propias if (r := responder(t, texto))]
    texto_final = "\n\n———\n\n".join(respuestas)
    if len(texto_final) > 1024:  # el cuerpo de un mensaje con botones no puede ser más largo
        return texto_final
    return conversation.Respuesta.con(texto_final, {"tipo": "botones", "cuerpo": texto_final, "botones": BOTONES})


# ---------- acciones (con confirmación) ----------

VENCE_ACCION = timedelta(minutes=10)
BOTONES_CONFIRMAR = [("prof:si", "Sí, hacelo"), ("prof:no", "No")]
_SI = re.compile(r"^(si|si hacelo|dale|ok|confirmo|confirmar|hacelo|de una)$")
_NO = re.compile(r"^(no|cancelar|cancela|dejalo|olvidalo|mejor no)$")
_COMPROBANTE = re.compile(r"^(aceptar|aceptale|acepta|validar|valida|rechazar|rechazale|rechaza)( el| la)? "
                          r"(comprobante|pago|transferencia)( de ([a-zñ ]{2,40}))?$")
_BLOQUEAR = re.compile(r"^bloquea(r|me)?( el| la| los)? (.+)$")
_PAUSAR = re.compile(r"^pausa(r|me)?( la pagina| mi pagina| los turnos)?( hasta( el)? (.+))?$")
_REACTIVAR = re.compile(r"^(reactivar|reactiva|despausar|sacar la pausa|volver a tomar turnos|activar la pagina)$")
# Sobre el texto ORIGINAL (el mensaje al paciente va tal cual lo escribió).
_AVISAR = re.compile(r"^\s*(?:avis[aá]le|dec[ií]le|mand[aá]le|escrib[ií]le)\s+a\s+(.{2,60}?)\s*(?:\s+que\s+|:\s*)(.{2,600})$",
                     re.IGNORECASE | re.DOTALL)
_MESES_CORTOS = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12}
_DIAS_SIN_TILDE = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]


_NOMBRE = r"(?P<nombre>[a-zñ]+(?: [a-zñ]+){0,3}?)"
_DIA = r"(?:hoy|ayer|manana|pasado manana|(?:el )?(?:lunes|martes|miercoles|jueves|viernes|sabado|domingo)|\d{1,2} \d{1,2}(?: \d{2,4})?)"
_REPROGRAMAR = re.compile(r"^(?:pasa|pasar|pasale|pasame|move|mover|movele|cambia|cambiar|reprograma|reprogramar)"
                          r"(?: la sesion| el turno| la)?(?: de| a)? " + _NOMBRE +
                          r"(?: (?:del|de el) (?P<desde>" + _DIA + r"))? (?:al|para el|a el|para) (?P<hasta>" + _DIA + r")"
                          r"(?: a las| a la)? (?P<h>\d{1,2})(?: (?P<m>\d{2}))?(?: hs| horas)?$")
_CANCELAR = re.compile(r"^(?:cancela|cancelar|cancelale|suspende|suspender|suspendele)(?: la sesion| el turno| la)?(?: de| a)? "
                       + _NOMBRE + r"(?: (?:del|de el|el|de) (?P<dia>" + _DIA + r"))?$")
_PAGADA = re.compile(r"^(?:marca(?:r|me)?(?: como)? (?:pagada|pagado|paga|cobrada)|cobre(?: a)?|anota(?:r)? (?:que )?pago|pago)"
                     r"(?: la sesion| la)?(?: (?:de|del) (?P<dia1>" + _DIA + r"))?(?: de| a)? " + _NOMBRE +
                     r"(?: (?:de|del) (?P<dia2>" + _DIA + r"))?$")
_RECORDAR = re.compile(r"^(?:recordale|recorda|recordarle|reclamale|cobrale)(?: (?:el pago|la deuda|lo que debe))?(?: a)? " + _NOMBRE +
                       r"(?: (?:lo que (?:me )?debe|que (?:me )?debe|la deuda|el pago|que (?:me )?pague))?$")
# Sobre el texto ORIGINAL: «contestale: …» o «contestale a Juana: …».
_CONTESTAR = re.compile(r"^\s*(?:contest[aá]le|respond[eé]le)(?:\s+a\s+(.{2,60}?))?\s*:\s*(.{2,600})$", re.IGNORECASE | re.DOTALL)


# «sumá a Juana el jueves a las 10», «agendá a Martín Gómez 1155667788 el
# viernes 10», «sumá a Juana todos los jueves a las 10 por 8 semanas». El verbo
# va sobre el texto ORIGINAL (las tildes del nombre); el resto, normalizado.
_SUMAR_VERBO = re.compile(r"^\s*(?:sum[aá](?:le|r)?|agend[aá](?:le|me|r)?|anot[aá](?:le|me|r)?|dale)"
                          r"(?:\s+(?:una\s+sesi[oó]n|un\s+turno|sesiones|turnos|sesi[oó]n|turno))?(?:\s+(?:a|para))?\s+(?P<resto>.+)$",
                          re.IGNORECASE | re.DOTALL)
_TEL = r"(?: (?P<tel>\d[\d ]{6,20}\d))?"
_HORA = r"(?: a las| a la)? (?P<h>\d{1,2})(?: (?P<m>\d{2}))?(?: hs| horas)?"
_SUMAR_UNO = re.compile(r"^" + _NOMBRE + _TEL + r"(?: (?:el|para el|para))? (?P<dia>" + _DIA + r")" + _HORA + r"$")
_SUMAR_SERIE = re.compile(r"^" + _NOMBRE + _TEL + r" (?:todos los|cada) (?P<dsem>lunes|martes|miercoles|jueves|viernes|sabado|domingo)"
                          r"(?P<medio> por medio)?" + _HORA +
                          r"(?: (?:por|durante) (?P<n>\d{1,2}) (?:semanas|sesiones|veces)| (?P<n2>\d{1,2}) (?:sesiones|veces))?$")
SESIONES_SERIE = 8      # como «renovar» del panel
MAX_SESIONES_SERIE = 26
_OFRECER = re.compile(r"^(?:ofrecele|ofrece|ofrecer|ofrecerle)(?: un horario| el horario| el turno| un turno| el hueco| un hueco)?(?: a)? "
                      + _NOMBRE + r"(?: (?:el horario |el turno |el hueco )?(?:del |de el |el |para el )?(?P<dia>" + _DIA + r")"
                      + _HORA + r")?$")
VENCE_OFRECER_MIN = 180  # la propuesta que llega con el aviso de cancelación
_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
_DESDE = r"(?: (?:desde|a partir de|a partir del)(?: el| la)? (?P<desde>" + _DIA + "|" + "|".join(_MESES) + r"|setiembre))?"
_CORTAR = re.compile(r"^(?:corta|cortar|cortale|termina|terminar|terminale|frena|frenale)"
                     r"(?: la serie| las sesiones fijas| las sesiones| los turnos fijos| los turnos)?(?: de| a)? " + _NOMBRE + _DESDE + r"$")
_VALOR = re.compile(r"^(?:cambia|cambiar|cambiale|actualiza|actualizale|pone|ponele|subi|subile|baja|bajale)"
                    r"(?: el valor| el precio| la sesion| la cuota)?(?: de| a)? " + _NOMBRE +
                    r" (?:a|en) (?P<monto>\d[\d ]{0,9})(?P<mil> mil)?(?: pesos)?" + _DESDE + r"$")
_DSEM = r"(?:lunes|martes|miercoles|jueves|viernes|sabado|domingo)"
_MOVER_SERIE = re.compile(r"^(?:pasa|pasar|pasale|move|mover|movele|cambia|cambiar|cambiale)(?: la serie| las sesiones| los turnos)?(?: de| a)? "
                          + _NOMBRE + r" (?:de )?los (?P<de>" + _DSEM + r")(?: (?:a las )?(?P<hde>\d{1,2})(?: \d{2})?)?"
                          r" (?:a|al|para) los (?P<a>" + _DSEM + r")(?: a las| a la)? (?P<h>\d{1,2})(?: (?P<m>\d{2}))?(?: hs| horas)?$")
_RENOVAR = re.compile(r"^(?:renova|renovar|renovale|segui|seguile|extende|extendele)(?: la serie| las sesiones| los turnos)?(?: de| a)? "
                      + _NOMBRE + r"$")


def _desde(texto: str | None, hoy: date) -> date | None:
    """«diciembre» = el 1/12 (el que viene si ya pasó); si no, como `_dia_proximo`."""
    if not texto:
        return hoy
    t = "septiembre" if texto == "setiembre" else texto
    if t in _MESES:
        mes = _MESES.index(t) + 1
        return date(hoy.year + (mes < hoy.month), mes, 1) if mes != hoy.month else hoy
    return _dia_proximo(t, hoy)


def _sumar_match(texto: str):
    """(match, nombre con tildes, es_serie) o None."""
    m = _SUMAR_VERBO.search(texto or "")
    if not m:
        return None
    resto = _normal(m.group("resto"))
    for regla, serie in ((_SUMAR_SERIE, True), (_SUMAR_UNO, False)):
        x = regla.search(resto)
        if x:
            palabras = [w.strip(",.;:") for w in m.group("resto").split()][:len(x.group("nombre").split())]
            return x, " ".join(palabras), serie
    return None


# Sobre el texto ORIGINAL, para no perder las tildes del nombre ni la preferencia.
_ESPERA_SUMAR = re.compile(r"^\s*(?:agreg[aá](?:le|r)?|sum[aá](?:le|r)?|anot[aá](?:le|r)?|pon[eé](?:le|r)?)\s+(?:a\s+)?"
                           r"(?P<nombre>[^\d,:+]{2,60}?)\s*(?P<tel>\+?\d[\d\s-]{6,20}\d)?\s*,?\s+(?:a|en)\s+(?:la\s+|mi\s+)?lista\s+de\s+espera"
                           r"(?:\s*[,:]?\s*(?:para|prefiere|que prefiere)\s+(?P<pref>.{2,120}?))?\s*\.?\s*$", re.IGNORECASE)
_ESPERA_SACAR = re.compile(r"^(?:saca|sacar|sacale|borra|borrar|quita|quitar|baja)(?: a)? " + _NOMBRE + r" de (?:la |mi )?lista de espera$")
# «sí» pero sin avisarle al paciente (cancelar / agendar).
_SIN_AVISAR = re.compile(r"^(?:si )?(?:pero )?(?:sin avisar(?:le)?|no (?:le )?avises)$")
BOTONES_AVISAR = [("prof:si", "Sí y avisarle"), ("prof:sin avisar", "Sí, sin avisarle"), ("prof:no", "No")]


def _fecha(texto: str, hoy: date) -> date | None:
    """«hoy», «mañana», «pasado mañana», «el viernes» (el próximo), «20 12»
    (20/12 ya normalizado; si ya pasó, el del año que viene)."""
    t = texto.strip()
    if t in ("hoy",):
        return hoy
    if t in ("manana",):
        return hoy + timedelta(days=1)
    if t in ("pasado manana",):
        return hoy + timedelta(days=2)
    t = re.sub(r"^(el|la|este|esta|el proximo|proximo) ", "", t)
    if t in _DIAS_SIN_TILDE:
        dias = (_DIAS_SIN_TILDE.index(t) - hoy.weekday()) % 7 or 7
        return hoy + timedelta(days=dias)
    m = re.fullmatch(r"(\d{1,2}) (\d{1,2})(?: (\d{2,4}))?", t)
    if m:
        dia, mes = int(m.group(1)), int(m.group(2))
        anio = int(m.group(3)) if m.group(3) else hoy.year
        anio += 2000 if anio < 100 else 0
        try:
            f = date(anio, mes, dia)
        except ValueError:
            return None
        return f.replace(year=f.year + 1) if not m.group(3) and f < hoy else f
    return None


def _rango(texto: str, hoy: date) -> tuple[date, date] | None:
    m = re.fullmatch(r"(?:del|de|desde)( el)? (.+?) (?:al|hasta)( el)? (.+)", texto)
    if m:
        desde, hasta = _fecha(m.group(2), hoy), _fecha(m.group(4), hoy)
        return (desde, hasta) if desde and hasta and hasta >= desde else None
    m = re.fullmatch(r"hasta( el)? (.+)", texto)
    if m:
        hasta = _fecha(m.group(2), hoy)
        return (hoy, hasta) if hasta and hasta >= hoy else None
    dia = _fecha(texto, hoy)
    return (dia, dia) if dia else None


def _dia_pasado(texto: str, hoy: date) -> date | None:
    """Como `_fecha` pero hacia atrás: «el jueves» es el último jueves (hoy incluido)."""
    t = re.sub(r"^el ", "", texto.strip())
    if t == "ayer":
        return hoy - timedelta(days=1)
    if t in _DIAS_SIN_TILDE:
        return hoy - timedelta(days=(hoy.weekday() - _DIAS_SIN_TILDE.index(t)) % 7)
    f = _fecha(t, hoy)
    if f and f > hoy and re.fullmatch(r"\d{1,2} \d{1,2}", t):
        f = f.replace(year=f.year - 1)
    return f


def _dia_proximo(texto: str, hoy: date) -> date | None:
    """«el jueves» es el próximo jueves, hoy incluido."""
    t = re.sub(r"^el ", "", texto.strip())
    if t in _DIAS_SIN_TILDE:
        return hoy + timedelta(days=(_DIAS_SIN_TILDE.index(t) - hoy.weekday()) % 7)
    return _fecha(t, hoy)


def _paciente_unico(tenant: dict, buscado: str) -> tuple[list[dict], str] | str:
    """Las sesiones reservadas de UN paciente que se llama así, o un texto
    (no está, o hay más de uno: que diga el apellido)."""
    palabras = _normal(buscado).split()
    fichas: dict[tuple, list[dict]] = {}
    for t in db.turnos_de_paciente_por_nombre(tenant["id"], " ".join(palabras)):
        if all(w in _normal(t.get("paciente_nombre") or "") for w in palabras):
            fichas.setdefault((t.get("paciente_telefono"), t.get("ficha") or ""), []).append(t)
    if not fichas:
        return f"No encontré a ningún paciente que se llame «{buscado}». Probá con el apellido."
    if len(fichas) > 1:
        return ("Hay más de un paciente con ese nombre: decime cuál, con el apellido.\n\n"
                + "\n".join(f"• {v[0].get('paciente_nombre')}" for v in list(fichas.values())[:8]))
    sesiones = sorted(next(iter(fichas.values())), key=lambda t: t["inicio"])
    return sesiones, sesiones[0].get("paciente_nombre") or buscado


def _hora_local(t: dict, tz: ZoneInfo) -> datetime:
    return datetime.fromisoformat(t["inicio"]).astimezone(tz)


def _dia_corto(d: date) -> str:
    return f"{DIAS[d.weekday()]} {d.day}/{d.month:02d}"


def _comprobantes_de(tenant: dict, nombre: str | None) -> list[tuple[dict, str]]:
    tz = _tz(tenant)
    lista = [(p, _de_quien(tenant, p, tz)) for p in db.pagos_para_revisar(tenant["id"]) if p.get("estado") == "comprobante"]
    if nombre:
        palabras = nombre.split()
        lista = [(p, q) for p, q in lista if all(w in _normal(q) for w in palabras)]
    return lista


def _preparar(tenant: dict, texto: str) -> tuple[dict, str] | str | None:
    """(acción, pregunta) si es un pedido de acción; un texto si no se puede;
    None si no es una acción."""
    n = _normal(texto)
    hoy = datetime.now(_tz(tenant)).date()
    m = _COMPROBANTE.search(n)
    if m:
        aceptar = m.group(1).startswith(("acept", "valid"))
        nombre = (m.group(5) or "").strip() or None
        lista = _comprobantes_de(tenant, nombre)
        if not lista:
            return f"No tenés comprobantes para validar{' de «' + nombre + '»' if nombre else ''}."
        if len(lista) > 1:
            return ("Tenés varios comprobantes" + (f" de «{nombre}»" if nombre else "") + ": decime de quién.\n\n"
                    + "\n".join(f"• {q}: {_pesos(p.get('monto'))}" for p, q in lista[:8]))
        pago, quien = lista[0]
        verbo = "Acepto" if aceptar else "Rechazo"
        consecuencia = ("la sesión queda pagada y le confirmamos al paciente" if aceptar
                        else "el horario queda libre y le avisamos al paciente")
        return ({"tipo": "aceptar" if aceptar else "rechazar", "pago": pago["id"]},
                f"¿{verbo} el comprobante de {quien} por {_pesos(pago.get('monto'))}? ({consecuencia})")
    m = _BLOQUEAR.search(n)
    if m:
        rango = _rango(m.group(3), hoy)
        if not rango:
            return "No entendí qué días. Probá con *bloquear mañana*, *bloquear el viernes* o *bloquear del 20/12 al 5/1*."
        desde, hasta = rango
        if desde < hoy:
            return "Esa fecha ya pasó."
        tz = _tz(tenant)
        inicio = datetime.combine(desde, dtime.min, tzinfo=tz)
        fin = datetime.combine(hasta + timedelta(days=1), dtime.min, tzinfo=tz)
        sesiones = [t for t in db.agenda_semana(tenant["id"], inicio, fin) if t.get("estado") == "reservado"]
        cuando = _dia_corto(desde) if desde == hasta else f"del {_dia_corto(desde)} al {_dia_corto(hasta)}"
        aviso = (f" Ojo: tenés {len(sesiones)} sesi{'ón' if len(sesiones) == 1 else 'ones'} esos días "
                 f"({', '.join(_cuando(t['inicio'], tz) + ' ' + (t.get('paciente_nombre') or '') for t in sesiones[:5])}): "
                 "no se cancelan solas, las reprogramás desde el panel.") if sesiones else ""
        return ({"tipo": "bloquear", "desde": desde.isoformat(), "hasta": hasta.isoformat()},
                f"¿Bloqueo {cuando}? Nadie va a poder reservar esos días.{aviso}")
    if _REACTIVAR.search(n):
        if not tenant.get("pausado"):
            return "Tu página ya está tomando turnos."
        return ({"tipo": "reactivar"}, "¿Reactivo tu página de turnos? Vuelve a tomar reservas ya.")
    m = _PAUSAR.search(n)
    if m:
        hasta = None
        if m.group(5):
            hasta = _fecha(m.group(5), hoy)
            if not hasta or hasta <= hoy:
                return "No entendí hasta cuándo (tiene que ser después de hoy). Probá con *pausar hasta el lunes* o *pausar hasta el 5/1*."
        return ({"tipo": "pausar", "hasta": hasta.isoformat() if hasta else None},
                f"¿Pauso tu página de turnos{' hasta el ' + _dia_corto(hasta) if hasta else ''}? Nadie va a poder reservar "
                + ("hasta ese día (ahí se reactiva sola y le avisamos a tu lista de espera)." if hasta else "hasta que escribas *reactivar*.")
                + " Las sesiones ya reservadas no se tocan.")
    m = _MOVER_SERIE.search(n)
    if m:
        return _preparar_mover_serie(tenant, m)
    m = _REPROGRAMAR.search(n)
    if m:
        tz = _tz(tenant)
        encontrado = _paciente_unico(tenant, m.group("nombre"))
        if isinstance(encontrado, str):
            return encontrado
        sesiones, nombre = encontrado
        ahora = datetime.now(tz)
        futuras = [t for t in sesiones if _hora_local(t, tz) > ahora]
        if m.group("desde"):
            desde = _dia_proximo(m.group("desde"), hoy)
            futuras = [t for t in futuras if _hora_local(t, tz).date() == desde]
        if not futuras:
            return f"No encontré una sesión próxima de {nombre}{' ese día' if m.group('desde') else ''}."
        turno = futuras[0]
        dia = _dia_proximo(m.group("hasta"), hoy)
        hora, minutos = int(m.group("h")), int(m.group("m") or 0)
        if not dia or hora > 23 or minutos > 59:
            return "No entendí el día o la hora nueva. Probá con *pasá a Juana del jueves al viernes 10:00*."
        nuevo = datetime.combine(dia, dtime(hora, minutos), tzinfo=tz)
        if nuevo <= ahora:
            return "Ese horario ya pasó."
        avisa = db.tiene_whatsapp(turno.get("paciente_telefono"))
        return ({"tipo": "reprogramar", "turno": turno["id"], "nuevo": nuevo.isoformat(), "nombre": nombre, "avisar": avisa},
                f"¿Paso la sesión de {nombre} del {_cuando(turno['inicio'], tz)} al {_cuando(nuevo.isoformat(), tz)}"
                + ("? ¿Le aviso por WhatsApp el horario nuevo?" if avisa else "? (No tiene WhatsApp: avisale vos.)"))
    m = _CANCELAR.search(n)
    if m:
        tz = _tz(tenant)
        encontrado = _paciente_unico(tenant, m.group("nombre"))
        if isinstance(encontrado, str):
            return encontrado
        sesiones, nombre = encontrado
        futuras = [t for t in sesiones if _hora_local(t, tz) > datetime.now(tz)]
        if m.group("dia"):
            dia = _dia_proximo(m.group("dia"), hoy)
            futuras = [t for t in futuras if _hora_local(t, tz).date() == dia]
        if not futuras:
            return f"No encontré una sesión próxima de {nombre}{' ese día' if m.group('dia') else ''}."
        turno = futuras[0]
        pagada = " Estaba pagada por la web: queda en «Pagos para revisar» para devolverle." if turno.get("pago_web") == "aprobado" else ""
        avisa = db.tiene_whatsapp(turno.get("paciente_telefono"))
        return ({"tipo": "cancelar", "turno": turno["id"], "nombre": nombre, "avisar": avisa},
                f"¿Cancelo la sesión de {nombre} del {_cuando(turno['inicio'], tz)}? El horario queda libre.{pagada}"
                + (" ¿Le aviso por WhatsApp que se canceló?" if avisa else " (No tiene WhatsApp: avisale vos.)"))
    m = _PAGADA.search(n)
    if m:
        tz = _tz(tenant)
        encontrado = _paciente_unico(tenant, m.group("nombre"))
        if isinstance(encontrado, str):
            return encontrado
        sesiones, nombre = encontrado
        fin_de_hoy = datetime.combine(hoy + timedelta(days=1), dtime.min, tzinfo=tz)
        sin_pagar = [t for t in sesiones if t.get("pago") not in ("pagado", "no_cobra") and _hora_local(t, tz) < fin_de_hoy]
        dia_txt = m.group("dia1") or m.group("dia2")
        if dia_txt:
            dia = _dia_pasado(dia_txt, hoy)
            sin_pagar = [t for t in sin_pagar if _hora_local(t, tz).date() == dia]
        if not sin_pagar:
            return f"No encontré una sesión sin pagar de {nombre}{' ese día' if dia_txt else ' hasta hoy'}."
        turno = sin_pagar[-1]
        valor = f" ({_pesos(turno['valor'])})" if turno.get("valor") else ""
        return ({"tipo": "pagada", "turno": turno["id"], "nombre": nombre},
                f"¿Marco pagada la sesión de {nombre} del {_cuando(turno['inicio'], tz)}{valor}?")
    m = _RECORDAR.search(n)
    if m:
        tz = _tz(tenant)
        encontrado = _paciente_unico(tenant, m.group("nombre"))
        if isinstance(encontrado, str):
            return encontrado
        sesiones, nombre = encontrado
        clave = db.clave_de(sesiones[0])
        deuda = [t for t in db.pendientes_de_pago(tenant["id"], datetime.now(timezone.utc), limite=500) if db.clave_de(t) == clave]
        total = sum(t.get("valor") or 0 for t in deuda)
        if not total:
            return f"{nombre} no tiene deuda con valor cargado."
        if not db.tiene_whatsapp(sesiones[0].get("paciente_telefono")):
            return f"{nombre} no tiene WhatsApp cargado."
        if (db.datos_de_pacientes(tenant["id"]).get(clave) or {}).get("ultimo_recordatorio_pago") == hoy.isoformat():
            return f"A {nombre} ya se le recordó hoy."
        return ({"tipo": "recordar", "clave": clave, "nombre": nombre, "monto": total},
                f"¿Le recuerdo a {nombre} que tiene {_pesos(total)} pendientes ({len(deuda)} sesi{'ón' if len(deuda) == 1 else 'ones'})?")
    sumar = _sumar_match(texto)
    if sumar:
        return _preparar_sumar(tenant, *sumar, hoy)
    m = _OFRECER.search(n)
    if m:
        return _preparar_ofrecer(tenant, m)
    m = _CORTAR.search(n)
    if m:
        return _preparar_cortar(tenant, m, hoy)
    m = _VALOR.search(n)
    if m:
        return _preparar_valor(tenant, m, hoy)
    m = _RENOVAR.search(n)
    if m:
        return _preparar_renovar(tenant, m.group("nombre"))
    m = _ESPERA_SUMAR.search(texto or "")
    if m:
        return _preparar_espera(tenant, m)
    m = _ESPERA_SACAR.search(n)
    if m:
        palabras = m.group("nombre").split()
        lista = [p for p in db.lista_de_espera(tenant["id"]) if all(w in _normal(p.get("nombre") or "") for w in palabras)]
        if not lista:
            return f"No hay nadie que se llame «{m.group('nombre')}» en tu lista de espera."
        if len(lista) > 1:
            return ("Hay más de uno con ese nombre en la lista: decime cuál, con el apellido.\n\n"
                    + "\n".join(f"• {p.get('nombre')}" for p in lista[:8]))
        p = lista[0]
        return ({"tipo": "espera_sacar", "espera": p["id"], "nombre": p.get("nombre")},
                f"¿Saco a {p.get('nombre')} ({_tel(p.get('telefono'))}) de tu lista de espera?")
    m = _CONTESTAR.search(texto or "")
    if m:
        tz = _tz(tenant)
        respuesta = " ".join(m.group(2).split())
        abiertos = _mensajes_para_contestar(tenant)
        if m.group(1):
            encontrado = _paciente_unico(tenant, m.group(1))
            if isinstance(encontrado, str):
                return encontrado
            sesiones, _ = encontrado
            abiertos = [x for x in abiertos if x["telefono"] == sesiones[0].get("paciente_telefono")]
        if not abiertos:
            return ("No hay mensajes para contestar de las últimas 24 h" + (" de esa persona" if m.group(1) else "")
                    + ". Para escribirle igual: *avisale a Juana que …*")
        msj = abiertos[0]
        quien = _nombre_del_telefono(tenant, msj["telefono"])
        cuando = datetime.fromisoformat(str(msj["creado"])).astimezone(tz).strftime("%d/%m %H:%M")
        return ({"tipo": "contestar", "mensaje": msj["id"], "telefono": msj["telefono"], "nombre": quien, "texto": respuesta},
                f"¿Le contesto a {quien} (que escribió el {cuando}: «{(msj.get('texto') or msj.get('tipo') or '')[:120]}») esto?\n\n"
                f"«{tenant['nombre']}: {respuesta}»")
    m = _AVISAR.search(texto or "")
    if m:
        buscado, mensaje = _normal(m.group(1)), " ".join(m.group(2).split())
        mensaje = mensaje[:1].upper() + mensaje[1:]
        fichas: dict[tuple, dict] = {}
        palabras = buscado.split()
        for t in db.turnos_de_paciente_por_nombre(tenant["id"], buscado):
            if t.get("paciente_telefono") and all(w in _normal(t.get("paciente_nombre") or "") for w in palabras):
                fichas.setdefault((t["paciente_telefono"], t.get("ficha") or ""), t)
        if not fichas:
            return f"No encontré a ningún paciente que se llame «{m.group(1).strip()}» (con teléfono). Probá con el apellido."
        telefonos = {tel for tel, _ in fichas}
        if len(telefonos) > 1:
            return ("Hay más de un paciente con ese nombre: decime cuál, con el apellido.\n\n"
                    + "\n".join(f"• {t.get('paciente_nombre')}" for t in list(fichas.values())[:8]))
        t = next(iter(fichas.values()))
        return ({"tipo": "avisar", "telefono": t["paciente_telefono"], "nombre": t.get("paciente_nombre"), "mensaje": mensaje},
                f"¿Le mando a {t.get('paciente_nombre')} (+{t['paciente_telefono']}) este mensaje?\n\n"
                f"«{tenant['nombre']}: {mensaje}»")
    return None


def _quien_para_sumar(tenant: dict, nombre: str, telefono: str) -> dict | str:
    """De dónde salen los datos de la sesión nueva: con WhatsApp en el mensaje,
    ese número (paciente nuevo, o el que ya lo usa); si no, el paciente que se
    llama así, con valor, modalidad y ficha de su última sesión."""
    if telefono:
        if len(telefono) < 8:
            return "Ese WhatsApp parece incompleto. Mandalo con característica, por ejemplo 1155667788."
        previas = [t for t in db.fichas_del_telefono(tenant["id"], telefono) if t.get("inicio")]
        ultima = previas[0] if previas else {}
        return {"telefono": telefono, "nombre": nombre[:1].upper() + nombre[1:], "valor": ultima.get("valor"),
                "modalidad": ultima.get("modalidad"), "ficha": None, "nuevo": not previas}
    encontrado = _paciente_unico(tenant, nombre)
    if isinstance(encontrado, str):
        if encontrado.startswith("No encontré"):
            return (f"No encontré a ningún paciente que se llame «{nombre}». Si es nuevo, mandame también su WhatsApp: "
                    f"*sumá a {nombre} 1155667788 el jueves a las 10*.")
        return encontrado
    sesiones, nombre = encontrado
    base = sesiones[-1]
    return {"telefono": base.get("paciente_telefono"), "nombre": nombre, "valor": base.get("valor"),
            "modalidad": base.get("modalidad"), "ficha": base.get("ficha") or "", "nuevo": False}


def _preparar_sumar(tenant: dict, m: re.Match, nombre: str, serie: bool, hoy: date) -> tuple[dict, str] | str:
    """Una sesión suelta, o una serie semanal / quincenal (como agendar desde el panel)."""
    tz = _tz(tenant)
    quien = _quien_para_sumar(tenant, nombre, "".join(ch for ch in m.group("tel") or "" if ch.isdigit()))
    if isinstance(quien, str):
        return quien
    hora, minutos = int(m.group("h")), int(m.group("m") or 0)
    dia = _dia_proximo(m.group("dsem") if serie else m.group("dia"), hoy)
    if not dia or hora > 23 or minutos > 59:
        return "No entendí el día o la hora. Probá con *sumá a Juana el jueves a las 10*."
    inicio = datetime.combine(dia, dtime(hora, minutos), tzinfo=tz)
    if serie and inicio <= datetime.now(tz):
        inicio += timedelta(days=7)
    if inicio <= datetime.now(tz):
        return "Ese horario ya pasó."
    duracion = tenant.get("duracion_turno_minutos") or 50
    avisa = db.tiene_whatsapp(quien["telefono"])
    detalle = [x for x in (quien.get("modalidad"), _pesos(quien["valor"]) if quien.get("valor") else None) if x]
    datos = (f" ({', '.join(detalle)}, como la última)" if detalle
             else " (paciente nuevo, sin valor: cargalo después desde el panel)" if quien["nuevo"] else "")
    pregunta_aviso = " ¿Le aviso por WhatsApp?" if avisa else " (No tiene WhatsApp: avisale vos.)"
    accion = {"tipo": "sumar", "inicio": inicio.isoformat(), "duracion": duracion, "telefono": quien["telefono"],
              "nombre": quien["nombre"], "valor": quien.get("valor"), "modalidad": quien.get("modalidad"),
              "ficha": quien.get("ficha"), "avisar": avisa}
    if not serie:
        ini_utc = inicio.astimezone(timezone.utc).isoformat()
        fin_utc = (inicio + timedelta(minutes=duracion)).astimezone(timezone.utc).isoformat()
        if db.horario_ocupado(tenant["id"], ini_utc, fin_utc):
            return f"El {_cuando(inicio.isoformat(), tz)} ya tenés otra sesión."
        bloqueado = " Ojo: ese día está bloqueado; la agendo igual." if db.bloqueo_en(tenant["id"], ini_utc, fin_utc) else ""
        return (accion, f"¿Le agendo a {quien['nombre']} (+{quien['telefono']}) una sesión el {_cuando(inicio.isoformat(), tz)}"
                        f"{datos}?{bloqueado}{pregunta_aviso}" if quien["nuevo"] else
                        f"¿Le agendo a {quien['nombre']} una sesión el {_cuando(inicio.isoformat(), tz)}{datos}?{bloqueado}{pregunta_aviso}")
    cantidad = min(int(m.group("n") or m.group("n2") or SESIONES_SERIE), MAX_SESIONES_SERIE)
    frecuencia = "quincenal" if m.group("medio") else "semanal"
    paso = timedelta(days=14 if frecuencia == "quincenal" else 7)
    salteadas = []
    for i in range(cantidad):
        ini = inicio + paso * i
        a, b = ini.astimezone(timezone.utc).isoformat(), (ini + timedelta(minutes=duracion)).astimezone(timezone.utc).isoformat()
        if db.horario_ocupado(tenant["id"], a, b) or db.bloqueo_en(tenant["id"], a, b):
            salteadas.append(f"{ini.day}/{ini.month:02d}")
    if len(salteadas) == cantidad:
        return "Todas esas fechas están ocupadas o bloqueadas: no hay nada para agendar."
    accion.update(serie=True, cantidad=cantidad, frecuencia=frecuencia)
    ultima = inicio + paso * (cantidad - 1)
    cada = f"los {DIAS[inicio.weekday()]}" + (" cada 15 días" if frecuencia == "quincenal" else "")
    return (accion, f"¿Le agendo a {quien['nombre']}" + (f" (+{quien['telefono']})" if quien["nuevo"] else "")
            + f" {cantidad} sesiones {cada} a las {inicio:%H:%M}, del {_dia_corto(inicio.date())} al {_dia_corto(ultima.date())}"
            + f"{datos}?" + (f" Quedarían sin agendar (ocupadas o bloqueadas): {', '.join(salteadas)}." if salteadas else "")
            + pregunta_aviso)


def _preparar_ofrecer(tenant: dict, m: re.Match) -> tuple[dict, str] | str:
    """Ofrecerle un horario libre (el que diga, o el próximo) a alguien de la
    lista de espera —o a un paciente con WhatsApp— con el link para reservarlo."""
    tz = _tz(tenant)
    if not tenant.get("slug"):
        return "Falta tu página de turnos para mandarle el link. Pedila a administración."
    palabras = m.group("nombre").split()
    lista = [p for p in db.lista_de_espera(tenant["id"]) if all(w in _normal(p.get("nombre") or "") for w in palabras)]
    if len(lista) > 1:
        return ("Hay más de uno con ese nombre en la lista de espera: decime cuál, con el apellido.\n\n"
                + "\n".join(f"• {p.get('nombre')}" for p in lista[:8]))
    if lista:
        persona = {"espera": lista[0]["id"], "telefono": lista[0].get("telefono"), "nombre": lista[0].get("nombre"),
                   "ofrecido_en": lista[0].get("ofrecido_en")}
    else:
        encontrado = _paciente_unico(tenant, m.group("nombre"))
        if isinstance(encontrado, str):
            return encontrado
        sesiones, nombre = encontrado
        persona = {"espera": None, "telefono": sesiones[-1].get("paciente_telefono"), "nombre": nombre}
    if not db.tiene_whatsapp(persona["telefono"]):
        return f"{persona['nombre']} no tiene WhatsApp cargado."
    if persona.get("ofrecido_en") == datetime.now(tz).date().isoformat():
        return f"A {persona['nombre']} ya se le ofreció un horario hoy."
    libres = db.turnos_libres(tenant["id"], datetime.now(timezone.utc), limite=60)
    if m.group("dia"):
        dia = _dia_proximo(m.group("dia"), datetime.now(tz).date())
        buscado = datetime.combine(dia, dtime(int(m.group("h")), int(m.group("m") or 0)), tzinfo=tz) if dia else None
        libres = [t for t in libres if buscado and datetime.fromisoformat(t["inicio"]) == buscado]
        if not libres:
            return "Ese horario no está libre en tu agenda. Escribí *libres* para ver los que tenés."
    if not libres:
        return "No tenés ningún horario libre para ofrecer."
    turno = libres[0]
    return ({"tipo": "ofrecer", **persona, "turno": turno["id"], "cuando": scheduler_cuando(tenant, turno)},
            f"¿Le ofrezco a {persona['nombre']} el {_cuando(turno['inicio'], tz)}? Le mandamos el link para que lo reserve "
            "(si otro lo toma antes, se lo pierde).")


def _preparar_cortar(tenant: dict, m: re.Match, hoy: date) -> tuple[dict, str] | str:
    """Cancela las sesiones fijas que le quedan desde una fecha (como
    cancelar una por una desde el panel)."""
    tz = _tz(tenant)
    encontrado = _paciente_unico(tenant, m.group("nombre"))
    if isinstance(encontrado, str):
        return encontrado
    sesiones, nombre = encontrado
    desde = _desde(m.group("desde"), hoy)
    if not desde:
        return "No entendí desde cuándo. Probá con *cortá la serie de Juana desde diciembre* o *desde el 15/12*."
    corte = max(datetime.combine(desde, dtime.min, tzinfo=tz), datetime.now(tz))
    fijas = [t for t in sesiones if t.get("frecuencia") and _hora_local(t, tz) >= corte]
    if not fijas:
        return f"{nombre} no tiene sesiones fijas desde el {_dia_corto(corte.date())}."
    pagadas = sum(1 for t in fijas if t.get("pago_web") == "aprobado")
    avisa = db.tiene_whatsapp(fijas[0].get("paciente_telefono"))
    return ({"tipo": "cortar", "turnos": [t["id"] for t in fijas], "nombre": nombre, "desde": fijas[0]["inicio"], "avisar": avisa},
            f"¿Cancelo {'la sesión fija' if len(fijas) == 1 else f'las {len(fijas)} sesiones fijas'} de {nombre} desde el "
            f"{_cuando(fijas[0]['inicio'], tz)}" + (f" (la última, el {_cuando(fijas[-1]['inicio'], tz)})" if len(fijas) > 1 else "")
            + "? Los horarios quedan libres."
            + (f" {pagadas} estaba{'n' if pagadas > 1 else ''} pagada{'s' if pagadas > 1 else ''} por la web: van a «Pagos para revisar»."
               if pagadas else "")
            + (" ¿Le aviso por WhatsApp?" if avisa else " (No tiene WhatsApp: avisale vos.)"))


def _preparar_mover_serie(tenant: dict, m: re.Match) -> tuple[dict, str] | str:
    """«Pasá a Lara de los jueves 17 a los martes 18»: todas sus sesiones fijas
    que vienen ese día se corren la misma cantidad de días (la más cercana
    hacia adelante o atrás, siempre al futuro) a la hora nueva."""
    tz = _tz(tenant)
    encontrado = _paciente_unico(tenant, m.group("nombre"))
    if isinstance(encontrado, str):
        return encontrado
    sesiones, nombre = encontrado
    ahora = datetime.now(tz)
    de, a = _DIAS_SIN_TILDE.index(m.group("de")), _DIAS_SIN_TILDE.index(m.group("a"))
    hora, minutos = int(m.group("h")), int(m.group("m") or 0)
    if hora > 23 or minutos > 59:
        return "No entendí la hora nueva."
    fijas = [t for t in sesiones if t.get("frecuencia") and _hora_local(t, tz) > ahora and _hora_local(t, tz).weekday() == de
             and (m.group("hde") is None or _hora_local(t, tz).hour == int(m.group("hde")))]
    if not fijas:
        return f"{nombre} no tiene sesiones fijas los {DIAS[de]}" + (f" a las {m.group('hde')}" if m.group("hde") else "") + "."
    dias = (a - de) % 7
    dias = dias - 7 if dias > 3 else dias
    primera = _hora_local(fijas[0], tz)
    if datetime.combine(primera.date() + timedelta(days=dias), dtime(hora, minutos), tzinfo=tz) <= ahora:
        dias += 7
    cambios, ocupadas = [], []
    for t in fijas:
        viejo = _hora_local(t, tz)
        nuevo = datetime.combine(viejo.date() + timedelta(days=dias), dtime(hora, minutos), tzinfo=tz)
        dur = int((datetime.fromisoformat(t["fin"]) - datetime.fromisoformat(t["inicio"])).total_seconds() // 60) \
            if t.get("fin") else (tenant.get("duracion_turno_minutos") or 50)
        if dias and db.horario_ocupado(tenant["id"], nuevo.astimezone(timezone.utc).isoformat(),
                                       (nuevo + timedelta(minutes=dur)).astimezone(timezone.utc).isoformat()):
            ocupadas.append(f"{nuevo.day}/{nuevo.month:02d}")
            continue
        cambios.append([t["id"], nuevo.isoformat(), dur])
    if not cambios:
        return "Todas esas fechas nuevas ya están ocupadas por otras sesiones."
    nueva = datetime.fromisoformat(cambios[0][1])
    avisa = db.tiene_whatsapp(fijas[0].get("paciente_telefono"))
    return ({"tipo": "mover_serie", "cambios": cambios, "nombre": nombre, "avisar": avisa,
             "dia": DIAS[a], "hora": f"{hora:02d}:{minutos:02d}", "desde": nueva.isoformat()},
            f"¿Paso {'la sesión fija' if len(cambios) == 1 else f'las {len(cambios)} sesiones fijas'} de {nombre} de los "
            f"{DIAS[de]} a los {DIAS[a]} a las {hora:02d}:{minutos:02d}, desde el {_cuando(nueva.isoformat(), tz)}?"
            + (f" Se saltearían (ya ocupadas): {', '.join(ocupadas)}." if ocupadas else "")
            + (" ¿Le aviso por WhatsApp?" if avisa else " (No tiene WhatsApp: avisale vos.)"))


def _preparar_valor(tenant: dict, m: re.Match, hoy: date) -> tuple[dict, str] | str:
    """Nuevo valor para sus próximas sesiones sin pagar (lo pasado o cobrado no se toca)."""
    tz = _tz(tenant)
    encontrado = _paciente_unico(tenant, m.group("nombre"))
    if isinstance(encontrado, str):
        return encontrado
    sesiones, nombre = encontrado
    monto = int(m.group("monto").replace(" ", "")) * (1000 if m.group("mil") else 1)
    if monto < 100:
        return "Ese valor parece muy bajo. Probá con *cambiá el valor de Juana a 30000*."
    desde = _desde(m.group("desde"), hoy)
    if not desde:
        return "No entendí desde cuándo. Probá con *… a 30000 desde diciembre*."
    corte = max(datetime.combine(desde, dtime.min, tzinfo=tz), datetime.now(tz))
    proximas = [t for t in sesiones if _hora_local(t, tz) >= corte and (t.get("pago") or "pendiente") == "pendiente"
                and t.get("valor") != monto]
    if not proximas:
        return f"{nombre} no tiene sesiones próximas sin pagar para cambiarles el valor."
    antes = sorted({_pesos(t["valor"]) for t in proximas if t.get("valor")})
    return ({"tipo": "valor", "turnos": [t["id"] for t in proximas], "nombre": nombre, "monto": monto},
            f"¿Pongo {_pesos(monto)} en {'la próxima sesión' if len(proximas) == 1 else f'las {len(proximas)} próximas sesiones'} "
            f"sin pagar de {nombre}, desde el {_cuando(proximas[0]['inicio'], tz)}?"
            + (f" (Hoy: {', '.join(antes)}.)" if antes else "") + " Lo pasado y lo ya cobrado no se toca.")


def _preparar_renovar(tenant: dict, buscado: str) -> tuple[dict, str] | str:
    """Como «Renovar» del panel: sigue la serie con el mismo horario y valor."""
    tz = _tz(tenant)
    encontrado = _paciente_unico(tenant, buscado)
    if isinstance(encontrado, str):
        return encontrado
    sesiones, nombre = encontrado
    return _accion_renovar(tenant, sesiones, nombre, tz)


def _accion_renovar(tenant: dict, sesiones: list[dict], nombre: str, tz: ZoneInfo, vence_min: int | None = None):
    ultima = sesiones[-1]
    frecuencia = ultima.get("frecuencia")
    if frecuencia not in ("semanal", "quincenal"):
        return f"{nombre} no tiene sesiones fijas para renovar. Para empezar una: *sumá a {nombre.split()[0]} todos los jueves a las 10*."
    paso = timedelta(days=14 if frecuencia == "quincenal" else 7)
    base = _hora_local(ultima, tz)
    ahora = datetime.now(tz)
    while base + paso <= ahora:
        base += paso
    duracion = int((datetime.fromisoformat(ultima["fin"]) - datetime.fromisoformat(ultima["inicio"])).total_seconds() // 60) \
        if ultima.get("fin") else (tenant.get("duracion_turno_minutos") or 50)
    primera = base + paso
    avisa = db.tiene_whatsapp(ultima.get("paciente_telefono"))
    accion = {"tipo": "sumar", "serie": True, "inicio": primera.isoformat(), "duracion": duracion, "cantidad": SESIONES_SERIE,
              "frecuencia": frecuencia, "telefono": ultima.get("paciente_telefono"), "nombre": nombre, "valor": ultima.get("valor"),
              "modalidad": ultima.get("modalidad"), "ficha": ultima.get("ficha") or "", "avisar": avisa}
    if vence_min:
        accion["vence_min"] = vence_min
    return (accion, f"¿Le renuevo a {nombre} {SESIONES_SERIE} sesiones {frecuencia}es los {DIAS[primera.weekday()]} a las "
                    f"{primera:%H:%M}, desde el {_dia_corto(primera.date())}"
                    + (f" ({_pesos(ultima['valor'])})" if ultima.get("valor") else "") + "?"
                    + (" ¿Le aviso por WhatsApp?" if avisa else ""))


# ---------- acciones pendientes: varias por número, numeradas ----------
# Una fila por WhatsApp en acciones_profesional; en `accion` va {"lista": [...]}
# con {"n", "tenant_id", "accion", "pregunta", "creado", "propuesta"}. Un
# pedido escrito reemplaza al anterior sin contestar; las propuestas (de un
# aviso: ofrecer a la espera, renovar una serie) se suman. «sí» va a la más
# nueva; «sí 2» a la número 2.

_RESPUESTA_NUM = re.compile(r"^(?P<base>.+?) (?P<n>\d{1,2})$")
_PENDIENTES = re.compile(r"^(que tengo |ver |mis )?pendientes?$|^que (me )?(queda|falta) (por )?confirmar$")


def _vence(item: dict) -> timedelta:
    return timedelta(minutes=(item.get("accion") or {}).get("vence_min") or VENCE_ACCION.total_seconds() / 60)


def _vencida(item: dict) -> bool:
    return datetime.now(timezone.utc) - datetime.fromisoformat(str(item["creado"])) > _vence(item)


def _cola(clave: str) -> list[dict]:
    fila = db.accion_profesional(clave) if clave else None
    if not fila:
        return []
    accion = fila.get("accion") or {}
    if "lista" in accion:
        return list(accion["lista"])
    return [{"n": 1, "tenant_id": fila["tenant_id"], "accion": accion, "pregunta": "", "creado": str(fila["creado"])}]


def _guardar_cola(clave: str, items: list[dict]) -> None:
    if not items:
        db.borrar_accion_profesional(clave)
    else:
        db.guardar_accion_profesional(clave, items[-1]["tenant_id"], {"lista": items})


def _encolar(clave: str, tenant_id: str, accion: dict, pregunta: str, propuesta: bool = False) -> tuple[int, int]:
    """Guarda la acción y devuelve (su número, cuántas quedan vivas en total)."""
    items = [x for x in _cola(clave) if not _vencida(x) and (propuesta or x.get("propuesta"))]
    usados = {x["n"] for x in items}
    n = next(i for i in range(1, 100) if i not in usados)
    items.append({"n": n, "tenant_id": tenant_id, "accion": accion, "pregunta": pregunta[:300],
                  "creado": datetime.now(timezone.utc).isoformat(), "propuesta": propuesta})
    _guardar_cola(clave, items)
    return n, len(items)


def _si(n: int, total: int) -> str:
    return "SÍ" if total == 1 else f"SÍ {n}"


def _pendientes_texto(clave: str) -> str:
    vivas = [x for x in _cola(clave) if not _vencida(x)]
    if not vivas:
        return "No tenés nada pendiente de confirmar."
    return ("Pendientes de confirmar:\n" + "\n".join(f"*{x['n']}.* {x.get('pregunta') or x['accion'].get('tipo')}" for x in vivas)
            + "\n\nRespondé *sí 1*, *no 2*… (*sí* solo va a la última).")


def series_por_terminar(tenant: dict) -> int:
    """Lo llama el scheduler una vez por día: a quién le quedan 1 o 2
    sesiones fijas. Un aviso por serie (pacientes.aviso_fin_serie = inicio de
    la última). Al primero le deja preparada la renovación (respondé SÍ);
    a los demás, cómo pedirla. Sólo con el WhatsApp vinculado."""
    import scheduler
    if not vinculado(tenant):
        return 0
    tz = _tz(tenant)
    ahora = datetime.now(timezone.utc)
    por_ficha: dict[str, list[dict]] = {}
    for t in db.sesiones_reservadas(tenant["id"]):
        if t.get("paciente_telefono"):
            por_ficha.setdefault(db.clave_de(t), []).append(t)
    datos = db.datos_de_pacientes(tenant["id"])
    avisados = 0
    for clave, sesiones in por_ficha.items():
        sesiones.sort(key=lambda t: t["inicio"])
        ultima = sesiones[-1]
        futuras = [t for t in sesiones if datetime.fromisoformat(t["inicio"]) > ahora]
        if not ultima.get("frecuencia") or not 1 <= len(futuras) <= 2 or not futuras[-1].get("frecuencia"):
            continue
        if (datos.get(clave) or {}).get("aviso_fin_serie") == ultima["inicio"]:
            continue
        db.guardar_paciente(tenant["id"], clave, {"aviso_fin_serie": ultima["inicio"]})  # antes de mandar: nunca dos
        nombre = ultima.get("paciente_nombre") or "un paciente"
        texto = (f"a {nombre} le queda{'n' if len(futuras) > 1 else ''} {len(futuras)} sesi{'ones' if len(futuras) > 1 else 'ón'} "
                 f"fija{'s' if len(futuras) > 1 else ''} (la última, el {_cuando(ultima['inicio'], tz)})")
        preparada = _accion_renovar(tenant, sesiones, nombre, tz, vence_min=VENCE_OFRECER_MIN)
        if isinstance(preparada, tuple):
            n, total = _encolar(_diez(tenant.get("aviso_telefono")), tenant["id"], preparada[0],
                                f"Renovarle {SESIONES_SERIE} sesiones a {nombre}", propuesta=True)
            texto += f": respondé {_si(n, total)} y le renuevo {SESIONES_SERIE} más"
        else:
            texto += f": escribí «renová a {nombre.split()[0]}» y le agendo {SESIONES_SERIE} más"
        scheduler.avisar_profesional(tenant, texto, con_nombre=texto)
        avisados += 1
    return avisados


def propuesta_tras_cancelar(tenant: dict, turno: dict) -> dict | None:
    """Cuando se libera un horario y hay lista de espera: deja preparada la
    oferta al primero que hoy no recibió otra (vale unas horas). Devuelve
    {"nombre", "n", "si"} («SÍ» o «SÍ 2» si hay otras pendientes). Sólo con el
    WhatsApp vinculado."""
    if not vinculado(tenant) or not tenant.get("slug"):
        return None
    hoy = datetime.now(_tz(tenant)).date().isoformat()
    persona = next((p for p in db.lista_de_espera(tenant["id"])
                    if p.get("ofrecido_en") != hoy and db.tiene_whatsapp(p.get("telefono"))), None)
    if not persona:
        return None
    nombre = persona.get("nombre") or "el primero de la lista"
    cuando = scheduler_cuando(tenant, turno)
    n, total = _encolar(_diez(tenant.get("aviso_telefono")), tenant["id"], {
        "tipo": "ofrecer", "espera": persona["id"], "telefono": persona["telefono"], "nombre": persona.get("nombre"),
        "turno": turno["id"], "cuando": cuando, "vence_min": VENCE_OFRECER_MIN},
        f"Ofrecerle a {nombre} el {_cuando(turno['inicio'], _tz(tenant))}", propuesta=True)
    return {"nombre": nombre, "n": n, "si": _si(n, total)}


def _preparar_espera(tenant: dict, m: re.Match) -> tuple[dict, str] | str:
    """Anotar en la lista de espera a un paciente que ya existe (su WhatsApp
    sale de sus sesiones) o a uno nuevo con el número en el mensaje."""
    nombre = " ".join(m.group("nombre").split())
    nombre = re.sub(r"^(?:a|al)\s+", "", nombre, flags=re.IGNORECASE)
    telefono = "".join(ch for ch in m.group("tel") or "" if ch.isdigit())
    preferencia = " ".join((m.group("pref") or "").split()) or None
    if not telefono:
        encontrado = _paciente_unico(tenant, nombre)
        if isinstance(encontrado, str):
            if encontrado.startswith("No encontré"):
                return (f"No encontré a ningún paciente que se llame «{nombre}». Si es nuevo, mandame también su WhatsApp: "
                        f"*agregá a {nombre} 1155667788 a la lista de espera*.")
            return encontrado
        sesiones, nombre = encontrado
        telefono = sesiones[-1].get("paciente_telefono") or ""
        if not db.tiene_whatsapp(telefono):
            return (f"{nombre} no tiene WhatsApp cargado, y sin eso no le podemos ofrecer horarios. Mandámelo así: "
                    f"*agregá a {nombre.split()[0]} 1155667788 a la lista de espera*.")
    elif len(telefono) < 8:
        return "Ese WhatsApp parece incompleto. Mandalo con característica, por ejemplo 1155667788."
    else:
        nombre = nombre[:1].upper() + nombre[1:]
    if any(_diez(p.get("telefono")) == _diez(telefono) for p in db.lista_de_espera(tenant["id"])):
        return f"{nombre} ya está en tu lista de espera."
    return ({"tipo": "espera_sumar", "nombre": nombre, "telefono": telefono, "preferencia": preferencia},
            f"¿Anoto a {nombre} (+{telefono}) en tu lista de espera" + (f" (prefiere {preferencia})" if preferencia else "") + "?")


def _al_paciente(tenant: dict, telefono: str, mensaje: str) -> tuple[bool, str]:
    """Le escribe al paciente en nombre del profesional: con la plantilla
    «Mensaje del profesional» si está, si no texto libre (que WhatsApp sólo
    entrega dentro de las 24 h desde que el paciente escribió)."""
    import whatsapp
    numero = whatsapp.numero_de(tenant)
    if config.MENSAJE_PROFESIONAL_TEMPLATE:
        return whatsapp.enviar_plantilla(numero, telefono, config.MENSAJE_PROFESIONAL_TEMPLATE,
                                         config.RECORDATORIO_TEMPLATE_LANG, [tenant["nombre"], mensaje]), ""
    return (whatsapp.enviar_texto(numero, telefono, f"*{tenant['nombre']}*: {mensaje}"),
            " (Si no te escribió en las últimas 24 h, WhatsApp puede no entregarlo: para eso falta la plantilla "
            "«Mensaje del profesional».)")


def _resultado_aviso(tenant: dict, accion: dict, telefono: str | None, mensaje: str) -> str:
    """Lo que se agrega al «Listo» según si se le avisó (o no) al paciente."""
    if not db.tiene_whatsapp(telefono):
        return ""
    if not accion.get("avisar"):
        primero = (accion.get("nombre") or "").split()[:1]
        return f" No le avisé; si querés: *avisale a {primero[0] if primero else 'tu paciente'} que …*"
    ok, nota = _al_paciente(tenant, telefono, mensaje)
    return f" Le avisamos al paciente.{nota}" if ok else " No se le pudo avisar: escribile vos."


VENTANA_RESPUESTA = timedelta(hours=23, minutes=50)


def _mensajes_para_contestar(tenant: dict) -> list[dict]:
    """De la bandeja, los que todavía se pueden contestar por texto (dentro
    de las 24 h de WhatsApp y sin respuesta), el más nuevo primero."""
    ahora = datetime.now(timezone.utc)
    lista = [x for x in db.mensajes_pacientes(tenant["id"]) if not x.get("respuesta")
             and ahora - datetime.fromisoformat(str(x["creado"])) < VENTANA_RESPUESTA]
    return sorted(lista, key=lambda x: str(x["creado"]), reverse=True)


def _ejecutar(tenant: dict, accion: dict) -> str:
    tipo = accion.get("tipo")
    if tipo == "reprogramar":
        import app as _app
        tz = _tz(tenant)
        turno = db.turno_por_id(tenant["id"], accion["turno"])
        if not turno or turno.get("estado") != "reservado":
            return "Esa sesión ya no está reservada (¿la cambiaste desde el panel?)."
        nuevo = datetime.fromisoformat(accion["nuevo"])
        error = db.reprogramar_turno(tenant["id"], turno["id"], nuevo.astimezone(timezone.utc),
                                     tenant.get("duracion_turno_minutos") or 50, ignorar_bloqueo=True)
        if error:
            return f"No la pude pasar: {error}"
        db.registrar_actividad("profesional (WhatsApp)", "reprogramó una sesión", tenant["id"])
        aviso = ""
        if db.tiene_whatsapp(turno.get("paciente_telefono")) and not accion.get("avisar", True):
            primero = (accion.get("nombre") or "").split()[:1]
            aviso = f" No le avisé; si querés: *avisale a {primero[0] if primero else 'tu paciente'} que …*"
        elif db.tiene_whatsapp(turno.get("paciente_telefono")):
            aviso = (" Le avisamos al paciente." if _app._avisar_cambio(tenant, turno, nuevo.astimezone(timezone.utc), tz)
                     else " No se le pudo avisar: escribile vos.")
        return f"Listo, la sesión de {accion.get('nombre')} quedó el {_cuando(nuevo.isoformat(), tz)}.{aviso}"
    if tipo == "cancelar":
        import pagos
        turno = db.turno_por_id(tenant["id"], accion["turno"])
        if not turno or turno.get("estado") != "reservado":
            return "Esa sesión ya no está reservada."
        db.cancelar_turno(tenant["id"], turno["id"], "profesional")
        db.registrar_actividad("profesional (WhatsApp)", "canceló una sesión", tenant["id"])
        extra = ""
        if turno.get("pago_web") == "aprobado":
            try:
                monto, _ = pagos.al_cancelar(tenant, turno, quien="profesional")
            except Exception:
                logger.exception("No se pudo anotar la devolución del turno %s", turno["id"])
                monto = 0
            if monto:
                extra = f" Estaba pagada: quedó en «Pagos para revisar» para devolverle {_pesos(monto)}."
        cuando = scheduler_cuando(tenant, {"inicio": turno["inicio"]})
        aviso = _resultado_aviso(tenant, accion, turno.get("paciente_telefono"), f"Tu sesión del {cuando} quedó cancelada.")
        listo = f"Listo, cancelada; el horario quedó libre.{extra}{aviso}"
        esperando = len(db.lista_de_espera(tenant["id"]))
        if not esperando:
            return listo
        libre = db.turno_por_id(tenant["id"], turno["id"]) or turno
        primero = propuesta_tras_cancelar(tenant, {**libre, "estado": "libre"})
        if primero:
            return _con_confirmar(f"{listo}\n\nTenés {esperando} en lista de espera. ¿Le ofrezco este horario a {primero['nombre']}? "
                                  "Le mandamos el link para que lo reserve.", n=primero["n"],
                                  numerada=primero["si"] != "SÍ")
        return f"{listo} Tenés {esperando} en lista de espera: escribí *lista de espera* para verlos."
    if tipo == "sumar":
        tz = _tz(tenant)
        inicio = datetime.fromisoformat(accion["inicio"])
        if inicio <= datetime.now(tz):
            return "Ese horario ya pasó."
        ficha = accion.get("ficha")
        if ficha is None:  # paciente con el WhatsApp del mensaje: la ficha se decide como en el panel
            ficha = db.ficha_para(tenant["id"], accion["telefono"], accion.get("nombre"))[0] \
                if db.tiene_whatsapp(accion["telefono"]) else ""
        cuando = scheduler_cuando(tenant, {"inicio": inicio.isoformat(), "modalidad": accion.get("modalidad")})
        if accion.get("serie"):
            import app as _app
            salteadas = _app._agendar_serie(tenant, inicio, accion["duracion"], range(0, accion["cantidad"]), accion["telefono"],
                                            accion.get("nombre"), accion.get("valor"), accion["frecuencia"],
                                            accion.get("modalidad"), ficha)
            hechas = accion["cantidad"] - len(salteadas)
            if not hechas:
                return "No pude agendar ninguna: todas esas fechas ya estaban ocupadas o bloqueadas."
            db.registrar_actividad("profesional (WhatsApp)", f"agendó {hechas} sesiones ({accion['frecuencia']})", tenant["id"])
            cada = "cada semana" if accion["frecuencia"] == "semanal" else "cada 15 días"
            aviso = _resultado_aviso(tenant, accion, accion["telefono"],
                                     f"Te agendé {hechas} sesiones {cada}, desde el {cuando}.")
            return (f"Listo, a {accion.get('nombre')} le agendé {hechas} sesiones ({accion['frecuencia']}) desde el "
                    f"{_cuando(inicio.isoformat(), tz)}." + (f" Sin agendar por ocupadas: {', '.join(salteadas)}." if salteadas else "")
                    + aviso)
        if not db.reservar_en_horario(tenant["id"], inicio.astimezone(timezone.utc), accion["duracion"], accion["telefono"],
                                      accion.get("nombre"), accion.get("valor"), None, accion.get("modalidad"),
                                      ficha, ignorar_bloqueo=True):
            return "No la pude agendar: ese horario ya está ocupado."
        db.registrar_actividad("profesional (WhatsApp)", "agendó una sesión", tenant["id"])
        aviso = _resultado_aviso(tenant, accion, accion["telefono"], f"Te agendé una sesión el {cuando}.")
        return f"Listo, {accion.get('nombre')} quedó agendada el {_cuando(inicio.isoformat(), tz)}.{aviso}"
    if tipo == "cortar":
        import pagos
        tz = _tz(tenant)
        canceladas, devolver = 0, 0
        telefono = None
        for turno_id in accion["turnos"]:
            turno = db.turno_por_id(tenant["id"], turno_id)
            if not turno or turno.get("estado") != "reservado":
                continue
            telefono = telefono or turno.get("paciente_telefono")
            db.cancelar_turno(tenant["id"], turno_id, "profesional")
            canceladas += 1
            if turno.get("pago_web") == "aprobado":
                try:
                    devolver += pagos.al_cancelar(tenant, turno, quien="profesional")[0] or 0
                except Exception:
                    logger.exception("No se pudo anotar la devolución del turno %s", turno_id)
        if not canceladas:
            return "Esas sesiones ya no estaban reservadas."
        db.registrar_actividad("profesional (WhatsApp)", f"cortó una serie ({canceladas} sesiones)", tenant["id"])
        cuando = scheduler_cuando(tenant, {"inicio": accion["desde"]})
        aviso = _resultado_aviso(tenant, accion, telefono, f"Tus sesiones fijas desde el {cuando} quedaron canceladas.")
        return (f"Listo, cancelé {canceladas} sesi{'ón' if canceladas == 1 else 'ones'} de {accion.get('nombre')}; "
                "los horarios quedaron libres." + (f" Quedó en «Pagos para revisar» devolverle {_pesos(devolver)}." if devolver else "")
                + aviso)
    if tipo == "mover_serie":
        movidas, fallidas, telefono = 0, [], None
        for turno_id, nuevo, dur in accion["cambios"]:
            turno = db.turno_por_id(tenant["id"], turno_id)
            if not turno or turno.get("estado") != "reservado":
                continue
            telefono = telefono or turno.get("paciente_telefono")
            nuevo_dt = datetime.fromisoformat(nuevo)
            error = db.reprogramar_turno(tenant["id"], turno_id, nuevo_dt.astimezone(timezone.utc), dur, ignorar_bloqueo=True)
            if error:
                fallidas.append(f"{nuevo_dt.day}/{nuevo_dt.month:02d}")
            else:
                movidas += 1
        if not movidas:
            return "No pude pasar ninguna: " + ("esos horarios se ocuparon." if fallidas else "ya no estaban reservadas.")
        db.registrar_actividad("profesional (WhatsApp)", f"pasó una serie de sesiones ({movidas})", tenant["id"])
        cuando = scheduler_cuando(tenant, {"inicio": accion["desde"]})
        aviso = _resultado_aviso(tenant, accion, telefono,
                                 f"Tus sesiones fijas pasan a los {accion['dia']} a las {accion['hora']}, desde el {cuando}.")
        return (f"Listo, pasé {movidas} sesi{'ón' if movidas == 1 else 'ones'} de {accion.get('nombre')} a los {accion['dia']} "
                f"a las {accion['hora']}." + (f" No se pudieron pasar: {', '.join(fallidas)}." if fallidas else "") + aviso)
    if tipo == "valor":
        cambiadas = 0
        for turno_id in accion["turnos"]:
            turno = db.turno_por_id(tenant["id"], turno_id)
            if turno and turno.get("estado") == "reservado" and (turno.get("pago") or "pendiente") == "pendiente":
                db.marcar_valor(tenant["id"], turno_id, accion["monto"])
                cambiadas += 1
        db.registrar_actividad("profesional (WhatsApp)", "cambió el valor de las sesiones de un paciente", tenant["id"])
        return f"Listo, {cambiadas} sesi{'ón' if cambiadas == 1 else 'ones'} de {accion.get('nombre')} quedaron en {_pesos(accion['monto'])}."
    if tipo == "ofrecer":
        import cancelaciones
        turno = db.turno_por_id(tenant["id"], accion["turno"])
        if not turno or turno.get("estado") != "libre":
            return "Ese horario ya no está libre (alguien lo tomó)."
        if accion.get("espera"):
            db.marcar_ofrecido(tenant["id"], accion["espera"], datetime.now(_tz(tenant)).date().isoformat())
        ok = cancelaciones.ofrecer_horario(tenant, accion["telefono"], accion["cuando"],
                                           f"{cancelaciones.base_url()}/{tenant['slug']}")
        db.registrar_actividad("profesional (WhatsApp)", "le ofreció un horario a la lista de espera", tenant["id"])
        return (f"Listo, se lo ofrecí a {accion.get('nombre')}. Si lo reserva, te llega el aviso." if ok
                else f"No se pudo escribirle a {accion.get('nombre')}. Probá desde el panel.")
    if tipo == "espera_sumar":
        if not db.agregar_a_espera(tenant["id"], accion["nombre"], accion["telefono"], accion.get("preferencia")):
            return f"{accion['nombre']} ya estaba en tu lista de espera."
        db.registrar_actividad("profesional (WhatsApp)", "anotó a alguien en la lista de espera", tenant["id"])
        return f"Listo, {accion['nombre']} quedó en tu lista de espera. Te aviso cuando se libere un horario."
    if tipo == "espera_sacar":
        db.quitar_de_espera(tenant["id"], accion["espera"])
        db.registrar_actividad("profesional (WhatsApp)", "sacó a alguien de la lista de espera", tenant["id"])
        return f"Listo, saqué a {accion.get('nombre')} de tu lista de espera."
    if tipo == "pagada":
        turno = db.turno_por_id(tenant["id"], accion["turno"])
        if not turno:
            return "Esa sesión ya no está."
        db.marcar_pago(tenant["id"], turno["id"], "pagado")
        db.registrar_actividad("profesional (WhatsApp)", "marcó pagada una sesión", tenant["id"])
        return f"Listo, la sesión de {accion.get('nombre')} quedó pagada."
    if tipo == "recordar":
        import app as _app
        telefono = db.partir_clave(accion["clave"])[0]
        db.guardar_paciente(tenant["id"], accion["clave"], {"ultimo_recordatorio_pago": datetime.now(_tz(tenant)).date().isoformat()})
        ok = _app._recordar_pago(tenant, telefono, _pesos(accion["monto"]))
        db.registrar_actividad("profesional (WhatsApp)", "le recordó un pago a un paciente", tenant["id"])
        return (f"Listo, le recordamos a {accion.get('nombre')} que tiene {_pesos(accion['monto'])} pendientes." if ok
                else "No se pudo mandar el recordatorio. Probá desde el panel.")
    if tipo == "contestar":
        import whatsapp
        msj = db.mensaje_paciente(tenant["id"], accion["mensaje"])
        if not msj or msj.get("respuesta"):
            return "Ese mensaje ya está contestado."
        if datetime.now(timezone.utc) - datetime.fromisoformat(str(msj["creado"])) >= VENTANA_RESPUESTA:
            return "Pasaron más de 24 h: WhatsApp ya no deja contestarle por acá. Probá con *avisale a … que …*."
        if not whatsapp.enviar_texto(whatsapp.numero_de(tenant), msj["telefono"], f"*{tenant['nombre']}*: {accion['texto']}"):
            return "WhatsApp no aceptó el mensaje. Probá de nuevo en un rato."
        db.actualizar_mensaje_paciente(tenant["id"], msj["id"], {"respuesta": accion["texto"], "leido": True,
                                                                 "respondido": datetime.now(timezone.utc).isoformat()})
        db.registrar_actividad("profesional (WhatsApp)", "contestó un mensaje de la bandeja", tenant["id"])
        return f"Listo, le contesté a {accion.get('nombre')}."
    if tipo in ("aceptar", "rechazar"):
        import pagos
        pago = db.pago_de(tenant["id"], accion["pago"])
        if not pago or pago.get("estado") != "comprobante":
            return "Ese comprobante ya no está para validar (¿lo resolviste desde el panel?)."
        if tipo == "aceptar":
            resultado = pagos.aprobar(tenant, pago)
            db.registrar_actividad("profesional (WhatsApp)", "aceptó un comprobante de transferencia", tenant["id"])
            return ("Listo: aceptado, la sesión quedó pagada y le confirmamos al paciente." if resultado == "aprobado"
                    else "Lo acepté, pero el horario ya se había liberado: devolvé la transferencia o reprogramalo desde el panel.")
        pagos.rechazar(tenant, pago, None)
        db.registrar_actividad("profesional (WhatsApp)", "rechazó un comprobante de transferencia", tenant["id"])
        return "Listo: rechazado, el horario quedó libre y le avisamos al paciente."
    if tipo == "bloquear":
        tz = _tz(tenant)
        desde, hasta = date.fromisoformat(accion["desde"]), date.fromisoformat(accion["hasta"])
        db.agregar_bloqueo(tenant["id"], datetime.combine(desde, dtime.min, tzinfo=tz).astimezone(timezone.utc),
                           datetime.combine(hasta + timedelta(days=1), dtime.min, tzinfo=tz).astimezone(timezone.utc), "desde WhatsApp")
        db.registrar_actividad("profesional (WhatsApp)", "bloqueó días", tenant["id"])
        return "Listo, bloqueado. Lo ves (y lo podés borrar) en el panel, en «Bloqueos»."
    if tipo == "pausar":
        db.guardar_ajustes(tenant["id"], {"pausado": True, "pausado_hasta": accion.get("hasta")})
        db.registrar_actividad("profesional (WhatsApp)", "pausó la página de turnos", tenant["id"])
        return "Listo, tu página está en pausa." + (" Escribí *reactivar* cuando quieras volver." if not accion.get("hasta") else "")
    if tipo == "reactivar":
        db.guardar_ajustes(tenant["id"], {"pausado": False, "pausado_hasta": None})
        db.registrar_actividad("profesional (WhatsApp)", "reactivó la página de turnos", tenant["id"])
        return "Listo, tu página vuelve a tomar turnos."
    if tipo == "avisar":
        ok, nota = _al_paciente(tenant, accion["telefono"], accion["mensaje"])
        db.registrar_actividad("profesional (WhatsApp)", "le mandó un mensaje a un paciente", tenant["id"])
        if ok:  # si tenía mensajes suyos en la bandeja, quedan contestados
            for x in _mensajes_para_contestar(tenant):
                if x["telefono"] == accion["telefono"]:
                    db.actualizar_mensaje_paciente(tenant["id"], x["id"], {"respuesta": accion["mensaje"], "leido": True,
                                                                         "respondido": datetime.now(timezone.utc).isoformat()})
        return (f"Listo, se lo mandé a {accion.get('nombre') or 'tu paciente'}.{nota}" if ok
                else "WhatsApp no aceptó el mensaje. Probá de nuevo en un rato o escribile desde tu teléfono.")
    return "No sé hacer eso."


def scheduler_cuando(tenant: dict, turno: dict) -> str:
    """El día y la hora como los ve el paciente en los demás avisos."""
    import scheduler
    return scheduler.cuando_de(turno, tenant, _tz(tenant))


def _con_confirmar(texto: str, con_aviso: bool = False, n: int = 1, numerada: bool = False):
    """La pregunta con botones. Los ids llevan el número de la acción
    («prof:si 2»): un botón viejo nunca confirma otra cosa."""
    import conversation
    k = f" {n}" if numerada else ""
    como = (f"Respondé *sí{k}* (y le aviso), *sin avisar{k}* o *no{k}*." if con_aviso
            else f"Respondé *sí{k}* o *no{k}*.")
    if len(texto) > 1024:
        return texto + "\n\n" + como
    return conversation.Respuesta.con(texto + "\n\n" + como,
                                     {"tipo": "botones", "cuerpo": texto,
                                      "botones": [(f"{i} {n}", t) for i, t in (BOTONES_AVISAR if con_aviso else BOTONES_CONFIRMAR)]})


def _partir_respuesta(n: str) -> tuple[str, int | None]:
    m = _RESPUESTA_NUM.search(n)
    if m and (_SI.search(m.group("base")) or _NO.search(m.group("base")) or _SIN_AVISAR.search(m.group("base"))):
        return m.group("base"), int(m.group("n"))
    return n, None


def _es_respuesta(n: str) -> bool:
    base, _ = _partir_respuesta(n)
    return bool(_SI.search(base) or _NO.search(base) or _SIN_AVISAR.search(base))


def _accion(propias: list[dict], texto: str, telefono: str):
    """Pedido de acción, o el «sí» / «no» a una pendiente. None si no es nada de eso."""
    n = _normal(texto)
    clave = _diez(telefono)
    if _PENDIENTES.search(n):
        return _pendientes_texto(clave)
    if _es_respuesta(n):
        base, numero = _partir_respuesta(n)
        items = _cola(clave) if clave else []
        if not items:
            return None
        if numero is None:
            item = max(items, key=lambda x: str(x["creado"]))
        else:
            item = next((x for x in items if x["n"] == numero), None)
            if not item:
                return f"No tengo nada pendiente con el número {numero}.\n\n" + _pendientes_texto(clave)
        resto = [x for x in items if x is not item]
        _guardar_cola(clave, resto)
        quedan = [x for x in resto if not _vencida(x)]
        sufijo = (f"\n\nTe queda{'n' if len(quedan) > 1 else ''} {len(quedan)} pendiente{'s' if len(quedan) > 1 else ''}: "
                  "escribí *pendientes* para verl" + ("as" if len(quedan) > 1 else "a") + ".") if quedan else ""
        if _NO.search(base):
            return "Listo, no hice nada." + sufijo
        if _vencida(item):
            return (f"Pasaron más de {'10 minutos' if _vence(item) == VENCE_ACCION else 'unas horas'}: "
                    "por las dudas no lo hice. Pedímelo de nuevo." + sufijo)
        tenant = next((t for t in propias if t["id"] == item["tenant_id"] and _diez(t.get("aviso_telefono")) == clave), None)
        if not tenant or len(propias) != 1:  # el número ya no está vinculado a esa cuenta (o está en varias)
            return "No lo hice: este WhatsApp ya no está vinculado a esa cuenta."
        accion = dict(item["accion"])
        if _SIN_AVISAR.search(base):
            accion["avisar"] = False
        try:
            resultado = _ejecutar(tenant, accion)
        except Exception:
            logger.exception("Falló una acción del asistente (%s)", tenant.get("id"))
            return "Uh, no pude hacerlo. Probá desde el panel."
        return resultado + sufijo if type(resultado) is str else resultado
    if len(propias) != 1:
        return _preparar_varias(propias, texto)
    tenant = propias[0]
    try:
        preparado = _preparar(tenant, texto)
    except Exception:
        logger.exception("Falló preparando una acción del asistente (%s)", tenant.get("id"))
        return "Uh, no pude preparar eso. Probá desde el panel."
    if preparado is None or isinstance(preparado, str):
        return preparado
    accion, pregunta = preparado
    numero, total = _encolar(clave, tenant["id"], accion, pregunta)
    return _con_confirmar(pregunta, con_aviso=bool(accion.get("avisar")), n=numero, numerada=total > 1)


def _preparar_varias(propias: list[dict], texto: str):
    """Con el número en varias cuentas no se adivina sobre cuál actuar."""
    if _preparar_es_accion(texto):
        return "Este WhatsApp está en varias cuentas: para no equivocarme de cuenta, eso hacelo desde el panel."
    return None


def _preparar_es_accion(texto: str) -> bool:
    n = _normal(texto)
    return bool(_COMPROBANTE.search(n) or _BLOQUEAR.search(n) or _PAUSAR.search(n) or _REACTIVAR.search(n)
                or _REPROGRAMAR.search(n) or _CANCELAR.search(n) or _PAGADA.search(n) or _RECORDAR.search(n)
                or _sumar_match(texto) or _OFRECER.search(n)
                or _CORTAR.search(n) or _VALOR.search(n) or _RENOVAR.search(n) or _MOVER_SERIE.search(n) or _ESPERA_SACAR.search(n) or _ESPERA_SUMAR.search(texto or "")
                or _AVISAR.search(texto or "") or _CONTESTAR.search(texto or ""))


def bienvenida(tenant: dict) -> str:
    """Lo que sale al dar de alta (con la plantilla de avisos)."""
    return ("te dimos de alta. Este es tu WhatsApp de avisos: te escribimos por acá cuando hay reservas, "
            "cancelaciones o pagos. Para consultarme tu agenda por acá, mandá a este chat el código de vinculación "
            "que está en tu panel")
