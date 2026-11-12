"""Qué páginas de profesionales se ofrecen a los buscadores.

Ya no hay lista de profesionales en el sitio: cada uno tiene su página en
/<slug> y la portada es genérica. Esto sólo decide qué entra al sitemap (y
qué páginas van indexables) y cuáles son cuentas de muestra.
"""


def visibles(tenants: list[dict]) -> list[dict]:
    """Los que tienen página pública y no pidieron ocultarla de Google
    (`en_directorio = false`)."""
    return [t for t in tenants if t.get("slug") and t.get("en_directorio", True) is not False]


def es_demo(t: dict) -> bool:
    """Las cuentas de muestra (`demo`, `demo-…`): las únicas que se muestran
    como ejemplo en /sumate."""
    slug = t.get("slug") or ""
    return slug == "demo" or slug.startswith("demo-")
