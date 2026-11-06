"""El asistente del profesional (asistente_profesional.py) y su entrada por el
webhook. Correrlo suelto: `python test_asistente_profesional.py` (monkeypatchea
`db` sin restaurarlo, como los demás)."""
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.fake_signature_not_real")
os.environ.setdefault("META_ACCESS_TOKEN", "fake")
os.environ.setdefault("META_APP_SECRET", "supersecret")
os.environ.setdefault("META_VERIFY_TOKEN", "verify123")
os.environ.setdefault("FLASK_SECRET_KEY", "fake")

import scheduler  # noqa: E402

scheduler.iniciar = lambda: None
import app  # noqa: E402
import asistente_profesional as ap  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import whatsapp  # noqa: E402

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
config.DOMINIO_PRINCIPAL = "consultorio.fibot.ar"
ANA = {"id": "t1", "nombre": "Lic. Ana", "slug": "ana", "aviso_telefono": "5491155550000", "timezone": "America/Argentina/Buenos_Aires",
       "sedes": [{"nombre": "Palermo"}]}
hoy = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)


def turno(i, dia, hora, **extra):
    return {"id": f"s{i}", "tenant_id": "t1", "estado": "reservado", "inicio": (hoy + timedelta(days=dia, hours=hora)).isoformat(),
            "paciente_nombre": f"Paciente Secreto {i}", "paciente_telefono": f"54911000000{i}", **extra}


TURNOS = [turno(1, 0, 10, modalidad="virtual"), turno(2, 0, 11, sede="Palermo", pago="pagado"),
          turno(3, 1, 9, pago_web="esperando"),
          {"id": "l1", "tenant_id": "t1", "estado": "libre", "inicio": (hoy + timedelta(days=1, hours=12)).isoformat()}]
db.agenda_semana = lambda tid, desde, hasta: [t for t in TURNOS if desde <= datetime.fromisoformat(t["inicio"]) < hasta] if tid == "t1" else []
db.turnos_libres = lambda tid, desde, limite=8: [t for t in TURNOS if t["estado"] == "libre"][:limite]
db.pagos_para_revisar = lambda tid: [{"id": "p1", "estado": "comprobante", "monto": 30000}, {"id": "p2", "estado": "aprobado", "a_devolver": 5000}]
db.pagos_esperando = lambda tid: [{"id": "p3"}]
db.pendientes_de_pago = lambda tid, hasta, limite=50: [
    {"paciente_telefono": "549110000001", "ficha": "", "valor": 25000}, {"paciente_telefono": "549110000001", "ficha": "", "valor": 25000},
    {"paciente_telefono": "549110000002", "ficha": "", "valor": None}]
db.mensajes_sin_leer = lambda tid: 2

r = ap.responder(ANA, "Hoy")
assert r.startswith("Hoy: 2 sesiones.") and "10:00 (virtual)" in r and "11:00 (Palermo, pagada)" in r, r
assert "Secreto" not in r and "54911" not in r, "por WhatsApp no salen nombres ni teléfonos de pacientes"
print("hoy: horas, modalidad, consultorio y pago, sin nombres OK")
r = ap.responder(ANA, "mañana?")
assert "Mañana: 1 sesión." in r and "pago web sin confirmar" in r and "Horarios libres: 1." in r, r
print("mañana: con el pago web pendiente y los libres OK")
r = ap.responder(ANA, "pagos")
assert "1 comprobante de transferencia para validar ($ 30.000)" in r and "$ 5.000 para devolver" in r and "1 reserva esperando" in r, r
print("pagos: comprobantes, devoluciones y reservas esperando OK")
r = ap.responder(ANA, "¿Quién me debe?")
assert "Te deben $ 50.000 de 3 sesiones pasadas, de 2 pacientes." in r and "1 no tienen valor" in r and "Secreto" not in r, r
print("deudas: monto, sesiones y pacientes, sin nombres OK")
assert "2 mensajes de pacientes sin leer" in ap.responder(ANA, "mensajes")
assert "https://consultorio.fibot.ar/ana" in ap.responder(ANA, "mi link")
assert "*hoy*" in ap.responder(ANA, "ayuda") and "*pagos*" in ap.responder(ANA, "hola")
assert "libres" in ap.responder(ANA, "libres").lower()
print("mensajes, link, ayuda y libres OK")
for paciente in ("turno", "2 Juan", "cancelar", "mi turno", "llego tarde"):
    assert ap.responder(ANA, paciente) is None, paciente
print("lo que no es un pedido del profesional sigue como paciente (puede probar su propio bot) OK")
dos = ap.responder_a([ANA, dict(ANA, id="t2", nombre="Lic. Demo")], "mensajes")
assert dos.count("mensajes de pacientes") == 2 and "*Lic. Ana*" in dos and "*Lic. Demo*" in dos, dos
print("mismo WhatsApp en dos cuentas: contesta por cada una OK")

# --- por el webhook ---
db.get_tenant_by_phone_number_id = lambda pid: None
db.marcar_evento = lambda clave, detalle=None: None
db.tenants_activos = lambda: [ANA, {"id": "t9", "nombre": "Otro", "aviso_telefono": "5491177770000"}]
db.huecos_pendientes = lambda tid: []
ENVIADOS, AL_PACIENTE = [], []
whatsapp.marcar_leido = lambda pid, mid: True
whatsapp.enviar_texto = lambda pid, to, texto: ENVIADOS.append((pid, to, texto)) or True
app._contestar = lambda tenant, tel, texto: AL_PACIENTE.append((tel, texto))
app._tenant_del_mensaje = lambda tdn, tel, pid, texto=None: (ANA, False)
client = app.app.test_client()


def postear(tel, texto):
    cuerpo = json.dumps({"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "PLAT"},
                                                          "messages": [{"id": "w1", "type": "text", "from": tel, "text": {"body": texto}}]}}]}]}).encode()
    f = hmac.new(b"supersecret", cuerpo, hashlib.sha256).hexdigest()
    return client.post("/webhook", data=cuerpo, content_type="application/json", headers={"X-Hub-Signature-256": f"sha256={f}"})


app.config.PLATAFORMA_PHONE_NUMBER_ID = "PLAT"
assert postear("5491155550000", "hoy").status_code == 200
assert ENVIADOS and ENVIADOS[-1][1] == "5491155550000" and ENVIADOS[-1][2].startswith("Hoy:") and not AL_PACIENTE, (ENVIADOS, AL_PACIENTE)
print("webhook: el profesional pregunta «hoy» desde su WhatsApp de avisos y le contesta el asistente OK")
postear("5491155550000", "turno")
assert AL_PACIENTE == [("5491155550000", "turno")], AL_PACIENTE
print("webhook: si escribe algo de paciente, sigue como paciente OK")
ENVIADOS.clear()
postear("5491122220000", "hoy")
assert not ENVIADOS and AL_PACIENTE[-1] == ("5491122220000", "hoy"), "un paciente que escribe «hoy» no ve la agenda de nadie"
print("webhook: un paciente que escribe «hoy» no ve ninguna agenda OK")
print("\nTODOS LOS TESTS DEL ASISTENTE DEL PROFESIONAL PASARON")
