"""Smoke test de la página pública de reserva (sin login). Correrlo suelto
(`python test_publico.py`), mismo motivo que los otros test_*.py: monkeypatchea
`db` sin restaurarlo."""
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

from datetime import datetime, timedelta, timezone  # noqa: E402

# El hilo del scheduler arranca solo al importar `app` y su primer tick pega
# contra Supabase de una — con la URL falsa de estos tests eso vuelca un
# traceback de DNS de 40 líneas en cada corrida del CI y tapa lo que sí
# importa. Se apaga ANTES de importar app; los recordatorios se prueban
# llamando a scheduler._tick() a mano, que es lo que interesa verificar, no
# que el hilo esté vivo.
import scheduler  # noqa: E402

scheduler.iniciar = lambda: None

import app  # noqa: E402
import db  # noqa: E402

TENANT = {"id": "t1", "nombre": "Lic. Ana Pérez", "rubro": "psicología", "slug": "anaperez",
          "timezone": "America/Argentina/Buenos_Aires"}

ahora = datetime.now(timezone.utc)
TURNO_LIBRE = {"id": "turno-a", "tenant_id": "t1", "inicio": (ahora + timedelta(hours=2)).isoformat(),
               "estado": "libre", "paciente_telefono": None, "paciente_nombre": None}

db.get_tenant_by_slug = lambda slug: TENANT if slug == "anaperez" else None
db.turnos_libres = lambda tenant_id, desde, limite=8: [TURNO_LIBRE] if TURNO_LIBRE["estado"] == "libre" else []
db.turno_por_id = lambda tenant_id, turno_id: TURNO_LIBRE if turno_id == "turno-a" else None


def fake_reservar_turno(tenant_id, turno_id, telefono, nombre):
    if turno_id == "turno-a" and TURNO_LIBRE["estado"] == "libre":
        TURNO_LIBRE["estado"] = "reservado"
        TURNO_LIBRE["paciente_telefono"] = telefono
        TURNO_LIBRE["paciente_nombre"] = nombre
        return True
    return False


db.reservar_turno = fake_reservar_turno
OTRA = {"id": "t2", "nombre": "Lic. Beto Gómez", "rubro": "psicopedagogía", "slug": "betogomez", "activo": True}
SIN_PAGINA = {"id": "t3", "nombre": "Lic. Sin Página", "rubro": "psicología", "slug": None, "activo": True}
OCULTA = {"id": "t4", "nombre": "Lic. Oculta", "rubro": "psicología", "slug": "oculta", "activo": True, "en_directorio": False}
db.tenants_activos = lambda: [dict(TENANT, activo=True), OTRA, SIN_PAGINA, OCULTA]
db.get_tenant_by_id = lambda tid: TENANT if tid == "t1" else None
ACTUALIZADOS = []


def fake_actualizar_reserva(tenant_id, turno_id, campos):
    ACTUALIZADOS.append((tenant_id, turno_id, campos))
    if turno_id == "turno-a":
        TURNO_LIBRE.update(campos)


db.actualizar_reserva = fake_actualizar_reserva
db.turno_por_token_calendario = lambda token: TURNO_LIBRE if token and token == TURNO_LIBRE.get("token_calendario") else None
PLANTILLAS = []
app.whatsapp.enviar_plantilla = lambda numero, to, plantilla, lang, params: PLANTILLAS.append((numero, to, plantilla, params)) or True
app.config.PLATAFORMA_PHONE_NUMBER_ID = "PLAT"

client = app.app.test_client()

r = client.get("/slug-inexistente")
assert r.status_code == 404, r.status_code
print("GET /<slug desconocido> -> 404 OK")

r = client.get("/turnos")
html = r.data.decode()
assert r.status_code == 200 and "Lic. Ana Pérez" in html and "Lic. Beto Gómez" in html and "Lic. Sin Página" not in html, html[:400]
assert 'href="/anaperez"' in html and 'href="/betogomez"' in html and "Lic. Oculta" not in html
print("quien no quiere aparecer en /turnos no aparece OK")
TENANT.update({"foto": "data:image/png;base64,AAAA", "descripcion": "Niños y adolescentes · Palermo", "modalidades_publicas": "ambas"})
html = client.get("/turnos").data.decode()
assert 'src="/anaperez/foto"' in html and "data:image" not in html and "Niños y adolescentes · Palermo" in html and "Presencial u online" in html
print("/turnos muestra foto, descripción y modalidad OK")
print("GET /turnos -> lista los profesionales con página y lleva a cada calendario OK")

