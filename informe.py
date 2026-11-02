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


# Una sección de plantilla se completa sola si su título contiene una de estas
# palabras: «Motivo de derivación» trae el motivo, «Evaluación inicial» los tests.
AUTOMATICAS = [("motivo", "motivo"), ("evaluaci", "tests"), ("test", "tests"), ("lectura", "lectura"),
               ("cuestionario", "cuestionarios"), ("objetivo", "objetivos")]
MAX_PLANTILLA = 30


def _automatico(titulo: str, textos: dict) -> str:
    t = titulo.casefold()
    return next((textos[clave] for palabra, clave in AUTOMATICAS if palabra in t), "")


def leer_plantilla(nombre: str, crudo: str) -> tuple[dict | None, str | None]:
    import json
    nombre = (nombre or "").strip()[:60]
    if not nombre:
        return None, "La plantilla necesita un nombre."
    try:
        titulos = [str(t).strip()[:MAX_TITULO] for t in json.loads(crudo or "")]
    except (ValueError, TypeError):
        return None, "La plantilla llegó incompleta. Probá de nuevo."
    titulos = [t for t in titulos if t][:MAX_PLANTILLA]
    if not titulos:
        return None, "La plantilla no tiene secciones."
    return {"nombre": nombre, "secciones": titulos}, None


def armar(ficha: dict, rubro: dict, hoy: date, plantilla: dict | None = None) -> dict:
    datos = [("Nombre", ficha["nombre"]), ("Edad", _edad(ficha.get("fecha_nacimiento"), hoy))]
    if rubro.get("escuela"):
        datos += [("Escuela", ficha.get("escuela")), ("Grado", ficha.get("grado"))]
    datos.append(("En tratamiento desde", ficha.get("desde")))
    tests = frases_tests(ficha.get("tests_por_test") or [])
    lectura = frases_fluidez(ficha.get("fluidez") or {})
    cuestionarios = frases_cuestionarios(ficha.get("cuestionarios") or {})
    if plantilla:
        textos = {"motivo": ficha.get("motivo_consulta") or "", "tests": "\n".join(tests), "lectura": "\n".join(lectura),
                  "cuestionarios": "\n".join(cuestionarios), "objetivos": ficha.get("objetivos") or ""}
        secciones = [{"titulo": t, "texto": _automatico(t, textos), "vacio": "Escribí acá."} for t in plantilla["secciones"]]
        return {"titulo": plantilla["nombre"], "datos": [(k, v) for k, v in datos if v], "tests": tests,
                "fluidez": lectura, "cuestionarios": cuestionarios, "secciones": secciones}
    secciones = [{"titulo": "Motivo de consulta", "texto": ficha.get("motivo_consulta") or "", "vacio": "Por qué consultan y quién deriva."}]
    if tests:
        secciones.append({"titulo": "Evaluación", "texto": "\n".join(tests), "vacio": ""})
    if lectura:
        secciones.append({"titulo": "Lectura", "texto": "\n".join(lectura), "vacio": ""})
    if cuestionarios:
        secciones.append({"titulo": "Cuestionarios", "texto": "\n".join(cuestionarios), "vacio": ""})
    secciones += [
        {"titulo": "Objetivos de trabajo", "texto": ficha.get("objetivos") or "", "vacio": "Qué se está trabajando."},
        {"titulo": "Conclusiones", "texto": "", "vacio": "Escribí acá tus conclusiones."},
        {"titulo": "Sugerencias", "texto": "", "vacio": "Para la familia, la escuela u otros profesionales."},
    ]
    return {
        "titulo": "Informe psicopedagógico" if rubro.get("tests") else "Informe psicológico",
        "datos": [(k, v) for k, v in datos if v],
        "tests": tests,
        "fluidez": lectura,
        "cuestionarios": cuestionarios,
        "secciones": secciones,
    }


MAX_SECCIONES = 30
MAX_TITULO = 120
MAX_TEXTO = 20000


def leer_guardado(crudo: str) -> tuple[dict | None, str | None]:
    """Lo que manda la pantalla al guardar (JSON) → contenido limpio o error."""
    import json
    try:
        datos = json.loads(crudo or "")
        titulo = str(datos["titulo"]).strip()[:MAX_TITULO]
        secciones = [{"titulo": str(x["titulo"]).strip()[:MAX_TITULO], "texto": str(x["texto"]).strip()[:MAX_TEXTO]}
                     for x in datos["secciones"]]
        filas = [[str(k)[:60], str(v)[:200]] for k, v in datos.get("datos") or []]
    except (ValueError, KeyError, TypeError):
        return None, "El informe llegó incompleto. Probá de nuevo."
    if not titulo or len(secciones) > MAX_SECCIONES:
        return None, "El informe llegó incompleto. Probá de nuevo."
    if not any(x["texto"] for x in secciones):
        return None, "El informe está vacío."
    return {"titulo": titulo, "datos": filas, "secciones": secciones}, None
