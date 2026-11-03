"""Freno de envíos (WHATSAPP_SOLO_A). Correrlo suelto: python test_freno_envios.py"""
import os

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "fake")
os.environ.setdefault("META_ACCESS_TOKEN", "fake")
os.environ.setdefault("META_APP_SECRET", "fake")
os.environ.setdefault("META_VERIFY_TOKEN", "fake")
os.environ.setdefault("FLASK_SECRET_KEY", "fake")

import config
import whatsapp

llamadas = []


class _Resp:
    status_code = 200
    text = "ok"


whatsapp.requests.post = lambda *a, **k: llamadas.append(k["json"]["to"]) or _Resp()


def test_sin_freno_manda_a_todos():
    config.WHATSAPP_SOLO_A = []
    llamadas.clear()
    assert whatsapp.enviar_texto("N", "5491100000000", "hola")
    assert llamadas == ["5491100000000"]


def test_con_freno_solo_a_la_lista():
    config.WHATSAPP_SOLO_A = ["+54 9 11 2222-3333"]
    llamadas.clear()
    # Mismo teléfono escrito distinto: pasa.
    assert whatsapp.enviar_texto("N", "5491122223333", "hola")
    assert whatsapp.enviar_plantilla("N", "1122223333", "p", "es_AR", ["a"])
    # Otro teléfono: no sale nada, pero devuelve True para no reintentar.
    assert whatsapp.enviar_texto("N", "5491199998888", "hola")
    assert whatsapp.enviar_plantilla("N", "5491199998888", "p", "es_AR", ["a"])
    assert whatsapp.enviar_texto("N", "", "hola")
    assert llamadas == ["5491122223333", "1122223333"]


def test_formato_del_15():
    assert whatsapp.con_15("5491128524957") == "54111528524957"
    assert whatsapp.con_15("+54 9 351 555-1234") == "54351155551234"
    assert whatsapp.con_15("5492954123456") == "54295415123456"  # Santa Rosa: 4 dígitos
    assert whatsapp.con_15("541128524957") is None
    assert whatsapp.con_15("15551234567") is None


def test_131030_reintenta_con_el_15():
    config.WHATSAPP_SOLO_A = []
    destinos = []

    class _Rechazo:
        status_code = 400
        text = "131030"

        def json(self):
            return {"error": {"code": 131030, "message": "not in allowed list"}}

    def post(url, headers=None, json=None, timeout=None):
        destinos.append(json["to"])
        return _Rechazo() if len(destinos) == 1 else _Resp()

    viejo = whatsapp.requests.post
    whatsapp.requests.post = post
    try:
        ok, detalle = whatsapp.enviar_plantilla_detalle("N", "5491128524957", "hello_world", "en_US", [])
        assert ok and detalle == "aceptado"
        assert destinos == ["5491128524957", "54111528524957"]
        # Otro error no reintenta.
        destinos.clear()

        class _Otro(_Rechazo):
            def json(self):
                return {"error": {"code": 132001}}
        whatsapp.requests.post = lambda url, headers=None, json=None, timeout=None: destinos.append(json["to"]) or _Otro()
        assert not whatsapp.enviar_texto("N", "5491128524957", "hola")
        assert destinos == ["5491128524957"]
    finally:
        whatsapp.requests.post = viejo


if __name__ == "__main__":
    for nombre, f in list(globals().items()):
        if nombre.startswith("test_"):
            f()
            print("ok", nombre)
