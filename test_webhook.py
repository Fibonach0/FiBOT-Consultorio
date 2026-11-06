"""Smoke test del webhook y el panel. Como en fleet-bot-pastor: pensado para
correrse SUELTO (`python test_webhook.py`), no en batch con otros test_*.py —
monkeypatchea `db` sin restaurarlo, así que correrlo junto a test_conversation.py
en el mismo proceso (pytest) puede pisar los parches del otro archivo."""
import hashlib
import hmac
import json
import os

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault(
    "SUPABASE_SERVICE_ROLE_KEY",
    "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.fake_signature_not_real",
)
os.environ.setdefault("META_ACCESS_TOKEN", "fake")
os.environ.setdefault("META_APP_SECRET", "supersecret")
os.environ.setdefault("META_VERIFY_TOKEN", "verify123")
os.environ.setdefault("FLASK_SECRET_KEY", "fake")

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

# get_tenant_by_phone_number_id pega contra Supabase de verdad — se pisa acá
# porque este test es de plomería (firma, ruteo, códigos de estado), no de
# la base. La lógica de esa función se lee, no hace falta mockear Supabase.
db.get_tenant_by_phone_number_id = lambda pid: None
db.marcar_evento = lambda clave, detalle=None: None  # la hora del último mensaje no es lo que se prueba acá

client = app.app.test_client()

r = client.get("/health")
assert r.status_code == 200, r.status_code
print("GET /health ->", r.status_code, r.data)

r = client.get("/login")
assert r.status_code == 200
print("GET /login -> 200 OK, tiene formulario:", b"usuario" in r.data)

r = client.get(
    "/webhook",
    query_string={"hub.mode": "subscribe", "hub.verify_token": "verify123", "hub.challenge": "abc123"},
)
assert r.status_code == 200 and r.data == b"abc123", (r.status_code, r.data)
print("GET /webhook (verify correcto) -> 200, challenge devuelto OK")

r = client.get(
    "/webhook",
    query_string={"hub.mode": "subscribe", "hub.verify_token": "MAL", "hub.challenge": "abc123"},
)
assert r.status_code == 403, r.status_code
print("GET /webhook (verify_token incorrecto) -> 403 OK")

payload = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "metadata": {"phone_number_id": "999999"},
                        "messages": [{"type": "text", "from": "5491100000000", "text": {"body": "hola"}}],
                    }
                }
            ]
        }
    ]
}
body = json.dumps(payload).encode()

r = client.post("/webhook", data=body, content_type="application/json")
assert r.status_code == 403, r.status_code
print("POST /webhook sin firma -> 403 OK")

r = client.post(
    "/webhook", data=body, content_type="application/json", headers={"X-Hub-Signature-256": "sha256=deadbeef"}
)
assert r.status_code == 403, r.status_code
print("POST /webhook con firma inválida -> 403 OK")

firma = hmac.new(b"supersecret", body, hashlib.sha256).hexdigest()
r = client.post(
    "/webhook", data=body, content_type="application/json", headers={"X-Hub-Signature-256": f"sha256={firma}"}
)
assert r.status_code == 200, (r.status_code, r.data)
print("POST /webhook con firma válida, tenant inexistente -> 200 OK (ignora sin explotar)")

print("\nTODOS LOS SMOKE TESTS PASARON")


# --- Lo que no es texto ya no se ignora, y todo se marca como leído ---
import conversation  # noqa: E402
import whatsapp  # noqa: E402

_t = {"id": "t1", "nombre": "Lic. Ana", "whatsapp_phone_number_id": "PN1"}
db.get_tenant_by_phone_number_id = lambda pid: _t if pid == "PN1" else None
LEIDOS, ENVIADOS, NO_TEXTO = [], [], []
whatsapp.marcar_leido = lambda pid, mid: LEIDOS.append((pid, mid)) or True
whatsapp.enviar_texto = lambda pid, to, texto: ENVIADOS.append((pid, to, texto)) or True
conversation.responder_no_texto = lambda t, tel, tipo, media_id=None: NO_TEXTO.append((t["id"], tel, tipo, media_id)) or (
    None if tipo == "reaction" else f"respuesta a {tipo}")


def _postear(mensajes):
    cuerpo = json.dumps({"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "PN1"}, "messages": mensajes}}]}]}).encode()
    f = hmac.new(b"supersecret", cuerpo, hashlib.sha256).hexdigest()
    return client.post("/webhook", data=cuerpo, content_type="application/json", headers={"X-Hub-Signature-256": f"sha256={f}"})


r = _postear([{"id": "wamid.A", "type": "audio", "from": "5491100000001", "audio": {"id": "media-a"}},
              {"id": "wamid.B", "type": "image", "from": "5491100000001", "image": {"id": "media-b"}},
              {"id": "wamid.C", "type": "reaction", "from": "5491100000001", "reaction": {"emoji": "👍"}}])
assert r.status_code == 200
assert NO_TEXTO == [("t1", "5491100000001", "audio", "media-a"), ("t1", "5491100000001", "image", "media-b")], NO_TEXTO
assert [e[2] for e in ENVIADOS] == ["respuesta a audio", "respuesta a image"] and all(e[0] == "PN1" for e in ENVIADOS), ENVIADOS
assert LEIDOS == [("PN1", "wamid.A"), ("PN1", "wamid.B")], LEIDOS
print("audio y foto: se contestan; la reacción no; todo menos la reacción queda leído OK")
