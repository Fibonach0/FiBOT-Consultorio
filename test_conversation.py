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

tenant = {"id": "t1", "nombre": "Lic. Ana Pérez", "timezone": "America/Argentina/Buenos_Aires",
          "cancelar_hasta_horas": 0}  # sin plazo: estos tests cancelan turnos de dentro de un rato

import cancelaciones  # noqa: E402

TRAS_CANCELAR = []
cancelaciones.tras_cancelar = lambda t, turno, por: TRAS_CANCELAR.append((turno["id"], por))

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


def fake_proximos_turnos_de(tenant_id, telefono):
    for t in FAKE_TURNOS:
        if t["estado"] == "reservado" and t["paciente_telefono"] == telefono:
            return [t]
    return []


def fake_cancelar_turno(tenant_id, turno_id, origen=None):
    for t in FAKE_TURNOS:
        if t["id"] == turno_id:
            t["estado"] = "libre"
            t["paciente_telefono"] = None
            t["paciente_nombre"] = None


db.turnos_libres = fake_turnos_libres
db.reservar_turno = fake_reservar_turno
db.proximos_turnos_de = fake_proximos_turnos_de
db.fichas_del_telefono = lambda tenant_id, telefono: []
db.nombre_ambiguo = lambda tenant_id, telefono, nombre: False
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


