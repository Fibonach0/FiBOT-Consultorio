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


def _ultimos10(numero: str) -> str:
    # Los últimos 10 dígitos: así 5491128524957, +54 9 11 2852-4957 y
    # 1128524957 son el mismo teléfono (el 9 y el 15 argentinos varían).
    return "".join(ch for ch in numero or "" if ch.isdigit())[-10:]


# Características argentinas de 2 y 3 dígitos; el resto tiene 4 (ENACOM).
_CARACTERISTICAS_3 = {
    "220", "221", "223", "230", "236", "237", "249", "260", "261", "263", "264", "266", "280", "291",
    "294", "297", "299", "336", "341", "342", "343", "345", "348", "351", "353", "358", "362", "364",
    "370", "376", "379", "380", "381", "383", "385", "387", "388",
}


def con_15(to: str) -> str | None:
    """5491128524957 → 54111528524957: el formato viejo de celular argentino
    (sin el 9, con el 15 después de la característica). Es como guarda Meta
    los destinatarios autorizados del número de prueba: con el 9 contesta
    131030 aunque el número esté en la lista. None si no es un celular
    argentino con el 9."""
    d = "".join(ch for ch in to or "" if ch.isdigit())
    if not (d.startswith("549") and len(d) == 13):
        return None
    nacional = d[3:]
    largo = 2 if nacional.startswith("11") else 3 if nacional[:3] in _CARACTERISTICAS_3 else 4
    return "54" + nacional[:largo] + "15" + nacional[largo:]


def permitido(to: str) -> bool:
    """Con WHATSAPP_SOLO_A cargada, sólo esos números reciben mensajes."""
    if not config.WHATSAPP_SOLO_A:
        return True
    destino = _ultimos10(to)
    return bool(destino) and any(_ultimos10(n) == destino for n in config.WHATSAPP_SOLO_A)


def _sin_numero(to: str) -> bool:
    """Paciente agendado sin WhatsApp (ver db.SIN_WHATSAPP): no hay a quién
    mandarle. Cuenta como resuelto, así el scheduler no reintenta."""
    if to and to.isdigit():
        return False
    logger.info("Envío salteado: el paciente no tiene WhatsApp cargado")
    return True


def enviar_texto(phone_number_id: str, to: str, texto: str) -> bool:
    if _sin_numero(to):
        return True
    if not permitido(to):
        # True a propósito: el envío quedó «resuelto» (frenado) y el scheduler
        # no lo reintenta cada 10 minutos.
        logger.info("Envío frenado por WHATSAPP_SOLO_A (destino ...%s)", _ultimos10(to)[-4:])
        return True
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
        r = _mandar(url, headers, payload)
        if r.status_code >= 300:
            logger.error("Meta rechazó el envío (%s): %s", r.status_code, r.text)
            return False
        return True
    except requests.RequestException:
        logger.exception("Error de red mandando WhatsApp")
        return False


def _codigo_de_error(r) -> int | None:
    try:
        return r.json().get("error", {}).get("code")
    except (ValueError, AttributeError):
        return None


def _mandar(url: str, headers: dict, payload: dict):
    """POST a Meta. Si contesta 131030 («no está en la lista de autorizados»)
    y es un celular argentino con el 9, reintenta UNA vez con el formato del 15
    (ver con_15). Con números reales no pasa: el 9 anda y no hay reintento."""
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    alternativo = con_15(payload["to"]) if r.status_code >= 300 and _codigo_de_error(r) == 131030 else None
    if alternativo:
        r = requests.post(url, headers=headers, json={**payload, "to": alternativo}, timeout=15)
    return r


def numero_de(tenant: dict) -> str:
    """Desde qué número sale un mensaje de este tenant.

    El suyo si lo tiene; el de la plataforma si todavía no lo dio de alta.
    Está acá y no en db.py porque es una decisión de envío, no un dato: db.py
    devuelve el tenant tal cual está guardado, y quien manda decide con qué
    número sale."""
    return (tenant.get("whatsapp_phone_number_id")
            or config.PLATAFORMA_PHONE_NUMBER_ID)


