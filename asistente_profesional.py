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

Sólo lee: las acciones siguen en el panel, salvo LIBERAR / ME LO TOMO, que ya
existían (`cancelaciones.respuesta_del_profesional`).

Reglas locales, sin servicios externos, como `conversation.py`. Si el texto no
es un pedido del profesional, devuelve None y el mensaje sigue como el de
cualquier paciente (así el profesional puede probar su propio bot reservando).
"""
import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta, timezone
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
_LINK = re.compile(r"^(mi )?(link|pagina|enlace)( de turnos| de reservas)?$")
_AYUDA = re.compile(r"^(ayuda|menu|opciones|comandos|hola|holis|buenas|buen dia|que podes hacer)$")
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
            "• *link* — tu página de turnos para compartir\n\n"
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


def _link(tenant: dict) -> str:
    if not tenant.get("slug") or not config.DOMINIO_PRINCIPAL:
        return "Todavía no tenés página de turnos. Pedila a administración."
    return f"Tu página de turnos: https://{config.DOMINIO_PRINCIPAL}/{tenant['slug']}\n\nReenviá este mensaje a quien quieras."


def _pedido(texto: str):
    """La función que contesta este texto, o None si no es un pedido del profesional."""
    n = _normal(texto)
    reglas = ((_AYUDA, menu), (_HOY, lambda t: _dia(t, 0, "Hoy")), (_MANANA, lambda t: _dia(t, 1, "Mañana")),
              (_SEMANA, _semana), (_LIBRES, _libres), (_PAGOS, _pagos), (_DEUDA, _deudas),
              (_MENSAJES, _mensajes), (_LINK, _link))
    for regla, funcion in reglas:
        if regla.search(n):
            return funcion
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
    if _pedido(texto) is None:
        return None
    propias = [t for t in tenants if vinculado(t)]
    if not propias:
        return sin_vincular()
    respuestas = [r if len(propias) == 1 else f"*{t['nombre']}*\n{r}" for t in propias if (r := responder(t, texto))]
    texto_final = "\n\n———\n\n".join(respuestas)
    if len(texto_final) > 1024:  # el cuerpo de un mensaje con botones no puede ser más largo
        return texto_final
    return conversation.Respuesta.con(texto_final, {"tipo": "botones", "cuerpo": texto_final, "botones": BOTONES})


def bienvenida(tenant: dict) -> str:
    """Lo que sale al dar de alta (con la plantilla de avisos)."""
    return ("te dimos de alta. Este es tu WhatsApp de avisos: te escribimos por acá cuando hay reservas, "
            "cancelaciones o pagos. Para consultarme tu agenda por acá, mandá a este chat el código de vinculación "
            "que está en tu panel")
