"""Lo que cambia del panel según el rubro del profesional.

Un solo sistema: el rubro sólo decide textos y si la ficha pide datos de la
escuela. `tenants.rubro` es texto libre, así que se compara sin acentos ni
mayúsculas. Cualquier rubro que no esté acá usa los textos de siempre.
"""
import unicodedata

_BASE = {
    "pestanias": [("terapia", "Terapia"), ("sesiones", "Sesiones"), ("crisis", "Crisis"),
                  ("personas", "Personas"), ("datos", "Datos")],
    "motivo": "Motivo de consulta",
    "tratantes": "Psiquiatra",
    "indicaciones": "Qué hacer en una crisis",
    "contacto": "WhatsApp",
    "vinculo_ejemplo": "Madre, perro…",
    "fecha_ejemplo": "Navidad, aniversario, fallecimiento de…",
    "escuela": False,
    "tests": False,
    # Cuestionarios por link que se ofrecen (claves de cuestionarios.INSTRUMENTOS).
    "cuestionarios": ["phq9", "gad7"],
}

_PSICOPEDAGOGIA = {
    **_BASE,
    "pestanias": [("terapia", "Trabajo"), ("sesiones", "Sesiones"), ("crisis", "Escuela"),
                  ("personas", "Familia"), ("datos", "Datos")],
    "motivo": "Motivo de consulta y quién deriva",
    "tratantes": "Otros profesionales",
    "indicaciones": "A tener en cuenta",
    "contacto": "WhatsApp de la familia",
    "vinculo_ejemplo": "Madre, padre, hermana…",
    "fecha_ejemplo": "Entrega de boletín, reunión con la escuela…",
    "escuela": True,
    "tests": True,
    # PHQ-9 y GAD-7 son para adultos; SNAP-IV y SCARED esperan licencia.
    "cuestionarios": [],
}


def _normal(texto: str | None) -> str:
    sin_acentos = unicodedata.normalize("NFD", texto or "")
    return "".join(c for c in sin_acentos if not unicodedata.combining(c)).strip().lower()


def de(tenant: dict) -> dict:
    return _PSICOPEDAGOGIA if _normal(tenant.get("rubro")).startswith("psicopedagog") else _BASE
