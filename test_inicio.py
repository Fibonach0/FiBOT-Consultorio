"""Smoke test de la portada (/), el directorio con filtros (/turnos), /sumate,
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

# ---- portada
html = client.get("/").data.decode()
assert "Encontrá a tu profesional" in html and "Para mi hijo/a" in html
assert "Lic. Ana Pérez" in html and "/anaperez" in html and "Psicóloga clínica" in html
assert "Lic. Oculto" not in html and "Sin Página" not in html
assert html.index("Lic. Ana Pérez") < html.index("Lic. Beto Pausa")
assert "vuelve a atender pronto" in html and ">lista de espera<" not in html   # Beto está pausado
assert "/turnos?tema=ansiedad" in html and 'name="obra"' in html
assert 'href="/sumate"' in html
ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S).group(1))
assert ld["@type"] == "WebSite"
# Para mi hijo/a: sólo Beto (psicopedagogía) está en esa vista; Ana sólo en «Para mí».
assert re.search(r'href="/anaperez" data-para-en="yo"', html)
assert re.search(r'href="/beto" data-para-en="yo hijo"', html)
assert "/turnos?tema=aprendizaje" in html and "Trabajan ansiedad:" in html
assert 'src="/static/portada/ansiedad.webp"' in html
print("GET / -> portada con buscador, temas que filtran, profesionales y JSON-LD OK")

# ---- filtros
assert directorio.normalizar("Pánico  Ñandú") == "panico nandu"
assert directorio.trabaja_tema(ANA, "ansiedad")           # «pánico» con tilde
assert directorio.trabaja_tema(ANA, "vinculos")           # «pareja»
assert not directorio.trabaja_tema(ANA, "infancia")
assert directorio.trabaja_tema(TENANTS[3], "infancia")    # por el rubro psicopedagogía
nombres = lambda **q: [t["nombre"] for t in directorio.filtrar(directorio.visibles(TENANTS), **q)]
assert nombres(modalidad="presencial") == ["Lic. Ana Pérez"]
assert nombres(modalidad="virtual") == ["Lic. Ana Pérez", "Lic. Beto Pausa"]
assert nombres(obra="osde") == ["Lic. Ana Pérez"] and nombres(obra="ioma") == []
assert nombres(tema="desconocido") == ["Lic. Ana Pérez", "Lic. Beto Pausa"]

html = client.get("/turnos?tema=infancia").data.decode()
assert "Lic. Beto Pausa" in html and "Lic. Ana Pérez" not in html and "Infancia y adolescencia" in html
html = client.get("/turnos?obra=ioma").data.decode()
assert "No encontramos profesionales" in html and "noindex" in html
html = client.get("/turnos?tema=<script>").data.decode()
assert "Elegí con quién hablar." in html and "Lic. Ana Pérez" in html
print("/turnos filtra por tema (sin tildes, también por rubro), modalidad y obra social OK")

# ---- /turnos con el diseño nuevo: especialidad, próximos libres, esta semana y orden
from datetime import datetime as _dt, timedelta as _td
from zoneinfo import ZoneInfo as _Z
_ahora = _dt.now(_Z("UTC"))
LIBRES = {"t1": [{"id": "a1", "inicio": (_ahora + _td(days=10)).isoformat()}],
          "t4": []}
db.turnos_libres = lambda tid, desde, limite=8: LIBRES.get(tid, [])[:limite]
app._proximos_cache.clear()
html = client.get("/turnos").data.decode()
assert "/anaperez/reservar/a1" in html and "Próximos libres" in html          # el horario lleva directo a reservar
assert "Vuelve a atender pronto" in html and "Por ahora no toma turnos nuevos" in html  # Beto está pausado
assert html.index("Lic. Ana Pérez") < html.index("Lic. Beto Pausa")           # con turno, primero
html = client.get("/turnos?especialidad=psicopedagogia").data.decode()
assert "Lic. Beto Pausa" in html and "Lic. Ana Pérez" not in html and "Lectoescritura" in html
assert "Psicología <span>1</span>" in html and "Psicopedagogía <span>1</span>" in html
html = client.get("/turnos?semana=1").data.decode()                           # Ana tiene el primero a 10 días
assert "No encontramos profesionales" in html and "noindex" in html
LIBRES["t1"] = [{"id": "a2", "inicio": (_ahora + _td(days=2)).isoformat()}]
app._proximos_cache.clear()
html = client.get("/turnos?semana=1").data.decode()
assert "Lic. Ana Pérez" in html and "Lic. Beto Pausa" not in html
html = client.get("/turnos?orden=nombre&tema=ansiedad").data.decode()
assert 'href="/turnos?tema=ansiedad"' in html                                 # «Próximo turno» saca el orden, deja el tema
assert 'href="/turnos?tema=ansiedad&amp;orden=nombre" aria-current="true"' in html
# con varios tipos de turno, el horario lleva a la semana (primero se elige el tipo)
ANA["tipos_turno"] = [{"nombre": "Sesión", "minutos": 45}, {"nombre": "Entrevista", "minutos": 90}]
app._proximos_cache.clear()
html = client.get("/turnos").data.decode()
assert "/anaperez/reservar/a2" not in html and "/anaperez?semana=" in html
ANA.pop("tipos_turno")
app._proximos_cache.clear()
# las demos van después de los profesionales reales, aunque estos estén pausados
TENANTS.append({"id": "t9", "nombre": "Lic. Aaa Demo", "slug": "demo", "activo": True, "foto": "x"})
html = client.get("/").data.decode()
assert html.index("Lic. Beto Pausa") < html.index("Lic. Aaa Demo")
html = client.get("/turnos").data.decode()
assert html.index("Lic. Beto Pausa") < html.index("Lic. Aaa Demo")
TENANTS.pop()
print("/turnos: especialidad, próximos libres con link, esta semana y orden OK")

# ---- sumate
guardados, avisos = [], []
db.crear_interesado = lambda d: guardados.append(d)
db.registrar_actividad = lambda *a, **k: None
db.intentos_login = lambda claves: []
db.guardar_intento_login = lambda clave, datos: None
whatsapp.avisar_admin = lambda texto: avisos.append(texto) or True

assert client.get("/sumate").status_code == 200
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
assert "oculto" not in xml and "<loc>http://localhost/sumate</loc>" in xml
print("robots.txt y sitemap.xml con los profesionales del directorio OK")

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
r = client.get("/")
assert r.status_code == 200 and 'id="profesionales"' not in r.data.decode()
assert client.get("/turnos").status_code == 200 and client.get("/sitemap.xml").status_code == 200
print("con la base caída portada, directorio y sitemap salen igual OK")

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
assert app._vuelve({"pausado": True, "pausado_hasta": "2026-12-03"}) == "Vuelve el 3/12"
print("pausados: «Vuelve a atender pronto» / «Vuelve el 3/12» OK")
