"""Mails transaccionales por Resend (https://resend.com/docs/api-reference/emails/send-email).

Cosas de la cuenta (recuperar la clave, avisos de seguridad) y, al paciente que
lo dejó al reservar, el link de su recibo. Nunca datos de la ficha ni notas.
"""
import logging

import requests

import config

log = logging.getLogger(__name__)


def disponible() -> bool:
    return bool(config.RESEND_API_KEY and config.MAIL_REMITENTE)


def enviar(para: str, asunto: str, texto: str) -> bool:
    if not disponible():
        return False
    try:
        r = requests.post("https://api.resend.com/emails", timeout=10,
                          headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
                          json={"from": config.MAIL_REMITENTE, "to": [para], "subject": asunto, "text": texto})
        if r.status_code >= 300:
            log.warning("Resend respondió %s", r.status_code)
            return False
        return True
    except requests.RequestException:
        log.warning("No se pudo mandar el mail", exc_info=True)
        return False
