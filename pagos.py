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
comprobante. Si vence, se libera (`vencer`); antes se le avisa al paciente una
vez (`avisar_por_vencer`). Un pago que llega tarde queda en 'aprobado_tarde' y
se le avisa al profesional para que lo devuelva.

Devoluciones: con Mercado Pago se hacen desde el panel (`devolver`, la API de
reintegros con el token del profesional); una transferencia se devuelve por
fuera y se anota (`a_devolver`). Si el paciente cancela una sesión pagada,
`al_cancelar` aplica la política del profesional (`tenants.devolucion_modo` y
`devolucion_horas`).

Nunca se confía en lo que dice el aviso de Mercado Pago: se consulta el pago
con el token del profesional y se compara la referencia y el monto.
"""
import base64
import hashlib
import logging
import secrets
import time
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
    if modo in ("mercadopago", "ambos") and mp_disponible() and tenant.get("mp_token_cifrado") and not tenant.get("mp_caido_at"):
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
        "mp_caido_at": None,
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
        try:
            motivo = str(r.json().get("error") or r.json().get("message") or "")[:60]
        except ValueError:
            motivo = ""
        logger.warning("Mercado Pago rechazó el código (%s %s)", r.status_code, motivo)
        return "Mercado Pago no aceptó la conexión" + (f" ({motivo})." if motivo else ".")
    datos = r.json()
    if not datos.get("access_token"):
        return "Mercado Pago no devolvió el permiso."
    _guardar_tokens(tenant_id, datos)
    return None


def conectar_con_token(tenant_id: str, token: str) -> str | None:
    """Solo con MP_PRUEBA: guarda a mano el Access Token de una cuenta de
    prueba (las cuentas de prueba no tienen Client Secret, así que el OAuth no
    se puede probar). Antes lo valida contra Mercado Pago. None si salió bien."""
    if not config.MP_PRUEBA:
        return "Esto solo se puede usar en modo de prueba."
    token = (token or "").strip()
    if not token.startswith(("APP_USR-", "TEST-")) or len(token) > 200 or any(c.isspace() for c in token):
        return "Eso no parece un Access Token de Mercado Pago (empieza con APP_USR-)."
    try:
        r = requests.get(f"{API}/users/me", headers={"Authorization": f"Bearer {token}"}, timeout=15)
    except requests.RequestException:
        return "No se pudo conectar con Mercado Pago."
    if r.status_code >= 300 or not r.json().get("id"):
        return "Mercado Pago no reconoció ese Access Token."
    _guardar_tokens(tenant_id, {"access_token": token, "user_id": r.json()["id"]})
    return None


def desconectar(tenant_id: str) -> None:
    db.guardar_ajustes(tenant_id, {"mp_user_id": None, "mp_token_cifrado": None, "mp_refresh_cifrado": None,
                                   "mp_vence": None, "mp_caido_at": None})


def marcar_caida(tenant: dict) -> None:
    """Mercado Pago ya no acepta el permiso del profesional (lo revocó desde
    su cuenta, o venció sin poder renovarse). Se deja de ofrecer el pago por
    Mercado Pago y se le avisa UNA vez para que reconecte."""
    if tenant.get("mp_caido_at") or not tenant.get("mp_token_cifrado"):
        return
    db.guardar_ajustes(tenant["id"], {"mp_caido_at": datetime.now(timezone.utc).isoformat()})
    tenant["mp_caido_at"] = True
    logger.warning("Se cortó la conexión con Mercado Pago del tenant %s", tenant["id"])
    try:
        import scheduler
        scheduler.avisar_profesional(tenant, "se cortó la conexión con tu Mercado Pago y las reservas web ya no se cobran "
                                             "por ahí; entrá al panel y tocá «Conectar Mercado Pago» para reconectarla")
    except Exception:
        logger.exception("No se pudo avisar la conexión caída de Mercado Pago")
    try:
        import whatsapp
        whatsapp.avisar_admin(f"se cortó la conexión con Mercado Pago de {tenant.get('nombre') or 'un profesional'}; "
                              "ya le avisamos para que reconecte")
    except Exception:
        logger.exception("No se pudo avisar a administración la conexión caída de Mercado Pago")


REINTENTOS_CAIDA = 2
_pausa = time.sleep  # los tests la reemplazan


def _rechazo_confirmado(token: str) -> bool:
    """Mercado Pago a veces contesta 401/403 por algo pasajero. Antes de dar
    la conexión por cortada se le pregunta de nuevo (`/users/me`), con una
    pausa corta entre intentos: sólo si sigue rechazando se corta. Sin red no
    se sabe nada, así que no se corta."""
    for intento in range(REINTENTOS_CAIDA):
        _pausa(intento + 1)
        try:
            r = requests.get(f"{API}/users/me", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        except requests.RequestException:
            return False
        if r.status_code not in (401, 403):
            logger.info("Mercado Pago rechazó una vez y después aceptó: no se corta la conexión")
            return False
    return True


_verificado: dict[str, object] = {}


def verificar_conexion(tenant: dict, hoy) -> None:
    """Una vez por día: ¿Mercado Pago sigue aceptando el permiso? Así el
    profesional se entera antes de que un paciente quiera pagar."""
    if not tenant.get("mp_token_cifrado") or tenant.get("mp_caido_at") or _verificado.get(tenant["id"]) == hoy:
        return
    _verificado[tenant["id"]] = hoy
    token = _token(tenant)
    if not token or tenant.get("mp_caido_at"):
        return
    try:
        r = requests.get(f"{API}/users/me", headers={"Authorization": f"Bearer {token}"}, timeout=15)
    except requests.RequestException:
        return
    if r.status_code in (401, 403) and _rechazo_confirmado(token):
        marcar_caida(tenant)


def _token(tenant: dict) -> str | None:
    """Token del profesional; si vence en menos de 15 días, lo renueva."""
    token = descifrar(tenant.get("mp_token_cifrado"))
    vence = tenant.get("mp_vence")
    if token and vence and datetime.fromisoformat(str(vence)) - datetime.now(timezone.utc) < timedelta(days=15):
        refresh = descifrar(tenant.get("mp_refresh_cifrado"))
        if refresh:
            # invalid_grant (el profesional revocó el permiso) se reintenta
            # una vez por si fue algo pasajero, antes de dar la conexión por cortada.
            for intento in range(REINTENTOS_CAIDA):
                try:
                    r = requests.post(f"{API}/oauth/token", json={
                        "client_id": config.MP_CLIENT_ID, "client_secret": config.MP_CLIENT_SECRET,
                        "grant_type": "refresh_token", "refresh_token": refresh}, timeout=15)
                except requests.RequestException:
                    logger.warning("No se pudo renovar el token de Mercado Pago del tenant %s", tenant["id"])
                    break
                if r.status_code < 300 and r.json().get("access_token"):
                    _guardar_tokens(tenant["id"], r.json())
                    return r.json()["access_token"]
                if r.status_code not in (400, 401, 403):
                    break
                if intento + 1 < REINTENTOS_CAIDA:
                    _pausa(intento + 1)
                    continue
                marcar_caida(tenant)
                return None
    return token


# --- El pago de una reserva ---

def iniciar(tenant: dict, turnos: list[dict], monto: int, email: str | None = None) -> dict:
    """Aparta los turnos (ya reservados) a la espera del pago y crea el pago.
    Con un solo medio que sea transferencia, el plazo es el del comprobante."""
    medios = metodos(tenant)
    plazo = timedelta(minutes=MINUTOS_PARA_PAGAR) if "mercadopago" in medios else timedelta(hours=HORAS_PARA_COMPROBANTE)
    vence = datetime.now(timezone.utc) + plazo
    pago = db.crear_pago({"tenant_id": tenant["id"], "token": secrets.token_urlsafe(18), "turnos": [t["id"] for t in turnos],
                          "monto": monto, "vence": vence.isoformat(), "estado": "esperando",
                          "metodo": medios[0] if len(medios) == 1 else None,
                          "paciente_nombre": next((t.get("paciente_nombre") for t in turnos if t.get("paciente_nombre")), None),
                          "sesiones": sorted(str(t["inicio"]) for t in turnos),
                          **({"paciente_email": email} if email else {})})
    for t in turnos:
        db.actualizar_reserva(tenant["id"], t["id"], {"pago_web": "esperando", "pago_vence": vence.isoformat(),
                                                      "pago_id": pago["id"]})
    return pago


def descriptor_resumen(tenant: dict) -> str:
    """Lo que el paciente ve en el resumen de la tarjeta: el nombre del
    profesional, sin tildes ni símbolos y hasta 22 caracteres (lo que admite
    Mercado Pago). Que reconozca el cobro evita desconocimientos."""
    import unicodedata
    plano = unicodedata.normalize("NFKD", tenant.get("nombre") or "").encode("ascii", "ignore").decode()
    limpio = " ".join("".join(c if c.isalnum() or c == " " else " " for c in plano).split())
    return (limpio or "Consultorio")[:22].strip()


def _comprador(nombre: str | None, email: str | None = None) -> dict | None:
    """Nombre, apellido y email del paciente para Mercado Pago (mejora la
    aprobación del pago). Sólo lo que dejó al reservar: nada de la ficha."""
    partes = (nombre or "").split()
    datos = {}
    if partes:
        datos["name"] = partes[0][:60]
        if len(partes) > 1:
            datos["surname"] = " ".join(partes[1:])[:60]
    if email:
        datos["email"] = email
    return datos or None


def preferencia(tenant: dict, pago: dict, descripcion: str, base: str) -> str | None:
    """Crea el cobro en Mercado Pago con la cuenta del profesional y devuelve
    el link de pago (o None)."""
    token = _token(tenant)
    if not token:
        return None
    vuelta = f"{base}/pago/{pago['token']}"
    cuerpo = {
        "items": [{"id": pago["id"], "title": descripcion[:250], "description": descripcion[:250],
                   "category_id": "services", "quantity": 1, "currency_id": "ARS",
                   "unit_price": float(pago["monto"])}],
        "external_reference": pago["token"],
        "statement_descriptor": descriptor_resumen(tenant),
        **({"payer": comprador} if (comprador := _comprador(pago.get("paciente_nombre"), pago.get("paciente_email"))) else {}),
        "back_urls": {"success": vuelta, "pending": vuelta, "failure": vuelta},
        "auto_return": "approved",
        "notification_url": f"{base}/webhook/mercadopago?t={tenant['id']}",
        "expires": True,
        "expiration_date_to": datetime.fromisoformat(str(pago["vence"])).astimezone(timezone.utc).isoformat(timespec="milliseconds"),
    }
    try:
        r = requests.post(f"{API}/checkout/preferences", json=cuerpo,
                          headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": f"pref-{pago['token']}"},
                          timeout=15)
    except requests.RequestException:
        return None
    if r.status_code in (401, 403):
        if _rechazo_confirmado(token):
            marcar_caida(tenant)
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
    if r.status_code in (401, 403):
        if _rechazo_confirmado(token):
            marcar_caida(tenant)
        return None
    if r.status_code >= 300:
        return None
    datos = r.json()
    pago = db.pago_por_token(datos.get("external_reference") or "")
    if not pago or pago["tenant_id"] != tenant["id"]:
        return None
    if datos.get("status") in ("refunded", "charged_back") or float(datos.get("transaction_amount_refunded") or 0) > 0:
        return _sincronizar_devolucion(tenant, pago, str(mp_pago_id), datos)
    if datos.get("status") != "approved":
        return pago["estado"]
    if float(datos.get("transaction_amount") or 0) + 0.01 < pago["monto"]:
        logger.warning("Pago %s de monto menor al esperado", mp_pago_id)
        return pago["estado"]
    return aprobar(tenant, pago, mp_pago_id=str(mp_pago_id))


def _sincronizar_devolucion(tenant: dict, pago: dict, mp_pago_id: str, datos: dict) -> str:
    """Una devolución (o un contracargo) hecha desde Mercado Pago, por fuera
    del panel: se anota lo que falte para que el panel coincida con Mercado
    Pago. Idempotente: si ya estaba anotado, no suma de nuevo."""
    if pago.get("mp_pago_id") != mp_pago_id or pago["estado"] not in ("aprobado", "aprobado_tarde", "devuelto"):
        return pago["estado"]
    if datos.get("status") in ("refunded", "charged_back"):
        total = int(pago["monto"])
    else:
        total = min(int(pago["monto"]), int(round(float(datos.get("transaction_amount_refunded") or 0))))
    falta = total - int(pago.get("devuelto") or 0)
    if falta <= 0:
        return pago["estado"]
    _anotar_devolucion(pago, falta)
    if datos.get("status") == "charged_back":
        import scheduler
        scheduler.avisar_profesional(tenant, f"el paciente desconoció un pago de ${pago['monto']:,} en Mercado Pago "
                                             "(contracargo); revisalo en el panel".replace(",", "."))
    db.registrar_actividad("mercadopago", "contracargo" if datos.get("status") == "charged_back"
                           else "devolución hecha desde Mercado Pago", tenant["id"])
    return pago["estado"]


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
        _numerar(tenant, pago)
        import scheduler
        scheduler.avisar_profesional(tenant, f"llegó un pago de ${pago['monto']:,} de una reserva que ya se había liberado; "
                                             "revisalo en el panel para devolverlo o reprogramar".replace(",", "."))
        return "aprobado_tarde"
    for t in vigentes:
        db.actualizar_reserva(tenant["id"], t["id"], {"pago_web": "aprobado", "pago_vence": None, "pago": "pagado",
                                                      "confirmado_at": ahora, "reserva_nueva": True, "reserva_avisada": False})
    db.actualizar_pago(pago["id"], {"estado": "aprobado", "mp_pago_id": mp_pago_id, "resuelto": ahora, "comprobante": None})
    _numerar(tenant, pago)
    _confirmar_al_paciente(tenant, vigentes)
    if mp_pago_id:
        _avisar_pago_mp(tenant, pago, vigentes)
    try:
        mandar_recibo(tenant, pago)
    except Exception:
        logger.exception("No se pudo mandar el recibo por mail")
    return "aprobado"


def _numerar(tenant: dict, pago: dict) -> None:
    try:
        db.asignar_numero_recibo(tenant["id"], pago["id"])
    except Exception:
        logger.exception("No se pudo numerar el recibo del pago %s", pago.get("id"))


def _avisar_pago_mp(tenant: dict, pago: dict, turnos: list[dict]) -> None:
    """WhatsApp al profesional cuando entra un pago por Mercado Pago: monto y
    cuándo es la sesión. Sin el nombre del paciente (está en el panel). No
    sale si el profesional lo apagó (`aviso_cada_pago`)."""
    if tenant.get("aviso_cada_pago") is False:
        return
    import scheduler
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    cuando = ", ".join(scheduler._fmt(t["inicio"], tz) for t in sorted(turnos, key=lambda t: t["inicio"])[:3])
    try:
        scheduler.avisar_profesional(tenant, f"entró un pago de {('$ ' + format(int(pago['monto']), ',')).replace(',', '.')} "
                                             f"por Mercado Pago de la reserva del {cuando}")
    except Exception:
        logger.exception("No se pudo avisar el pago de Mercado Pago")


def mandar_recibo(tenant: dict, pago: dict) -> bool:
    """Si el paciente dejó su email, le llega el link del recibo. Sólo el
    profesional y el link: nada de la sesión ni de la ficha."""
    import mail
    if not pago.get("paciente_email") or not config.DOMINIO_PRINCIPAL or not mail.disponible():
        return False
    import recibos
    link = f"https://{config.DOMINIO_PRINCIPAL}/pago/{pago['token']}/recibo"
    pago_completo = db.pago_por_token(pago["token"]) or pago
    contenido = recibos.pdf(tenant, pago_completo)
    adjuntos = [(f"recibo-{recibos.datos(tenant, pago_completo)['numero']}.pdf", contenido)] if contenido else None
    return mail.enviar(pago["paciente_email"], f"Tu recibo de pago — {tenant['nombre']}",
                       f"Hola. Recibimos tu pago del turno con {tenant['nombre']}.\n\n"
                       + ("Te adjuntamos el recibo en PDF. " if adjuntos else "")
                       + f"También lo podés ver acá: {link}\n\nNo es una factura. Si tenés dudas, escribile directamente a {tenant['nombre']}.",
                       adjuntos)


def mandar_resumen_mensual(tenant: dict, ahora) -> bool:
    """El día 1 (desde las 9), al mail del profesional: lo cobrado por la web
    el mes anterior, con el detalle en PDF. Una sola vez por mes
    (`tenants.resumen_cobros_mes`); si no cobró nada no se le escribe."""
    import mail
    import recibos
    if ahora.hour < 9 or not tenant.get("email") or not mail.disponible() or tenant.get("resumen_cobros_mail") is False:
        return False
    fin = ahora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    inicio = (fin - timedelta(days=1)).replace(day=1)
    clave = inicio.strftime("%Y-%m")
    if tenant.get("resumen_cobros_mes") == clave:
        return False
    lista = db.pagos_resueltos_entre(tenant["id"], inicio.astimezone(timezone.utc), fin.astimezone(timezone.utc))
    if not lista:
        db.guardar_ajustes(tenant["id"], {"resumen_cobros_mes": clave})
        tenant["resumen_cobros_mes"] = clave
        return False
    t = recibos.totales(lista)
    nombre_mes = f"{recibos.MESES[inicio.month - 1]} {inicio.year}"
    contenido = recibos.pdf_resumen(tenant, inicio.year, inicio.month, lista)
    texto = (f"Hola. En {nombre_mes} cobraste por tu página de reservas {recibos.pesos(t['neto'])} "
             f"en {t['cantidad']} pago{'s' if t['cantidad'] != 1 else ''}: Mercado Pago {recibos.pesos(t['mercadopago'])}, "
             f"transferencia {recibos.pesos(t['transferencia'])}"
             + (f", devuelto {recibos.pesos(t['devuelto'])}" if t["devuelto"] else "") + ".\n\n"
             + ("Te adjuntamos el detalle en PDF. " if contenido else "")
             + "También lo ves en el panel, en «Ver los pagos del mes».")
    ok = mail.enviar(tenant["email"], f"Tus cobros de {nombre_mes}", texto,
                     [(f"cobros-{clave}.pdf", contenido)] if contenido else None)
    if ok:
        db.guardar_ajustes(tenant["id"], {"resumen_cobros_mes": clave})
        tenant["resumen_cobros_mes"] = clave
    return ok


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


MOTIVOS_RECHAZO = ["no llegó la transferencia", "el monto no coincide", "el comprobante no se lee",
                   "el comprobante es de otro pago"]


def rechazar(tenant: dict, pago: dict, motivo: str | None = None) -> None:
    """El profesional dice que el comprobante no es válido: se libera y se le
    avisa al paciente (mail si dejó email; WhatsApp si hay plantilla)."""
    telefono = None
    for tid in pago["turnos"]:
        t = db.turno_por_id(tenant["id"], tid)
        telefono = telefono or (t or {}).get("paciente_telefono")
        if t and t["estado"] == "reservado" and t.get("pago_web") in ("esperando", "comprobante"):
            db.cancelar_turno(tenant["id"], tid)
    db.actualizar_pago(pago["id"], {"estado": "rechazado", "resuelto": datetime.now(timezone.utc).isoformat(),
                                    "comprobante": None})
    motivo = motivo if motivo in MOTIVOS_RECHAZO else None
    try:
        avisar_rechazo(tenant, pago, motivo)
    except Exception:
        logger.exception("No se pudo avisar el rechazo del comprobante")
    try:
        avisar_rechazo_whatsapp(tenant, telefono)
    except Exception:
        logger.warning("No se pudo avisar el rechazo por WhatsApp", exc_info=True)


def _link_reservas(tenant: dict) -> str | None:
    return f"https://{config.DOMINIO_PRINCIPAL}/{tenant['slug']}" if tenant.get("slug") and config.DOMINIO_PRINCIPAL else None


def avisar_rechazo_whatsapp(tenant: dict, telefono: str | None) -> bool:
    """Con la plantilla PAGO_RECHAZADO_TEMPLATE (el paciente no escribió:
    fuera de las 24 h hace falta plantilla). Sin plantilla, no se manda."""
    import whatsapp
    numero = whatsapp.numero_de(tenant)
    link = _link_reservas(tenant)
    if not config.PAGO_RECHAZADO_TEMPLATE or not numero or not telefono or not link:
        return False
    return bool(whatsapp.enviar_plantilla(numero, telefono, config.PAGO_RECHAZADO_TEMPLATE,
                                          config.RECORDATORIO_TEMPLATE_LANG, [tenant["nombre"], link]))


def avisar_rechazo(tenant: dict, pago: dict, motivo: str | None = None) -> bool:
    """Si el paciente dejó su email, le avisamos que el comprobante no se
    aceptó (y por qué, si el profesional eligió un motivo) y que el horario
    se liberó, con el link para volver a reservar."""
    import mail
    if not pago.get("paciente_email") or not config.DOMINIO_PRINCIPAL or not mail.disponible():
        return False
    link = _link_reservas(tenant)
    return mail.enviar(pago["paciente_email"], f"Tu reserva con {tenant['nombre']} no se confirmó",
                       f"Hola. {tenant['nombre']} revisó el comprobante de transferencia que subiste y no lo pudo "
                       "validar, así que el horario quedó libre."
                       + (f" Motivo: {motivo}." if motivo else "") + "\n\n"
                       + (f"Si querés, podés volver a reservar acá: {link}\n\n" if link else "")
                       + f"Si ya transferiste, escribile directamente a {tenant['nombre']} para resolverlo.")


def subir_comprobante(tenant: dict, pago: dict, crudo: bytes, email: str | None = None) -> str | None:
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
                                    "comprobante": f"data:{tipo};base64,{base64.b64encode(crudo).decode()}",
                                    "comprobante_at": datetime.now(timezone.utc).isoformat(),
                                    **({"paciente_email": email} if email else {})})
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


# --- Aviso antes de que venza el plazo ---

def avisar_por_vencer(tenant: dict, base: str | None, ahora: datetime | None = None) -> int:
    """Una vez por pago, cuando queda un tercio del plazo (10 minutos con
    Mercado Pago, 40 para subir el comprobante), le manda al paciente el link
    para terminar de pagar. Necesita `PAGO_PENDIENTE_TEMPLATE` (el paciente no
    nos escribió: estamos fuera de la ventana de 24 h)."""
    if not config.PAGO_PENDIENTE_TEMPLATE or not base:
        return 0
    import scheduler
    import whatsapp
    from zoneinfo import ZoneInfo
    ahora = ahora or datetime.now(timezone.utc)
    numero = whatsapp.numero_de(tenant)
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    avisados = 0
    for pago in db.pagos_esperando(tenant["id"]):
        if pago.get("avisado_at"):
            continue
        vence = datetime.fromisoformat(str(pago["vence"]))
        plazo = vence - datetime.fromisoformat(str(pago["creado"]))
        falta = vence - ahora
        if not timedelta(minutes=2) < falta <= plazo / 3:
            continue
        turnos = sorted((t for t in (db.turno_por_id(tenant["id"], tid) for tid in pago["turnos"])
                         if t and t["estado"] == "reservado" and t.get("pago_web") == "esperando"),
                        key=lambda t: t["inicio"])
        if not turnos or not numero:
            continue
        db.actualizar_pago(pago["id"], {"avisado_at": ahora.isoformat()})   # antes de mandar: una sola vez
        try:
            whatsapp.enviar_plantilla(numero, turnos[0]["paciente_telefono"], config.PAGO_PENDIENTE_TEMPLATE,
                                      config.RECORDATORIO_TEMPLATE_LANG,
                                      [tenant["nombre"], scheduler.cuando_de(turnos[0], tenant, tz),
                                       f"{base}/pago/{pago['token']}"])
            avisados += 1
        except Exception:
            logger.warning("No se pudo avisar el pago por vencer %s", pago["id"], exc_info=True)
    return avisados


# --- Devoluciones ---

def devolvible(pago: dict) -> int:
    """Cuánto queda por devolver de un pago cobrado."""
    if pago.get("estado") not in ("aprobado", "aprobado_tarde"):
        return 0
    return max(0, int(pago["monto"]) - int(pago.get("devuelto") or 0))


def puede_devolver_mp(pago: dict) -> bool:
    return pago.get("metodo") == "mercadopago" and bool(pago.get("mp_pago_id")) and devolvible(pago) > 0


def devolver(tenant: dict, pago: dict, monto: int | None = None) -> str | None:
    """Reintegro por Mercado Pago (todo lo que queda, o `monto`). Devuelve
    None si salió bien o el motivo."""
    queda = devolvible(pago)
    monto = min(int(monto or queda), queda)
    if not puede_devolver_mp(pago) or monto <= 0:
        return "Este pago no se puede devolver por Mercado Pago."
    token = _token(tenant)
    if not token:
        return "Tu Mercado Pago no está conectado: conectalo para devolver."
    try:
        r = requests.post(f"{API}/v1/payments/{pago['mp_pago_id']}/refunds", json={"amount": float(monto)},
                          headers={"Authorization": f"Bearer {token}",
                                   # La misma devolución dos veces (doble clic) no se duplica.
                                   "X-Idempotency-Key": f"{pago['id']}-{int(pago.get('devuelto') or 0) + monto}"},
                          timeout=20)
    except requests.RequestException:
        return "No se pudo conectar con Mercado Pago. Probá de nuevo."
    if r.status_code >= 300:
        logger.warning("Mercado Pago no hizo la devolución (%s): %s", r.status_code, r.text[:300])
        if r.status_code in (401, 403):
            return ("Mercado Pago no permite devolver con esta conexión. Devolvelo desde tu cuenta de Mercado Pago "
                    "(se anota solo acá) o volvé a conectarla.")
        return "Mercado Pago no aceptó la devolución (puede que no tengas saldo disponible). Probá más tarde o devolvelo desde tu cuenta."
    _anotar_devolucion(pago, monto)
    return None


def anotar_devuelto(pago: dict) -> None:
    """El profesional devolvió por fuera (transferencia) lo que debía."""
    _anotar_devolucion(pago, int(pago.get("a_devolver") or 0) or devolvible(pago), a_mano=True)


def _anotar_devolucion(pago: dict, monto: int, a_mano: bool = False) -> None:
    devuelto = int(pago.get("devuelto") or 0) + monto
    campos = {"devuelto": devuelto, "a_devolver": max(0, int(pago.get("a_devolver") or 0) - monto),
              "devuelto_at": datetime.now(timezone.utc).isoformat()}
    if devuelto >= int(pago["monto"]):
        campos["estado"] = "devuelto"
        # Las sesiones que siguen reservadas con este pago dejan de figurar pagas.
        for tid in pago.get("turnos") or []:
            t = db.turno_por_id(pago["tenant_id"], tid)
            if t and t["estado"] == "reservado" and t.get("pago_id") == pago["id"]:
                db.actualizar_reserva(pago["tenant_id"], tid, {"pago": "pendiente", "pago_web": "devuelto"})
    elif pago.get("estado") == "aprobado_tarde":
        campos["estado"] = "aprobado"
    db.actualizar_pago(pago["id"], campos)
    pago.update(campos)


def politica(tenant: dict) -> str | None:
    """La política de devolución en una frase, para el paciente. None si no
    se cobra por la web."""
    if not activo(tenant):
        return None
    modo = tenant.get("devolucion_modo") or "todo"
    horas = tenant.get("devolucion_horas") if tenant.get("devolucion_horas") is not None else 24
    if modo == "nada":
        return "Si cancelás, lo pagado no se devuelve."
    que = "te devolvemos todo" if modo == "todo" else "te devolvemos todo menos la seña"
    if (tenant.get("cobro_modo") or "total") == "sena" and modo == "menos_sena":
        return "Si cancelás, la seña no se devuelve."
    cuando = f"con {horas} horas o más de anticipación" if horas else "antes de que empiece el turno"
    return f"Si cancelás {cuando}, {que}." + (" Después, no se devuelve." if horas else "")


def cuanto_devolver(tenant: dict, turno: dict, ahora: datetime | None = None) -> tuple[dict | None, int]:
    """Si el paciente cancela `turno` ahora: (el pago que lo cubre, cuánto le
    corresponde según la política). Una serie se reparte en partes iguales."""
    if not turno.get("pago_id") or turno.get("pago_web") != "aprobado":
        return None, 0
    pago = db.pago_de(tenant["id"], turno["pago_id"])
    if not pago or devolvible(pago) <= 0:
        return pago, 0
    ahora = ahora or datetime.now(timezone.utc)
    modo = tenant.get("devolucion_modo") or "todo"
    horas = tenant.get("devolucion_horas") if tenant.get("devolucion_horas") is not None else 24
    if modo == "nada" or datetime.fromisoformat(str(turno["inicio"])) - ahora < timedelta(hours=horas):
        return pago, 0
    parte = int(pago["monto"]) // max(1, len(pago["turnos"]))
    if modo == "menos_sena":
        if (tenant.get("cobro_modo") or "total") == "sena":
            return pago, 0
        parte -= min(parte, int(tenant.get("sena_monto") or 0))
    return pago, max(0, min(parte, devolvible(pago)))


def al_cancelar(tenant: dict, turno: dict, quien: str = "paciente") -> tuple[int, bool]:
    """Después de cancelar una sesión pagada por la web (`turno` es la fila
    de antes de liberarla). Con el paciente se aplica la política; si cancela
    el profesional, se devuelve su parte entera. Con Mercado Pago se devuelve
    sola; si no se puede (o fue transferencia) queda para devolver a mano.
    Devuelve (monto, si ya se devolvió)."""
    if quien == "profesional":
        if not turno.get("pago_id") or turno.get("pago_web") != "aprobado":
            return 0, False
        pago = db.pago_de(tenant["id"], turno["pago_id"])
        monto = min(int(pago["monto"]) // max(1, len(pago["turnos"])), devolvible(pago)) if pago else 0
    else:
        pago, monto = cuanto_devolver(tenant, turno)
    if not pago or monto <= 0:
        return 0, False
    import scheduler
    texto = f"${monto:,}".replace(",", ".")
    if quien == "profesional":
        # Lo decide el profesional desde el panel: queda en «Pagos para revisar».
        db.actualizar_pago(pago["id"], {"a_devolver": int(pago.get("a_devolver") or 0) + monto})
        return monto, False
    if puede_devolver_mp(pago) and devolver(tenant, pago, monto) is None:
        scheduler.avisar_profesional(tenant, f"se le devolvieron {texto} por Mercado Pago a un paciente que canceló, según tu política")
        return monto, True
    db.actualizar_pago(pago["id"], {"a_devolver": int(pago.get("a_devolver") or 0) + monto})
    scheduler.avisar_profesional(tenant, f"un paciente canceló una sesión pagada: según tu política le corresponden {texto}; "
                                         "devolvéselos y marcalo en el panel")
    return monto, False