def enviar_plantilla(phone_number_id: str, to: str, plantilla: str,
                     lang: str, params: list[str], boton_url: str | None = None) -> bool:
    """Manda un mensaje de PLANTILLA aprobada.

    Es lo único que Meta deja mandar fuera de la ventana de 24 h desde el
    último mensaje del paciente — y un recordatorio del día antes cae SIEMPRE
    fuera de esa ventana, porque el paciente que reservó por la página web
    nunca le escribió al bot. Con `enviar_texto` esto devuelve error 131047 y
    el recordatorio no llega nunca.

    Los `params` van en el mismo orden que los {{1}}, {{2}}… de la plantilla.
    `boton_url` es el final del link del primer botón «URL dinámica» (el
    {{1}} del botón), si la plantilla lo tiene.
    """
    ok, _ = enviar_plantilla_detalle(phone_number_id, to, plantilla, lang, params, boton_url)
    return ok


def enviar_plantilla_detalle(phone_number_id: str, to: str, plantilla: str,
                             lang: str, params: list[str], boton_url: str | None = None) -> tuple[bool, str]:
    """Igual que enviar_plantilla, pero devuelve también qué dijo Meta (para
    la prueba de envío de administración)."""
    if _sin_numero(to):
        return True, "sin WhatsApp"
    if not permitido(to):
        logger.info("Plantilla '%s' frenada por WHATSAPP_SOLO_A (destino ...%s)", plantilla, _ultimos10(to)[-4:])
        return True, "frenado"
    url = f"https://graph.facebook.com/{config.META_GRAPH_VERSION}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {config.META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    template = {"name": plantilla, "language": {"code": lang}}
    if params:
        # Sin parámetros (ej. hello_world) no va «components»: Meta rechaza
        # un body con la lista de parámetros vacía.
        template["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": p} for p in params],
        }]
    if boton_url:
        template.setdefault("components", []).append({
            "type": "button", "sub_type": "url", "index": "0",
            "parameters": [{"type": "text", "text": boton_url}]})
    payload = {"messaging_product": "whatsapp", "to": to, "type": "template", "template": template}
    try:
        r = _mandar(url, headers, payload)
        if r.status_code >= 300:
            logger.error("Meta rechazó la plantilla '%s' (%s): %s", plantilla, r.status_code, r.text)
            return False, _error_de_meta(r)
        return True, "aceptado"
    except requests.RequestException:
        logger.exception("Error de red mandando la plantilla '%s'", plantilla)
        return False, "No se pudo conectar con Meta."


def _error_de_meta(r) -> str:
    try:
        e = r.json().get("error", {})
        detalle = (e.get("error_data") or {}).get("details") or e.get("message") or r.text
        return f"{detalle} (código {e.get('code')})"
    except ValueError:
        return f"HTTP {r.status_code}"


def consultar(ruta: str, params: dict | None = None) -> tuple[bool, dict | str]:
    """GET a la Graph API con el token de la plataforma. Devuelve (ok, datos
    o el error de Meta en texto). Nunca devuelve ni registra el token."""
    url = f"https://graph.facebook.com/{config.META_GRAPH_VERSION}/{ruta}"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {config.META_ACCESS_TOKEN}"},
                         params=params or {}, timeout=8)
    except requests.RequestException:
        return False, "No se pudo conectar con Meta."
    if r.status_code >= 300:
        return False, _error_de_meta(r)
    try:
        return True, r.json()
    except ValueError:
        return False, "Meta respondió algo que no es JSON."


def plantillas_configuradas() -> dict[str, str]:
    """Para qué se usa cada plantilla → su nombre (sólo las cargadas)."""
    usos = {"Recordatorio": config.RECORDATORIO_TEMPLATE, "Reprogramado": config.REPROGRAMADO_TEMPLATE,
            "Resumen al profesional": config.AVISO_TEMPLATE, "Resumen de la mañana con botones": config.RESUMEN_TEMPLATE, "Recordatorio de pago": config.RECORDATORIO_PAGO_TEMPLATE,
            "Lista de espera": config.LISTA_ESPERA_TEMPLATE,
            "Confirmación de reserva web": config.CONFIRMACION_TEMPLATE,
            "Pago por vencer": config.PAGO_PENDIENTE_TEMPLATE,
            "Comprobante rechazado": config.PAGO_RECHAZADO_TEMPLATE}
    return {uso: nombre for uso, nombre in usos.items() if nombre}


