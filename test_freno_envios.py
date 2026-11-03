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
    assert llamadas == ["541100000000"]


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
    assert llamadas == ["541122223333", "1122223333"]


def test_argentina_sin_el_9():
    assert whatsapp.destino("5491128524957") == "541128524957"
    assert whatsapp.destino("+54 9 11 2852-4957") == "541128524957"
    assert whatsapp.destino("541128524957") == "541128524957"
    assert whatsapp.destino("15551234567") == "15551234567"
    assert whatsapp.destino("5215512345678") == "5215512345678"


if __name__ == "__main__":
    for nombre, f in list(globals().items()):
        if nombre.startswith("test_"):
            f()
            print("ok", nombre)