r = client.get("/anaperez")
html = r.data.decode()
hora_local = datetime.fromisoformat(TURNO_LIBRE["inicio"]).astimezone(app.ZoneInfo(TENANT["timezone"])).strftime("%H:%M")
assert r.status_code == 200 and "Lic. Ana P" in html and 'href="/anaperez/reservar/turno-a"' in html and f'>{hora_local}</a>' in html, html[:400]
assert "Semana del" in html and "Semana anterior" not in html
print("GET /anaperez -> 200 OK, calendario de la semana con el turno libre")
# Sitio propio: la página del profesional (y su reserva) no lleva al directorio ni a otros profesionales.
for _url in ("/anaperez", "/anaperez/reservar/turno-a"):
    _h = client.get(_url).data.decode()
    assert ('href="/"' not in _h and 'href="/turnos' not in _h and 'href="/sumate"' not in _h and "Otros profesionales" not in _h
            and "fibot.ar" not in _h.split("<body")[1] and 'class="s-marca" href="/anaperez"' in _h), _url
print("sitio propio: su nombre arriba y ningún link al directorio ni a otros profesionales OK")

r = client.get("/anaperez?semana=2020-01-06")
assert r.status_code == 200 and "Semana anterior" not in r.data.decode()
print("una semana pasada vuelve a la actual OK")
siguiente = (datetime.fromisoformat(TURNO_LIBRE["inicio"]) + timedelta(days=7)).date().isoformat()
TURNOS_FUTUROS = []
r = client.get(f"/anaperez?semana={siguiente}")
html = r.data.decode()
assert r.status_code == 200 and "Semana anterior" in html and "turno-a" not in html and "No hay horarios libres esta semana" in html
print("semana siguiente sin horarios: lo dice y deja volver OK")

r = client.get("/anaperez/reservar/turno-a")
assert r.status_code == 200 and b"nombre" in r.data and b"telefono" in r.data, r.status_code
print("GET /anaperez/reservar/turno-a -> 200 OK, formulario")

r = client.get("/anaperez/reservar/turno-a")
assert "Turno con <strong>Lic. Ana Pérez</strong>" in r.data.decode() and 'value="virtual"' in r.data.decode()
print("el formulario dice con quién es el turno y deja elegir modalidad OK")

r = client.post(
    "/anaperez/reservar/turno-a",
    data={"nombre": "Juan Pérez", "telefono": "+54 9 11 2345-6789", "modalidad": "virtual"},
)
html = r.data.decode()
assert "Por videollamada" in html and 'name="modalidad" value="virtual"' in html
assert r.status_code == 200 and "Revisá antes de reservar" in html and "Lic. Ana Pérez" in html and "+5491123456789" in html, html[:300]
assert TURNO_LIBRE["estado"] == "libre", "el primer paso NO reserva"
print("primer POST -> pantalla de revisión, sin reservar OK")

r = client.post("/anaperez/reservar/turno-a", data={"nombre": "Juan", "telefono": "1234"}, follow_redirects=True)
assert "Revisá el WhatsApp" in r.data.decode() and TURNO_LIBRE["estado"] == "libre"
print("WhatsApp incompleto -> vuelve al formulario OK")

r = client.post(
    "/anaperez/reservar/turno-a",
    data={"nombre": "Juan Pérez", "telefono": "5491123456789", "confirmado": "1", "modalidad": "virtual"},
)
assert r.status_code == 200 and b"reservado" in r.data.lower() and "Tu turno con <strong>Lic. Ana Pérez</strong>" in r.data.decode(), r.status_code
assert not PLANTILLAS and TURNO_LIBRE["modalidad"] == "virtual" and TURNO_LIBRE["token_calendario"]
assert "Revisá tu WhatsApp" not in r.data.decode() and "/agendar/" in r.data.decode()
print("sin plantilla de confirmación: reserva con modalidad y link para agendar, sin WhatsApp OK")

# con plantilla de confirmación
app.config.CONFIRMACION_TEMPLATE = "confirmar_turno"
TURNO_LIBRE.update({"estado": "libre", "paciente_telefono": None, "paciente_nombre": None})
r = client.post("/anaperez/reservar/turno-a",
                data={"nombre": "Juan Pérez", "telefono": "5491123456789", "confirmado": "1", "modalidad": "presencial"})
html = r.data.decode()
assert PLANTILLAS and PLANTILLAS[-1][1] == "5491123456789" and PLANTILLAS[-1][2] == "confirmar_turno"
assert PLANTILLAS[-1][3][0] == "Lic. Ana Pérez" and TURNO_LIBRE.get("confirmacion_enviada_at") and TURNO_LIBRE["modalidad"] is None
assert "Revisá tu WhatsApp" in html
print("con plantilla: le llega el WhatsApp para confirmar y queda marcado OK")

