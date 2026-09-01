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

client = app.app.test_client()

r = client.get("/slug-inexistente")
assert r.status_code == 404, r.status_code
print("GET /<slug desconocido> -> 404 OK")

r = client.get("/anaperez")
assert r.status_code == 200 and b"Lic. Ana P" in r.data and b"Reservar" in r.data, r.status_code
print("GET /anaperez -> 200 OK, lista el turno libre")

r = client.get("/anaperez/reservar/turno-a")
assert r.status_code == 200 and b"nombre" in r.data and b"telefono" in r.data, r.status_code
print("GET /anaperez/reservar/turno-a -> 200 OK, formulario")

r = client.post(
    "/anaperez/reservar/turno-a",
    data={"nombre": "Juan Pérez", "telefono": "+54 9 11 2345-6789"},
)
assert r.status_code == 200 and b"reservado" in r.data.lower(), r.status_code
assert TURNO_LIBRE["estado"] == "reservado"
assert TURNO_LIBRE["paciente_telefono"] == "5491123456789", TURNO_LIBRE["paciente_telefono"]
print("POST /anaperez/reservar/turno-a -> 200 OK, confirmado y teléfono normalizado a solo dígitos")

r = client.post(
    "/anaperez/reservar/turno-a",
    data={"nombre": "Otra Persona", "telefono": "5491100000000"},
    follow_redirects=True,
)
assert r.status_code == 200 and "no está disponible".encode() in r.data
print("POST sobre un turno ya tomado -> redirige con aviso OK")

print("\nTODOS LOS SMOKE TESTS DE LA PÁGINA PÚBLICA PASARON")
