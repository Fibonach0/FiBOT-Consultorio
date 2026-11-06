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
            "Y puedo hacer (siempre te pregunto antes):\n"
            "• *aceptar comprobante de Juana* / *rechazar comprobante de Juana*\n"
            "• *bloquear mañana* / *bloquear el viernes* / *bloquear del 20/12 al 5/1*\n"
            "• *pausar hasta el lunes* / *pausar* / *reactivar*\n"
            "• *avisale a Juan que llego 10 minutos tarde*\n\n"
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
    propias = [t for t in tenants if vinculado(t)]
    if _pedido(texto) is None:
        if not _preparar_es_accion(texto) and not (_SI.search(_normal(texto)) or _NO.search(_normal(texto))):
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


def _ejecutar(tenant: dict, accion: dict) -> str:
    tipo = accion.get("tipo")
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
        import whatsapp
        numero = whatsapp.numero_de(tenant)
        if config.MENSAJE_PROFESIONAL_TEMPLATE:
            ok = whatsapp.enviar_plantilla(numero, accion["telefono"], config.MENSAJE_PROFESIONAL_TEMPLATE,
                                           config.RECORDATORIO_TEMPLATE_LANG, [tenant["nombre"], accion["mensaje"]])
            nota = ""
        else:
            ok = whatsapp.enviar_texto(numero, accion["telefono"], f"*{tenant['nombre']}*: {accion['mensaje']}")
            nota = (" (Si no te escribió en las últimas 24 h, WhatsApp puede no entregarlo: para eso falta la plantilla "
                    "«Mensaje del profesional».)")
        db.registrar_actividad("profesional (WhatsApp)", "le mandó un mensaje a un paciente", tenant["id"])
        return (f"Listo, se lo mandé a {accion.get('nombre') or 'tu paciente'}.{nota}" if ok
                else "WhatsApp no aceptó el mensaje. Probá de nuevo en un rato o escribile desde tu teléfono.")
    return "No sé hacer eso."


def _con_confirmar(texto: str):
    import conversation
    if len(texto) > 1024:
        return texto + "\n\nRespondé *sí* para hacerlo o *no* para dejarlo."
    return conversation.Respuesta.con(texto + "\n\nRespondé *sí* o *no*.",
                                     {"tipo": "botones", "cuerpo": texto, "botones": BOTONES_CONFIRMAR})


def _accion(propias: list[dict], texto: str, telefono: str):
    """Pedido de acción, o el «sí» / «no» a una pendiente. None si no es nada de eso."""
    n = _normal(texto)
    clave = _diez(telefono)
    if _SI.search(n) or _NO.search(n):
        pendiente = db.accion_profesional(clave) if clave else None
        if not pendiente:
            return None
        db.borrar_accion_profesional(clave)
        if _NO.search(n):
            return "Listo, no hice nada."
        creado = datetime.fromisoformat(str(pendiente["creado"]))
        if datetime.now(timezone.utc) - creado > VENCE_ACCION:
            return "Pasaron más de 10 minutos: por las dudas no lo hice. Pedímelo de nuevo."
        tenant = next((t for t in propias if t["id"] == pendiente["tenant_id"] and _diez(t.get("aviso_telefono")) == clave), None)
        if not tenant or len(propias) != 1:  # el número ya no está vinculado a esa cuenta (o está en varias)
            return "No lo hice: este WhatsApp ya no está vinculado a esa cuenta."
        try:
            return _ejecutar(tenant, pendiente["accion"])
        except Exception:
            logger.exception("Falló una acción del asistente (%s)", tenant.get("id"))
            return "Uh, no pude hacerlo. Probá desde el panel."
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
    db.guardar_accion_profesional(clave, tenant["id"], accion)
    return _con_confirmar(pregunta)


def _preparar_varias(propias: list[dict], texto: str):
    """Con el número en varias cuentas no se adivina sobre cuál actuar."""
    if _preparar_es_accion(texto):
        return "Este WhatsApp está en varias cuentas: para no equivocarme de cuenta, eso hacelo desde el panel."
    return None


def _preparar_es_accion(texto: str) -> bool:
    n = _normal(texto)
    return bool(_COMPROBANTE.search(n) or _BLOQUEAR.search(n) or _PAUSAR.search(n) or _REACTIVAR.search(n)
                or _AVISAR.search(texto or ""))


def bienvenida(tenant: dict) -> str:
    """Lo que sale al dar de alta (con la plantilla de avisos)."""
    return ("te dimos de alta. Este es tu WhatsApp de avisos: te escribimos por acá cuando hay reservas, "
            "cancelaciones o pagos. Para consultarme tu agenda por acá, mandá a este chat el código de vinculación "
            "que está en tu panel")