def diagnostico(ultimo_webhook: str | None = None, ultimo_mensaje: str | None = None) -> list[dict]:
    """Los pasos para que WhatsApp funcione, cada uno con ok True/False/None
    (None = informativo o no se pudo revisar). Consulta a Meta en vivo: se usa
    sólo desde la página de WhatsApp de administración, no en cada carga."""
    paso = lambda nombre, ok, detalle, como="": {"nombre": nombre, "ok": ok, "detalle": detalle, "como": como}
    pasos = []
    real = bool(config.META_ACCESS_TOKEN) and config.META_ACCESS_TOKEN != "fake"
    pasos.append(paso("Credenciales de Meta", real,
                      "Token, clave de la app y token del webhook cargados." if real else "Falta META_ACCESS_TOKEN real.",
                      "Railway → META_ACCESS_TOKEN (token del usuario del sistema, caducidad «Nunca»)."))

    numero_id = config.PLATAFORMA_PHONE_NUMBER_ID
    if not numero_id:
        pasos.append(paso("Número de la plataforma", False, "Falta el Phone Number ID.",
                          "Cargalo abajo, en Ajustes. Está en Meta for Developers → WhatsApp → Configuración de la API."))
    elif real:
        ok, d = consultar(numero_id, {"fields": "display_phone_number,verified_name,quality_rating,code_verification_status"})
        if ok:
            calidad = {"GREEN": "alta", "YELLOW": "media", "RED": "baja"}.get(d.get("quality_rating"), d.get("quality_rating") or "sin dato")
            pasos.append(paso("Número de la plataforma", True,
                              f"{d.get('display_phone_number')} · «{d.get('verified_name')}» · calidad {calidad}"
                              + (f" · verificación {d['code_verification_status'].lower()}" if d.get("code_verification_status") else "")))
        else:
            pasos.append(paso("Número de la plataforma", False, f"Meta no lo encuentra con este token: {d}",
                              "Revisá el Phone Number ID y que la cuenta de WhatsApp esté asignada al usuario del sistema."))

    waba = config.META_WABA_ID
    if not waba:
        pasos.append(paso("Cuenta de WhatsApp", None, "Sin el identificador de la cuenta: no se pueden revisar la suscripción ni las plantillas.",
                          "Cargalo abajo, en Ajustes. Está en Meta for Developers → WhatsApp → Configuración de la API."))
    elif real:
        ok, d = consultar(f"{waba}/subscribed_apps")
        if ok:
            datos = [a.get("whatsapp_business_api_data", {}) for a in d.get("data", [])]
            apps = [a.get("name") or "una app" for a in datos]
            # Hay que encontrar NUESTRA app: la cuenta de prueba trae suscripta
            # una app interna de Meta (la del visor de webhooks) y con eso la
            # lista no está vacía, pero los mensajes no llegan al servidor.
            ok_app, propia = consultar("app", {"fields": "id,name"})
            if ok_app:
                suscripta = any(a.get("id") == propia.get("id") for a in datos)
                detalle = (f"«{propia.get('name')}» está suscripta." if suscripta else
                           f"«{propia.get('name')}» NO está suscripta: los mensajes que escriben los pacientes no llegan."
                           + (f" (Suscriptas: {', '.join(apps)}.)" if apps else ""))
            else:
                suscripta = None if apps else False
                detalle = f"Suscriptas: {', '.join(apps)}. Fijate que esté la de FiBOT." if apps else "Ninguna app suscripta: los mensajes que escriben los pacientes no llegan."
            pasos.append(paso("App suscripta a la cuenta", suscripta, detalle,
                              "" if suscripta else "Tocá «Suscribir la app» acá abajo."))
        else:
            pasos.append(paso("App suscripta a la cuenta", False, f"No se pudo revisar: {d}",
                              "Generá de nuevo el token del usuario del sistema DESPUÉS de asignarle la cuenta."))
        configuradas = plantillas_configuradas()
        ok, d = consultar(f"{waba}/message_templates", {"fields": "name,status,language,category", "limit": 200})
        if not configuradas:
            pasos.append(paso("Plantillas", False, "No hay ninguna cargada en Railway: los recordatorios salen como texto libre y Meta los rechaza.",
                              "Railway → RECORDATORIO_TEMPLATE y las demás."))
        elif ok:
            en_meta = {}
            for t in d.get("data", []):
                if t.get("language") == config.RECORDATORIO_TEMPLATE_LANG or t.get("name") not in en_meta:
                    en_meta[t.get("name")] = t
            estados = {"APPROVED": "aprobada", "PENDING": "en revisión", "REJECTED": "rechazada",
                       "PAUSED": "pausada", "DISABLED": "deshabilitada"}
            partes, todas = [], True
            for uso, nombre in configuradas.items():
                t = en_meta.get(nombre)
                estado = estados.get(t["status"], t["status"].lower()) if t else "no existe en Meta"
                todas = todas and bool(t) and t["status"] == "APPROVED"
                partes.append(f"{uso} ({nombre}): {estado}")
            faltan = [n for n in configuradas.values() if n not in en_meta]
            rechazadas = [n for n in configuradas.values() if n in en_meta and en_meta[n]["status"] in ("REJECTED", "PAUSED", "DISABLED")]
            if todas:
                como = ""
            elif faltan or rechazadas:
                como = ("WhatsApp Manager → Plantillas: " + (f"creá {', '.join(faltan)} con el mismo nombre, en es_AR, categoría Utilidad. " if faltan else "")
                        + (f"Revisá por qué Meta frenó {', '.join(rechazadas)} y corregila." if rechazadas else ""))
            else:
                como = "Sólo falta que Meta las apruebe: suele tardar de minutos a un día. Recargá más tarde."
            pasos.append(paso("Plantillas", todas, " · ".join(partes) + ".", como.strip()))
        else:
            pasos.append(paso("Plantillas", False, f"No se pudo revisar: {d}"))

    pasos.append(paso("Mensajes que llegan", bool(ultimo_mensaje) if real else None,
                      (f"Último mensaje de un paciente: {ultimo_mensaje}." if ultimo_mensaje else "Todavía no llegó ningún mensaje de un paciente.")
                      + (f" Último aviso de Meta al webhook: {ultimo_webhook}." if ultimo_webhook else " Meta nunca llamó al webhook."),
                      "" if ultimo_mensaje else "Mandale «hola» al número desde tu WhatsApp y recargá esta página."))
    if config.WHATSAPP_SOLO_A:
        finales = ", ".join("…" + _ultimos10(n)[-4:] for n in config.WHATSAPP_SOLO_A)
        pasos.append(paso("Freno de envíos", None, f"Activo: sólo salen mensajes a los teléfonos terminados en {finales}.",
                          "Para el uso real con pacientes, vacialo abajo y en Railway."))
    else:
        pasos.append(paso("Freno de envíos", None, "Apagado: los mensajes salen a todos los pacientes.",
                          "Para probar sin riesgo con datos de demo, cargalo abajo, en Ajustes."))
    return pasos


