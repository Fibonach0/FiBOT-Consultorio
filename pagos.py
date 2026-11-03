"""Cobro de las reservas web.

Cada profesional elige en el panel (`tenants.cobro_web`):
- 'mercadopago': conecta SU cuenta con «Conectar Mercado Pago» (OAuth: entra
  con su usuario, no copia claves). El paciente paga con Checkout Pro y Mercado
  Pago avisa al webhook; la reserva se acepta sola.
- 'transferencia': el paciente ve el alias/CBU y sube el comprobante; el
  profesional lo valida desde el panel.
- 'ambos': el paciente elige.

La reserva queda apartada (`turnos.pago_web = 'esperando'`) hasta que se paga:
`MINUTOS_PARA_PAGAR` con Mercado Pago, `HORAS_PARA_COMPROBANTE` para subir el
comprobante. Si vence, se libera (`vencer`). Un pago que llega tarde queda en
'aprobado_tarde' y se le avisa al profesional para que lo devuelva.

Nunca se confía en lo que dice el aviso de Mercado Pago: se consulta el pago
con el token del profesional y se compara la referencia y el monto.
"""
import base64
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken

import config
import db

logger = logging.getLogger("pagos")

MINUTOS_PARA_PAGAR = 30
HORAS_PARA_COMPROBANTE = 2
API = "https://api.mercadopago.com"
COMPROBANTE_MAX = 2_000_000
COMPROBANTE_TIPOS = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg", b"%PDF-": "application/pdf"}


# --- Cifrado del token del profesional ---

