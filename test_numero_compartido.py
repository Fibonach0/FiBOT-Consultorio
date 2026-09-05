"""Tests del modo "sin número propio" y del recordatorio por plantilla.

Correrlo SUELTO (`python test_numero_compartido.py`), igual que los otros
test_*.py de este repo: monkeypatchea `db` y `whatsapp` sin restaurarlos.

Cubre las dos cosas que hacen que un profesional pueda arrancar hoy:
  1. Que el mensaje que entra al número COMPARTIDO se resuelva por paciente.
  2. Que el recordatorio salga como PLANTILLA, que es lo único que Meta deja
     mandar fuera de la ventana de 24 h — y el recordatorio siempre cae afuera.
"""
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault(
    "SUPABASE_SERVICE_ROLE_KEY",
    "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.fake_signature_not_real",
)
os.environ.setdefault("META_ACCESS_TOKEN", "fake")
os.environ.setdefault("META_APP_SECRET", "supersecret")
os.environ.setdefault("META_VERIFY_TOKEN", "verify123")
os.environ.setdefault("FLASK_SECRET_KEY", "fake")
os.environ["PLATAFORMA_PHONE_NUMBER_ID"] = "PLATAFORMA1"

import app  # noqa: E402
import config  # noqa: E402
import conversation  # noqa: E402
import db  # noqa: E402
import scheduler  # noqa: E402
import whatsapp  # noqa: E402

fallos = []


def chequear(nombre, ok, detalle=""):
    print(f"  {nombre:.<56} {'OK' if ok else 'FALLO'}{'  ' + str(detalle) if detalle else ''}")
    if not ok:
        fallos.append(nombre)


TENANT_COMPARTIDO = {"id": "t-compartido", "nombre": "Lic. Ana Pérez",
                     "whatsapp_phone_number_id": None,
                     "timezone": "America/Argentina/Buenos_Aires"}
TENANT_PROPIO = {"id": "t-propio", "nombre": "Dr. Juan Gómez",
                 "whatsapp_phone_number_id": "PROPIO9",
                 "timezone": "America/Argentina/Buenos_Aires"}

print("De qué número sale cada tenant")
chequear("con número propio usa el suyo", whatsapp.numero_de(TENANT_PROPIO) == "PROPIO9")
chequear("sin número propio cae al de la plataforma",
         whatsapp.numero_de(TENANT_COMPARTIDO) == "PLATAFORMA1")
chequear("string vacío también cae a la plataforma",
         whatsapp.numero_de({"whatsapp_phone_number_id": ""}) == "PLATAFORMA1")

# ── Webhook: mensaje al número compartido ────────────────────────────────────
print("\nWebhook contra el número compartido")
enviados = []
whatsapp.enviar_texto = lambda pid, to, txt: enviados.append((pid, to, txt)) or True
conversation.responder = lambda tenant, tel, txt: f"respuesta de {tenant['id']}"
# Nadie tiene el número compartido asignado: por eso hay que resolver por paciente.
db.get_tenant_by_phone_number_id = lambda pid: None
db.tenant_por_paciente = lambda tel: TENANT_COMPARTIDO if tel == "5491133334444" else None

client = app.app.test_client()


def postear(phone_number_id, telefono):
    payload = {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": phone_number_id},
        "messages": [{"type": "text", "from": telefono, "text": {"body": "hola"}}],
    }}]}]}
    body = json.dumps(payload).encode()
    firma = hmac.new(b"supersecret", body, hashlib.sha256).hexdigest()
    return client.post("/webhook", data=body, content_type="application/json",
                       headers={"X-Hub-Signature-256": f"sha256={firma}"})


enviados.clear()
r = postear("PLATAFORMA1", "5491133334444")
chequear("paciente conocido -> 200 y contesta", r.status_code == 200 and len(enviados) == 1)
chequear("contesta al tenant correcto", enviados and "t-compartido" in enviados[0][2], enviados)
chequear("la respuesta sale por el número de la plataforma",
         enviados and enviados[0][0] == "PLATAFORMA1", enviados)

enviados.clear()
r = postear("PLATAFORMA1", "5491199998888")
chequear("teléfono sin turnos -> 200 y no contesta nada",
         r.status_code == 200 and enviados == [], enviados)

enviados.clear()
r = postear("OTRO_NUMERO_CUALQUIERA", "5491133334444")
chequear("número desconocido que NO es el compartido -> se ignora",
         r.status_code == 200 and enviados == [], enviados)

# ── Recordatorio ─────────────────────────────────────────────────────────────
print("\nRecordatorio del scheduler")
manana = datetime.now(timezone.utc) + timedelta(hours=20)
TURNO = {"id": "turno-1", "inicio": manana.isoformat(), "paciente_telefono": "5491133334444"}
marcados = []
db.marcar_recordatorio_enviado = lambda tid: marcados.append(tid)
db.turnos_para_recordar = lambda tenant_id, horas: [TURNO]

plantillas = []
whatsapp.enviar_plantilla = lambda pid, to, tpl, lang, params: (
    plantillas.append((pid, to, tpl, lang, params)) or True)

# Con plantilla configurada
config.RECORDATORIO_TEMPLATE = "turno_recordatorio"
config.RECORDATORIO_TEMPLATE_LANG = "es_AR"
db.tenants_activos = lambda: [TENANT_COMPARTIDO]
plantillas.clear(); enviados.clear(); marcados.clear()
scheduler._tick()
chequear("con plantilla: manda plantilla y NO texto libre",
         len(plantillas) == 1 and enviados == [], (plantillas, enviados))
chequear("sale por el número de la plataforma", plantillas and plantillas[0][0] == "PLATAFORMA1")
chequear("primer parámetro = nombre del profesional",
         plantillas and plantillas[0][4][0] == "Lic. Ana Pérez", plantillas and plantillas[0][4])
chequear("segundo parámetro = cuándo es el turno",
         plantillas and ":" in plantillas[0][4][1], plantillas and plantillas[0][4])
chequear("marca el recordatorio como enviado", marcados == ["turno-1"], marcados)

# Sin plantilla: cae a texto libre (sirve para probar, no para producción)
config.RECORDATORIO_TEMPLATE = ""
plantillas.clear(); enviados.clear(); marcados.clear()
scheduler._tick()
chequear("sin plantilla: cae a texto libre", len(enviados) == 1 and plantillas == [])
chequear("el texto nombra al profesional",
         enviados and "Lic. Ana Pérez" in enviados[0][2])

# Sin número propio Y sin número de plataforma: no se puede mandar nada
print("\nCaso borde: no hay ningún número")
config.PLATAFORMA_PHONE_NUMBER_ID = ""
plantillas.clear(); enviados.clear(); marcados.clear()
scheduler._tick()
chequear("no manda nada y no marca el turno como avisado",
         enviados == [] and plantillas == [] and marcados == [], (enviados, marcados))
config.PLATAFORMA_PHONE_NUMBER_ID = "PLATAFORMA1"

print()
if fallos:
    print(f"FALLARON {len(fallos)}: {', '.join(fallos)}")
    raise SystemExit(1)
print("TODOS LOS TESTS DEL NÚMERO COMPARTIDO PASARON")
