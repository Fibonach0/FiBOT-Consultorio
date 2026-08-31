"""Test del flujo de reserva por WhatsApp (conversation.py) contra una tabla
de turnos fake en memoria. Correrlo suelto: `python test_conversation.py`
(mismo motivo que test_webhook.py — no está pensado para pytest en batch)."""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault(
    "SUPABASE_SERVICE_ROLE_KEY",
    "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.fake_signature_not_real",
)
os.environ.setdefault("META_ACCESS_TOKEN", "fake")
os.environ.setdefault("META_APP_SECRET", "fake")
os.environ.setdefault("META_VERIFY_TOKEN", "fake")
os.environ.setdefault("FLASK_SECRET_KEY", "fake")

import db  # noqa: E402
import conversation  # noqa: E402

tenant = {"id": "t1", "nombre": "Lic. Ana Pérez", "timezone": "America/Argentina/Buenos_Aires"}

ahora = datetime.now(timezone.utc)
FAKE_TURNOS = [
    {"id": "a", "tenant_id": "t1", "inicio": (ahora + timedelta(hours=2)).isoformat(), "estado": "libre",
     "paciente_telefono": None, "paciente_nombre": None},
    {"id": "b", "tenant_id": "t1", "inicio": (ahora + timedelta(hours=3)).isoformat(), "estado": "libre",
     "paciente_telefono": None, "paciente_nombre": None},
    {"id": "c", "tenant_id": "t1", "inicio": (ahora + timedelta(days=1)).isoformat(), "estado": "libre",
     "paciente_telefono": None, "paciente_nombre": None},
]


def fake_turnos_libres(tenant_id, desde, limite=8):
    return [t for t in FAKE_TURNOS if t["estado"] == "libre"][:limite]


def fake_reservar_turno(tenant_id, turno_id, telefono, nombre):
    for t in FAKE_TURNOS:
        if t["id"] == turno_id and t["estado"] == "libre":
            t["estado"] = "reservado"
            t["paciente_telefono"] = telefono
            t["paciente_nombre"] = nombre
            return True
    return False


def fake_turno_reservado_de(tenant_id, telefono):
    for t in FAKE_TURNOS:
        if t["estado"] == "reservado" and t["paciente_telefono"] == telefono:
            return t
    return None


def fake_cancelar_turno(tenant_id, turno_id):
    for t in FAKE_TURNOS:
        if t["id"] == turno_id:
            t["estado"] = "libre"
            t["paciente_telefono"] = None
            t["paciente_nombre"] = None


db.turnos_libres = fake_turnos_libres
db.reservar_turno = fake_reservar_turno
db.turno_reservado_de = fake_turno_reservado_de
db.cancelar_turno = fake_cancelar_turno

TEL = "5491100000000"

r = conversation.responder(tenant, TEL, "hola")
assert "turno" in r.lower(), r
print("saludo -> menu OK")

r = conversation.responder(tenant, TEL, "turno")
assert "1." in r and "2." in r and "3." in r, r
print("listar libres -> 3 opciones OK")

r = conversation.responder(tenant, TEL, "2 Juan Perez")
assert "reservado" in r.lower() or "quedó" in r.lower(), r
assert FAKE_TURNOS[1]["estado"] == "reservado"
assert FAKE_TURNOS[1]["paciente_nombre"] == "Juan Perez"
assert FAKE_TURNOS[1]["paciente_telefono"] == TEL
print("reservar '2 Juan Perez' -> OK, turno b quedó reservado a nombre de Juan Perez")

r = conversation.responder(tenant, TEL, "5")
assert "no corresponde" in r.lower(), r
print("elegir un índice fuera de rango -> mensaje de error OK")

r = conversation.responder(tenant, "5491199999999", "cancelar")
assert "no tenés" in r.lower(), r
print("cancelar sin turno propio -> aviso OK")

r = conversation.responder(tenant, TEL, "cancelar")
assert "cancelado" in r.lower(), r
assert FAKE_TURNOS[1]["estado"] == "libre"
print("cancelar turno propio -> OK, vuelve a estar libre")

print("\nTODOS LOS TESTS DE CONVERSACIÓN PASARON")