def _fernet() -> Fernet:
    # Derivada de FLASK_SECRET_KEY: si se cambia esa clave, cada profesional
    # tiene que volver a conectar Mercado Pago (los tokens viejos no se leen).
    clave = hashlib.sha256(("mp-tokens:" + config.FLASK_SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(clave))


def cifrar(texto: str) -> str:
    return _fernet().encrypt(texto.encode()).decode()


def descifrar(texto: str | None) -> str | None:
    if not texto:
        return None
    try:
        return _fernet().decrypt(texto.encode()).decode()
    except InvalidToken:
        return None


# --- Qué cobra cada profesional ---

def activo(tenant: dict) -> bool:
    """¿Las reservas web de este profesional se pagan? Sólo si hay al menos un
    medio que funcione."""
    return bool(metodos(tenant))


def metodos(tenant: dict) -> list[str]:
    modo = tenant.get("cobro_web") or "no"
    salida = []
    if modo in ("mercadopago", "ambos") and mp_disponible() and tenant.get("mp_token_cifrado"):
        salida.append("mercadopago")
    if modo in ("transferencia", "ambos") and (tenant.get("transferencia_datos") or "").strip():
        salida.append("transferencia")
    return salida


def mp_disponible() -> bool:
    return bool(config.MP_CLIENT_ID and config.MP_CLIENT_SECRET)


def monto_de(tenant: dict, precio: int | None, sesiones: int = 1) -> int | None:
    """Cuánto se cobra: el total (precio del tipo, o el de la sesión) o la
    seña, por cada sesión. None si no hay precio cargado (no se puede cobrar)."""
    precio = precio or tenant.get("precio_sesion")
    if (tenant.get("cobro_modo") or "total") == "sena" and tenant.get("sena_monto"):
        por_sesion = min(tenant["sena_monto"], precio) if precio else tenant["sena_monto"]
    else:
        por_sesion = precio
    return por_sesion * sesiones if por_sesion else None


# --- OAuth con Mercado Pago ---

def url_conectar(state: str, redirect_uri: str) -> str:
    return "https://auth.mercadopago.com/authorization?" + urlencode({
        "client_id": config.MP_CLIENT_ID, "response_type": "code", "platform_id": "mp",
        "state": state, "redirect_uri": redirect_uri})


def _guardar_tokens(tenant_id: str, datos: dict) -> None:
    db.guardar_ajustes(tenant_id, {
        "mp_user_id": str(datos.get("user_id") or ""),
        "mp_token_cifrado": cifrar(datos["access_token"]),
        "mp_refresh_cifrado": cifrar(datos["refresh_token"]) if datos.get("refresh_token") else None,
        "mp_vence": (datetime.now(timezone.utc) + timedelta(seconds=int(datos.get("expires_in") or 15552000))).isoformat(),
    })


def conectar(tenant_id: str, code: str, redirect_uri: str) -> str | None:
    """Canjea el código de la vuelta del login por los tokens del profesional.
    Devuelve None si salió bien o el motivo."""
    try:
        r = requests.post(f"{API}/oauth/token", json={
            "client_id": config.MP_CLIENT_ID, "client_secret": config.MP_CLIENT_SECRET,
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "test_token": "true" if config.MP_PRUEBA else "false"}, timeout=15)
    except requests.RequestException:
        return "No se pudo conectar con Mercado Pago."
    if r.status_code >= 300:
        logger.warning("Mercado Pago rechazó el código (%s)", r.status_code)
        return "Mercado Pago no aceptó la conexión. Probá de nuevo."
    datos = r.json()
    if not datos.get("access_token"):
        return "Mercado Pago no devolvió el permiso."
    _guardar_tokens(tenant_id, datos)
    return None


def desconectar(tenant_id: str) -> None:
    db.guardar_ajustes(tenant_id, {"mp_user_id": None, "mp_token_cifrado": None, "mp_refresh_cifrado": None,
                                   "mp_vence": None})


def _token(tenant: dict) -> str | None:
    """Token del profesional; si vence en menos de 15 días, lo renueva."""
    token = descifrar(tenant.get("mp_token_cifrado"))
    vence = tenant.get("mp_vence")
    if token and vence and datetime.fromisoformat(str(vence)) - datetime.now(timezone.utc) < timedelta(days=15):
        refresh = descifrar(tenant.get("mp_refresh_cifrado"))
        if refresh:
            try:
                r = requests.post(f"{API}/oauth/token", json={
                    "client_id": config.MP_CLIENT_ID, "client_secret": config.MP_CLIENT_SECRET,
                    "grant_type": "refresh_token", "refresh_token": refresh}, timeout=15)
                if r.status_code < 300 and r.json().get("access_token"):
                    _guardar_tokens(tenant["id"], r.json())
                    return r.json()["access_token"]
            except requests.RequestException:
                logger.warning("No se pudo renovar el token de Mercado Pago del tenant %s", tenant["id"])
    return token


# --- El pago de una reserva ---

def iniciar(tenant: dict, turnos: list[dict], monto: int) -> dict:
    """Aparta los turnos (ya reservados) a la espera del pago y crea el pago.
    Con un solo medio que sea transferencia, el plazo es el del comprobante."""
    medios = metodos(tenant)
    plazo = timedelta(minutes=MINUTOS_PARA_PAGAR) if "mercadopago" in medios else timedelta(hours=HORAS_PARA_COMPROBANTE)
    vence = datetime.now(timezone.utc) + plazo
    pago = db.crear_pago({"tenant_id": tenant["id"], "token": secrets.token_urlsafe(18), "turnos": [t["id"] for t in turnos],
                          "monto": monto, "vence": vence.isoformat(), "estado": "esperando",
                          "metodo": medios[0] if len(medios) == 1 else None})
    for t in turnos:
        db.actualizar_reserva(tenant["id"], t["id"], {"pago_web": "esperando", "pago_vence": vence.isoformat()})
    return pago


def preferencia(tenant: dict, pago: dict, descripcion: str, base: str) -> str | None:
    """Crea el cobro en Mercado Pago con la cuenta del profesional y devuelve
    el link de pago (o None)."""
    token = _token(tenant)
    if not token:
        return None
    vuelta = f"{base}/pago/{pago['token']}"
    cuerpo = {
        "items": [{"id": pago["id"], "title": descripcion[:250], "quantity": 1, "currency_id": "ARS",
                   "unit_price": float(pago["monto"])}],
        "external_reference": pago["token"],
        "back_urls": {"success": vuelta, "pending": vuelta, "failure": vuelta},
        "auto_return": "approved",
        "notification_url": f"{base}/webhook/mercadopago?t={tenant['id']}",
        "expires": True,
        "expiration_date_to": datetime.fromisoformat(str(pago["vence"])).astimezone(timezone.utc).isoformat(timespec="milliseconds"),
    }
    try:
        r = requests.post(f"{API}/checkout/preferences", json=cuerpo,
                          headers={"Authorization": f"Bearer {token}"}, timeout=15)
    except requests.RequestException:
        return None
    if r.status_code >= 300:
        logger.warning("Mercado Pago no creó la preferencia (%s): %s", r.status_code, r.text[:300])
        return None
    datos = r.json()
    db.actualizar_pago(pago["id"], {"metodo": "mercadopago"})
    return datos.get("sandbox_init_point") if config.MP_PRUEBA and datos.get("sandbox_init_point") else datos.get("init_point")


def revisar_pago_mp(tenant: dict, mp_pago_id: str) -> str | None:
    """Consulta un pago en Mercado Pago (con el token del profesional) y, si
    está aprobado y corresponde a uno de nuestros pagos por referencia y
    monto, acepta la reserva. Devuelve el estado de nuestro pago o None."""
    token = _token(tenant)
    if not token or not str(mp_pago_id).isdigit():
        return None
    try:
        r = requests.get(f"{API}/v1/payments/{mp_pago_id}", headers={"Authorization": f"Bearer {token}"}, timeout=15)
    except requests.RequestException:
        return None
    if r.status_code >= 300:
        return None
    datos = r.json()
    pago = db.pago_por_token(datos.get("external_reference") or "")
    if not pago or pago["tenant_id"] != tenant["id"]:
        return None
    if datos.get("status") != "approved":
        return pago["estado"]
    if float(datos.get("transaction_amount") or 0) + 0.01 < pago["monto"]:
        logger.warning("Pago %s de monto menor al esperado", mp_pago_id)
        return pago["estado"]
    return aprobar(tenant, pago, mp_pago_id=str(mp_pago_id))


def aprobar(tenant: dict, pago: dict, mp_pago_id: str | None = None) -> str:
    """Acepta la reserva: los turnos pasan a pagados y se le confirma al
    paciente. Idempotente (Mercado Pago avisa más de una vez). Si los turnos ya
    no estaban apartados para este pago, queda 'aprobado_tarde' y se le avisa
    al profesional."""
    if pago["estado"] in ("aprobado", "aprobado_tarde"):
        return pago["estado"]
    ahora = datetime.now(timezone.utc).isoformat()
    turnos = [db.turno_por_id(tenant["id"], tid) for tid in pago["turnos"]]
    vigentes = [t for t in turnos if t and t["estado"] == "reservado" and t.get("pago_web") in ("esperando", "comprobante")]
    if len(vigentes) != len(pago["turnos"]):
        db.actualizar_pago(pago["id"], {"estado": "aprobado_tarde", "mp_pago_id": mp_pago_id, "resuelto": ahora,
                                        "comprobante": None})
        import scheduler
        scheduler.avisar_profesional(tenant, f"llegó un pago de ${pago['monto']:,} de una reserva que ya se había liberado; "
                                             "revisalo en el panel para devolverlo o reprogramar".replace(",", "."))
        return "aprobado_tarde"
    for t in vigentes:
        db.actualizar_reserva(tenant["id"], t["id"], {"pago_web": "aprobado", "pago_vence": None, "pago": "pagado",
                                                      "confirmado_at": ahora, "reserva_nueva": True, "reserva_avisada": False})
    db.actualizar_pago(pago["id"], {"estado": "aprobado", "mp_pago_id": mp_pago_id, "resuelto": ahora, "comprobante": None})
    _confirmar_al_paciente(tenant, vigentes)
    return "aprobado"


def _confirmar_al_paciente(tenant: dict, turnos: list[dict]) -> None:
    """Ya pagó: le llega la confirmación con el link para agendar (dentro de
    la ventana de 24 h no hace falta: el paciente no escribió; va con la
    plantilla de recordatorio, que dice con quién y cuándo)."""
    import scheduler
    import whatsapp
    from zoneinfo import ZoneInfo
    numero = whatsapp.numero_de(tenant)
    if not numero or not turnos:
        return
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    primero = min(turnos, key=lambda t: t["inicio"])
    try:
        if config.RECORDATORIO_TEMPLATE:
            whatsapp.enviar_plantilla(numero, primero["paciente_telefono"], config.RECORDATORIO_TEMPLATE,
                                      config.RECORDATORIO_TEMPLATE_LANG,
                                      [tenant["nombre"], scheduler.cuando_de(primero, tenant, tz)])
    except Exception:
        logger.warning("No se pudo confirmar el pago al paciente", exc_info=True)


def rechazar(tenant: dict, pago: dict) -> None:
    """El profesional dice que el comprobante no es válido: se libera."""
    for tid in pago["turnos"]:
        t = db.turno_por_id(tenant["id"], tid)
        if t and t["estado"] == "reservado" and t.get("pago_web") in ("esperando", "comprobante"):
            db.cancelar_turno(tenant["id"], tid)
    db.actualizar_pago(pago["id"], {"estado": "rechazado", "resuelto": datetime.now(timezone.utc).isoformat(),
                                    "comprobante": None})


def subir_comprobante(tenant: dict, pago: dict, crudo: bytes) -> str | None:
    """Guarda el comprobante y deja la reserva apartada hasta que el
    profesional lo valide. Devuelve None o el motivo del rechazo."""
    tipo = next((t for firma, t in COMPROBANTE_TIPOS.items() if crudo.startswith(firma)), None)
    if not crudo:
        return "Elegí el comprobante."
    if len(crudo) > COMPROBANTE_MAX:
        return "El archivo es muy pesado: hasta 2 MB."
    if not tipo:
        return "El comprobante tiene que ser una foto (JPG o PNG) o un PDF."
    db.actualizar_pago(pago["id"], {"estado": "comprobante", "metodo": "transferencia",
                                    "comprobante": f"data:{tipo};base64,{base64.b64encode(crudo).decode()}"})
    for tid in pago["turnos"]:
        db.actualizar_reserva(tenant["id"], tid, {"pago_web": "comprobante", "pago_vence": None})
    import scheduler
    scheduler.avisar_profesional(tenant, "un paciente subió el comprobante de una transferencia: validalo en el panel")
    return None


def vencer(tenant: dict, ahora: datetime | None = None) -> int:
    """Libera las reservas web que no se pagaron a tiempo. Lo llama el
    scheduler. Antes de liberar una de Mercado Pago, no hace falta consultar:
    si el pago llega después, queda 'aprobado_tarde'."""
    ahora = ahora or datetime.now(timezone.utc)
    vencidos = 0
    for pago in db.pagos_esperando(tenant["id"]):
        if datetime.fromisoformat(str(pago["vence"])) > ahora:
            continue
        for tid in pago["turnos"]:
            t = db.turno_por_id(tenant["id"], tid)
            if t and t["estado"] == "reservado" and t.get("pago_web") == "esperando":
                db.cancelar_turno(tenant["id"], tid)
        db.actualizar_pago(pago["id"], {"estado": "vencido", "resuelto": ahora.isoformat()})
        vencidos += 1
    return vencidos
