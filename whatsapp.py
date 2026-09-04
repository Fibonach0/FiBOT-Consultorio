"""Envío de mensajes por WhatsApp Cloud API y verificación de firma del
webhook. Una sola META_ACCESS_TOKEN sirve para mandar mensajes DESDE
cualquier número de la plataforma (todos los tenants viven bajo el mismo
Business Portfolio) — lo que cambia por tenant es `phone_number_id`, que
se pasa como parámetro, nunca hardcodeado."""
import hashlib
import hmac
import logging

import requests

import config

logger = logging.getLogger("whatsapp")


def enviar_texto(phone_number_id: str, to: str, texto: str) -> bool:
    url = f"https://graph.facebook.com/{config.META_GRAPH_VERSION}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {config.META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": texto},
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        if r.status_code >= 300:
            logger.error("Meta rechazó el envío (%s): %s", r.status_code, r.text)
            return False
        return True
    except requests.RequestException:
        logger.exception("Error de red mandando WhatsApp")
        return False


def numero_de(tenant: dict) -> str:
    """Desde qué número sale un mensaje de este tenant.

    El suyo si lo tiene; el de la plataforma si todavía no lo dio de alta.
    Está acá y no en db.py porque es una decisión de envío, no un dato: db.py
    devuelve el tenant tal cual está guardado, y quien manda decide con qué
    número sale."""
    return (tenant.get("whatsapp_phone_number_id")
            or config.PLATAFORMA_PHONE_NUMBER_ID)


def enviar_plantilla(phone_number_id: str, to: str, plantilla: str,
                     lang: str, params: list[str]) -> bool:
    """Manda un mensaje de PLANTILLA aprobada.

    Es lo único que Meta deja mandar fuera de la ventana de 24 h desde el
    último mensaje del paciente — y un recordatorio del día antes cae SIEMPRE
    fuera de esa ventana, porque el paciente que reservó por la página web
    nunca le escribió al bot. Con `enviar_texto` esto devuelve error 131047 y
    el recordatorio no llega nunca.

    Los `params` van en el mismo orden que los {{1}}, {{2}}… de la plantilla.
    """
    url = f"https://graph.facebook.com/{config.META_GRAPH_VERSION}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {config.META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": plantilla,
            "language": {"code": lang},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in params],
            }],
        },
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        if r.status_code >= 300:
            logger.error("Meta rechazó la plantilla '%s' (%s): %s", plantilla, r.status_code, r.text)
            return False
        return True
    except requests.RequestException:
        logger.exception("Error de red mandando la plantilla '%s'", plantilla)
        return False


def firma_valida(payload_bytes: bytes, firma_header: str | None) -> bool:
    """Valida X-Hub-Signature-256 contra META_APP_SECRET. Sin esto, cualquiera
    que adivine la URL del webhook podría inyectar mensajes falsos y hacerle
    reservar/cancelar turnos a nombre de otro paciente."""
    if not firma_header or not firma_header.startswith("sha256="):
        return False
    esperada = hmac.new(
        config.META_APP_SECRET.encode(), payload_bytes, hashlib.sha256
    ).hexdigest()
    recibida = firma_header.split("sha256=", 1)[1]
    return hmac.compare_digest(esperada, recibida)
