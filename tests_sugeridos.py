"""Qué tests conviene mirar según lo anotado en la ficha de psicopedagogía.

Son reglas locales a propósito: las notas de menores no salen a ningún servicio
externo. Cada sugerencia muestra la palabra que la disparó, para que se pueda
juzgar. Sugiere, no indica: la elección siempre es del profesional.

Las edades son orientativas (`desde` incluido, `hasta` excluido, en años) y hay
que validarlas contra el manual y el baremo que se use.
"""
import re
import unicodedata

AREAS = {
    "Lectura": ("lectu", "lector", "lee", "dislex", "decodific", "silab", "fluidez", "lectoescritura"),
    "Escritura": ("escrit", "escribi", "ortograf", "disgraf", "dictado", "caligraf", "lectoescritura"),
    "Matemática": ("matemat", "calcul", "numer", "discalcul", "resolucion de problemas", "multiplic"),
    "Atención": ("atencion", "atent", "distra", "concentr", "tdah", "tda", "impuls", "hiperactiv", "inquiet"),
    "Capacidad intelectual": ("intelig", "cognitiv", "repit", "repite", "rendimiento", "discapacidad", "cud"),
    "Lenguaje": ("lenguaje", "habla", "vocabul", "fonolog", "tartamud", "comprension verbal", "expresion oral"),
    "Visomotor": ("visomot", "motricidad", "grafismo", "coordinacion", "dibuj", "espacial", "lateralidad", "visoconstruc"),
    "Memoria": ("memoria", "olvid", "recuerd"),
    "Funciones ejecutivas": ("funciones ejecutivas", "planific", "organiz", "flexibilidad", "inhibi"),
    "Hábitos de estudio": ("habitos", "estudio", "tecnicas de estudio"),
    "Emocional": ("emocion", "ansiedad", "autoestima", "angusti", "vincul", "duelo", "separacion", "conducta", "miedo", "tristeza"),
}

# (nombre, edad desde, edad hasta o None si no tiene tope, áreas)
CATALOGO = [
    ("WISC-V", 6, 17, ("Capacidad intelectual",)),
    ("WISC-IV", 6, 17, ("Capacidad intelectual",)),
    ("WPPSI-IV", 2.5, 7.6, ("Capacidad intelectual",)),
    ("BAS-II", 2.5, 18, ("Capacidad intelectual",)),
    ("Raven (matrices coloreadas)", 4, 12, ("Capacidad intelectual",)),
    ("Figura Humana de Goodenough-Harris", 3, 15, ("Capacidad intelectual", "Visomotor")),
    ("Dibujo de la Figura Humana (Koppitz)", 5, 12, ("Visomotor", "Emocional")),
    ("Bender (B-G II)", 4, None, ("Visomotor",)),
    ("Figura Compleja de Rey-Osterrieth", 4, None, ("Visomotor", "Memoria", "Funciones ejecutivas")),
    ("Benton-Luria", 8, None, ("Memoria", "Visomotor")),
    ("CUMANES", 7, 12, ("Lenguaje", "Memoria", "Visomotor")),
    ("Luria Inicial", 4, 7, ("Lenguaje", "Funciones ejecutivas")),
    ("PROLEC-R", 6, 13, ("Lectura",)),
    ("PROLEC-SE", 12, 19, ("Lectura",)),
    ("TEDE", 6, 14, ("Lectura",)),
    ("TALE", 6, 11, ("Lectura", "Escritura")),
    ("PROESC", 8, 16, ("Escritura",)),
    ("BEVTA", 7, 13, ("Lenguaje", "Lectura")),
    ("TOKEN Test", 3, 13, ("Lenguaje",)),
    ("PLON-R", 3, 7, ("Lenguaje",)),
    ("Peabody (PPVT)", 2.5, None, ("Lenguaje",)),
    ("EVAMAT", 6, 18, ("Matemática",)),
    ("TEMA-3", 3, 9, ("Matemática",)),
    ("PROCALC", 6, 9, ("Matemática",)),
    ("D2", 8, None, ("Atención",)),
    ("CARAS-R", 6, 19, ("Atención",)),
    ("Stroop", 7, None, ("Atención", "Funciones ejecutivas")),
    ("ENFEN", 6, 13, ("Atención", "Funciones ejecutivas")),
    ("NEPSY-II", 3, 17, ("Atención", "Memoria", "Lenguaje", "Funciones ejecutivas")),
    ("BRIEF-2", 5, 19, ("Funciones ejecutivas",)),
    ("CHTE", 10, None, ("Hábitos de estudio",)),
    ("Test Desiderativo", 7, None, ("Emocional",)),
    ("HTP (Casa-Árbol-Persona)", 5, None, ("Emocional",)),
    ("Test de la Familia", 5, None, ("Emocional",)),
]


def _normal(texto: str) -> str:
    sin_acentos = unicodedata.normalize("NFD", texto or "")
    return "".join(c for c in sin_acentos if not unicodedata.combining(c)).lower()


def _rango(desde: float, hasta: float | None) -> str:
    inicio = {2.5: "2 años y medio"}.get(desde, f"{desde} años")
    if hasta is None:
        return f"desde {inicio}"
    return f"{inicio.replace(' años', '')} a {int(hasta) - 1 if hasta == int(hasta) else int(hasta)} años"


def sugerir(textos: list[str | None], edad: float | None) -> list[dict]:
    """Una fila por área detectada, con la palabra que la disparó y los tests
    que corresponden a la edad. Sin edad conocida no filtra."""
    texto = " ".join(_normal(t) for t in textos if t)
    filas = []
    for area, claves in AREAS.items():
        hallada = next((m for m in (re.search(rf"\b{re.escape(c)}\w*", texto) for c in claves) if m), None)
        if not hallada:
            continue
        tests = [
            {"nombre": nombre, "edades": _rango(desde, hasta)}
            for nombre, desde, hasta, areas in CATALOGO
            if area in areas and (edad is None or (edad >= desde and (hasta is None or edad < hasta)))
        ]
        if tests:
            filas.append({"area": area, "palabra": hallada.group(0), "tests": tests})
    return filas
