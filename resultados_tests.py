"""Resultados de los tests ya corregidos y su evolución entre tomas.

El sistema NO corrige: los baremos son de las editoriales (Pearson, TEA,
Paidós) y no se pueden incluir. La profesional corrige como siempre y carga
acá los puntajes; el sistema los guarda y compara una toma con la anterior.

La comparación es descriptiva («subió», «bajó», «se mantuvo»), no dice si es
mejor o peor: en los cuestionarios de síntomas un puntaje más alto es peor.
"""

import math
import re

# tipo: (etiqueta, mínimo, máximo, umbral). Por debajo del umbral se toma
# como «se mantuvo»: un tercio de desvío aprox., para no leer ruido como
# cambio. No es una prueba de significación clínica.
TIPOS = {
    "compuesto": ("Compuesto / CI (media 100)", 40, 160, 5),
    "escalar": ("Escalar (media 10)", 1, 19, 1),
    "t": ("Puntaje T (media 50)", 10, 90, 4),
    "percentil": ("Percentil", 0, 100, 10),
    "eneatipo": ("Eneatipo / decatipo", 1, 10, 1),
    "bruto": ("Puntaje bruto", 0, 9999, 0),
}

# Escalas que se repiten en los tests más usados, para no tipearlas.
ESCALAS_CONOCIDAS = {
    "WISC-V": ["CI Total", "Comprensión Verbal", "Visoespacial", "Razonamiento Fluido",
               "Memoria de Trabajo", "Velocidad de Procesamiento"],
    "WISC-IV": ["CI Total", "Comprensión Verbal", "Razonamiento Perceptivo",
                "Memoria de Trabajo", "Velocidad de Procesamiento"],
    "WPPSI-IV": ["CI Total", "Comprensión Verbal", "Visoespacial", "Razonamiento Fluido",
                 "Memoria de Trabajo", "Velocidad de Procesamiento"],
}

FILAS_VACIAS = 3
MAX_FILAS = 30


def _numero(texto: str) -> float | None:
    texto = (texto or "").strip().replace(",", ".")
    if not texto:
        return None
    if not re.fullmatch(r"-?\d+(\.\d+)?", texto):
        raise ValueError
    valor = float(texto)
    return int(valor) if valor.is_integer() else valor


def leer_formulario(form) -> tuple[list[dict], str | None]:
    """Filas escala-N, tipo-N, puntaje-N, nivel-N → lista limpia o error."""
    filas = []
    for n in range(MAX_FILAS):
        escala = (form.get(f"escala-{n}") or "").strip()[:60]
        tipo = form.get(f"tipo-{n}") or "compuesto"
        crudo = (form.get(f"puntaje-{n}") or "").strip()
        nivel = (form.get(f"nivel-{n}") or "").strip()[:60]
        if not (escala or crudo or nivel):
            continue
        if not escala:
            return [], "Cada resultado necesita el nombre de la escala."
        if tipo not in TIPOS:
            return [], f"No conozco ese tipo de puntaje en «{escala}»."
        try:
            puntaje = _numero(crudo)
        except ValueError:
            return [], f"El puntaje de «{escala}» tiene que ser un número."
        if puntaje is None and not nivel:
            return [], f"A «{escala}» le falta el puntaje o el nivel."
        etiqueta, minimo, maximo, _ = TIPOS[tipo]
        if puntaje is not None and not minimo <= puntaje <= maximo:
            return [], f"En «{escala}», un {etiqueta.lower()} va de {minimo} a {maximo}."
        filas.append({"escala": escala, "tipo": tipo, "puntaje": puntaje, "nivel": nivel})
    nombres = [f["escala"].casefold() for f in filas]
    if len(set(nombres)) != len(nombres):
        return [], "Hay una escala repetida."
    return filas, None


def _clave(fila: dict) -> tuple[str, str]:
    return fila["escala"].casefold(), fila["tipo"]


def _mostrar(valor) -> str:
    if valor is None:
        return ""
    if isinstance(valor, float) and not math.isclose(valor, round(valor)):
        return f"{valor:g}".replace(".", ",")
    return str(int(round(valor)))


def evolucion(tomas: list[dict]) -> list[dict]:
    """tomas: las de un mismo test, de la más vieja a la más nueva, con
    «_fecha» y «resultados». Devuelve una fila por escala que aparece en
    al menos dos tomas."""
    orden: list[tuple[str, str]] = []
    por_escala: dict[tuple[str, str], dict] = {}
    for toma in tomas:
        for r in toma.get("resultados") or []:
            k = _clave(r)
            if k not in por_escala:
                orden.append(k)
                por_escala[k] = {"escala": r["escala"], "tipo": r["tipo"], "valores": []}
            por_escala[k]["valores"].append({"fecha": toma["_fecha"], "puntaje": r.get("puntaje"),
                                             "nivel": r.get("nivel") or "",
                                             "texto": _mostrar(r.get("puntaje")) or r.get("nivel") or ""})
    filas = []
    for k in orden:
        e = por_escala[k]
        if len(e["valores"]) < 2:
            continue
        antes, ahora = e["valores"][-2], e["valores"][-1]
        e["tipo_texto"] = TIPOS[e["tipo"]][0]
        e["cambio"] = ""
        if antes["puntaje"] is not None and ahora["puntaje"] is not None:
            dif = ahora["puntaje"] - antes["puntaje"]
            umbral = TIPOS[e["tipo"]][3]
            if abs(dif) <= umbral:
                e["sentido"] = "igual"
                e["cambio"] = "se mantuvo"
            else:
                e["sentido"] = "sube" if dif > 0 else "baja"
                e["cambio"] = f"{'subió' if dif > 0 else 'bajó'} {_mostrar(abs(dif))}"
        else:
            e["sentido"] = "igual" if antes["nivel"].casefold() == ahora["nivel"].casefold() else "nivel"
            e["cambio"] = "mismo nivel" if e["sentido"] == "igual" else "cambió de nivel"
        if antes["nivel"] and ahora["nivel"] and antes["nivel"].casefold() != ahora["nivel"].casefold():
            e["nivel_cambio"] = f"{antes['nivel']} → {ahora['nivel']}"
        filas.append(e)
    return filas


def por_test(tomados: list[dict]) -> list[dict]:
    """Agrupa las tomas de una ficha por test (orden del más reciente)."""
    grupos: dict[str, dict] = {}
    for x in tomados:  # vienen de la más nueva a la más vieja
        g = grupos.setdefault(x["test"].casefold(), {"test": x["test"], "tomas": []})
        x["resultados"] = [{**r, "texto": _mostrar(r.get("puntaje"))} for r in x.get("resultados") or []]
        g["tomas"].append(x)
    conocidas = {k.casefold(): v for k, v in ESCALAS_CONOCIDAS.items()}
    for g in grupos.values():
        g["evolucion"] = evolucion(list(reversed(g["tomas"])))
        escalas = list(conocidas.get(g["test"].casefold(), []))
        for toma in g["tomas"]:
            for r in toma["resultados"]:
                if r["escala"].casefold() not in {e.casefold() for e in escalas}:
                    escalas.append(r["escala"])
        g["escalas"] = escalas
    return list(grupos.values())
