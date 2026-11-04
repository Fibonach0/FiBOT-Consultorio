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
    return enviar_detalle(para, asunto, texto)[0]


def enviar_detalle(para: str, asunto: str, texto: str) -> tuple[bool, str]:
    """(salió, motivo). El motivo es lo que dijo Resend, sin la clave."""
    if not disponible():
        return False, "faltan RESEND_API_KEY y MAIL_REMITENTE en Railway"
    try:
        r = requests.post("https://api.resend.com/emails", timeout=10,
                          headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
                          json={"from": config.MAIL_REMITENTE, "to": [para], "subject": asunto, "text": texto})
        if r.status_code >= 300:
            log.warning("Resend respondió %s", r.status_code)
            try:
                motivo = str((r.json() or {}).get("message") or "")[:200]
            except ValueError:
                motivo = ""
            return False, f"Resend respondió {r.status_code}" + (f": {motivo}" if motivo else "")
        return True, "ok"
    except requests.RequestException:
        log.warning("No se pudo mandar el mail", exc_info=True)
        return False, "no se pudo hablar con Resend"
