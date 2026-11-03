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


def destino(to: str) -> str:
    """El número tal como lo acepta la API de Meta para mandar.

    Los celulares argentinos llegan con el 9 (5491122223333: así es el wa_id
    del webhook y así lo cargan los pacientes), pero la API los quiere SIN el 9
    (541122223333). Con el 9, el número de prueba de Meta contesta 131030 («no
    está en la lista de autorizados») aunque esté autorizado. Sin el 9, Meta lo
    entrega igual al mismo WhatsApp."""
    digitos = "".join(ch for ch in to or "" if ch.isdigit())
    if digitos.startswith("549") and len(digitos) == 13:
        return "54" + digitos[3:]
    return digitos or to


def permitido(to: str) -> bool:
    """Con WHATSAPP_SOLO_A cargada, sólo esos números reciben mensajes."""
    if not config.WHATSAPP_SOLO_A:
        return True
    destino = _ultimos10(to)
    return bool(destino) and any(_ultimos10(n) == destino for n in config.WHATSAPP_SOLO_A)


def enviar_texto(phone_number_id: str, to: str, texto: str) -> bool:
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
        "to": destino(to),
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
    ok, _ = enviar_plantilla_detalle(phone_number_id, to, plantilla, lang, params)
    return ok


def enviar_plantilla_detalle(phone_number_id: str, to: str, plantilla: str,
                             lang: str, params: list[str]) -> tuple[bool, str]:
    """Igual que enviar_plantilla, pero devuelve también qué dijo Meta (para
    la prueba de envío de administración)."""
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
    payload = {"messaging_product": "whatsapp", "to": destino(to), "type": "template", "template": template}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
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
            "Resumen al profesional": config.AVISO_TEMPLATE, "Recordatorio de pago": config.RECORDATORIO_PAGO_TEMPLATE,
            "Lista de espera": config.LISTA_ESPERA_TEMPLATE}
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
        pasos.append(paso("Número de la plataforma", False, "Falta PLATAFORMA_PHONE_NUMBER_ID.",
                          "Meta for Developers → WhatsApp → Configuración de la API → «Phone Number ID»."))
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
        pasos.append(paso("Cuenta de WhatsApp", None, "Sin META_WABA_ID: no se pueden revisar la suscripción ni las plantillas.",
                          "Meta for Developers → WhatsApp → Configuración de la API → «Identificador de la cuenta»."))
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
                              "" if suscripta else f"railway run sh -c 'curl -s -X POST \"https://graph.facebook.com/{config.META_GRAPH_VERSION}/{waba}/subscribed_apps\" -H \"Authorization: Bearer $META_ACCESS_TOKEN\"'"))
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
                          "Para el uso real con pacientes, vaciá WHATSAPP_SOLO_A en Railway."))
    else:
        pasos.append(paso("Freno de envíos", None, "Apagado: los mensajes salen a todos los pacientes.",
                          "Para probar sin riesgo con datos de demo, cargá WHATSAPP_SOLO_A en Railway."))
    return pasos


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
