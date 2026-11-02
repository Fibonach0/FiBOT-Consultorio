"""Fluidez lectora tomada en sesión.

La psicopedagoga elige un texto propio (no se incluyen textos de tests: son
de las editoriales), el chico lee, ella marca los errores en pantalla y el
sistema cronometra. Con eso calcula:

- palabras por minuto: leídas × 60 / segundos
- palabras correctas por minuto: (leídas − errores) × 60 / segundos, la
  medida habitual de fluidez
- precisión: correctas / leídas

No compara contra normas por grado: las publicadas son de editoriales o de
estudios con muestras puntuales. Se compara al chico consigo mismo.
"""

import re

MAX_SEGUNDOS = 30 * 60
MAX_PALABRAS = 1000
MAX_TITULO = 80
MAX_TEXTO = 8000


def palabras(texto: str) -> list[str]:
    return [p for p in re.split(r"\s+", texto or "") if re.search(r"\w", p)]


def calcular(leidas: int, errores: int, segundos: float) -> dict:
    correctas = leidas - errores
    return {
        "ppm": round(leidas * 60 / segundos),
        "pcpm": round(correctas * 60 / segundos),
        "precision": round(100 * correctas / leidas) if leidas else 0,
    }


def validar(total: int, leidas_txt: str, errores_txt: str, segundos_txt: str) -> tuple[dict | None, str | None]:
    """Lo que manda la pantalla de lectura → números limpios o un error."""
    try:
        leidas = int(leidas_txt)
        segundos = float(segundos_txt)
        errores = sorted({int(x) for x in (errores_txt or "").split(",") if x.strip()})
    except ValueError:
        return None, "La lectura llegó incompleta. Probá de nuevo."
    if not 1 <= leidas <= total:
        return None, "Falta marcar hasta dónde leyó."
    if not 1 <= segundos <= MAX_SEGUNDOS:
        return None, "El tiempo de lectura no es válido."
    if any(not 0 <= i < leidas for i in errores):
        return None, "Hay errores marcados después de la última palabra leída."
    return {"leidas": leidas, "errores": errores, "segundos": round(segundos, 1)}, None


def para_ficha(lecturas: list[dict]) -> dict:
    """lecturas de una ficha, de la más nueva a la más vieja → filas para
    mostrar, el cambio contra la anterior y los puntos del gráfico."""
    filas = []
    for x in lecturas:
        filas.append({**x, **calcular(x["palabras_leidas"], x["errores"], float(x["segundos"]))})
    for nueva, vieja in zip(filas, filas[1:]):
        nueva["cambio"] = nueva["pcpm"] - vieja["pcpm"]
    return {"filas": filas, "grafico": grafico([f["pcpm"] for f in reversed(filas)])}


def grafico(valores: list[int], ancho: int = 280, alto: int = 70) -> dict | None:
    """Puntos de una línea simple en SVG (de la más vieja a la más nueva)."""
    if len(valores) < 2:
        return None
    techo = max(max(valores), 1)
    margen = 6
    paso = (ancho - 2 * margen) / (len(valores) - 1)
    puntos = [(round(margen + i * paso, 1), round(alto - margen - v * (alto - 2 * margen) / techo, 1))
              for i, v in enumerate(valores)]
    return {"ancho": ancho, "alto": alto, "techo": techo,
            "linea": " ".join(f"{x},{y}" for x, y in puntos),
            "puntos": [{"x": x, "y": y, "valor": v} for (x, y), v in zip(puntos, valores)]}
