"""Cálculo de fechas que se repiten todos los años. Aparte porque lo usan el
panel y el scheduler, y scheduler no puede importar app."""
from datetime import date


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
