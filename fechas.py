"""Cálculos de fechas que comparten el panel y el scheduler. Están aparte
porque scheduler no puede importar app."""
from datetime import date, datetime, timedelta


SESIONES_PARA_RENOVAR = 2
DIAS_DE_BAJA = 30


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
