"""Cálculos de fechas que comparten el panel y el scheduler. Están aparte
porque scheduler no puede importar app."""
from datetime import date, datetime, timedelta


SESIONES_PARA_RENOVAR = 2
DIAS_DE_BAJA = 30
FALTAS_PARA_ALERTA = 2
DIAS_SIN_VENIR = 30
DIAS_DE_ALTA = 120


def proxima_vez(mes: int, dia: int, hoy: date) -> date:
    """Próxima vez que cae esta fecha, hoy incluido. Un 29/2 se festeja el 28
    en los años que no son bisiestos."""
    for anio in (hoy.year, hoy.year + 1):
        try:
            cae = date(anio, mes, dia)
        except ValueError:
            cae = date(anio, 2, 28)
        if cae >= hoy:
            return cae
    raise AssertionError("una fecha siempre cae dentro del año que viene")


def sesiones_que_quedan(sesiones: list[dict], ahora: datetime) -> int | None:
    """Cuántas sesiones futuras le quedan a un paciente fijo que hay que
    renovar, o None si no hace falta. `sesiones` es de un solo paciente, de la
    más vieja a la más nueva. Si hace más de un mes que no viene y no tiene
    nada agendado, se da por dado de baja y no se insiste."""
    futuras = [t for t in sesiones if datetime.fromisoformat(t["inicio"]) > ahora]
    ultima = sesiones[-1]
    if not ultima.get("frecuencia") or len(futuras) > SESIONES_PARA_RENOVAR:
        return None
    if not futuras and datetime.fromisoformat(ultima["inicio"]) < ahora - timedelta(days=DIAS_DE_BAJA):
        return None
    return len(futuras)


def alerta_de_seguimiento(sesiones: list[dict], ahora: datetime) -> str | None:
    """Paciente que se puede estar alejando. Pasados los 120 días sin venir ya
    se lo da por terminado y deja de aparecer."""
    pasadas = [t for t in sesiones if datetime.fromisoformat(t["inicio"]) <= ahora]
    if not pasadas:
        return None
    faltas = 0
    for t in reversed(pasadas):
        if t.get("asistencia") != "no_vino":
            break
        faltas += 1
    if faltas >= FALTAS_PARA_ALERTA:
        return f"faltó {faltas} veces seguidas"
    dias = (ahora - datetime.fromisoformat(pasadas[-1]["inicio"])).days
    if len(pasadas) == len(sesiones) and DIAS_SIN_VENIR <= dias < DIAS_DE_ALTA:
        return f"hace {dias} días que no viene y no tiene turno"
    return None
