"""Qué le contesta FiBOT Consultorio a un paciente. Deliberadamente SIN estado
de conversación en memoria: interpretar "3" como "el turno #3 de la lista de
libres AHORA MISMO" (no de una lista que le mostramos hace rato) hace que todo
sea stateless — no hay sesión que se pierda si el proceso reinicia, y no hay
lista vieja que quede desincronizada. El costo es chico: si alguien tarda en
contestar y la lista cambió, en el peor caso reserva un horario distinto al
que vio, nunca uno inexistente (reservar_turno() sigue siendo atómico igual)."""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
import db

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

_SELECCION = re.compile(r"^\s*(\d{1,2})\b(.*)$")
_CANCELAR = re.compile(r"^cancelar(?:\s+(?:el\s+)?turno)?(?:\s+(?:de|a|para))?(?:\s+(.*))?$", re.IGNORECASE)


def _fmt(dt_iso: str, tz: ZoneInfo) -> str:
    dt = datetime.fromisoformat(dt_iso).astimezone(tz)
    return f"{DIAS[dt.weekday()]} {dt.day} de {MESES[dt.month - 1]}, {dt.hour:02d}:{dt.minute:02d}"


def _tz(tenant: dict) -> ZoneInfo:
    return ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")


def _menu(tenant: dict) -> str:
    return (
        f"Hola, soy el asistente de *{tenant['nombre']}*. Puedo ayudarte con:\n\n"
        "• *turno* — ver horarios disponibles\n"
        "• *cancelar* — cancelar tu próximo turno\n\n"
        "Para lo demás, escribile directo."
    )


def _pausado(tenant: dict) -> str:
    vuelve = f" Vuelve el {str(tenant['pausado_hasta'])[8:10]}/{str(tenant['pausado_hasta'])[5:7]}." if tenant.get("pausado_hasta") else ""
    return (f"*{tenant['nombre']}* no está tomando turnos nuevos por ahora.{vuelve}"
            + (f" {tenant['mensaje_pausa']}" if tenant.get("mensaje_pausa") else "")
            + " Si ya tenés un turno, sigue en pie.")


def _listar_libres(tenant: dict) -> str:
    if tenant.get("pausado"):
        return _pausado(tenant)
    tz = _tz(tenant)
    libres = db.turnos_libres(tenant["id"], datetime.now(timezone.utc))
    if not libres:
        return f"Por ahora no hay horarios cargados con *{tenant['nombre']}*. Probá de nuevo más tarde."
    lineas = [f"{i}. {_fmt(t['inicio'], tz)}" for i, t in enumerate(libres, start=1)]
    return (
        "Estos son los próximos horarios libres:\n\n"
        + "\n".join(lineas)
        + "\n\nRespondé con el número para reservar (podés sumar tu nombre, ej: *2 Juan Pérez*)."
    )


def _reservar(tenant: dict, telefono: str, indice: int, nombre: str | None) -> str:
    if tenant.get("pausado"):
        return _pausado(tenant)
    tz = _tz(tenant)
    libres = db.turnos_libres(tenant["id"], datetime.now(timezone.utc), limite=max(indice, 8))
    if indice < 1 or indice > len(libres):
        return "Ese número no corresponde a ningún horario de la lista. Escribí *turno* para ver los disponibles."
    elegido = libres[indice - 1]
    if not nombre and len(db.fichas_del_telefono(tenant["id"], telefono)) > 1:
        return f"Este WhatsApp es de más de un paciente. Escribí el número y el nombre, ej: *{indice} Lola*."
    if nombre and db.nombre_ambiguo(tenant["id"], telefono, nombre):
        return (f"Hay más de un paciente que se llama {nombre} con este WhatsApp. Escribí el nombre completo, "
                f"o pedile la reserva directamente a *{tenant['nombre']}*.")
    if db.reservar_turno(tenant["id"], elegido["id"], telefono, nombre):
        return (
            f"Listo, quedó reservado para el {_fmt(elegido['inicio'], tz)}. "
            "Te aviso antes por acá. Si necesitás cancelarlo, escribí *cancelar*."
        )
    return "Justo se acaba de reservar ese horario. Escribí *turno* para ver los que quedan libres."