# --- Plazo para cancelar también por WhatsApp ---
TRAS_CANCELAR.clear()
_cerca = {"id": "cerca", "inicio": (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat(),
          "paciente_telefono": "549110000", "paciente_nombre": "Tito"}
_canceladas = []
db.proximos_turnos_de = lambda tid, tel: [dict(_cerca)]
db.cancelar_turno = lambda tid, turno_id, origen=None: _canceladas.append(turno_id)
r = conversation.responder(dict(tenant, cancelar_hasta_horas=24), "549110000", "cancelar")
assert "menos de 24 horas" in r and "Todavía no se canceló nada" in r and not _canceladas and not TRAS_CANCELAR, r
print("cancelar por WhatsApp respeta el plazo del profesional OK")
r = conversation.responder(dict(tenant, cancelar_hasta_horas=2), "549110000", "cancelar")
assert "Cancelado" in r and _canceladas == ["cerca"] and TRAS_CANCELAR == [("cerca", "WhatsApp")], r
print("dentro del plazo cancela y sigue el flujo de cancelaciones (aviso, retener o lista de espera) OK")


# --- Saludo: número propio vs. número compartido de la plataforma ---
import config  # noqa: E402

r = conversation.responder(dict(tenant, whatsapp_phone_number_id="PN1"), TEL, "hola")
assert "soy el asistente de *Lic. Ana Pérez*" in r and config.PLATAFORMA_NOMBRE not in r, r
print("número propio: se presenta como el asistente del profesional OK")
r = conversation.responder(dict(tenant, whatsapp_phone_number_id=None), TEL, "hola")
assert f"asistente de turnos de *{config.PLATAFORMA_NOMBRE}*" in r, r
assert "Tus turnos son con *Lic. Ana Pérez*" in r and "soy el asistente de *Lic. Ana Pérez*" not in r, r
print("número compartido: se presenta como la plataforma y nombra al profesional OK")


# --- Bandeja, frases naturales, «mi turno» y lo que no es texto ---
BANDEJA, AVISOS = [], []
db.mensajes_sin_leer = lambda tid: sum(1 for m in BANDEJA if m[0] == tid)
db.guardar_mensaje_paciente = lambda tid, tel, texto, tipo="texto": BANDEJA.append((tid, tel, texto, tipo)) or {}
import scheduler  # noqa: E402
scheduler.avisar_profesional = lambda t, texto: AVISOS.append(texto) or True
_turno_mio = {"id": "mio", "inicio": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
              "paciente_telefono": TEL, "paciente_nombre": "Juan"}
db.proximos_turnos_de = lambda tid, tel: [dict(_turno_mio)] if tel == TEL else []
_canceladas.clear()

r = conversation.responder(tenant, TEL, "llego 10 minutos tarde")
assert "Le pasé tu mensaje a *Lic. Ana Pérez*" in r and BANDEJA[-1] == ("t1", TEL, "llego 10 minutos tarde", "texto"), (r, BANDEJA)
assert len(AVISOS) == 1 and "panel" in AVISOS[0] and "10 minutos" not in AVISOS[0], AVISOS
print("bandeja: lo que no entiende se guarda y se le avisa al profesional, sin el texto OK")
conversation.responder(tenant, TEL, "¿puedo llevar a mi hijo?")
assert len(BANDEJA) == 2 and len(AVISOS) == 1, AVISOS
print("bandeja: con mensajes sin leer no vuelve a avisar OK")
assert conversation.responder(tenant, TEL, "10 minutos tarde llego").startswith("Le pasé"), "un número con texto no es reservar"
print("«10 minutos tarde» no se toma como elegir el horario 10 OK")

for frase in ("¿Cuándo es mi turno?", "mi turno", "a qué hora tengo?"):
    r = conversation.responder(tenant, TEL, frase)
    assert "Tu próximo turno con *Lic. Ana Pérez*" in r, (frase, r)
print("«mi turno» y variantes OK")
assert "No tenés turnos" in conversation.responder(tenant, "5491199999999", "mi turno")

for frase in ("No voy a poder ir", "cancelo", "Necesito cancelar el turno del jueves"):
    r = conversation.responder(tenant, TEL, frase)
    assert "¿Querés cancelar tu turno" in r and "Todavía no se canceló nada" in r and not _canceladas, (frase, r)
print("frases de cancelar: pregunta y no cancela nada sin la palabra *cancelar* OK")

assert "escribí *cancelar*" in conversation.responder(tenant, TEL, "quiero cambiar el turno")
print("reprogramar: explica cómo OK")

_lun = datetime.now(timezone.utc)
FAKE_TURNOS[:] = [{"id": f"x{i}", "tenant_id": "t1", "estado": "libre", "paciente_telefono": None, "paciente_nombre": None,
                   "inicio": (_lun + timedelta(days=i, hours=1)).isoformat()} for i in range(1, 15)]
_dia_obj = datetime.fromisoformat(FAKE_TURNOS[4]["inicio"]).astimezone(conversation._tz(tenant))
_nombre_dia = conversation.DIAS[_dia_obj.weekday()]
r = conversation.responder(tenant, TEL, f"Hola, quiero un turno el {_nombre_dia}")
assert f"Horarios libres el {_nombre_dia}" in r and "5. " in r and "1. " not in r.replace("11. ", ""), r
print("«quiero un turno el <día>»: sólo ese día, con su número de la lista completa OK")
assert "próximos horarios libres" in conversation.responder(tenant, TEL, "¿hay lugar esta semana?")
print("«¿hay lugar?» lista horarios OK")

_n0 = len(BANDEJA)
assert conversation.responder(tenant, TEL, "gracias!") == conversation.CORTESIA and len(BANDEJA) == _n0
assert conversation.responder(tenant, TEL, "👍") == conversation.CORTESIA
assert "*mi turno*" in conversation.responder(tenant, TEL, "Buenas tardes")
print("gracias, emoji y saludo: no van a la bandeja OK")

# no es texto
assert conversation.responder_no_texto(tenant, TEL, "reaction") is None
assert conversation.responder_no_texto(tenant, TEL, "sticker") == conversation.CORTESIA
r = conversation.responder_no_texto(tenant, TEL, "audio", "m1")
assert "leo sólo texto" in r and BANDEJA[-1][3] == "audio" and BANDEJA[-1][2] is None, (r, BANDEJA[-1])
print("audio: pide que lo escriba y queda la marca en la bandeja OK")

import pagos  # noqa: E402
import whatsapp  # noqa: E402
_pago = {"id": "pg1", "token": "tok123", "estado": "esperando", "turnos": ["mio"]}
db.pago_esperando_de = lambda tid, tel: _pago if tel == TEL else None
pagos.metodos = lambda t: ["transferencia"]
_subidos = []
pagos.subir_comprobante = lambda t, p, crudo, email=None: _subidos.append((p["id"], crudo)) or None
whatsapp.bajar_media = lambda media_id, maximo: b"%PDF-1.4 x" if media_id == "doc1" else None
r = conversation.responder_no_texto(tenant, TEL, "document", "doc1")
assert "Recibí tu comprobante" in r and _subidos == [("pg1", b"%PDF-1.4 x")], (r, _subidos)
print("foto/PDF con transferencia pendiente: se toma como comprobante OK")
r = conversation.responder_no_texto(tenant, TEL, "image", "roto")
assert "No pude abrir el archivo" in r and "/pago/tok123" in r and len(_subidos) == 1, r
pagos.subir_comprobante = lambda t, p, crudo, email=None: "El comprobante tiene que ser una foto (JPG o PNG) o un PDF."
r = conversation.responder_no_texto(tenant, TEL, "document", "doc1")
assert r.startswith("El comprobante tiene que ser") and "/pago/tok123" in r, r
n = len(BANDEJA)
r = conversation.responder_no_texto(tenant, "5491188887777", "image", "doc1")
assert "le avisé a *Lic. Ana Pérez*" in r and BANDEJA[-1][3] == "imagen" and len(BANDEJA) == n + 1, r
print("foto sin pago pendiente: a la bandeja (sin el archivo) OK")

print("\nTODOS LOS TESTS DE BANDEJA Y FRASES PASARON")
