"""Borrador de informe: junta lo cargado en la ficha y arma frases simples.

Reglas locales a propósito, sin servicios externos (son datos de pacientes,
muchas veces menores). El texto es un punto de partida: la profesional lo
edita en pantalla antes de imprimir. Nunca interpreta clínicamente: describe
puntajes y cambios con las mismas palabras que la ficha.
"""

from datetime import date


def _edad(nacimiento: str | None, hoy: date) -> str | None:
    if not nacimiento:
        return None
    n = date.fromisoformat(nacimiento)
    anios = hoy.year - n.year - ((hoy.month, hoy.day) < (n.month, n.day))
    meses = (hoy.month - n.month - (hoy.day < n.day)) % 12
    return f"{anios} años y {meses} mes{'es' if meses != 1 else ''}" if meses else f"{anios} años"


def frases_tests(grupos: list[dict]) -> list[str]:
    frases = []
    for g in grupos:
        ultima = g["tomas"][0]
        if not ultima["resultados"]:
            continue
        partes = [f"{r['escala']}: {r['texto'] or r['nivel']}" + (f" ({r['nivel']})" if r["texto"] and r["nivel"] else "")
                  for r in ultima["resultados"]]
        frases.append(f"{g['test']} ({ultima['_fecha']}): " + "; ".join(partes) + ".")
        for e in g["evolucion"]:
            antes, ahora = e["valores"][-2], e["valores"][-1]
            if e["sentido"] == "igual":
                frases.append(f"En {e['escala']} se mantuvo respecto de la toma del {antes['fecha']} ({antes['texto']} → {ahora['texto']}).")
            elif e["sentido"] in ("sube", "baja"):
                frases.append(f"En {e['escala']} {e['cambio'].split()[0]} de {antes['texto']} a {ahora['texto']} respecto de la toma del {antes['fecha']}.")
            elif e.get("nivel_cambio"):
                frases.append(f"En {e['escala']} pasó de «{antes['nivel']}» a «{ahora['nivel']}».")
    return frases


def frases_fluidez(fluidez: dict) -> list[str]:
    filas = fluidez.get("filas") or []
    if not filas:
        return []
    u = filas[0]
    frase = (f"En la lectura del {u['_fecha']} ({u['texto_titulo']}) leyó {u['pcpm']} palabras correctas por minuto, "
             f"con {u['precision']}% de precisión.")
    if len(filas) > 1:
        p = filas[-1]
        frase += f" En la primera lectura registrada ({p['_fecha']}) había leído {p['pcpm']}."
    return [frase]


def frases_cuestionarios(cuestionarios: dict) -> list[str]:
    frases = []
    for g in (cuestionarios or {}).get("grupos", []):
        u = g["respuestas"][0]
        frase = f"{g['nombre']} ({u['_fecha']}): {u['puntaje']} de {g['maximo']}, severidad {u['severidad']}."
        if len(g["respuestas"]) > 1:
            p = g["respuestas"][1]
            frase += f" La vez anterior ({p['_fecha']}) había dado {p['puntaje']}."
        frases.append(frase)
    return frases


def armar(ficha: dict, rubro: dict, hoy: date) -> dict:
    datos = [("Nombre", ficha["nombre"]), ("Edad", _edad(ficha.get("fecha_nacimiento"), hoy))]
    if rubro.get("escuela"):
        datos += [("Escuela", ficha.get("escuela")), ("Grado", ficha.get("grado"))]
    datos.append(("En tratamiento desde", ficha.get("desde")))
    return {
        "titulo": "Informe psicopedagógico" if rubro.get("tests") else "Informe psicológico",
        "datos": [(k, v) for k, v in datos if v],
        "motivo": ficha.get("motivo_consulta") or "",
        "tests": frases_tests(ficha.get("tests_por_test") or []),
        "fluidez": frases_fluidez(ficha.get("fluidez") or {}),
        "cuestionarios": frases_cuestionarios(ficha.get("cuestionarios") or {}),
        "objetivos": ficha.get("objetivos") or "",
    }
