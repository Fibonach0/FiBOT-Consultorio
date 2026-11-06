"""Lo que le contesta el bot al PROFESIONAL cuando le escribe desde su
WhatsApp de avisos (`tenants.aviso_telefono`): su agenda de hoy, mañana o la
semana, pagos para revisar, lo que le deben, mensajes sin leer y el link de su
página.

Mismo criterio que el resumen diario: por WhatsApp no sale ningún nombre ni
dato de un paciente (horas, cantidades y montos; el detalle está en el panel).
Sólo lee: las acciones siguen en el panel, salvo LIBERAR / ME LO TOMO, que ya
existían (`cancelaciones.respuesta_del_profesional`).

Reglas locales, sin servicios externos, como `conversation.py`. Si el texto no
es un pedido del profesional, devuelve None y el mensaje sigue como el de
cualquier paciente (así el profesional puede probar su propio bot reservando).
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import db

logger = logging.getLogger("asistente_profesional")

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

_HOY = re.compile(r"^(que tengo )?(para )?hoy\??$|^agenda( de)? hoy$|^mi agenda$|^agenda$|^sesiones( de)? hoy$")
_MANANA = re.compile(r"^(que tengo )?(para )?manana\??$|^agenda( de)? manana$|^sesiones( de)? manana$")
_SEMANA = re.compile(r"^(esta |la )?semana$|^agenda( de la)? semana$|^que tengo (esta|la) semana$")
_PAGOS = re.compile(r"^pagos?( para revisar| pendientes)?$|^comprobantes?$|^cobros?( de la web| web)?$")
_DEUDA = re.compile(r"^(quien|quienes) (me )?debe[n]?$|^deudas?$|^me deben$|^pendientes? de cobro$|^que me deben$")
_MENSAJES = re.compile(r"^mensajes?( sin leer| nuevos)?$|^bandeja$")
_LIBRES = re.compile(r"^(horarios? )?libres?$|^que tengo libre$|^huecos$")
_LINK = re.compile(r"^(mi )?(link|pagina|enlace)( de turnos| de reservas)?$")
_AYUDA = re.compile(r"^(ayuda|menu|opciones|comandos|hola|holis|buenas|buen dia|que podes hacer|\?)$")


def _normal(texto: str) -> str:
    import conversation
    return conversation._normal(texto)


def _tz(tenant: dict) -> ZoneInfo:
    return ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")


def _pesos(v) -> str:
    return f"$ {int(v or 0):,}".replace(",", ".")


def _panel() -> str:
    return f"https://{config.DOMINIO_PRINCIPAL}/agenda" if config.DOMINIO_PRINCIPAL else "el panel"


def menu(tenant: dict) -> str:
    return (f"Hola, *{tenant['nombre']}*. Soy tu asistente de FiBOT Consultorio. Desde este WhatsApp podés preguntarme:\n\n"
            "• *hoy* / *mañana* / *semana* — tus sesiones (sin nombres)\n"
            "• *libres* — tus próximos horarios libres\n"
            "• *pagos* — comprobantes y devoluciones para revisar\n"
            "• *deudas* — cuánto te deben de sesiones pasadas\n"
            "• *mensajes* — mensajes de pacientes sin leer\n"
            "• *link* — tu página de turnos para compartir\n\n"
            "También te aviso por acá de reservas, cancelaciones y pagos. "
            f"Los nombres y el detalle están en {_panel()}.")


def _linea_sesion(t: dict, tz: ZoneInfo, con_dia: bool = False) -> str:
    dt = datetime.fromisoformat(t["inicio"]).astimezone(tz)
    texto = (f"{DIAS[dt.weekday()][:3]} {dt.day:02d}/{dt.month:02d} " if con_dia else "") + f"{dt:%H:%M}"
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
    return "• " + texto + (f" ({', '.join(extras)})" if extras else "")


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
        texto += f"\n\nHorarios libres: {len(libres)}."
    return texto + f"\n\nCon nombres, en {_panel()}."


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


def _pagos(tenant: dict) -> str:
    pendientes = db.pagos_para_revisar(tenant["id"])
    comprobantes = [p for p in pendientes if p.get("estado") == "comprobante"]
    tarde = [p for p in pendientes if p.get("estado") == "aprobado_tarde"]
    devolver = [p for p in pendientes if (p.get("a_devolver") or 0) > 0]
    partes = []
    if comprobantes:
        partes.append(f"• {len(comprobantes)} comprobante{'s' if len(comprobantes) > 1 else ''} de transferencia para validar "
                      f"({_pesos(sum(p.get('monto') or 0 for p in comprobantes))})")
    if tarde:
        partes.append(f"• {len(tarde)} pago{'s' if len(tarde) > 1 else ''} que llegó tarde (el horario ya se había liberado)")
    if devolver:
        partes.append(f"• {_pesos(sum(p['a_devolver'] for p in devolver))} para devolver a mano")
    esperando = db.pagos_esperando(tenant["id"])
    if esperando:
        partes.append(f"• {len(esperando)} reserva{'s' if len(esperando) > 1 else ''} esperando el pago")
    if not partes:
        return "No tenés pagos para revisar."
    return "Pagos:\n\n" + "\n".join(partes) + f"\n\nSe resuelven en {_panel()}, en «Pagos para revisar»."


def _deudas(tenant: dict) -> str:
    sin_pagar = db.pendientes_de_pago(tenant["id"], datetime.now(timezone.utc), limite=500)
    con_valor = [t for t in sin_pagar if t.get("valor")]
    if not sin_pagar:
        return "No te deben sesiones: todas las pasadas están cobradas o marcadas «no cobra»."
    pacientes = len({(t.get("paciente_telefono"), t.get("ficha")) for t in sin_pagar})
    texto = (f"Te deben {_pesos(sum(t['valor'] for t in con_valor))} de {len(sin_pagar)} "
             f"sesi{'ón' if len(sin_pagar) == 1 else 'ones'} pasadas, de {pacientes} paciente{'s' if pacientes > 1 else ''}.")
    if len(con_valor) < len(sin_pagar):
        texto += f" {len(sin_pagar) - len(con_valor)} no tienen valor cargado."
    return texto + f"\n\nQuién debe y el botón para recordarle, en {_panel()} → «Quién debe»."


def _mensajes(tenant: dict) -> str:
    n = db.mensajes_sin_leer(tenant["id"])
    if not n:
        return "No tenés mensajes de pacientes sin leer."
    return f"Tenés {n} mensaje{'s' if n > 1 else ''} de pacientes sin leer. Los leés y contestás en {_panel()}, en «Mensajes»."


def _link(tenant: dict) -> str:
    if not tenant.get("slug") or not config.DOMINIO_PRINCIPAL:
        return "Todavía no tenés página de turnos. Pedila a administración."
    return f"Tu página de turnos: https://{config.DOMINIO_PRINCIPAL}/{tenant['slug']}\n\nReenviá este mensaje a quien quieras."


def responder(tenant: dict, texto: str) -> str | None:
    """None si no es un pedido del profesional (el mensaje sigue su camino)."""
    n = _normal(texto)
    for regla, funcion in ((_AYUDA, menu), (_HOY, lambda t: _dia(t, 0, "Hoy")), (_MANANA, lambda t: _dia(t, 1, "Mañana")),
                           (_SEMANA, _semana), (_LIBRES, _libres), (_PAGOS, _pagos), (_DEUDA, _deudas),
                           (_MENSAJES, _mensajes), (_LINK, _link)):
        if regla.search(n):
            try:
                return funcion(tenant)
            except Exception:
                logger.exception("Falló el asistente del profesional (%s)", tenant.get("id"))
                return "Uh, no pude buscar eso. Probá de nuevo en un rato o mirá el panel."
    return None


def responder_a(tenants: list[dict], texto: str) -> str | None:
    """Con el mismo WhatsApp de avisos en más de una cuenta, contesta por
    cada una (con el nombre arriba)."""
    respuestas = []
    for t in tenants:
        r = responder(t, texto)
        if r is None:
            return None
        respuestas.append(r if len(tenants) == 1 else f"*{t['nombre']}*\n{r}")
    return "\n\n———\n\n".join(respuestas) if respuestas else None


def bienvenida(tenant: dict) -> str:
    """Lo que sale al dar de alta (con la plantilla de avisos)."""
    return ("te dimos de alta. Este es tu WhatsApp de avisos: te escribimos por acá cuando hay reservas, "
            "cancelaciones o pagos. Escribí *ayuda* a este número para ver qué podés consultar")
