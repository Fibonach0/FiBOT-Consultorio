"""Smoke test de la portada (/), /turnos (redirige), /sumate,
robots, sitemap y datos estructurados. Correrlo suelto (`python test_inicio.py`),
mismo motivo que los otros test_*.py: monkeypatchea `db` sin restaurarlo."""
import json
import os
import re

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault(
    "SUPABASE_SERVICE_ROLE_KEY",
    "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.fake_signature_not_real",
)
os.environ.setdefault("META_ACCESS_TOKEN", "fake")
os.environ.setdefault("META_APP_SECRET", "fake")
os.environ.setdefault("META_VERIFY_TOKEN", "fake")
os.environ.setdefault("FLASK_SECRET_KEY", "fake")

import scheduler  # noqa: E402

scheduler.iniciar = lambda: None

import app  # noqa: E402
import db  # noqa: E402
import directorio  # noqa: E402
import whatsapp  # noqa: E402

client = app.app.test_client()

ANA = {"id": "t1", "nombre": "Lic. Ana Pérez", "rubro": "psicología", "slug": "anaperez", "activo": True,
       "titulo": "Psicóloga clínica", "enfoques": ["Ataques de pánico", "Duelos", "Pareja", "Autoestima"],
       "foto": None, "modalidades_publicas": "ambas", "obras_sociales": "OSDE, Swiss Medical",
       "ubicacion": "Palermo, CABA", "timezone": "America/Argentina/Buenos_Aires"}
TENANTS = [
    ANA,
    {"id": "t2", "nombre": "Lic. Oculto", "slug": "oculto", "activo": True, "en_directorio": False},
    {"id": "t3", "nombre": "Sin Página", "slug": None, "activo": True},
    {"id": "t4", "nombre": "Lic. Beto Pausa", "slug": "beto", "activo": True, "pausado": True,
     "rubro": "psicopedagogía", "modalidades_publicas": "virtual"},
]
db.tenants_activos = lambda: TENANTS
db.turnos_libres = lambda *a, **k: []

# ---- portada: genérica, sin profesionales (cada uno tiene su sitio en /<slug>)
html = client.get("/").data.decode()
assert "Sacá turno con tu profesional" in html and "Para mi hijo/a" in html
for _t in TENANTS:
    assert _t["nombre"] not in html, _t["nombre"]
assert "/anaperez" not in html and "/beto" not in html and 'href="/turnos' not in html and 'name="obra"' not in html
assert 'href="/sumate"' in html and "No hay un listado de profesionales" not in html and "Acá no hay un listado" in html
assert "google-site-verification" not in html
ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S).group(1))
assert ld["@type"] == "WebSite"
app.config.GOOGLE_SITE_VERIFICATION = "abc123"
assert '<meta name="google-site-verification" content="abc123">' in client.get("/").data.decode()
app.config.GOOGLE_SITE_VERIFICATION = ""
print("GET / -> portada genérica: ningún profesional ni link a la lista, JSON-LD y verificación de Google OK")

r = client.get("/turnos?tema=ansiedad")
assert r.status_code == 301 and r.headers["Location"].endswith("/")
print("/turnos (la lista vieja) redirige a la portada OK")

# ---- sumate
guardados, avisos = [], []
db.crear_interesado = lambda d: guardados.append(d)
db.registrar_actividad = lambda *a, **k: None
db.intentos_login = lambda claves: []
db.guardar_intento_login = lambda clave, datos: None
whatsapp.avisar_admin = lambda texto: avisos.append(texto) or True

html = client.get("/sumate").data.decode()
assert "/anaperez" not in html                     # el ejemplo es sólo una cuenta de muestra
TENANTS.append({"id": "t9", "nombre": "Lic. Muestra", "slug": "demo", "activo": True, "foto": "x"})
assert 'href="/demo"' in client.get("/sumate").data.decode()
TENANTS.pop()
r = client.post("/sumate", data={"nombre": "", "whatsapp": "1123456789"})
assert r.status_code == 400 and not guardados
r = client.post("/sumate", data={"nombre": "Laura Gómez", "profesion": "Psicología"})
assert r.status_code == 400 and "WhatsApp o un mail" in r.data.decode()
r = client.post("/sumate", data={"nombre": "Robot", "whatsapp": "1123456789", "web": "http://spam"})
assert r.status_code == 200 and not guardados
r = client.post("/sumate", data={"nombre": "  Laura   Gómez ", "profesion": "Psicología", "whatsapp": "+54 9 11 2345-6789",
                                 "email": "", "zona": "Belgrano", "mensaje": "Hola"})
assert r.status_code == 200 and "Listo, gracias" in r.data.decode()
assert guardados == [{"nombre": "Laura Gómez", "profesion": "Psicología", "whatsapp": "5491123456789",
                      "email": None, "zona": "Belgrano", "mensaje": "Hola"}]
