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

import app  # noqa: E402
import db  # noqa: E402

# get_tenant_by_phone_number_id pega contra Supabase de verdad — se pisa acá
# porque este test es de plomería (firma, ruteo, códigos de estado), no de
# la base. La lógica de esa función se lee, no hace falta mockear Supabase.
db.get_tenant_by_phone_number_id = lambda pid: None

client = app.app.test_client()

r = client.get("/")
assert r.status_code == 200, r.status_code
print("GET / ->", r.status_code, r.data)

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