def _cancelar(tenant: dict, telefono: str, nombre: str | None = None) -> str:
    """Nunca adivina de quién es: con varias personas en este teléfono, o con
    un nombre que no coincide, pregunta y no cancela nada."""
    tz = _tz(tenant)
    turnos = db.proximos_turnos_de(tenant["id"], telefono)
    if not turnos:
        return "No tenés ningún turno reservado a tu nombre."
    if nombre:
        elegidos = db.elegir_por_nombre(turnos, nombre)
        if not elegidos and len(turnos) == 1 and not turnos[0].get("paciente_nombre"):
            elegidos = turnos
    else:
        elegidos = turnos if len(turnos) == 1 else []
    if len(elegidos) != 1:
        detalle = "\n".join(f"• {t.get('paciente_nombre') or 'Sin nombre'}: {_fmt(t['inicio'], tz)}" for t in turnos)
        ejemplo = next((t["paciente_nombre"] for t in turnos if t.get("paciente_nombre")), None)
        if len(elegidos) > 1:
            inicio = f"Hay más de un turno que coincide con {nombre}."
        else:
            inicio = f"No encontré un turno de {nombre}." if nombre else "Este WhatsApp tiene turnos de más de un paciente."
        iguales = len({db.nombre_normal(t.get("paciente_nombre")) for t in turnos}) < len(turnos)
        if iguales:
            pedido = f" Para cancelar, escribile directamente a *{tenant['nombre']}*."
        else:
            pedido = f" Escribí *cancelar* y el nombre completo, por ejemplo *cancelar {ejemplo}*." if ejemplo else ""
        return f"{inicio}\n\n{detalle}\n\nTodavía no se canceló nada.{pedido}"
    turno = elegidos[0]
    import cancelaciones  # acá: cancelaciones importa scheduler, que no hace falta para el resto del bot
    if not cancelaciones.puede_modificar(turno, tenant):
        return (f"Tu turno del {_fmt(turno['inicio'], tz)} es en menos de {cancelaciones.horas_limite(tenant)} horas: "
                f"para cancelarlo, escribile directamente a *{tenant['nombre']}*. Todavía no se canceló nada.")
    db.cancelar_turno(tenant["id"], turno["id"], "paciente")
    try:
        cancelaciones.tras_cancelar(tenant, turno, "WhatsApp")
    except Exception:
        import logging
        logging.getLogger("conversation").exception("Falló el aviso de la cancelación del turno %s", turno["id"])
    de_quien = f" de {turno['paciente_nombre']}" if nombre and turno.get("paciente_nombre") else ""
    return f"Cancelado el turno{de_quien} del {_fmt(turno['inicio'], tz)}. Escribí *turno* si querés reservar otro horario."


# «Sí, confirmo» llega como botón de la plantilla de confirmación o tipeado.
_CONFIRMAR = {"si", "sí", "si confirmo", "sí confirmo", "si, confirmo", "sí, confirmo", "confirmo", "confirmar",
              "confirmado", "ok", "dale"}


def _base_url() -> str:
    if config.DOMINIO_PRINCIPAL:
        return f"https://{config.DOMINIO_PRINCIPAL}"
    from flask import has_request_context, request
    return request.host_url.rstrip("/") if has_request_context() else ""


def _confirmar(tenant: dict, telefono: str) -> str | None:
    """Respuesta a la confirmación de una reserva hecha en la web, con el link
    para agendarla. None si no había nada esperando confirmación."""
    turno = db.confirmar_proximo(tenant["id"], telefono)
    if not turno:
        return None
    texto = f"¡Listo! Quedó confirmado tu turno con *{tenant['nombre']}* el {_fmt(turno['inicio'], _tz(tenant))}."
    if turno.get("modalidad") == "virtual" and tenant.get("link_videollamada"):
        texto += f"\n\nEs por videollamada: {tenant['link_videollamada']}"
    if turno.get("token_calendario"):
        texto += (f"\n\nAgendalo en tu calendario (y desde ahí podés cambiarlo o cancelarlo): "
                  f"{_base_url()}/agendar/{turno['token_calendario']}")
        return texto
    return texto + "\n\nSi no podés ir, escribí *cancelar*."


def responder(tenant: dict, telefono: str, texto: str) -> str:
    limpio = (texto or "").strip()
    baja = limpio.lower()

    if baja.rstrip(".!") in _CONFIRMAR:
        confirmado = _confirmar(tenant, telefono)
        if confirmado:
            return confirmado

    m = _SELECCION.match(limpio)
    if m:
        indice = int(m.group(1))
        nombre = m.group(2).strip() or None
        return _reservar(tenant, telefono, indice, nombre)

    if baja in ("turno", "turnos", "horario", "horarios", "reservar", "quiero un turno"):
        return _listar_libres(tenant)

    m = _CANCELAR.match(limpio)
    if m:
        return _cancelar(tenant, telefono, (m.group(1) or "").strip() or None)

    if baja in ("hola", "buenas", "buen dia", "buen día", "ayuda", "menu", "menú"):
        return _menu(tenant)

    return _menu(tenant)
