"""Límite de intentos de login y códigos de un solo uso (TOTP, RFC 6238).

TOTP está implementado acá (unas líneas con hmac) para no sumar una
dependencia: es el mismo algoritmo que usan Google Authenticator, Authy, etc.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

MAX_FALLOS = 5
# Por IP más holgado: un consultorio entero puede salir por la misma IP.
MAX_FALLOS_IP = 20
BLOQUEO = timedelta(minutes=15)


def bloqueado_hasta(filas: list[dict], ahora: datetime) -> datetime | None:
    """filas: las de login_intentos para el usuario y para la IP. Si alguna
    está bloqueada, hasta cuándo."""
    hasta = [datetime.fromisoformat(f["bloqueado_hasta"]) for f in filas if f.get("bloqueado_hasta")]
    hasta = [h for h in hasta if h > ahora]
    return max(hasta) if hasta else None


def tras_fallo(fila: dict | None, ahora: datetime, maximo: int = MAX_FALLOS) -> dict:
    """Nuevo estado de una clave de conteo después de un intento fallido.
    Los fallos viejos (más de BLOQUEO sin intentos) no cuentan."""
    fallos = 0
    if fila and fila.get("ultimo") and ahora - datetime.fromisoformat(fila["ultimo"]) < BLOQUEO:
        fallos = fila.get("fallos") or 0
    fallos += 1
    if fallos >= maximo:
        return {"fallos": 0, "bloqueado_hasta": (ahora + BLOQUEO).isoformat(), "ultimo": ahora.isoformat()}
    return {"fallos": fallos, "bloqueado_hasta": None, "ultimo": ahora.isoformat()}


def minutos_restantes(hasta: datetime, ahora: datetime) -> int:
    return max(1, -(-int((hasta - ahora).total_seconds()) // 60))


# --- TOTP ---

PASO = 30


def nuevo_secreto() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _codigo(secreto: str, paso: int) -> str:
    clave = base64.b32decode(secreto + "=" * (-len(secreto) % 8))
    digest = hmac.new(clave, struct.pack(">Q", paso), hashlib.sha1).digest()
    desde = digest[-1] & 0x0F
    numero = (struct.unpack(">I", digest[desde:desde + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{numero:06d}"


def verificar(secreto: str, codigo: str, ultimo_paso: int | None = None, cuando: float | None = None) -> int | None:
    """Devuelve el paso usado si el código vale (acepta un paso de margen
    para relojes corridos) y no se usó antes; si no, None."""
    codigo = "".join(ch for ch in codigo or "" if ch.isdigit())
    if len(codigo) != 6:
        return None
    actual = int((cuando if cuando is not None else time.time()) // PASO)
    for paso in (actual - 1, actual, actual + 1):
        if hmac.compare_digest(_codigo(secreto, paso), codigo) and (ultimo_paso is None or paso > ultimo_paso):
            return paso
    return None


def uri(secreto: str, usuario: str, emisor: str = "FiBOT Consultorio") -> str:
    return (f"otpauth://totp/{quote(emisor)}:{quote(usuario)}?secret={secreto}"
            f"&issuer={quote(emisor)}&algorithm=SHA1&digits=6&period={PASO}")


def agrupado(secreto: str) -> str:
    """Para tipearlo a mano en la app: de a 4 letras."""
    return " ".join(secreto[i:i + 4] for i in range(0, len(secreto), 4))


def ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


# --- Dispositivo, tokens y QR ---

def dispositivo(user_agent: str | None) -> str:
    """«Chrome en Windows»: lo justo para reconocer una sesión, sin guardar el
    user agent entero."""
    ua = user_agent or ""
    navegador = next((n for clave, n in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                                         ("Chrome/", "Chrome"), ("Safari/", "Safari")) if clave in ua), "Navegador")
    sistema = next((s for clave, s in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                       ("Windows", "Windows"), ("Mac OS", "Mac"), ("Linux", "Linux")) if clave in ua), "")
    return f"{navegador} en {sistema}" if sistema else navegador


def nuevo_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def qr_svg(texto: str, escala: int = 5) -> str:
    """SVG en línea (sin servicios externos: el QR del doble factor lleva el
    secreto y no puede salir a ningún lado)."""
    import segno
    return segno.make(texto, error="m").svg_inline(scale=escala, border=2, dark="#1c1b19", light="#ffffff")