# --- Ajustes cargados desde administración (pisan a los de Railway) ---

# Lo que vino de Railway al arrancar: si en administración se vacía un ajuste,
# se vuelve a esto.
_DE_RAILWAY = {
    "phone_number_id": config.PLATAFORMA_PHONE_NUMBER_ID,
    "waba_id": config.META_WABA_ID,
    "solo_a": ",".join(config.WHATSAPP_SOLO_A),
    "aviso_admin": "",
}
AJUSTES = {"phone_number_id": "PLATAFORMA_PHONE_NUMBER_ID", "waba_id": "META_WABA_ID",
           "solo_a": "WHATSAPP_SOLO_A", "aviso_admin": "teléfono para avisos de administración"}
_origen: dict[str, str] = {}
aviso_admin = ""


def aplicar_ajustes(guardados: dict[str, str]) -> None:
    """Pone en `config` lo guardado en administración, o lo de Railway si el
    ajuste está vacío. Hay un solo worker (ver CLAUDE.md), así que cambiar
    `config` en memoria alcanza para todo el proceso."""
    global aviso_admin
    valores = {}
    for clave in AJUSTES:
        propio = (guardados.get(clave) or "").strip()
        valores[clave] = propio or _DE_RAILWAY[clave]
        _origen[clave] = "administración" if propio else ("Railway" if _DE_RAILWAY[clave] else "")
    config.PLATAFORMA_PHONE_NUMBER_ID = valores["phone_number_id"]
    config.META_WABA_ID = valores["waba_id"]
    config.WHATSAPP_SOLO_A = [n.strip() for n in valores["solo_a"].split(",") if n.strip()]
    aviso_admin = valores["aviso_admin"]


