"""Smoke test de la portada del sitio (/). Correrlo suelto (`python test_inicio.py`),
mismo motivo que los otros test_*.py: monkeypatchea `db` sin restaurarlo."""
import os

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

client = app.app.test_client()

TENANTS = [
    {"id": "t1", "nombre": "Lic. Ana Pérez", "rubro": "psicología", "slug": "anaperez", "activo": True,
     "titulo": "Psicóloga clínica", "enfoques": ["Ansiedad", "Duelos", "Pareja", "Autoestima"], "foto": None},
    {"id": "t2", "nombre": "Lic. Oculto", "slug": "oculto", "activo": True, "en_directorio": False},
    {"id": "t3", "nombre": "Sin Página", "slug": None, "activo": True},
    {"id": "t4", "nombre": "Lic. Beto Pausa", "slug": "beto", "activo": True, "pausado": True},
]
db.tenants_activos = lambda: TENANTS

html = client.get("/").data.decode()
assert "Encontrá a tu psicólogo" in html
assert "Lic. Ana Pérez" in html and "/anaperez" in html and "Psicóloga clínica" in html
assert "Ansiedad" in html and "Autoestima" not in html.split('id="profesionales"')[1].split("</section>")[0]
assert "Lic. Oculto" not in html and "Sin Página" not in html
assert "lista de espera" in html
assert html.index("Lic. Ana Pérez") < html.index("Lic. Beto Pausa")
assert "Quiero sumarme" not in html
print("GET / -> portada con los profesionales del directorio, sin ocultos ni sin página OK")

os.environ["CONTACTO_PROFESIONALES"] = "mailto:hola@example.com"
assert "Quiero sumarme" in client.get("/").data.decode()
print("con CONTACTO_PROFESIONALES aparece «Quiero sumarme» OK")

db.tenants_activos = lambda: (_ for _ in ()).throw(RuntimeError("base caída"))
r = client.get("/")
assert r.status_code == 200 and 'id="profesionales"' not in r.data.decode()
print("con la base caída la portada sale igual, sin la grilla OK")

assert client.get("/health").data == b"FiBOT Consultorio activo."
print("/health sigue igual OK")