assert avisos and "Laura Gómez" in avisos[0]
r = client.post("/sumate", data={"nombre": "X", "profesion": "Astrología", "email": "x@ejemplo.com"})
assert guardados[-1]["profesion"] is None
def _sin_tabla(d):
    raise RuntimeError("relation interesados does not exist")
db.crear_interesado = _sin_tabla
import logging  # noqa: E402
logging.getLogger("app").disabled = True
avisos.clear()
r = client.post("/sumate", data={"nombre": "Sin Tabla", "whatsapp": "1123456789"})
assert r.status_code == 200 and avisos and "5491123456789" not in avisos[0] and "1123456789" in avisos[0]
whatsapp.avisar_admin = lambda texto: False
r = client.post("/sumate", data={"nombre": "Sin Tabla", "whatsapp": "1123456789"})
assert r.status_code == 503 and "No se pudo enviar" in r.data.decode()
logging.getLogger("app").disabled = False
print("/sumate valida, ignora robots, guarda limpio y avisa a administración (sin tabla, manda el contacto) OK")
assert "sumate" in app.SLUGS_RESERVADOS

# ---- robots y sitemap
txt = client.get("/robots.txt").data.decode()
assert "Disallow: /agenda" in txt and "Disallow: /admin" in txt and "Sitemap: http://localhost/sitemap.xml" in txt
xml = client.get("/sitemap.xml").data.decode()
assert "<loc>http://localhost/anaperez</loc>" in xml and "<loc>http://localhost/beto</loc>" in xml
assert "oculto" not in xml and "<loc>http://localhost/sumate</loc>" in xml and "/turnos" not in xml
print("robots.txt y sitemap.xml con la página de cada profesional (menos los que la ocultaron) OK")

# ---- datos estructurados del profesional
with app.app.test_request_context():
    d = app.perfil_ld(ANA)
assert d["@type"] == "Person" and d["jobTitle"] == "Psicóloga clínica" and d["workLocation"]["name"] == "Palermo, CABA"
assert "image" not in d and d["knowsAbout"][0] == "Ataques de pánico"
print("datos estructurados de la página del profesional OK")

# ---- base caída (el logger.exception es lo esperado: se apaga para no ensuciar el CI)
import logging  # noqa: E402
logging.getLogger("app").disabled = True
db.tenants_activos = lambda: (_ for _ in ()).throw(RuntimeError("base caída"))
assert client.get("/").status_code == 200 and client.get("/sitemap.xml").status_code == 200
print("con la base caída portada y sitemap salen igual OK")

assert client.get("/health").data == b"FiBOT Consultorio activo."
print("/health sigue igual OK")

# Un reemplazo a medias dejó «var(--card)df5» (CSS inválido = fondo transparente):
# la ficha abierta se veía con el panel atrás. Ningún var() puede quedar pegado a un color.
import glob  # noqa: E402
import re as _re  # noqa: E402
for _ruta in glob.glob("templates/*.html"):
    _malo = _re.search(r"var\(--[a-z0-9-]+\)[0-9a-fA-F]{2,6}\b", open(_ruta, encoding="utf8").read())
    assert not _malo, (_ruta, _malo.group(0))
print("ningún var(--x) pegado a restos de un color en los templates OK")

# Cada GIF de «Recorrido por tu panel» existe en static/ayuda (si se renombra una escena, se rompe la guía).
_ayuda = open("templates/ayuda.html", encoding="utf8").read()
_gifs = _re.findall(r'\("([a-z-]+)", "[^"]+", "[^"]+"\)', _ayuda[_ayuda.index('id="recorrido"'):])
assert len(_gifs) >= 10 and all(os.path.exists(f"static/ayuda/{g}.gif") for g in _gifs), _gifs
assert all(os.path.getsize(f"static/ayuda/{g}.gif") < 800_000 for g in _gifs), "GIF demasiado pesado"
print("recorrido de la guía: cada GIF existe y pesa menos de 800 KB OK")

# ---- pausados: la tarjeta dice cuándo vuelve, no «lista de espera»
assert app._vuelve({"pausado": True}) == "Vuelve a atender pronto"
from datetime import date as _date
_p = {"pausado": True, "pausado_hasta": "2026-12-03"}
assert app._vuelve(_p, _date(2026, 11, 30)) == "Vuelve el 3/12 · faltan 3 días"
assert app._vuelve(_p, _date(2026, 12, 2)) == "Vuelve mañana (3/12)"
assert app._vuelve(_p, _date(2026, 12, 3)) == "Vuelve hoy"
print("pausados: «Vuelve a atender pronto» / «Vuelve el 3/12 · faltan 3 días» / mañana / hoy OK")