def origen(clave: str) -> str:
    return _origen.get(clave) or ("Railway" if _DE_RAILWAY.get(clave) else "")


def cargar_ajustes() -> None:
    """Al arrancar. Si la base no responde, quedan los de Railway."""
    import db
    try:
        aplicar_ajustes(db.ajustes_plataforma())
    except Exception as e:
        logger.warning("No se pudieron leer los ajustes de WhatsApp (quedan los de Railway): %s", e)


def suscribir_app() -> tuple[bool, str]:
    """Suscribe la app del token a la cuenta de WhatsApp: sin esto Meta no
    manda al webhook los mensajes que escriben los pacientes."""
    if not config.META_WABA_ID:
        return False, "Falta el identificador de la cuenta de WhatsApp."
    url = f"https://graph.facebook.com/{config.META_GRAPH_VERSION}/{config.META_WABA_ID}/subscribed_apps"
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {config.META_ACCESS_TOKEN}"}, timeout=15)
    except requests.RequestException:
        return False, "No se pudo conectar con Meta."
    if r.status_code >= 300:
        return False, _error_de_meta(r)
    return True, "Listo: Meta confirmó la suscripción."


ESTADOS_PLANTILLA = {"APPROVED": "aprobada", "REJECTED": "rechazada", "PENDING": "en revisión",
                     "PAUSED": "pausada", "DISABLED": "deshabilitada", "FLAGGED": "marcada por baja calidad",
                     "IN_APPEAL": "en apelación", "PENDING_DELETION": "por borrarse", "REINSTATED": "rehabilitada"}


def aviso_de_plantilla(campo: str, valor: dict) -> dict | None:
    """Lo que importa de un aviso de Meta sobre una plantilla, o None si no es
    uno. `campo` es el `field` del webhook."""
    nombre = valor.get("message_template_name")
    if not nombre:
        return None
    if campo == "message_template_status_update":
        estado = ESTADOS_PLANTILLA.get(valor.get("event"), (valor.get("event") or "").lower())
        motivo = valor.get("reason")
        motivo = None if not motivo or motivo == "NONE" else motivo.replace("_", " ").lower()
        return {"nombre": nombre, "idioma": valor.get("message_template_language") or "",
                "estado": estado, "motivo": motivo}
    if campo == "template_category_update":
        nueva = (valor.get("new_category") or "").lower()
        return {"nombre": nombre, "idioma": valor.get("message_template_language") or "",
                "estado": f"pasó a {nueva}" if nueva else "cambió de categoría",
                "motivo": f"antes era {(valor.get('previous_category') or '').lower()}" if valor.get("previous_category") else None}
    return None


def avisar_admin(texto: str) -> bool:
    """WhatsApp al teléfono de avisos de administración, si está cargado."""
    if not aviso_admin or not config.PLATAFORMA_PHONE_NUMBER_ID:
        return False
    if config.AVISO_TEMPLATE:
        return enviar_plantilla(config.PLATAFORMA_PHONE_NUMBER_ID, aviso_admin, config.AVISO_TEMPLATE,
                                config.RECORDATORIO_TEMPLATE_LANG, [texto])
    return enviar_texto(config.PLATAFORMA_PHONE_NUMBER_ID, aviso_admin, f"FiBOT Consultorio: {texto}.")


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


def marcar_leido(phone_number_id: str, message_id: str | None) -> bool:
    """Los dos tildes azules apenas llega el mensaje. No es un mensaje al
    paciente (no pasa por el freno) y si falla no importa."""
    if not phone_number_id or not message_id:
        return False
    url = f"https://graph.facebook.com/{config.META_GRAPH_VERSION}/{phone_number_id}/messages"
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {config.META_ACCESS_TOKEN}"},
                          json={"messaging_product": "whatsapp", "status": "read", "message_id": message_id}, timeout=10)
        return r.status_code < 300
    except requests.RequestException:
        return False