# agendar
TURNO_LIBRE["fin"] = (datetime.fromisoformat(TURNO_LIBRE["inicio"]) + timedelta(minutes=50)).isoformat()
token = TURNO_LIBRE["token_calendario"]
r = client.get(f"/agendar/{token}")
html = r.data.decode()
assert r.status_code == 200 and "calendar.google.com" in html and "Juan" not in html and r.headers.get("X-Robots-Tag") == "noindex"
r = client.get(f"/agendar/{token}.ics")
ics = r.data.decode()
assert r.status_code == 200 and r.mimetype == "text/calendar" and "BEGIN:VEVENT" in ics and "Turno con Lic. Ana Pérez" in ics and "Juan" not in ics
assert client.get("/agendar/inventado").status_code == 404 and client.get("/agendar/inventado.ics").status_code == 404
print("agendar: Google Calendar y .ics sin datos del paciente OK")
# ---- reservar en la misma página (/<slug>, con X-Parcial): pedazos, no páginas
P = {"X-Parcial": "1"}
TURNO_LIBRE.update({"estado": "libre", "paciente_telefono": None, "paciente_nombre": None})
html = client.get("/anaperez").data.decode()
assert 'id="reserva-form"' in html and 'data-post="/anaperez/reservar/turno-a"' in html
r = client.post("/anaperez/reservar/turno-a", data={"nombre": "Juan Pérez", "telefono": "5491123456789"}, headers=P)
html = r.data.decode()
assert r.status_code == 200 and 'data-paso="revisar"' in html and "<html" not in html and "data-parcial" in html
assert TURNO_LIBRE["estado"] == "libre", "el primer paso tampoco reserva acá"
r = client.post("/anaperez/reservar/turno-a", data={"nombre": "Juan", "telefono": "1234"}, headers=P)
assert r.status_code == 409 and 'data-paso="error"' in r.data.decode() and "Revisá el WhatsApp" in r.data.decode()
assert "data-otro" not in r.data.decode()
r = client.post("/anaperez/reservar/turno-a", data={"nombre": "Juan Pérez", "telefono": "5491123456789", "confirmado": "1"}, headers=P)
html = r.data.decode()
assert r.status_code == 200 and 'data-paso="lista"' in html and "¡Listo, Juan!" in html and "/agendar/" in html and "<html" not in html
assert TURNO_LIBRE["estado"] == "reservado"
r = client.post("/anaperez/reservar/turno-a", data={"nombre": "Otra", "telefono": "5491100000000", "confirmado": "1"}, headers=P)
assert r.status_code == 409 and "ya no está disponible" in r.data.decode() and "data-otro" in r.data.decode()
print("reservar en la misma página: revisar, error y listo como pedazos de la tarjeta OK")
_antes = dict(TURNO_LIBRE)
TURNO_LIBRE.update({"estado": "libre", "paciente_telefono": None, "paciente_nombre": None})
html = client.get("/anaperez").data.decode()
assert html.count('data-recordar="nombre"') == 2 and html.count("data-recordar-ok checked") == 2 and "fibot_paciente" in html
assert 'data-recordar="telefono"' in client.get("/anaperez/reservar/turno-a").data.decode()
TURNO_LIBRE.clear(); TURNO_LIBRE.update(_antes)
print("nombre y WhatsApp se recuerdan en el navegador (reserva y lista de espera) OK")

TENANT.update({"modalidades_publicas": "presencial"})
assert TURNO_LIBRE["estado"] == "reservado"
assert TURNO_LIBRE["paciente_telefono"] == "5491123456789", TURNO_LIBRE["paciente_telefono"]
print("POST /anaperez/reservar/turno-a -> 200 OK, confirmado y teléfono normalizado a solo dígitos")

r = client.post(
    "/anaperez/reservar/turno-a",
    data={"nombre": "Otra Persona", "telefono": "5491100000000", "confirmado": "1"},
    follow_redirects=True,
)
assert r.status_code == 200 and "no está disponible".encode() in r.data
print("POST sobre un turno ya tomado -> redirige con aviso OK")
r = client.post("/anaperez/reservar/turno-a", data={"nombre": "Otra", "telefono": "5491100000000"}, follow_redirects=True)
assert "no está disponible".encode() in r.data
print("revisar un turno ya tomado -> avisa OK")

r = client.get("/privacidad")
assert r.status_code == 200 and "Política de privacidad" in r.data.decode() and "Ley 25.326" in r.data.decode(), "privacidad"
print("GET /privacidad -> 200 OK, sin login")

print("\nTODOS LOS SMOKE TESTS DE LA PÁGINA PÚBLICA PASARON")
