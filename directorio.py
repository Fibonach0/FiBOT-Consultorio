"""Directorio público de profesionales: quiénes aparecen y cómo se filtran.

Lo usan la portada (`/`), `/turnos` y el sitemap. Los temas son los de la
portada; cada uno se reconoce por palabras sueltas (sin tildes) en lo que el
profesional escribió en su perfil: temas que trabaja, descripción, sobre mí,
título y rubro. No hay una lista cerrada de temas en el perfil a propósito: el
profesional escribe con sus palabras y acá se lo encuentra igual.
"""
import unicodedata

# clave → (nombre, bajada de la portada, palabras que lo identifican)
TEMAS = {
    "ansiedad": ("Ansiedad", "Preocupación constante, nervios, ataques de pánico.",
                 ("ansiedad", "ansios", "panico", "angustia", "obsesi", "toc")),
    "animo": ("Ánimo", "Tristeza, desgano, sentir que nada tiene sentido.",
              ("animo", "depresi", "triste", "desgano", "bipolar")),
    "estres": ("Estrés y cansancio", "Trabajo, estudio, sentir que no das más.",
               ("estres", "burnout", "agotamiento", "cansancio", "laboral", "insomnio", "sueno")),
    "vinculos": ("Vínculos", "Pareja, familia, amistades, separaciones.",
                 ("vincul", "pareja", "familia", "relacion", "separacion", "divorcio", "sexualidad")),
    "autoestima": ("Autoestima", "Inseguridades, cómo te ves y te tratás.",
                   ("autoestima", "insegur", "identidad", "imagen corporal", "alimentari")),
    "duelos": ("Duelos y cambios", "Pérdidas, mudanzas, etapas nuevas.",
               ("duelo", "perdida", "migra", "mudanza", "jubila", "maternidad", "paternidad", "crisis vital")),
    "infancia": ("Infancia y adolescencia", "Aprendizaje, conducta, acompañar a tus hijos.",
                 ("infan", "nino", "nina", "adolesc", "crianza", "aprendizaje", "escolar",
                  "psicopedagog", "tdah", "autismo", "tea ")),
    "miedos": ("Miedos", "Fobias, miedos que te frenan en el día a día.",
               ("miedo", "fobia", "panico")),
}

MODALIDADES = {"presencial": ("presencial", "ambas"), "virtual": ("virtual", "ambas")}


def normalizar(texto: str | None) -> str:
    """Minúsculas y sin tildes («Pánico» → «panico»)."""
    sin_tildes = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return " ".join(sin_tildes.casefold().split())


def visibles(tenants: list[dict]) -> list[dict]:
    """Los que tienen página pública y no pidieron quedar fuera del directorio."""
    return [t for t in tenants if t.get("slug") and t.get("en_directorio", True) is not False]


def _texto_perfil(t: dict) -> str:
    partes = [t.get("descripcion"), t.get("sobre_mi"), t.get("titulo"), t.get("rubro"),
              " ".join(str(x) for x in (t.get("enfoques") or []))]
    return " " + normalizar(" ".join(p for p in partes if p)) + " "


def trabaja_tema(t: dict, tema: str) -> bool:
    if tema not in TEMAS:
        return True
    texto = _texto_perfil(t)
    return any(p in texto for p in TEMAS[tema][2])


def filtrar(tenants: list[dict], tema: str = "", modalidad: str = "", obra: str = "") -> list[dict]:
    """Filtros de /turnos. Un valor vacío o desconocido no filtra."""
    obra_n = normalizar(obra)
    salida = []
    for t in tenants:
        if tema and not trabaja_tema(t, tema):
            continue
        if modalidad in MODALIDADES and (t.get("modalidades_publicas") or "presencial") not in MODALIDADES[modalidad]:
            continue
        if obra_n and obra_n not in normalizar(t.get("obras_sociales")):
            continue
        salida.append(t)
    return salida