def bajar_media(media_id: str, maximo: int) -> bytes | None:
    """El archivo que mandó el paciente (foto o PDF), para usarlo como
    comprobante. None si no se pudo o pesa más de `maximo`."""
    headers = {"Authorization": f"Bearer {config.META_ACCESS_TOKEN}"}
    try:
        r = requests.get(f"https://graph.facebook.com/{config.META_GRAPH_VERSION}/{media_id}", headers=headers, timeout=15)
        if r.status_code >= 300:
            logger.warning("Meta no dio el archivo (%s)", r.status_code)
            return None
        datos = r.json()
        if int(datos.get("file_size") or 0) > maximo or not datos.get("url"):
            return None
        r = requests.get(datos["url"], headers=headers, timeout=30)
        if r.status_code >= 300 or len(r.content) > maximo:
            return None
        return r.content
    except (requests.RequestException, ValueError):
        logger.warning("No se pudo bajar el archivo de WhatsApp", exc_info=True)
        return None


def _interactivo(phone_number_id: str, to: str, interactivo: dict) -> bool:
    """Lista o botones (sólo dentro de las 24 h: es una respuesta). False si
    Meta no lo aceptó: quien llama manda el texto de siempre."""
    if _sin_numero(to):
        return True
    if not permitido(to):
        logger.info("Envío frenado por WHATSAPP_SOLO_A (destino ...%s)", _ultimos10(to)[-4:])
        return True
    url = f"https://graph.facebook.com/{config.META_GRAPH_VERSION}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {config.META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    try:
        r = _mandar(url, headers, {"messaging_product": "whatsapp", "to": to, "type": "interactive", "interactive": interactivo})
    except requests.RequestException:
        logger.exception("Error de red mandando un mensaje interactivo")
        return False
    if r.status_code >= 300:
        logger.error("Meta rechazó el interactivo (%s): %s", r.status_code, r.text)
        return False
    return True


def enviar_lista(phone_number_id: str, to: str, cuerpo: str, boton: str, filas: list[dict], titulo: str = "Horarios") -> bool:
    """Lista desplegable: hasta 10 filas {id, title (24), description (72)}."""
    filas = [{"id": f["id"][:200], "title": f["title"][:24], **({"description": f["description"][:72]} if f.get("description") else {})}
             for f in filas[:10]]
    return _interactivo(phone_number_id, to, {"type": "list", "body": {"text": cuerpo[:1024]},
                                              "action": {"button": boton[:20], "sections": [{"title": titulo[:24], "rows": filas}]}})


def enviar_botones(phone_number_id: str, to: str, cuerpo: str, botones: list[tuple[str, str]]) -> bool:
    """Hasta 3 botones (id, texto de 20)."""
    return _interactivo(phone_number_id, to, {"type": "button", "body": {"text": cuerpo[:1024]}, "action": {"buttons": [
        {"type": "reply", "reply": {"id": i[:256], "title": t[:20]}} for i, t in botones[:3]]}})


def enviar_boton_link(phone_number_id: str, to: str, cuerpo: str, texto: str, url: str) -> bool:
    """Un botón que abre un link (por ejemplo «Pagar»). Sólo dentro de las 24 h."""
    return _interactivo(phone_number_id, to, {"type": "cta_url", "body": {"text": cuerpo[:1024]}, "action": {
        "name": "cta_url", "parameters": {"display_text": texto[:20], "url": url}}})


def enviar_respuesta(phone_number_id: str, to: str, respuesta) -> bool:
    """Lo que devuelve conversation: texto, o texto con una versión interactiva
    (`respuesta.interactivo`). Si la interactiva falla, sale el texto."""
    extra = getattr(respuesta, "interactivo", None)
    if extra:
        if extra["tipo"] == "lista" and enviar_lista(phone_number_id, to, extra["cuerpo"], extra["boton"], extra["filas"],
                                                    extra.get("titulo", "Horarios")):
            return True
        if extra["tipo"] == "botones" and enviar_botones(phone_number_id, to, extra["cuerpo"], extra["botones"]):
            return True
        if extra["tipo"] == "link" and enviar_boton_link(phone_number_id, to, extra["cuerpo"], extra["boton"], extra["url"]):
            return True
    return enviar_texto(phone_number_id, to, str(respuesta))
