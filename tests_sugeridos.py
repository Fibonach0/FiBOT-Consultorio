"""Qué tests conviene mirar según lo anotado en la ficha de psicopedagogía.

Son reglas locales a propósito: las notas de menores no salen a ningún servicio
externo. Cada sugerencia muestra la palabra que la disparó, para que se pueda
juzgar. Sugiere, no indica: la elección siempre es del profesional.

Las edades son orientativas (`desde` incluido, `hasta` excluido, en años) y hay
que validarlas contra el manual y el baremo que se use.
"""
import re
import unicodedata
from datetime import date

# Un test tomado hace menos de sus `meses` no se vuelve a sugerir: repetirlo
# antes mide en parte la práctica. No hay plazos oficiales por test, así que
# son criterios por tipo, que cada profesional cambia en Ajustes:
#   24  escalas de inteligencia: se usan para comparar a largo plazo.
#   12  atención, memoria y neuropsicológicos: tienen efecto de práctica
#       documentado a seis meses (Stroop, Rey).
#    6  lectura, escritura, cálculo, lenguaje, proyectivos y cuestionarios:
#       sirven para seguir la evolución de un tratamiento.
MAX_MESES = 60

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

# (nombre, edad desde, edad hasta o None si no tiene tope, meses para repetir, áreas)
CATALOGO = [
    ("WISC-V", 6, 17, 24, ("Capacidad intelectual",)),
    ("WISC-IV", 6, 17, 24, ("Capacidad intelectual",)),
    ("WPPSI-IV", 2.5, 7.6, 24, ("Capacidad intelectual",)),
    ("BAS-II", 2.5, 18, 24, ("Capacidad intelectual",)),
    ("Raven (matrices coloreadas)", 4, 12, 12, ("Capacidad intelectual",)),
    ("Figura Humana de Goodenough-Harris", 3, 15, 6, ("Capacidad intelectual", "Visomotor")),
    ("Dibujo de la Figura Humana (Koppitz)", 5, 12, 6, ("Visomotor", "Emocional")),
    ("Bender (B-G II)", 4, None, 6, ("Visomotor",)),
    ("Figura Compleja de Rey-Osterrieth", 4, None, 12, ("Visomotor", "Memoria", "Funciones ejecutivas")),
    ("Benton-Luria", 8, None, 12, ("Memoria", "Visomotor")),
    ("CUMANES", 7, 12, 12, ("Lenguaje", "Memoria", "Visomotor")),
    ("Luria Inicial", 4, 7, 12, ("Lenguaje", "Funciones ejecutivas")),
    ("PROLEC-R", 6, 13, 6, ("Lectura",)),
    ("PROLEC-SE", 12, 19, 6, ("Lectura",)),
    ("TEDE", 6, 14, 6, ("Lectura",)),
    ("TALE", 6, 11, 6, ("Lectura", "Escritura")),
    ("PROESC", 8, 16, 6, ("Escritura",)),
    ("BEVTA", 7, 13, 6, ("Lenguaje", "Lectura")),
    ("TOKEN Test", 3, 13, 6, ("Lenguaje",)),
    ("PLON-R", 3, 7, 6, ("Lenguaje",)),
    ("Peabody (PPVT)", 2.5, None, 6, ("Lenguaje",)),
    ("EVAMAT", 6, 18, 6, ("Matemática",)),
    ("TEMA-3", 3, 9, 6, ("Matemática",)),
    ("PROCALC", 6, 9, 6, ("Matemática",)),
    ("D2", 8, None, 12, ("Atención",)),
    ("CARAS-R", 6, 19, 12, ("Atención",)),
    ("Stroop", 7, None, 12, ("Atención", "Funciones ejecutivas")),
    ("ENFEN", 6, 13, 12, ("Atención", "Funciones ejecutivas")),
    ("NEPSY-II", 3, 17, 12, ("Atención", "Memoria", "Lenguaje", "Funciones ejecutivas")),
    ("BRIEF-2", 5, 19, 6, ("Funciones ejecutivas",)),
    ("CHTE", 10, None, 6, ("Hábitos de estudio",)),
    ("Test Desiderativo", 7, None, 6, ("Emocional",)),
    ("HTP (Casa-Árbol-Persona)", 5, None, 6, ("Emocional",)),
    ("Test de la Familia", 5, None, 6, ("Emocional",)),
]


def _normal(texto: str) -> str:
    sin_acentos = unicodedata.normalize("NFD", texto or "")
    return "".join(c for c in sin_acentos if not unicodedata.combining(c)).lower()


def _rango(desde: float, hasta: float | None) -> str:
    inicio = {2.5: "2 años y medio"}.get(desde, f"{desde} años")
    if hasta is None:
        return f"desde {inicio}"
    return f"{inicio.replace(' años', '')} a {int(hasta) - 1 if hasta == int(hasta) else int(hasta)} años"


def plazos(propios: dict | None) -> dict[str, int]:
    """Meses de cada test del catálogo: los del profesional pisan los de fábrica."""
    propios = propios or {}
    return {nombre: propios.get(nombre, meses) for nombre, _, _, meses, _ in CATALOGO}


def _se_puede_repetir(ultima: date, meses: int, hoy: date) -> bool:
    mes = ultima.month - 1 + meses
    anio, mes = ultima.year + mes // 12, mes % 12 + 1
    dia = min(ultima.day, [31, 29 if anio % 4 == 0 and (anio % 100 or anio % 400 == 0) else 28,
                           31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mes - 1])
    return hoy >= date(anio, mes, dia)


def sugerir(textos: list[str | None], edad: float | None,
            tomados: list[dict] | None = None, hoy: date | None = None,
            plazos_propios: dict | None = None) -> list[dict]:
    """Una fila por área detectada, con la palabra que la disparó y los tests
    que corresponden a la edad. Sin edad conocida no filtra. Lo tomado en el
    plazo de cada test no se sugiere; pasado el plazo vuelve con la fecha anterior."""
    hoy = hoy or date.today()
    meses_de = plazos(plazos_propios)
    ultima: dict[str, date] = {}
    for t in tomados or []:
        fecha = date.fromisoformat(t["fecha"])
        clave = _normal(t["test"]).strip()
        ultima[clave] = max(fecha, ultima.get(clave, fecha))
    texto = " ".join(_normal(t) for t in textos if t)
    filas = []
    for area, claves in AREAS.items():
        hallada = next((m for m in (re.search(rf"\b{re.escape(c)}\w*", texto) for c in claves) if m), None)
        if not hallada:
            continue
        tests = [
            {"nombre": nombre, "edades": _rango(desde, hasta), "ultima": ultima.get(_normal(nombre))}
            for nombre, desde, hasta, _, areas in CATALOGO
            if area in areas and (edad is None or (edad >= desde and (hasta is None or edad < hasta)))
            and (_normal(nombre) not in ultima or _se_puede_repetir(ultima[_normal(nombre)], meses_de[nombre], hoy))
        ]
        if tests:
            filas.append({"area": area, "palabra": hallada.group(0), "tests": tests})
    return filas
