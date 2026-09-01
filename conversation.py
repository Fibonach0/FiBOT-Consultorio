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

import db

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

_SELECCION = re.compile(r"^\s*(\d{1,2})\b(.*)$")


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


def _listar_libres(tenant: dict) -> str:
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
    tz = _tz(tenant)
    libres = db.turnos_libres(tenant["id"], datetime.now(timezone.utc), limite=max(indice, 8))
    if indice < 1 or indice > len(libres):
        return "Ese número no corresponde a ningún horario de la lista. Escribí *turno* para ver los disponibles."
    elegido = libres[indice - 1]
    if db.reservar_turno(tenant["id"], elegido["id"], telefono, nombre):
        return (
            f"Listo, quedó reservado para el {_fmt(elegido['inicio'], tz)}. "
            "Te aviso antes por acá. Si necesitás cancelarlo, escribí *cancelar*."
        )
    return "Justo se acaba de reservar ese horario. Escribí *turno* para ver los que quedan libres."


def _cancelar(tenant: dict, telefono: str) -> str:
    tz = _tz(tenant)
    turno = db.turno_reservado_de(tenant["id"], telefono)
    if not turno:
        return "No tenés ningún turno reservado a tu nombre."
    db.cancelar_turno(tenant["id"], turno["id"])
    return f"Cancelado el turno del {_fmt(turno['inicio'], tz)}. Escribí *turno* si querés reservar otro horario."


def responder(tenant: dict, telefono: str, texto: str) -> str:
    limpio = (texto or "").strip()
    baja = limpio.lower()

    m = _SELECCION.match(limpio)
    if m:
        indice = int(m.group(1))
        nombre = m.group(2).strip() or None
        return _reservar(tenant, telefono, indice, nombre)

    if baja in ("turno", "turnos", "horario", "horarios", "reservar", "quiero un turno"):
        return _listar_libres(tenant)

    if baja in ("cancelar", "cancelar turno"):
        return _cancelar(tenant, telefono)

    if baja in ("hola", "buenas", "buen dia", "buen día", "ayuda", "menu", "menú"):
        return _menu(tenant)

    return _menu(tenant)
