"""FiBOT Consultorio — plataforma.

UN solo deploy sirve a TODOS los profesionales (tenants). El webhook de Meta
es uno solo; cada mensaje entrante trae el phone_number_id del número al que
le escribieron, y eso es lo que resuelve a qué tenant pertenece (ver
db.get_tenant_by_phone_number_id). Nada de esto tiene nada de un cliente en
particular — lo particular vive en la tabla `tenants` y se carga con
scripts/crear_tenant.py, nunca hardcodeado acá."""
import base64
import copy
import csv
import functools
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import time
import unicodedata
from datetime import date, datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import bcrypt
from urllib.parse import urlencode

from flask import Flask, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from openpyxl import Workbook

import config
import cancelaciones
import pagos
import asistente_profesional
import conversation
import db
import rubros
import cuestionarios
import directorio
import fluidez
import informe
import mail
import recibos
import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)
import resultados_tests
import tests_sugeridos
from fechas import alerta_de_seguimiento, proxima_vez, sesiones_que_quedan
import scheduler
import seguridad
import whatsapp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

app = Flask(__name__)
# Railway termina TLS en su proxy: sin esto, los links absolutos (el del
# cuestionario, el de reservas en la guía) salen con http://.
# x_for=1: la IP del cliente es la que agrega el proxy de Railway (la última),
# no lo que mande el cliente; la usa el límite de intentos de login.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_for=1)
app.secret_key = config.FLASK_SECRET_KEY
# Secure sólo en Railway: en local el panel corre por http y no habría login.
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RAILWAY_ENVIRONMENT_NAME")),
)

DIAS_LABEL = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MESES_LABEL = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto",
               "Septiembre", "Octubre", "Noviembre", "Diciembre"]


# ---------- salud ----------

@app.get("/health")
def salud():
    return "FiBOT Consultorio activo.", 200


# ---------- portada del sitio (consultorio.fibot.ar) ----------

INICIO_PROFESIONALES = 6


def _directorio() -> list[dict]:
    """Profesionales del directorio público. Si la base no responde, lista vacía:
    las páginas de pacientes salen igual, sin profesionales."""
    try:
        return directorio.visibles(db.tenants_activos())
    except Exception:
        logger.exception("directorio: no se pudieron leer los profesionales")
        return []


@app.get("/")
def inicio():
    """La main para futuros pacientes: qué es, búsqueda, temas, algunos
    profesionales del directorio (los mismos que /turnos) y cómo funciona.
    Con «Para mi hijo/a» cambian los temas y los profesionales que se ven."""
    visibles = _directorio()
    # Primero los reales (las demos al final), después los que pueden tomar
    # turnos y los que tienen foto: es lo que más invita a entrar.
    visibles.sort(key=lambda t: (directorio.es_demo(t), bool(t.get("pausado")), not t.get("foto"), t["nombre"].casefold()))
    de_chicos = [t for t in visibles
                 if any(directorio.trabaja_tema(t, k) for k in directorio.TEMAS_CHICOS + ("infancia",))]
    para_mi = visibles[:INICIO_PROFESIONALES]
    para_hijo = de_chicos[:INICIO_PROFESIONALES] or para_mi
    # Una sola tarjeta por profesional, marcada con en qué vista aparece.
    ids_mi, ids_hijo = {t["id"] for t in para_mi}, {t["id"] for t in para_hijo}
    tarjetas = [dict(t, _para=" ".join(p for p, ids in (("yo", ids_mi), ("hijo", ids_hijo)) if t["id"] in ids))
                for t in visibles if t["id"] in ids_mi | ids_hijo]
    # Quién trabaja cada tema (hasta 3), para el panel que se abre al tocarlo.
    por_tema = {k: [{"nombre": t["nombre"], "url": url_for("publico", slug=t["slug"])}
                    for t in visibles if directorio.trabaja_tema(t, k)][:3]
                for k in directorio.TEMAS}
    return render_template("inicio.html", profesionales=tarjetas,
                           hay_mas=len(visibles) > INICIO_PROFESIONALES, temas=directorio.TEMAS,
                           temas_adultos=directorio.TEMAS_ADULTOS, temas_chicos=directorio.TEMAS_CHICOS,
                           por_tema=por_tema, total=len(visibles), anio=datetime.now().year)


@app.get("/robots.txt")
def robots():
    privadas = ["/agenda", "/admin", "/login", "/logout", "/olvide-la-clave", "/clave-nueva",
                "/agendar/", "/pago/", "/entrega/", "/cuestionario/", "/webhook", "/mercadopago/"]
    texto = "User-agent: *\n" + "".join(f"Disallow: {p}\n" for p in privadas)
    texto += f"\nSitemap: {url_for('sitemap', _external=True)}\n"
    return app.response_class(texto, mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap():
    """Las páginas que tiene sentido que encuentre un buscador: portada,
    directorio, sumate y la página de cada profesional del directorio."""
    urls = [url_for("inicio", _external=True), url_for("turnos_directorio", _external=True),
            url_for("sumate", _external=True), url_for("privacidad", _external=True)]
    urls += [url_for("publico", slug=t["slug"], _external=True) for t in _directorio()]
    from xml.sax.saxutils import escape
    cuerpo = "".join(f"<url><loc>{escape(u)}</loc></url>" for u in urls)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{cuerpo}</urlset>'
    return app.response_class(xml, mimetype="application/xml")


@app.template_filter("perfil_ld")
def perfil_ld(tenant: dict) -> dict:
    """Datos estructurados (schema.org) de la página de un profesional, para
    que los buscadores entiendan quién es, qué trabaja y dónde atiende."""
    url = url_for("publico", slug=tenant["slug"], _external=True)
    datos = {"@context": "https://schema.org", "@type": "Person", "@id": url, "url": url,
             "name": tenant["nombre"],
             "jobTitle": tenant.get("titulo") or (tenant.get("rubro") or "").capitalize() or None,
             "description": tenant.get("descripcion") or None,
             "knowsAbout": list(tenant.get("enfoques") or []) or None}
    if tenant.get("ubicacion"):
        datos["workLocation"] = {"@type": "Place", "name": tenant["ubicacion"]}
    if tenant.get("portada_version"):
        datos["image"] = url_for("publico_portada", slug=tenant["slug"], v=tenant["portada_version"], _external=True)
    return {k: v for k, v in datos.items() if v is not None}


# ---------- sumate: profesionales que quieren entrar ----------

PROFESIONES_SUMATE = ["Psicología", "Psicopedagogía", "Psiquiatría", "Otra"]


@app.route("/sumate", methods=["GET", "POST"])
def sumate():
    """Formulario para profesionales que quieren usar FiBOT Consultorio. Queda
    en `interesados` (lo ve administración) y avisa por WhatsApp. El alta sigue
    siendo a mano: esto sólo junta los datos de contacto."""
    if request.method == "GET":
        # «Ver una página de ejemplo»: alguien del directorio, mejor con foto y sin pausa.
        ejemplo = min(_directorio(), key=lambda t: (bool(t.get("pausado")), not t.get("foto")), default=None)
        return render_template("sumate.html", enviado=False, error=None, datos={}, profesiones=PROFESIONES_SUMATE,
                               ejemplo=ejemplo["slug"] if ejemplo else None)
    f = request.form
    datos = {
        "nombre": " ".join((f.get("nombre") or "").split())[:80],
        "profesion": (f.get("profesion") or "")[:40] if f.get("profesion") in PROFESIONES_SUMATE else None,
        "whatsapp": "".join(ch for ch in f.get("whatsapp") or "" if ch.isdigit())[:20] or None,
        "email": (f.get("email") or "").strip()[:120] or None,
        "zona": " ".join((f.get("zona") or "").split())[:80] or None,
        "mensaje": (f.get("mensaje") or "").strip()[:600] or None,
    }
    if f.get("web"):  # campo trampa, invisible para personas: lo llenan los robots
        return render_template("sumate.html", enviado=True, error=None, datos={}, profesiones=PROFESIONES_SUMATE)
    error = None
    if not datos["nombre"]:
        error = "Poné tu nombre."
    elif not datos["whatsapp"] and not datos["email"]:
        error = "Dejanos un WhatsApp o un mail para contactarte."
    elif datos["whatsapp"] and len(datos["whatsapp"]) < 10:
        error = "El WhatsApp tiene que tener la característica (ej. 11 2345 6789)."
    elif datos["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", datos["email"]):
        error = "Revisá el mail."
    if error:
        return render_template("sumate.html", enviado=False, error=error, datos=datos, profesiones=PROFESIONES_SUMATE), 400
    claves = [f"sumate:{request.remote_addr or 'desconocida'}"]
    bloqueo = _bloqueo_login(claves)
    if bloqueo:
        return render_template("sumate.html", enviado=False, error=bloqueo, datos=datos, profesiones=PROFESIONES_SUMATE), 429
    _registrar_fallo(claves)  # cada envío cuenta: 5 por IP cada 15 minutos
    try:
        db.crear_interesado(datos)
        guardado = True
    except Exception:  # p. ej. falta aplicar migrations/060: que el contacto no se pierda
        logger.exception("sumate: no se pudo guardar el interesado")
        guardado = False
    contacto = "" if guardado else f" · {datos['whatsapp'] or ''} {datos['email'] or ''}".rstrip()
    try:
        avisado = whatsapp.avisar_admin(f"quiere sumarse {datos['nombre']} ({datos['profesion'] or 'profesional'})"
                                        + (". Está en administración" if guardado else contacto))
    except Exception:
        logger.warning("sumate: no se pudo avisar a administración", exc_info=True)
        avisado = False
    if not guardado and not avisado:
        return render_template("sumate.html", enviado=False, error="No se pudo enviar. Probá de nuevo en un rato.",
                               datos=datos, profesiones=PROFESIONES_SUMATE), 503
    try:
        db.registrar_actividad("sitio", f"pidió sumarse: {datos['nombre']} ({datos['profesion'] or 'sin profesión'})")
    except Exception:
        logger.warning("sumate: no se pudo registrar la actividad", exc_info=True)
    return render_template("sumate.html", enviado=True, error=None, datos={}, profesiones=PROFESIONES_SUMATE)


# ---------- webhook de Meta ----------

@app.get("/webhook")
def webhook_verify():
    if (
        request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == config.META_VERIFY_TOKEN
    ):
        return request.args.get("hub.challenge", ""), 200
    return "forbidden", 403


@app.post("/webhook")
def webhook_receive():
    if not whatsapp.firma_valida(request.get_data(), request.headers.get("X-Hub-Signature-256")):
        logger.warning("Webhook con firma inválida — descartado")
        return jsonify(ok=False), 403

    body = request.get_json(silent=True) or {}
    _marcar("webhook", body.get("object"))
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            aviso = whatsapp.aviso_de_plantilla(change.get("field") or "", value)
            if aviso:
                _aviso_de_plantilla(aviso)
                continue
            phone_number_id = value.get("metadata", {}).get("phone_number_id")
            if not phone_number_id:
                continue
            tenant_del_numero = db.get_tenant_by_phone_number_id(phone_number_id)
            # Modo "sin número propio": varios tenants comparten el número de
            # la plataforma, así que el phone_number_id ya no identifica a
            # nadie. En ese caso el tenant se resuelve por paciente, adentro
            # del loop — porque depende de QUIÉN escribió, no de a dónde.
            es_compartido = (
                tenant_del_numero is None
                and bool(config.PLATAFORMA_PHONE_NUMBER_ID)
                and phone_number_id == config.PLATAFORMA_PHONE_NUMBER_ID
            )
            if tenant_del_numero is None and not es_compartido:
                logger.warning("Mensaje a un phone_number_id sin tenant activo: %s", phone_number_id)
                continue
            for msg in value.get("messages", []):
                # Los dos tildes azules apenas llega, sea lo que sea.
                if msg.get("type") != "reaction":
                    whatsapp.marcar_leido(phone_number_id, msg.get("id"))
                # Texto, o el botón de respuesta rápida de una plantilla (la de
                # confirmación: «Sí, confirmo» / «Cancelar»).
                if msg.get("type") == "button":
                    msg = {**msg, "type": "text", "text": {"body": (msg.get("button") or {}).get("text", "")}}
                telefono = msg.get("from")
                if msg.get("type") == "interactive":
                    _interactivo(msg, phone_number_id, tenant_del_numero, telefono)
                    continue
                if msg.get("type") != "text":
                    _no_texto(msg, phone_number_id, tenant_del_numero, telefono)
                    continue
                _marcar("mensaje", phone_number_id)
                # ¿Es un profesional contestando «liberar» / «me lo tomo» sobre
                # un horario que le cancelaron? (cancelaciones.py)
                try:
                    decision = cancelaciones.respuesta_del_profesional(telefono, msg.get("text", {}).get("body", ""))
                except Exception:
                    logger.exception("Falló la respuesta del profesional (…%s)", (telefono or "")[-4:])
                    decision = None
                if decision:
                    whatsapp.enviar_texto(phone_number_id, telefono, decision)
                    continue
                texto = msg.get("text", {}).get("body", "")
                # ¿El profesional preguntando por su agenda desde su WhatsApp de avisos?
                para_profesional = _respuesta_al_profesional(tenant_del_numero, telefono, texto)
                if para_profesional:
                    whatsapp.enviar_texto(phone_number_id, telefono, para_profesional)
                    continue
                tenant, resuelto = _tenant_del_mensaje(tenant_del_numero, telefono, phone_number_id, texto)
                if resuelto:
                    continue
                if not tenant:
                    _sin_destino(phone_number_id, telefono)
                    continue
                _contestar(tenant, telefono, texto)

    return jsonify(ok=True), 200


def _respuesta_al_profesional(tenant_del_numero: dict | None, telefono: str, texto: str) -> str | None:
    """Si escribe un profesional desde su WhatsApp de avisos y es un pedido
    suyo («hoy», «pagos»…), la respuesta. Con número propio, sólo esa cuenta.
    None: el mensaje sigue como el de un paciente."""
    try:
        cuentas = db.tenants_por_aviso(telefono)
        if tenant_del_numero:
            cuentas = [t for t in cuentas if t["id"] == tenant_del_numero["id"]]
        return asistente_profesional.responder_a(cuentas, texto) if cuentas else None
    except Exception:
        logger.exception("Falló el asistente del profesional (…%s)", (telefono or "")[-4:])
        return None


def _contestar(tenant: dict, telefono: str, texto: str) -> None:
    try:
        respuesta = conversation.responder(tenant, telefono, texto)
    except Exception:
        logger.exception("Fallo procesando mensaje de …%s (tenant %s)", (telefono or "")[-4:], tenant["id"])
        respuesta = "Uh, tuve un problema procesando eso. Probá de nuevo en un rato."
    whatsapp.enviar_respuesta(whatsapp.numero_de(tenant), telefono, respuesta)


MINUTOS_ELECCION = 120


def _tenant_del_mensaje(tenant_del_numero: dict | None, telefono: str, phone_number_id: str,
                        texto: str | None = None) -> tuple[dict | None, bool]:
    """(tenant, ya_resuelto). Con número propio, ese. En el número compartido,
    por el paciente; si tiene turnos con más de un profesional, se usa el que
    eligió hace poco o se le pregunta «¿con quién es?» (ya_resuelto=True: el
    mensaje queda guardado y se contesta cuando elige). «con Micaela» por
    escrito también elige."""
    if tenant_del_numero:
        return tenant_del_numero, False
    try:
        candidatos = db.tenants_de_paciente(telefono)
    except Exception:
        logger.warning("No se pudieron buscar los profesionales del paciente", exc_info=True)
        candidatos = []
    if len(candidatos) <= 1:
        return (candidatos[0] if candidatos else db.tenant_por_paciente(telefono)), False
    eleccion = db.eleccion_whatsapp(telefono)
    pedido = conversation._normal(texto or "")
    if pedido.startswith("con "):
        coinciden = [t for t in candidatos if all(p in conversation._normal(t["nombre"]).split() for p in pedido[4:].split())]
        if len(coinciden) == 1:
            _elegir_profesional(coinciden[0], telefono, (eleccion or {}).get("pendiente"))
            return None, True
    elegido = next((t for t in candidatos if eleccion and t["id"] == eleccion.get("tenant_id")), None)
    if elegido:
        db.guardar_eleccion_whatsapp(telefono, elegido["id"], None,
                                     datetime.now(ZoneInfo("UTC")) + timedelta(minutes=MINUTOS_ELECCION))
        return elegido, False
    db.guardar_eleccion_whatsapp(telefono, None, (texto or "")[:1000] or None,
                                 datetime.now(ZoneInfo("UTC")) + timedelta(minutes=MINUTOS_ELECCION))
    cuerpo = "Tenés turnos con más de un profesional. ¿Con quién es?"
    a_mano = cuerpo + "\n\n" + "\n".join(f"• Escribí *con {t['nombre'].split()[-1]}* para {t['nombre']}" for t in candidatos)
    interactivo = ({"tipo": "botones", "cuerpo": cuerpo, "botones": [(f"con:{t['id']}", t["nombre"]) for t in candidatos]}
                   if len(candidatos) <= 3 else
                   {"tipo": "lista", "cuerpo": cuerpo, "boton": "Elegir", "titulo": "Profesionales",
                    "filas": [{"id": f"con:{t['id']}", "title": t["nombre"]} for t in candidatos]})
    whatsapp.enviar_respuesta(phone_number_id, telefono, conversation.Respuesta.con(a_mano, interactivo))
    return None, True


def _elegir_profesional(tenant: dict, telefono: str, pendiente: str | None) -> None:
    """Queda elegido un rato; si había un mensaje esperando, se contesta."""
    db.guardar_eleccion_whatsapp(telefono, tenant["id"], None,
                                 datetime.now(ZoneInfo("UTC")) + timedelta(minutes=MINUTOS_ELECCION))
    if pendiente and not conversation._normal(pendiente).startswith("con "):
        _contestar(tenant, telefono, pendiente)
    else:
        whatsapp.enviar_texto(whatsapp.numero_de(tenant), telefono,
                              f"Listo, hablamos de tus turnos con *{tenant['nombre']}*. Escribí *turno*, *mi turno* o *cancelar*. "
                              "Para hablar de otro profesional, escribí *con* y su nombre.")


def _interactivo(msg: dict, phone_number_id: str, tenant_del_numero: dict | None, telefono: str) -> None:
    """El paciente tocó una fila de una lista o un botón. Los ids vienen del
    teléfono del paciente: se revisa todo de nuevo (que el turno sea suyo,
    que el horario siga libre), nunca se confía en ellos."""
    _marcar("mensaje", phone_number_id)
    datos = msg.get("interactive") or {}
    elegido = datos.get("list_reply") or datos.get("button_reply") or {}
    ident, titulo = str(elegido.get("id") or ""), elegido.get("title") or ""
    if ident.startswith("con:"):
        candidatos = {t["id"]: t for t in db.tenants_de_paciente(telefono)}
        tenant = candidatos.get(ident[4:])
        if tenant:
            eleccion = db.eleccion_whatsapp(telefono)
            _elegir_profesional(tenant, telefono, (eleccion or {}).get("pendiente"))
        return
    tenant, resuelto = _tenant_del_mensaje(tenant_del_numero, telefono, phone_number_id)
    if resuelto:
        return
    if not tenant:
        _sin_destino(phone_number_id, telefono)
        return
    try:
        if ident.startswith("reservar:"):
            respuesta = conversation.elegir_horario(tenant, telefono, ident[len("reservar:"):])
        elif ident.startswith("mover:") and ident.count(":") == 2:
            _, turno_id, nuevo_id = ident.split(":")
            respuesta = _mover_desde_whatsapp(tenant, telefono, turno_id, nuevo_id)
        else:
            respuesta = conversation.responder(tenant, telefono, titulo)
    except Exception:
        logger.exception("Fallo procesando una elección de …%s (tenant %s)", (telefono or "")[-4:], tenant["id"])
        respuesta = "Uh, tuve un problema procesando eso. Probá de nuevo en un rato."
    whatsapp.enviar_respuesta(whatsapp.numero_de(tenant), telefono, respuesta)


def _mover_desde_whatsapp(tenant: dict, telefono: str, turno_id: str, nuevo_id: str) -> str:
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    turno = db.turno_por_id(tenant["id"], turno_id)
    if (not turno or turno.get("estado") != "reservado" or turno.get("paciente_telefono") != telefono
            or datetime.fromisoformat(turno["inicio"]) <= datetime.now(ZoneInfo("UTC"))):
        return "Ese turno ya no está reservado a tu nombre. Escribí *mi turno* para ver los tuyos."
    if not _puede_modificar(turno, tenant):
        return (f"Tu turno es en menos de {cancelaciones.horas_limite(tenant)} horas: para cambiarlo, "
                f"escribile directamente a *{tenant['nombre']}*. Todavía no se cambió nada.")
    nuevo = db.turno_por_id(tenant["id"], nuevo_id)
    error = _mover_por_paciente(tenant, turno, nuevo, "WhatsApp") if nuevo else "no está"
    if error:
        return "Ese horario ya no está disponible. Escribí *cambiar el turno* para ver los que quedan."
    return (f"Listo: tu turno pasó al {conversation._fmt(nuevo['inicio'], tz)}. "
            f"Ya le avisamos a *{tenant['nombre']}*. Si no podés ir, escribí *cancelar*.")


def _mover_por_paciente(tenant: dict, turno: dict, nuevo: dict, origen: str) -> str | None:
    """El paciente pasa su turno a otro horario libre (desde la web o por
    WhatsApp). None si salió; si no, por qué. Usa el mismo reprogramar_turno
    del panel; el turno nuevo queda confirmado y como reserva nueva."""
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    if (tenant.get("pausado") or nuevo["estado"] != "libre" or nuevo.get("hueco")
            or datetime.fromisoformat(nuevo["inicio"]) <= datetime.now(ZoneInfo("UTC"))):
        return "Ese horario ya no está disponible."
    duracion = _minutos_de(turno) or tenant.get("duracion_turno_minutos") or 50
    if not _entra(tenant, nuevo["id"], duracion):
        return f"En ese horario no entra tu turno de {duracion} minutos."
    error = db.reprogramar_turno(tenant["id"], turno["id"], datetime.fromisoformat(nuevo["inicio"]), duracion,
                                 ignorar_bloqueo=bool(nuevo.get("abierto")))
    if error:
        return error
    db.actualizar_reserva(tenant["id"], nuevo["id"], {
        "token_calendario": turno.get("token_calendario"), "confirmado_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "confirmacion_enviada_at": turno.get("confirmacion_enviada_at"), "reserva_nueva": True, "reserva_avisada": False})
    db.registrar_actividad(f"paciente ({origen})", f"cambió el horario de un turno desde {'el link de agendar' if origen == 'web' else origen}",
                           tenant["id"])
    scheduler.avisar_profesional(tenant, f"un paciente pasó {'desde la web' if origen == 'web' else 'por ' + origen} su turno del "
                                         f"{scheduler.cuando_de(turno, tenant, tz)} al {scheduler.cuando_de(nuevo, tenant, tz)}")
    _evento(tenant, "cambio")
    db.actualizar_reserva(tenant["id"], nuevo["id"], {"reserva_avisada": True})
    return None


def _no_texto(msg: dict, phone_number_id: str, tenant_del_numero: dict | None, telefono: str) -> None:
    """Audio, foto, PDF, sticker…: antes se ignoraban y el paciente quedaba
    esperando. Ver conversation.responder_no_texto."""
    tipo = msg.get("type") or ""
    if tipo == "reaction" or not telefono:
        return
    _marcar("mensaje", phone_number_id)
    tenant, resuelto = _tenant_del_mensaje(tenant_del_numero, telefono, phone_number_id)
    if resuelto:
        return
    if not tenant:
        _sin_destino(phone_number_id, telefono)
        return
    media_id = (msg.get(tipo) or {}).get("id") if isinstance(msg.get(tipo), dict) else None
    try:
        respuesta = conversation.responder_no_texto(tenant, telefono, tipo, media_id)
    except Exception:
        logger.exception("Fallo procesando un %s de …%s (tenant %s)", tipo, telefono[-4:], tenant["id"])
        respuesta = "Uh, tuve un problema con eso. ¿Me lo escribís?"
    if respuesta:
        whatsapp.enviar_texto(whatsapp.numero_de(tenant), telefono, respuesta)


RESPUESTA_SIN_DESTINO = (
    "Hola. Este es el WhatsApp de turnos de FiBOT Consultorio. No encontramos un turno con este número.\n\n"
    "Para reservar, entrá a {link}. Si ya reservaste, escribinos desde el mismo número que dejaste al reservar.")
SIN_LINK = "la página que te pasó tu profesional"


def _link_sin_destino() -> str | None:
    """El link para quien escribe sin turno: la página del profesional si es
    el único en el número compartido, o /turnos si son varios (el número no
    dice a quién le escribieron)."""
    compartidos = [t for t in db.tenants_activos() if not t.get("whatsapp_phone_number_id") and t.get("slug")]
    if not compartidos:
        return None
    base = f"https://{config.DOMINIO_PRINCIPAL}" if config.DOMINIO_PRINCIPAL else request.host_url.rstrip("/")
    # Con uno solo, directo a su calendario; con varios, a la lista de
    # profesionales (/turnos), donde el paciente elige con quién.
    return f"{base}/{compartidos[0]['slug']}" if len(compartidos) == 1 else f"{base}/turnos"


def _respuesta_sin_destino() -> tuple[str, str]:
    """(texto con el link puesto, texto tal como está guardado)."""
    try:
        guardado = (db.ajustes_plataforma().get("respuesta_sin_destino") or "").strip()
    except Exception:
        guardado = ""
    plantilla = guardado or RESPUESTA_SIN_DESTINO
    try:
        link = _link_sin_destino()
    except Exception:
        link = None
    return plantilla.replace("{link}", link or SIN_LINK), plantilla


def _sin_destino(phone_number_id: str, telefono: str) -> None:
    """Alguien sin turno le escribió al número compartido: no hay a qué
    profesional mandarlo. Se le contesta una vez por día (está dentro de las
    24 h, así que va texto libre) y queda en la bandeja de administración,
    sólo con los últimos 4 dígitos."""
    clave = hashlib.sha256(f"sin-destino:{telefono}".encode()).hexdigest()
    final = "".join(ch for ch in telefono or "" if ch.isdigit())[-4:]
    logger.info("Mensaje al número compartido de un teléfono sin turnos (…%s)", final)
    try:
        desde = (datetime.now(ZoneInfo("UTC")) - timedelta(hours=24)).isoformat()
        ya = db.respondido_sin_destino_desde(clave, desde)
        respondido = False if ya else whatsapp.enviar_texto(phone_number_id, telefono, _respuesta_sin_destino()[0])
        db.registrar_sin_destino(final, clave, bool(respondido))
    except Exception:
        logger.warning("No se pudo registrar el mensaje sin destino (…%s)", final, exc_info=True)


def _aviso_de_plantilla(aviso: dict) -> None:
    """Meta avisó que una plantilla cambió: se guarda y se avisa a
    administración por WhatsApp. Si algo falla, el webhook contesta 200 igual
    (si no, Meta reintenta el mismo aviso durante horas)."""
    try:
        db.guardar_aviso_plantilla(aviso)
        db.registrar_actividad("Meta", f"plantilla {aviso['nombre']}: {aviso['estado']}"
                                       + (f" ({aviso['motivo']})" if aviso.get("motivo") else ""))
        whatsapp.avisar_admin(f"Meta dice que la plantilla {aviso['nombre']} está {aviso['estado']}"
                              if not aviso["estado"].startswith("pasó") else
                              f"Meta cambió la categoría de la plantilla {aviso['nombre']}: {aviso['estado']}")
    except Exception:
        logger.warning("No se pudo procesar el aviso de la plantilla %s", aviso.get("nombre"), exc_info=True)


def _marcar(clave: str, detalle: str | None) -> None:
    """Para el «último mensaje recibido» de administración. Si falla, el
    mensaje se procesa igual."""
    try:
        db.marcar_evento(clave, detalle)
    except Exception:
        logger.warning("No se pudo marcar el evento %s", clave, exc_info=True)


# ---------- panel ----------

def _csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = _csrf_token

TEMAS = ("claro", "oscuro", "auto", "noche")
LETRAS = ("normal", "grande", "enorme")


def _vista() -> dict:
    """Tema y tamaño de letra de esta página. Con la sesión de un profesional
    manda lo guardado en su cuenta (lo sigue a cualquier dispositivo); si no,
    las cookies del navegador."""
    if "vista" in g:
        return g.vista
    v = {"tema": request.cookies.get("tema"), "letra": request.cookies.get("letra"), "noche": request.cookies.get("noche")}
    if session.get("tenant_id"):
        try:
            pref = (db.get_tenant_by_id(session["tenant_id"]) or {}).get("preferencias_vista") or {}
        except Exception:
            pref = {}
        v.update({k: pref[k] for k in ("tema", "letra", "noche") if pref.get(k)})
    g.vista = {"tema": v["tema"] if v["tema"] in TEMAS else "auto",
               "letra": v["letra"] if v["letra"] in LETRAS else "normal",
               "noche": v["noche"] if _horas_noche(v["noche"]) else NOCHE_DEFECTO}
    return g.vista


NOCHE_DEFECTO = "20-8"


def _horas_noche(valor) -> tuple[int, int] | None:
    """«20-8» → (20, 8): desde qué hora y hasta qué hora va oscuro. None si no sirve."""
    try:
        desde, hasta = (int(x) for x in str(valor).split("-"))
    except (TypeError, ValueError):
        return None
    return (desde, hasta) if 0 <= desde <= 23 and 0 <= hasta <= 23 and desde != hasta else None


app.jinja_env.globals["tiene_whatsapp"] = db.tiene_whatsapp
app.jinja_env.globals["vista_pagina"] = _vista

# Color de la página pública: (claro, oscuro), cada uno con
# (acento, fondo de botones/encabezado, fondo suave, intermedio). «verde» es el de siempre.
COLORES_PAGINA = {
    "verde": {"nombre": "Verde", "claro": None, "oscuro": None, "muestra": "#2f5233"},
    "azul": {"nombre": "Azul", "claro": ("#2b4f7a", "#2b4f7a", "#eaf0f7", "#6b8db5"),
             "oscuro": ("#8fb4e0", "#345a86", "#1f2a38", "#46648a"), "muestra": "#2b4f7a"},
    "terracota": {"nombre": "Terracota", "claro": ("#9a4a2c", "#9a4a2c", "#f8ece6", "#c58767"),
                  "oscuro": ("#e6a284", "#8a4a30", "#35241d", "#7a4a35"), "muestra": "#9a4a2c"},
    "violeta": {"nombre": "Violeta", "claro": ("#5b3f86", "#5b3f86", "#f0ebf7", "#9580b8"),
                "oscuro": ("#b8a2e0", "#5e4590", "#2a2336", "#5f4f80"), "muestra": "#5b3f86"},
    "pizarra": {"nombre": "Pizarra", "claro": ("#37474f", "#37474f", "#eceff1", "#78909c"),
                "oscuro": ("#a7bcc6", "#455a64", "#222a2e", "#4e6570"), "muestra": "#37474f"},
}
app.jinja_env.globals["colores_pagina"] = COLORES_PAGINA
app.jinja_env.globals["anio_actual"] = lambda: datetime.now().year
app.jinja_env.filters["pesos"] = lambda v: f"$ {v:,}".replace(",", ".")
_pesos = app.jinja_env.filters["pesos"]


@app.before_request
def ir_al_dominio_principal():
    """Con dominio propio, la dirección vieja de Railway redirige. Las llaves
    de acceso quedan atadas al dominio: conviene que todos entren por uno solo.
    308 conserva el método (un POST sigue siendo POST)."""
    principal = config.DOMINIO_PRINCIPAL
    if not principal or request.host.split(":")[0].lower() == principal:
        return None
    if request.path.startswith(("/webhook", "/health")):
        return None
    return redirect(f"https://{principal}{request.full_path.rstrip('?')}", code=308)


@app.before_request
def verificar_csrf():
    """Toda acción del panel tiene que traer el token de la sesión, para que
    otra página no pueda disparar un POST con la cookie del profesional. El
    webhook tiene su firma y la reserva pública no usa sesión."""
    # /login/qr/<token> es la aprobación desde el celular (con sesión): necesita token.
    # /agendar/<token>/… es del paciente, sin sesión: el token es la credencial
    # (como en la reserva pública). Ojo: "/agendar" empieza con "/agenda".
    if request.method != "POST" or not request.path.startswith(("/agenda", "/admin", "/login/qr/")) \
            or request.path.startswith(("/admin/login", "/agendar/")):
        return None
    esperado = session.get("csrf_token")
    recibido = request.form.get("csrf_token") or ""
    if not esperado or not hmac.compare_digest(esperado, recibido):
        return "sesión vencida, volvé a cargar la página", 403
    return None


def login_required(vista):
    @functools.wraps(vista)
    def envoltorio(*args, **kwargs):
        if not session.get("tenant_id"):
            return redirect(url_for("login", siguiente=request.path if request.method == "GET" else None))
        # La sesión tiene que seguir en la tabla: cerrarla desde el panel, desde
        # /admin o al cambiar la clave borra la fila. Una cuenta desactivada
        # pierde también la sesión abierta.
        tenant = db.get_tenant_by_id(session["tenant_id"])
        fila = db.sesion(session.get("sesion_id") or "")
        if not tenant or tenant.get("activo") is False or not fila or fila["tenant_id"] != tenant["id"]:
            session.clear()
            return redirect(url_for("login"))
        ahora = datetime.now(ZoneInfo("UTC"))
        if ahora - datetime.fromisoformat(fila["ultima"]) > timedelta(minutes=5):
            db.tocar_sesion(fila["id"], ahora.isoformat())
        return vista(*args, **kwargs)

    return envoltorio


@app.get("/login")
def login():
    return render_template("login.html", error=None)


VIGENCIA_LINK_MAIL = timedelta(hours=1)


@app.route("/olvide-la-clave", methods=["GET", "POST"])
def olvide_clave():
    """Con mail configurado, manda un link de una hora. La respuesta es
    siempre la misma: no dice si el usuario o el mail existen."""
    if request.method == "GET" or not mail.disponible():
        return render_template("olvide.html", por_mail=mail.disponible(), enviado=False, error=None)
    dato = (request.form.get("dato") or "").strip()
    claves = _claves_de_intento(f"olvido:{dato.casefold()}")
    bloqueo = _bloqueo_login(claves)
    if bloqueo:
        return render_template("olvide.html", por_mail=True, enviado=False, error=bloqueo), 429
    _registrar_fallo(claves)  # cada pedido cuenta: limita los mails por usuario y por IP
    tenant = db.tenant_por_email(dato) if "@" in dato else db.get_tenant_by_panel_user(dato)
    if dato and tenant and tenant.get("email"):
        token = seguridad.nuevo_token()
        db.crear_enlace_clave(seguridad.hash_token(token), tenant["id"],
                              (datetime.now(ZoneInfo("UTC")) + VIGENCIA_LINK_MAIL).isoformat())
        link = url_for("clave_nueva", token=token, _external=True)
        mail.enviar(tenant["email"], "Clave nueva para FiBOT Consultorio",
                    f"Hola. Pediste poner una clave nueva para tu panel de FiBOT Consultorio.\n\n"
                    f"Entrá a este link (sirve una sola vez y vence en una hora):\n{link}\n\n"
                    "Si no lo pediste vos, ignorá este mail: tu clave sigue igual.")
        db.registrar_actividad(tenant["nombre"], "pidió recuperar la clave por mail", tenant["id"])
    return render_template("olvide.html", por_mail=True, enviado=True, error=None)


@app.route("/clave-nueva/<token>", methods=["GET", "POST"])
def clave_nueva(token):
    """Link de un solo uso que genera la administración. GET sólo muestra el
    formulario (los previsualizadores no lo gastan)."""
    h = seguridad.hash_token(token)
    e = db.enlace_clave(h)
    ahora = datetime.now(ZoneInfo("UTC"))
    if not e or e.get("usado_at") or datetime.fromisoformat(e["vence"]) <= ahora:
        return _sin_cache(app.make_response(render_template("clave_nueva.html", valido=False, error=None)))
    if request.method == "GET":
        return _sin_cache(app.make_response(render_template("clave_nueva.html", valido=True, error=None)))
    nueva = request.form.get("nueva") or ""
    error = None
    if len(nueva) < MIN_CLAVE:
        error = f"La clave tiene que tener al menos {MIN_CLAVE} caracteres."
    elif nueva != (request.form.get("repetir") or ""):
        error = "Las dos claves no coinciden."
    if error:
        return _sin_cache(app.make_response(render_template("clave_nueva.html", valido=True, error=error)))
    tenant = db.get_tenant_by_id(e["tenant_id"])
    if not tenant or not db.usar_enlace_clave(h, ahora.isoformat()):
        return _sin_cache(app.make_response(render_template("clave_nueva.html", valido=False, error=None)))
    db.cambiar_clave("tenants", tenant["id"], "panel_pass_hash", bcrypt.hashpw(nueva.encode(), bcrypt.gensalt()).decode(),
                     (tenant.get("sesion_version") or 0) + 1)
    db.cerrar_sesiones(tenant["id"])
    db.borrar_intentos_login(f"panel:{tenant['panel_user'].casefold()}")
    db.registrar_actividad(tenant["nombre"], "puso una clave nueva con el link de la administración", tenant["id"])
    _avisar_cuenta(tenant, "Se puso una clave nueva en tu cuenta",
                   "Se puso una clave nueva con un link de recuperación y se cerraron todas las sesiones.")
    return render_template("clave_nueva.html", valido=False, listo=True, error=None)


def usuario_de_clave(clave: str) -> str:
    return clave.split(":", 1)[1]


def _claves_de_intento(clave_usuario: str) -> list[str]:
    return [clave_usuario, f"ip:{request.remote_addr or 'desconocida'}"]


def _bloqueo_login(claves: list[str]) -> str | None:
    ahora = seguridad.ahora_utc()
    hasta = seguridad.bloqueado_hasta(db.intentos_login(claves), ahora)
    if hasta:
        return f"Demasiados intentos. Probá de nuevo en {seguridad.minutos_restantes(hasta, ahora)} minutos."
    return None


def _registrar_fallo(claves: list[str]) -> None:
    ahora = seguridad.ahora_utc()
    filas = {f["clave"]: f for f in db.intentos_login(claves)}
    for clave in claves:
        maximo = seguridad.MAX_FALLOS_IP if clave.startswith("ip:") else seguridad.MAX_FALLOS
        nuevo = seguridad.tras_fallo(filas.get(clave), ahora, maximo)
        db.guardar_intento_login(clave, nuevo)
        if nuevo["bloqueado_hasta"] and clave.startswith("panel:"):
            # Que la profesional se entere (panel y resumen), sin decir desde dónde.
            tenant = db.get_tenant_by_panel_user(usuario_de_clave(clave))
            if tenant:
                db.marcar_bloqueo(tenant["id"], ahora.isoformat())
                db.registrar_actividad("sistema", f"bloqueó 15 minutos el usuario de {tenant['nombre']} por claves equivocadas", tenant["id"])
        elif nuevo["bloqueado_hasta"] and clave.startswith("admin:"):
            db.registrar_actividad("sistema", f"bloqueó 15 minutos el usuario de administración «{usuario_de_clave(clave)}»")


@app.post("/login")
def login_post():
    usuario = (request.form.get("usuario") or "").strip()
    clave = request.form.get("clave") or ""
    claves = _claves_de_intento(f"panel:{usuario.casefold()}")
    bloqueo = _bloqueo_login(claves)
    if bloqueo:
        return render_template("login.html", error=bloqueo), 429
    tenant = db.get_tenant_by_panel_user(usuario)
    if not tenant or not bcrypt.checkpw(clave.encode(), tenant["panel_pass_hash"].encode()):
        _registrar_fallo(claves)
        return render_template("login.html", error="Usuario o clave incorrectos."), 401
    db.borrar_intentos_login(claves[0])
    siguiente = _siguiente_seguro(request.form.get("siguiente"))
    session.clear()
    if tenant.get("totp_secreto"):
        session["pendiente_tenant"] = tenant["id"]
        session["siguiente"] = siguiente
        return redirect(url_for("login_codigo"))
    _abrir_sesion(tenant, "clave")
    return redirect(siguiente or url_for("agenda"))


def _siguiente_seguro(destino: str | None) -> str | None:
    """Sólo rutas propias conocidas: nunca redirigir afuera del sitio."""
    if destino and destino.startswith(("/agenda", "/login/qr/")) and "//" not in destino and "\\" not in destino:
        return destino
    return None


def _abrir_sesion(tenant: dict, origen: str, disp: str | None = None) -> None:
    """Sesión nueva del panel: fila en `sesiones` + cookie con su id."""
    session.clear()
    sesion_id = seguridad.nuevo_token()
    disp = disp or seguridad.dispositivo(request.headers.get("User-Agent"))
    db.crear_sesion(sesion_id, tenant["id"], disp, origen)
    session["tenant_id"] = tenant["id"]
    session["sesion_id"] = sesion_id
    db.marcar_ingreso("tenants", tenant["id"], datetime.now(ZoneInfo("UTC")).isoformat())
    db.registrar_actividad(tenant["nombre"], f"entró al panel ({origen}, {disp})", tenant["id"])


@app.route("/login/codigo", methods=["GET", "POST"])
def login_codigo():
    tenant = db.get_tenant_by_id(session["pendiente_tenant"]) if session.get("pendiente_tenant") else None
    if not tenant or not tenant.get("totp_secreto") or tenant.get("activo") is False:
        return redirect(url_for("login"))
    if request.method == "GET":
        return render_template("login_codigo.html", error=None)
    claves = _claves_de_intento(f"totp-panel:{tenant['id']}")
    bloqueo = _bloqueo_login(claves)
    if bloqueo:
        return render_template("login_codigo.html", error=bloqueo), 429
    paso = seguridad.verificar(tenant["totp_secreto"], request.form.get("codigo") or "", tenant.get("totp_ultimo_paso"))
    if paso is None:
        _registrar_fallo(claves)
        return render_template("login_codigo.html", error="Código incorrecto o ya usado."), 401
    db.borrar_intentos_login(claves[0])
    db.guardar_totp_tenant(tenant["id"], {"totp_ultimo_paso": paso})
    siguiente = _siguiente_seguro(session.get("siguiente"))
    _abrir_sesion(tenant, "clave y código")
    return redirect(siguiente or url_for("agenda"))


# --- Llaves de acceso (passkeys / WebAuthn) ---
# La llave queda en el celular o la compu (huella, cara o PIN del dispositivo);
# acá sólo se guarda la clave pública. Exigimos verificación del usuario, así
# que la llave vale por clave + segundo factor.

def _rp() -> tuple[str, str]:
    """(rp_id, origen) del sitio tal como lo ve el navegador."""
    return request.host.split(":")[0], f"{request.scheme}://{request.host}"


def _opciones_registro(clave_reto: str, user_id: bytes, usuario: str, nombre: str, existentes: list[str]):
    rp_id, _ = _rp()
    opciones = webauthn.generate_registration_options(
        rp_id=rp_id, rp_name="FiBOT Consultorio", user_name=usuario, user_display_name=nombre, user_id=user_id,
        authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED,
                                                               user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(x)) for x in existentes])
    session[clave_reto] = bytes_to_base64url(opciones.challenge)
    return app.response_class(webauthn.options_to_json(opciones), mimetype="application/json")


def _verificar_registro(clave_reto: str):
    """Devuelve lo verificado o None. El reto se usa una sola vez."""
    reto = session.pop(clave_reto, None)
    rp_id, origen = _rp()
    try:
        if not reto:
            raise ValueError("sin reto pendiente")
        return webauthn.verify_registration_response(credential=request.form.get("credencial") or "",
                                                     expected_challenge=base64url_to_bytes(reto),
                                                     expected_rp_id=rp_id, expected_origin=origen,
                                                     require_user_verification=True)
    except Exception:
        logger.info("Registro de llave rechazado", exc_info=True)
        return None


def _opciones_login(clave_reto: str):
    rp_id, _ = _rp()
    opciones = webauthn.generate_authentication_options(rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED)
    session[clave_reto] = bytes_to_base64url(opciones.challenge)
    return app.response_class(webauthn.options_to_json(opciones), mimetype="application/json")


def _verificar_login(clave_reto: str, buscar):
    """buscar(llave_id) -> fila de la llave o None. Devuelve (fila, nuevo
    contador) o None; cuenta el intento fallido por IP."""
    claves = [f"ip:{request.remote_addr or 'desconocida'}"]
    reto = session.pop(clave_reto, None)
    crudo = request.form.get("credencial") or ""
    try:
        llave_id = json.loads(crudo)["id"]
    except (ValueError, KeyError, TypeError):
        llave_id = ""
    fila = buscar(llave_id) if llave_id else None
    rp_id, origen = _rp()
    try:
        if not (fila and reto):
            raise ValueError("llave desconocida")
        v = webauthn.verify_authentication_response(credential=crudo, expected_challenge=base64url_to_bytes(reto),
                                                    expected_rp_id=rp_id, expected_origin=origen,
                                                    credential_public_key=base64url_to_bytes(fila["clave_publica"]),
                                                    credential_current_sign_count=fila["contador"] or 0,
                                                    require_user_verification=True)
    except Exception:
        _registrar_fallo(claves)
        return None
    return fila, v.new_sign_count


ERROR_LLAVE = "No se reconoció la llave. Probá de nuevo o entrá con tu clave."


@app.post("/agenda/llaves/opciones")
@login_required
def agenda_llave_opciones():
    tenant = db.get_tenant_by_id(session["tenant_id"])
    return _opciones_registro("llave_reto", tenant["id"].encode(), tenant["panel_user"], tenant["nombre"],
                              [x["id"] for x in db.llaves_de(tenant["id"])])


@app.post("/agenda/llaves/registrar")
@login_required
def agenda_llave_registrar():
    tenant = db.get_tenant_by_id(session["tenant_id"])
    v = _verificar_registro("llave_reto")
    if not v:
        return jsonify(ok=False, error="No se pudo guardar la llave. Probá de nuevo."), 400
    nombre = seguridad.dispositivo(request.headers.get("User-Agent"))
    db.agregar_llave({"id": bytes_to_base64url(v.credential_id), "tenant_id": tenant["id"],
                      "clave_publica": bytes_to_base64url(v.credential_public_key), "contador": v.sign_count,
                      "nombre": nombre})
    db.registrar_actividad(tenant["nombre"], f"agregó una llave de acceso ({nombre})", tenant["id"])
    _avisar_cuenta(tenant, "Se agregó una llave de acceso a tu cuenta",
                   f"Se agregó una llave de acceso en {nombre}. Con ella se puede entrar a tu panel sin clave.")
    return jsonify(ok=True)


@app.post("/agenda/llaves/<path:llave_id>/borrar")
@login_required
def agenda_llave_borrar(llave_id):
    tenant = db.get_tenant_by_id(session["tenant_id"])
    db.borrar_llave(tenant["id"], llave_id)
    db.registrar_actividad(tenant["nombre"], "borró una llave de acceso", tenant["id"])
    flash("Se borró la llave. Borrala también del celular o la compu si ya no la usás.")
    return _volver_a_agenda()


@app.post("/login/llave/opciones")
def login_llave_opciones():
    return _opciones_login("llave_reto_login")


@app.post("/login/llave")
def login_llave():
    bloqueo = _bloqueo_login([f"ip:{request.remote_addr or 'desconocida'}"])
    if bloqueo:
        return jsonify(ok=False, error=bloqueo), 429

    def buscar(llave_id):
        fila = db.llave(llave_id)
        tenant = db.get_tenant_by_id(fila["tenant_id"]) if fila else None
        return fila if tenant and tenant.get("activo") is not False else None

    r = _verificar_login("llave_reto_login", buscar)
    if not r:
        return jsonify(ok=False, error=ERROR_LLAVE), 400
    fila, contador = r
    db.usar_llave(fila["id"], contador, datetime.now(ZoneInfo("UTC")).isoformat())
    _abrir_sesion(db.get_tenant_by_id(fila["tenant_id"]), "llave de acceso")
    return jsonify(ok=True, destino=url_for("agenda"))


def _avisar_cuenta(tenant: dict, asunto: str, que_paso: str) -> None:
    """Mail de seguridad a la profesional (si cargó su mail y hay proveedor).
    Sin datos de pacientes ni IP: qué pasó, cuándo y qué hacer si no fue ella."""
    if not tenant.get("email") or not mail.disponible():
        return
    cuando = datetime.now(_tz_de(tenant)).strftime("%d/%m/%Y a las %H:%M")
    mail.enviar(tenant["email"], f"FiBOT Consultorio: {asunto.lower()}",
                f"Hola. {que_paso}\n\nCuándo: {cuando}.\n\n"
                "Si fuiste vos, no hace falta que hagas nada.\n"
                "Si no fuiste vos: entrá a tu panel, en «Ajustes» cambiá la clave y cerrá las sesiones que no reconozcas, "
                "o pedile ayuda a la administración.")


# --- Entrar leyendo un QR con el celular ---
QR_VIGENCIA = timedelta(minutes=2)


@app.post("/login/qr")
def login_qr_nuevo():
    """La compu pide un QR. El nonce queda en SU sesión: el link del QR solo
    no alcanza para entrar, hace falta además el navegador que lo pidió."""
    token, nonce = seguridad.nuevo_token(), seguridad.nuevo_token()
    session["qr_nonce"] = nonce
    vence = datetime.now(ZoneInfo("UTC")) + QR_VIGENCIA
    db.crear_qr_login(seguridad.hash_token(token), seguridad.hash_token(nonce),
                      seguridad.dispositivo(request.headers.get("User-Agent")), vence.isoformat())
    link = url_for("login_qr_aprobar", token=token, _external=True)
    return jsonify(token=token, svg=seguridad.qr_svg(link, escala=4), segundos=int(QR_VIGENCIA.total_seconds()),
                   estado=url_for("login_qr_estado", token=token))


@app.get("/login/qr/<token>/estado")
def login_qr_estado(token):
    q = db.qr_login(seguridad.hash_token(token))
    nonce = session.get("qr_nonce") or ""
    if not q or (q.get("tipo") or "panel") != "panel" or not hmac.compare_digest(q["nonce_hash"], seguridad.hash_token(nonce)):
        return jsonify(estado="invalido"), 404
    if datetime.fromisoformat(q["vence"]) <= datetime.now(ZoneInfo("UTC")) or q.get("usado_at"):
        return jsonify(estado="vencido")
    if not q.get("aprobado_at"):
        return jsonify(estado="esperando")
    tenant = db.get_tenant_by_id(q["tenant_id"])
    if not tenant or tenant.get("activo") is False or not db.usar_qr_login(q["token_hash"], datetime.now(ZoneInfo("UTC")).isoformat()):
        return jsonify(estado="vencido")
    _abrir_sesion(tenant, "QR", q.get("dispositivo"))
    return jsonify(estado="listo", destino=url_for("agenda"))


@app.route("/login/qr/<token>", methods=["GET", "POST"])
@login_required
def login_qr_aprobar(token):
    """Se abre en el celular, que ya tiene sesión. Aprobar le da a la compu que
    mostró el QR una sesión de esta misma cuenta."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    q = db.qr_login(seguridad.hash_token(token))
    vigente = q and (q.get("tipo") or "panel") == "panel" and not q.get("aprobado_at") \
        and datetime.fromisoformat(q["vence"]) > datetime.now(ZoneInfo("UTC"))
    if request.method == "POST" and vigente:
        if request.form.get("respuesta") == "aprobar" and \
                db.aprobar_qr_login(q["token_hash"], tenant["id"], datetime.now(ZoneInfo("UTC")).isoformat()):
            db.registrar_actividad(tenant["nombre"], f"aprobó entrar con QR en {q.get('dispositivo')}", tenant["id"])
            return render_template("login_qr.html", tenant=tenant, estado="aprobado", q=q)
        return render_template("login_qr.html", tenant=tenant, estado="rechazado", q=q)
    return render_template("login_qr.html", tenant=tenant, estado="pregunta" if vigente else "vencido", q=q)


def admin_requerido(vista):
    """Sesión de admin con el doble factor ya configurado: sin él, sólo se
    puede entrar a configurarlo."""
    @functools.wraps(vista)
    def envoltorio(*args, **kwargs):
        admin = db.admin_completo(session["admin_id"]) if session.get("admin_id") else None
        if not admin:
            volver = request.path if request.method == "GET" and request.path.startswith("/admin/qr/") else None
            return redirect(url_for("admin_login", siguiente=volver))
        if (admin.get("sesion_version") or 0) != session.get("admin_version", 0):
            session.clear()
            return redirect(url_for("admin_login"))
        if not admin.get("totp_secreto"):
            return redirect(url_for("admin_doble_factor"))
        return vista(*args, **kwargs)

    return envoltorio


@app.get("/admin/login")
def admin_login():
    return render_template("admin_login.html", error=None)


@app.post("/admin/login")
def admin_login_post():
    usuario = (request.form.get("usuario") or "").strip()
    claves = _claves_de_intento(f"admin:{usuario.casefold()}")
    bloqueo = _bloqueo_login(claves)
    if bloqueo:
        return render_template("admin_login.html", error=bloqueo), 429
    admin = db.admin_por_usuario(usuario)
    if not admin or not bcrypt.checkpw((request.form.get("clave") or "").encode(), admin["pass_hash"].encode()):
        _registrar_fallo(claves)
        return render_template("admin_login.html", error="Usuario o clave incorrectos."), 401
    db.borrar_intentos_login(claves[0])
    siguiente = request.form.get("siguiente") or ""
    session.clear()
    if admin.get("totp_secreto"):
        # Falta el código: todavía no es una sesión de admin.
        session["admin_pendiente"] = admin["id"]
        if siguiente.startswith("/admin/qr/") and "//" not in siguiente:
            session["siguiente_admin"] = siguiente
        return redirect(url_for("admin_codigo"))
    session["admin_id"] = admin["id"]
    session["admin_version"] = admin.get("sesion_version") or 0
    db.marcar_ingreso("admins", admin["id"], datetime.now(ZoneInfo("UTC")).isoformat())
    return redirect(url_for("admin_doble_factor"))


@app.route("/admin/codigo", methods=["GET", "POST"])
def admin_codigo():
    admin = db.admin_completo(session["admin_pendiente"]) if session.get("admin_pendiente") else None
    if not admin or not admin.get("totp_secreto"):
        return redirect(url_for("admin_login"))
    if request.method == "GET":
        return render_template("admin_codigo.html", error=None)
    claves = _claves_de_intento(f"totp:{admin['id']}")
    bloqueo = _bloqueo_login(claves)
    if bloqueo:
        return render_template("admin_codigo.html", error=bloqueo), 429
    paso = seguridad.verificar(admin["totp_secreto"], request.form.get("codigo") or "", admin.get("totp_ultimo_paso"))
    if paso is None:
        _registrar_fallo(claves)
        return render_template("admin_codigo.html", error="Código incorrecto o ya usado."), 401
    db.borrar_intentos_login(claves[0])
    db.guardar_totp(admin["id"], {"totp_ultimo_paso": paso})
    siguiente = session.get("siguiente_admin")
    session.clear()
    session["admin_id"] = admin["id"]
    session["admin_version"] = admin.get("sesion_version") or 0
    db.marcar_ingreso("admins", admin["id"], datetime.now(ZoneInfo("UTC")).isoformat())
    db.registrar_actividad(f"administración ({admin['usuario']})", "entró a administración")
    return redirect(siguiente or url_for("admin_panel"))


@app.route("/admin/doble-factor", methods=["GET", "POST"])
def admin_doble_factor():
    """Configuración obligatoria del doble factor. El secreto vive en la
    sesión hasta que un código lo confirma: recién ahí se guarda."""
    admin = db.admin_completo(session["admin_id"]) if session.get("admin_id") else None
    if not admin or (admin.get("sesion_version") or 0) != session.get("admin_version", 0):
        session.clear()
        return redirect(url_for("admin_login"))
    if admin.get("totp_secreto"):
        return redirect(url_for("admin_panel"))
    secreto = session.get("totp_nuevo") or seguridad.nuevo_secreto()
    session["totp_nuevo"] = secreto
    error = None
    if request.method == "POST":
        paso = seguridad.verificar(secreto, request.form.get("codigo") or "")
        if paso is not None:
            db.guardar_totp(admin["id"], {"totp_secreto": secreto, "totp_ultimo_paso": paso})
            db.registrar_actividad(f"administración ({admin['usuario']})", "activó el doble factor")
            session.pop("totp_nuevo", None)
            flash("Doble factor activado. Desde ahora, al entrar te va a pedir el código de la app.")
            return redirect(url_for("admin_panel"))
        error = "Ese código no coincide. Fijate que la hora del celular esté bien y probá con el próximo."
    uri = seguridad.uri(secreto, admin["usuario"])
    return render_template("doble_factor.html", cuenta=f"{admin['usuario']} (administración)", clave=seguridad.agrupado(secreto),
                           uri=uri, qr=seguridad.qr_svg(uri), error=error, volver=None, salir=url_for("logout"),
                           obligatorio=True)


def _tablero_cobros(cobros: dict, previos: dict | None, cuentas: int) -> dict:
    """Lo de toda la plataforma en el mes: total neto, por medio, cuántos
    profesionales cobraron y la variación contra el mes anterior."""
    suma = lambda d, k: sum(int(c.get(k) or 0) for c in d.values())
    tablero = {"total": suma(cobros, "total"), "pagos": suma(cobros, "cantidad"),
               "mercadopago": suma(cobros, "mercadopago"), "transferencia": suma(cobros, "transferencia"),
               "devuelto": suma(cobros, "devuelto"), "profesionales": sum(1 for c in cobros.values() if c.get("cantidad")),
               "cuentas": cuentas, "anterior": None, "variacion": None}
    if previos is not None:
        tablero["anterior"] = suma(previos, "total")
        if tablero["anterior"]:
            tablero["variacion"] = round((tablero["total"] - tablero["anterior"]) * 100 / tablero["anterior"])
    return tablero


def _serie_cobros(inicio_mes: datetime, cobros: dict, previos: dict | None) -> list[dict]:
    """Los últimos 6 meses hasta el elegido (total neto de la plataforma),
    para el gráfico de barras de /admin. Reusa los dos meses ya sumados."""
    meses = [inicio_mes]
    for _ in range(5):
        meses.insert(0, (meses[0] - timedelta(days=1)).replace(day=1))
    serie = []
    for i, m in enumerate(meses):
        if i == 5:
            datos = cobros
        elif i == 4 and previos is not None:
            datos = previos
        else:
            try:
                datos = db.cobros_por_tenant(m, meses[i + 1])
            except Exception:
                logger.warning("admin: no se pudo sumar %s", m, exc_info=True)
                datos = None
        total = sum(int(c.get("total") or 0) for c in datos.values()) if datos is not None else None
        serie.append({"etiqueta": f"{recibos.MESES[m.month - 1][:3]} {m:%y}", "total": total})
    tope = max([s["total"] or 0 for s in serie] + [1])
    for s in serie:
        s["alto"] = round((s["total"] or 0) * 100 / tope)
    return serie


@app.get("/admin/uso.csv")
@admin_requerido
def admin_uso_csv():
    """Uso por profesional de un mes, para facturar. Sólo cuentas."""
    hoy = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date()
    try:
        primero = datetime.strptime(request.args.get("mes") or "", "%Y-%m").date()
    except ValueError:
        primero = hoy.replace(day=1)
    siguiente = (primero.replace(day=28) + timedelta(days=4)).replace(day=1)
    tz = ZoneInfo("America/Argentina/Buenos_Aires")
    desde = datetime.combine(primero, dtime(), tz).isoformat()
    hasta = datetime.combine(siguiente, dtime(), tz).isoformat()
    uso = db.uso_de_tenants(desde, hasta)
    try:
        cobros = db.cobros_por_tenant(datetime.combine(primero, dtime(), tz), datetime.combine(siguiente, dtime(), tz))
    except Exception:
        logger.warning("uso.csv: no se pudieron sumar los cobros", exc_info=True)
        cobros = {}
    salida = io.StringIO()
    escritor = csv.writer(salida, delimiter=";")
    escritor.writerow(["Profesional", "Usuario", "Rubro", "Activa", "Mes", "Sesiones", "Pacientes", "Cuestionarios", "Entregas",
                       "Pagos web", "Cobrado web (neto)", "Mercado Pago", "Transferencia", "Devuelto"])
    vacio = {"cantidad": 0, "total": 0, "mercadopago": 0, "transferencia": 0, "devuelto": 0}
    for t in db.todos_los_tenants():
        u = uso.get(t["id"], {"sesiones": 0, "pacientes": 0, "cuestionarios": 0, "entregas": 0})
        c = cobros.get(t["id"], vacio)
        escritor.writerow([t["nombre"], t["panel_user"], t.get("rubro") or "", "no" if t.get("activo") is False else "sí",
                           primero.strftime("%Y-%m"), u["sesiones"], u["pacientes"], u["cuestionarios"], u["entregas"],
                           c["cantidad"], c["total"], c["mercadopago"], c["transferencia"], c["devuelto"]])
    # BOM: Excel en castellano abre bien acentos y el «;».
    return app.response_class("\ufeff" + salida.getvalue(), mimetype="text/csv",
                              headers={"Content-Disposition": f'attachment; filename="uso-{primero:%Y-%m}.csv"'})


SLUGS_RESERVADOS = {"login", "logout", "agenda", "health", "webhook", "admin", "entrega", "cuestionario", "privacidad", "turnos", "agendar", "pago", "mercadopago", "sumate", "static", "sin-conexion"}
RUBROS_ALTA = ["psicología", "psicopedagogía"]


@app.get("/admin")
@admin_requerido
def admin_panel():
    """Cuentas, uso y biblioteca. Nunca datos de pacientes: sólo cuentas."""
    tz = ZoneInfo("America/Argentina/Buenos_Aires")
    ahora = datetime.now(ZoneInfo("UTC"))
    uso = db.uso_de_tenants((ahora - timedelta(days=30)).isoformat())
    tenants = db.todos_los_tenants()
    inicio_mes = datetime.now(tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        inicio_mes = datetime.strptime(request.args.get("cobros") or "", "%Y-%m").replace(tzinfo=tz)
    except ValueError:
        pass
    try:
        cobros = db.cobros_por_tenant(inicio_mes, (inicio_mes + timedelta(days=32)).replace(day=1))
    except Exception:
        logger.warning("admin: no se pudieron sumar los cobros del mes", exc_info=True)
        cobros = {}
    try:
        mes_previo = (inicio_mes - timedelta(days=1)).replace(day=1)
        cobros_previos = db.cobros_por_tenant(mes_previo, inicio_mes)
    except Exception:
        logger.warning("admin: no se pudieron sumar los cobros del mes anterior", exc_info=True)
        cobros_previos = None
    tablero = _tablero_cobros(cobros, cobros_previos, len(tenants))
    tablero["serie"] = _serie_cobros(inicio_mes, cobros, cobros_previos)
    try:
        demorados = db.comprobantes_demorados((ahora - timedelta(hours=24)).isoformat())
    except Exception:
        logger.warning("admin: no se pudieron contar los comprobantes demorados", exc_info=True)
        demorados = {}
    for t in tenants:
        t["cobros"] = cobros.get(t["id"])
        t["demorados"] = demorados.get(t["id"], 0)
        t["uso"] = uso.get(t["id"], {"sesiones": 0, "pacientes": 0, "cuestionarios": 0, "entregas": 0})
        t["_ingreso"] = datetime.fromisoformat(t["ultimo_ingreso"]).astimezone(tz).strftime("%d/%m/%Y %H:%M") if t.get("ultimo_ingreso") else "nunca"
    biblioteca = [dict(e, titulo=e["contenido"]["nombre"] + (f" · {e['contenido']['version']}" if e["contenido"].get("version") else ""))
                  for e in db.biblioteca()]
    estado = _estado_plataforma(tenants, tz)
    filtro = request.args.get("actividad_de") or None
    actividad = db.actividad_reciente(200, filtro)
    for a in actividad:
        a["_cuando"] = datetime.fromisoformat(a["cuando"]).astimezone(tz).strftime("%d/%m/%Y %H:%M") if "T" in str(a["cuando"]) else a["cuando"]
    return render_template("admin.html", admin=db.admin_por_id(session["admin_id"]), tenants=tenants,
                           admin_llaves=db.admin_llaves_de(session["admin_id"]),
                           actividad=actividad, filtro=filtro, estado=estado,
                           biblioteca=biblioteca, rubros=RUBROS_ALTA, interesados=_interesados_o_nada(),
                           mes_actual=datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).strftime("%Y-%m"),
                           mes_cobros=inicio_mes.strftime("%Y-%m"), tablero=tablero,
                           demorados=[(t["nombre"], t["demorados"]) for t in tenants if t["demorados"]],
                           mes_cobros_es_actual=inicio_mes.strftime("%Y-%m") == datetime.now(tz).strftime("%Y-%m"))


def _interesados_o_nada() -> list[dict] | None:
    """None = la tabla no responde (falta aplicar migrations/060): /admin sale igual."""
    try:
        return db.interesados()
    except Exception:
        logger.warning("admin: no se pudieron leer los interesados", exc_info=True)
        return None


def _estado_plataforma(tenants: list[dict], tz: ZoneInfo) -> list[dict]:
    """Qué está configurado y qué falta, de un vistazo. Sólo dice si las
    variables están cargadas, nunca sus valores."""
    host = request.host.split(":")[0]
    ok = lambda nombre, bien, detalle: {"nombre": nombre, "ok": bien, "detalle": detalle}
    principal = config.DOMINIO_PRINCIPAL
    whatsapp_real = bool(config.META_ACCESS_TOKEN) and config.META_ACCESS_TOKEN != "fake"
    plantillas = [n for n, v in (("recordatorio", config.RECORDATORIO_TEMPLATE), ("avisos", config.AVISO_TEMPLATE)) if v]
    tick = scheduler._ultimo_tick
    activos = [t for t in tenants if t.get("activo") is not False]
    completos = db.estado_seguridad_tenants()
    eventos = _eventos()
    return [
        ok("Dominio", bool(principal) and host == principal,
           f"Entraste por {host}." + (f" Dominio principal: {principal}." if principal
                                      else " Falta cargar DOMINIO_PRINCIPAL en Railway (por ahora no redirige).")),
        dict(ok("Mail", mail.disponible(), "Recuperar la clave, avisos de seguridad y recibos de pago por mail." if mail.disponible()
                else "Sin proveedor: faltan RESEND_API_KEY y MAIL_REMITENTE. La clave se recupera con el link de administración."),
             prueba_mail=True),
        dict(ok("WhatsApp", whatsapp_real and bool(config.PLATAFORMA_PHONE_NUMBER_ID) and bool(plantillas),
                ("Conectado" if whatsapp_real else "Sin credenciales reales de Meta")
                + (f"; número de la plataforma cargado" if config.PLATAFORMA_PHONE_NUMBER_ID else "; falta el número de la plataforma")
                + (f"; plantillas: {', '.join(plantillas)}." if plantillas else "; sin plantillas aprobadas.")
                + f" Último mensaje recibido: {_hace(eventos.get('mensaje'), tz)}."
                + (" Freno de envíos activo." if config.WHATSAPP_SOLO_A else "")),
             enlace=url_for("admin_whatsapp"), enlace_texto="Revisar WhatsApp"),
        ok("Recordatorios y limpieza", bool(tick) and datetime.now(ZoneInfo("UTC")) - tick < timedelta(minutes=30),
           f"Última vuelta: {tick.astimezone(tz).strftime('%d/%m %H:%M')}." if tick else "Todavía no corrió desde que arrancó el servidor."),
        ok("Respaldo", None, "Lo hace Supabase y desde la app no se puede ver: revisalo en Supabase → Database → Backups."),
        ok("Profesionales", None, f"{len(activos)} activas · {completos['sin_mail']} sin mail cargado · "
                                  f"{completos['con_doble_factor']} con doble factor · {completos['con_llave']} con llave de acceso."),
    ]


def _eventos() -> dict[str, dict]:
    try:
        return db.eventos_plataforma()
    except Exception:
        logger.warning("No se pudieron leer los eventos de la plataforma", exc_info=True)
        return {}


def _hace(evento: dict | None, tz: ZoneInfo) -> str:
    """«hace 5 min (03/11 12:31)» o «nunca»."""
    if not evento:
        return "nunca"
    cuando = datetime.fromisoformat(evento["cuando"])
    minutos = int((datetime.now(ZoneInfo("UTC")) - cuando).total_seconds() // 60)
    hace = (f"hace {minutos} min" if minutos < 60 else f"hace {minutos // 60} h" if minutos < 48 * 60
            else f"hace {minutos // 1440} días")
    return f"{hace} ({cuando.astimezone(tz).strftime('%d/%m %H:%M')})"


@app.get("/admin/whatsapp")
@admin_requerido
def admin_whatsapp():
    """Lista de pasos de WhatsApp revisada en vivo contra Meta, y prueba de
    envío. Nunca muestra el token ni otros secretos."""
    tz = ZoneInfo("America/Argentina/Buenos_Aires")
    eventos = _eventos()
    pasos = whatsapp.diagnostico(_hace(eventos["webhook"], tz) if eventos.get("webhook") else None,
                                 _hace(eventos["mensaje"], tz) if eventos.get("mensaje") else None)
    plantillas = [("hello_world", "hello_world (en_US, la trae toda cuenta nueva)")]
    if config.RECORDATORIO_TEMPLATE:
        plantillas.append((config.RECORDATORIO_TEMPLATE, f"{config.RECORDATORIO_TEMPLATE} (el recordatorio, con datos de ejemplo)"))
    try:
        avisos = db.avisos_plantillas()
    except Exception:
        avisos = []
    for a in avisos:
        a["_cuando"] = datetime.fromisoformat(a["cuando"]).astimezone(tz).strftime("%d/%m %H:%M")
    ajustes = [{"clave": c, "nombre": n, "valor": {"phone_number_id": config.PLATAFORMA_PHONE_NUMBER_ID,
                                                   "waba_id": config.META_WABA_ID,
                                                   "solo_a": ", ".join(config.WHATSAPP_SOLO_A),
                                                   "aviso_admin": whatsapp.aviso_admin}[c],
                "origen": whatsapp.origen(c)} for c, n in whatsapp.AJUSTES.items()]
    try:
        bandeja = db.mensajes_sin_destino()
    except Exception:
        bandeja = []
    for m in bandeja:
        m["_cuando"] = datetime.fromisoformat(m["cuando"]).astimezone(tz).strftime("%d/%m %H:%M")
    simulacion = session.get("simulacion") or {}
    cuentas = [t for t in db.todos_los_tenants() if t.get("activo") is not False]
    import plantillas as catalogo_plantillas
    catalogo = [dict(p, cargada=getattr(config, p["variable"], ""),
                     boton_activo=bool(p.get("url") and getattr(config, p["url"]["variable"], False)))
                for p in catalogo_plantillas.CATALOGO]
    return render_template("admin_whatsapp.html", pasos=pasos, plantillas=plantillas, avisos=avisos, ajustes=ajustes,
                           catalogo=catalogo, que_hace=catalogo_plantillas.QUE_HACE,
                           bandeja=bandeja, cuentas=cuentas, simulacion=simulacion, simulado=SIMULADO[-4:],
                           respuesta_sin_destino=_respuesta_sin_destino(), por_defecto=RESPUESTA_SIN_DESTINO,
                           vista=session.pop("vista_previa", None),
                           resultado=session.pop("prueba_whatsapp", None))


@app.post("/admin/whatsapp/respuesta")
@admin_requerido
def admin_whatsapp_respuesta():
    """Texto que recibe quien escribe sin turno. Vacío = el de fábrica.
    `{link}` se reemplaza por la página pública cuando hay un solo profesional
    en el número compartido."""
    texto = (request.form.get("texto") or "").replace("\r\n", "\n").strip()[:1000]
    if request.form.get("fabrica") or texto == RESPUESTA_SIN_DESTINO:
        texto = ""
    db.guardar_ajustes_plataforma({"respuesta_sin_destino": texto})
    db.registrar_actividad(_actor_admin(), "cambió la respuesta a quien escribe sin turno" if texto
                           else "volvió a la respuesta de fábrica para quien escribe sin turno")
    session["prueba_whatsapp"] = {"ok": True, "texto": "Respuesta guardada." if texto else "Volvió el texto de fábrica."}
    return redirect(url_for("admin_whatsapp") + "#sin-turno")


@app.post("/admin/whatsapp/vista-previa")
@admin_requerido
def admin_whatsapp_vista_previa():
    """Qué saldría «como si fuera» otro día y hora: resumen diario y
    recordatorios de una cuenta. No manda nada ni marca nada."""
    tenant = db.get_tenant_by_id(request.form.get("tenant_id") or "")
    tz = ZoneInfo((tenant or {}).get("timezone") or "America/Argentina/Buenos_Aires")
    try:
        momento = datetime.fromisoformat(request.form.get("momento") or "").replace(tzinfo=tz)
    except ValueError:
        momento = None
    if not tenant or not momento:
        session["prueba_whatsapp"] = {"ok": False, "texto": "Elegí una cuenta, un día y una hora."}
        return redirect(url_for("admin_whatsapp") + "#vista-previa")
    try:
        vista = scheduler.vista_previa(tenant, momento)
    except Exception:
        logger.exception("Falló la vista previa del tenant %s", tenant["id"])
        session["prueba_whatsapp"] = {"ok": False, "texto": "No se pudo armar la vista previa: mirá los registros del servidor."}
        return redirect(url_for("admin_whatsapp") + "#vista-previa")
    session["vista_previa"] = {**vista, "nombre": tenant["nombre"], "tenant_id": tenant["id"],
                               "momento": momento.strftime("%Y-%m-%dT%H:%M"),
                               "momento_texto": momento.strftime("%d/%m/%Y %H:%M")}
    return redirect(url_for("admin_whatsapp") + "#vista-previa")


# El «paciente» del simulador. Nunca recibe nada: el simulador no manda
# WhatsApp, y si el bot le reserva un turno, el recordatorio lo para el freno
# o Meta (no es un número real).
SIMULADO = "5490000000000"


@app.post("/admin/whatsapp/simular")
@admin_requerido
def admin_whatsapp_simular():
    """Le escribe al bot como si fuera un paciente y muestra qué contesta, sin
    mandar nada por WhatsApp. Lo que hace el bot es de verdad (si reserva, el
    turno queda reservado a nombre del simulador): para eso está «Deshacer»."""
    tenant = db.get_tenant_by_id(request.form.get("tenant_id") or "")
    texto = (request.form.get("texto") or "").strip()[:500]
    if not tenant or not texto:
        session["prueba_whatsapp"] = {"ok": False, "texto": "Elegí una cuenta y escribí un mensaje."}
        return redirect(url_for("admin_whatsapp") + "#simulador")
    simulacion = session.get("simulacion") or {}
    if simulacion.get("tenant_id") != tenant["id"]:
        simulacion = {"tenant_id": tenant["id"], "nombre": tenant["nombre"], "chat": []}
    try:
        respuesta = conversation.responder(tenant, SIMULADO, texto)
    except Exception:
        logger.exception("Falló el simulador con el tenant %s", tenant["id"])
        respuesta = "(El bot falló procesando esto: mirá los registros del servidor.)"
    simulacion["chat"] = (simulacion["chat"] + [{"de": "paciente", "texto": texto}, {"de": "bot", "texto": respuesta}])[-20:]
    session["simulacion"] = simulacion
    db.registrar_actividad(_actor_admin(), f"probó el bot de {tenant['nombre']} con el simulador")
    return redirect(url_for("admin_whatsapp") + "#simulador")


@app.post("/admin/whatsapp/simular/deshacer")
@admin_requerido
def admin_whatsapp_simular_deshacer():
    """Libera los turnos futuros que reservó el simulador y borra la charla."""
    simulacion = session.pop("simulacion", None) or {}
    liberados = 0
    if simulacion.get("tenant_id"):
        for _ in range(50):
            turnos = db.proximos_turnos_de(simulacion["tenant_id"], SIMULADO)
            if not turnos:
                break
            for t in turnos:
                db.cancelar_turno(simulacion["tenant_id"], t["id"])
                liberados += 1
    session["prueba_whatsapp"] = {"ok": True, "texto": "Charla borrada" + (f" y {liberados} turno(s) del simulador liberados." if liberados else ".")}
    return redirect(url_for("admin_whatsapp") + "#simulador")


@app.post("/admin/whatsapp/suscribir")
@admin_requerido
def admin_whatsapp_suscribir():
    ok, texto = whatsapp.suscribir_app()
    db.registrar_actividad(_actor_admin(), "suscribió la app a la cuenta de WhatsApp" if ok
                           else "intentó suscribir la app a la cuenta de WhatsApp (Meta lo rechazó)")
    session["prueba_whatsapp"] = {"ok": ok, "texto": texto if ok else f"Meta no la suscribió: {texto}"}
    return redirect(url_for("admin_whatsapp"))


@app.post("/admin/whatsapp/ajustes")
@admin_requerido
def admin_whatsapp_ajustes():
    """Número, cuenta, freno y teléfono de avisos. Vacío = vuelve a lo de
    Railway. Se aplican en el momento, sin deploy."""
    f = request.form
    solo_digitos = lambda v: "".join(ch for ch in v or "" if ch.isdigit())
    valores = {
        "phone_number_id": solo_digitos(f.get("phone_number_id")),
        "waba_id": solo_digitos(f.get("waba_id")),
        "solo_a": ",".join(n for n in (solo_digitos(x) for x in (f.get("solo_a") or "").split(",")) if n),
        "aviso_admin": solo_digitos(f.get("aviso_admin")),
    }
    malos = [n for n in valores["solo_a"].split(",") if n and len(n) < 10]
    if malos or (valores["aviso_admin"] and len(valores["aviso_admin"]) < 10):
        session["prueba_whatsapp"] = {"ok": False, "texto": "Los teléfonos van completos, con código de país (ej. 5491122223333)."}
        return redirect(url_for("admin_whatsapp"))
    antes = {c: whatsapp.origen(c) for c in whatsapp.AJUSTES}
    db.guardar_ajustes_plataforma(valores)
    whatsapp.aplicar_ajustes(valores)
    cambios = [whatsapp.AJUSTES[c] for c in valores if valores[c] or antes[c] == "administración"]
    db.registrar_actividad(_actor_admin(), "cambió los ajustes de WhatsApp" + (f" ({', '.join(cambios)})" if cambios else ""))
    session["prueba_whatsapp"] = {"ok": True, "texto": "Ajustes guardados: ya están en uso, sin deploy."}
    return redirect(url_for("admin_whatsapp"))


@app.post("/admin/whatsapp/prueba")
@admin_requerido
def admin_whatsapp_prueba():
    telefono = "".join(ch for ch in request.form.get("telefono") or "" if ch.isdigit())
    plantilla = request.form.get("plantilla") or "hello_world"
    if plantilla not in ("hello_world", config.RECORDATORIO_TEMPLATE) or not plantilla:
        plantilla = "hello_world"
    if len(telefono) < 10:
        resultado = {"ok": False, "texto": "Escribí el teléfono completo, con código de país (ej. 5491122223333)."}
    elif not config.PLATAFORMA_PHONE_NUMBER_ID:
        resultado = {"ok": False, "texto": "Falta el número de la plataforma: cargalo en Ajustes."}
    else:
        if plantilla == "hello_world":
            lang, params = "en_US", []
        else:
            lang, params = config.RECORDATORIO_TEMPLATE_LANG, ["Lic. Prueba", "mañana a las 10:00 (es una prueba)"]
        ok, detalle = whatsapp.enviar_plantilla_detalle(config.PLATAFORMA_PHONE_NUMBER_ID, telefono, plantilla, lang, params)
        final = "…" + telefono[-4:]
        if detalle == "frenado":
            resultado = {"ok": False, "texto": f"No salió: el teléfono terminado en {final} no está en el freno de envíos. Agregalo en Ajustes para probar."}
        elif ok:
            resultado = {"ok": True, "texto": f"Meta aceptó «{plantilla}» para el teléfono terminado en {final}. Tiene que llegar en unos segundos."}
        else:
            resultado = {"ok": False, "texto": f"Meta lo rechazó: {detalle}"}
        db.registrar_actividad(_actor_admin(), f"mandó un WhatsApp de prueba ({plantilla}) al …{telefono[-4:]}: "
                                               + ("aceptado" if ok and detalle != "frenado" else "no salió"))
    session["prueba_whatsapp"] = resultado
    return redirect(url_for("admin_whatsapp"))


@app.post("/admin/mail/prueba")
@admin_requerido
def admin_mail_prueba():
    """Manda un mail de prueba para confirmar que Resend anda antes de que
    lo necesite un paciente (recibo) o un profesional (recuperar la clave)."""
    para = (request.form.get("email") or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", para) or len(para) > 200:
        flash("Escribí un mail válido para la prueba.")
        return redirect(url_for("admin_panel"))
    ok, detalle = mail.enviar_detalle(para, "Prueba de FiBOT Consultorio",
                                      "Si te llegó este mail, los recibos de pago y los avisos por mail funcionan.")
    oculto = para[:2] + "…@" + para.split("@", 1)[1]
    flash(f"Salió el mail de prueba a {oculto}: revisá la bandeja (y el spam)." if ok
          else f"No salió el mail de prueba: {detalle}.")
    db.registrar_actividad(_actor_admin(), f"mandó un mail de prueba a {oculto}: " + ("salió" if ok else "no salió"))
    return redirect(url_for("admin_panel"))


@app.post("/admin/profesionales")
@admin_requerido
def admin_alta():
    f = request.form
    nombre, panel_user, clave = (f.get("nombre") or "").strip(), (f.get("panel_user") or "").strip(), f.get("clave") or ""
    slug = (f.get("slug") or "").strip().lower() or None
    rubro = f.get("rubro") if f.get("rubro") in RUBROS_ALTA else None
    error = None
    if not (nombre and panel_user and rubro):
        error = "Nombre, rubro y usuario son obligatorios."
    elif len(clave) < 10:
        error = "La clave inicial tiene que tener al menos 10 caracteres."
    elif slug and (not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug) or slug in SLUGS_RESERVADOS):
        error = "Página pública inválida: minúsculas, números y guiones, y no puede ser una ruta del sistema."
    elif db.tenant_por_slug_o_usuario(slug, panel_user):
        error = "Ya existe una cuenta con ese usuario o esa página pública."
    aviso = whatsapp_de_alta(f.get("telefono"))
    if not error and not aviso:
        error = "Falta el WhatsApp del profesional (con característica, ej. 11 2345 6789): ahí le llegan los avisos."
    if error:
        flash(error)
        return redirect(url_for("admin_panel"))
    email = (f.get("email") or "").strip() or None
    datos = {"nombre": nombre, "rubro": rubro, "slug": slug, "panel_user": panel_user, "email": email,
             "aviso_telefono": aviso,
             "panel_pass_hash": bcrypt.hashpw(clave.encode(), bcrypt.gensalt()).decode(),
             "timezone": "America/Argentina/Buenos_Aires", "duracion_turno_minutos": 50,
             "recordatorio_horas_antes": 24}
    db.crear_tenant(datos)
    db.registrar_actividad(_actor_admin(), f"dio de alta a {nombre}")
    try:
        saludo = scheduler.avisar_profesional({k: v for k, v in datos.items() if k != "panel_pass_hash"},
                                              asistente_profesional.bienvenida(datos))
    except Exception:
        logger.warning("No salió la bienvenida por WhatsApp", exc_info=True)
        saludo = False
    flash(f"Se creó la cuenta de {nombre}. Pasale el usuario y la clave por un canal seguro. "
          + ("Le mandamos la bienvenida a su WhatsApp: desde ahí puede escribirle «ayuda» al asistente."
             if saludo else "La bienvenida por WhatsApp no salió (¿falta la plantilla de avisos o el número de la plataforma?)."))
    return redirect(url_for("admin_panel"))


def whatsapp_de_alta(texto: str | None) -> str | None:
    """Sólo dígitos, en formato internacional. Diez dígitos (11 2345 6789) se
    toman como celular argentino: 549 + número."""
    numero = "".join(ch for ch in texto or "" if ch.isdigit())
    if numero.startswith("0"):
        numero = numero.lstrip("0")
    if len(numero) == 10:
        numero = "549" + numero
    return numero if 11 <= len(numero) <= 15 else None


@app.post("/admin/profesionales/<tenant_id>/<accion>")
@admin_requerido
def admin_activar(tenant_id, accion):
    if accion not in ("activar", "desactivar"):
        return "No encontrado.", 404
    db.activar_tenant(tenant_id, accion == "activar")
    if accion == "desactivar":
        db.cerrar_sesiones(tenant_id)
    t = db.get_tenant_by_id(tenant_id)
    db.registrar_actividad(_actor_admin(), f"{'activó' if accion == 'activar' else 'desactivó'} la cuenta de {t['nombre'] if t else '?'}", tenant_id)
    flash("Cuenta activada." if accion == "activar" else "Cuenta desactivada: ya no puede entrar al panel.")
    return redirect(url_for("admin_panel"))


@app.post("/admin/interesados/<interesado_id>/<accion>")
@admin_requerido
def admin_interesado(interesado_id, accion):
    if accion not in ("visto", "pendiente"):
        return "No encontrado.", 404
    db.marcar_interesado_visto(interesado_id, accion == "visto")
    return redirect(url_for("admin_panel") + "#interesados")


@app.post("/admin/biblioteca/<entrada_id>/<accion>")
@admin_requerido
def admin_moderar(entrada_id, accion):
    if accion not in ("ocultar", "mostrar"):
        return "No encontrado.", 404
    admin = db.admin_por_id(session["admin_id"])
    db.moderar_biblioteca(entrada_id, accion == "ocultar", f"administración ({admin['usuario']})" if accion == "ocultar" else None)
    db.registrar_actividad(_actor_admin(), f"{'ocultó' if accion == 'ocultar' else 'volvió a mostrar'} un cuestionario de la biblioteca")
    flash("Se ocultó de la biblioteca." if accion == "ocultar" else "Volvió a la biblioteca.")
    return redirect(url_for("admin_panel"))


MIN_CLAVE = 10


def _validar_cambio_de_clave(hash_actual: str, clave_usuario: str) -> str | None:
    """Mismo límite de intentos que el login: la clave actual no se puede
    adivinar por acá."""
    f = request.form
    claves = _claves_de_intento(clave_usuario)
    bloqueo = _bloqueo_login(claves)
    if bloqueo:
        return bloqueo
    if not bcrypt.checkpw((f.get("actual") or "").encode(), hash_actual.encode()):
        _registrar_fallo(claves)
        return "La clave actual no es correcta."
    nueva = f.get("nueva") or ""
    if len(nueva) < MIN_CLAVE:
        return f"La clave nueva tiene que tener al menos {MIN_CLAVE} caracteres."
    if nueva != (f.get("repetir") or ""):
        return "Las dos claves nuevas no coinciden."
    if bcrypt.checkpw(nueva.encode(), hash_actual.encode()):
        return "La clave nueva tiene que ser distinta de la actual."
    return None


@app.post("/agenda/clave")
@login_required
def agenda_clave():
    tenant = db.get_tenant_by_id(session["tenant_id"])
    error = _validar_cambio_de_clave(tenant["panel_pass_hash"], f"panel:{tenant['panel_user'].casefold()}")
    if error:
        flash(error)
        return _volver_a_agenda()
    version = (tenant.get("sesion_version") or 0) + 1
    db.cambiar_clave("tenants", tenant["id"], "panel_pass_hash",
                     bcrypt.hashpw(request.form["nueva"].encode(), bcrypt.gensalt()).decode(), version)
    db.cerrar_sesiones(tenant["id"], excepto=session.get("sesion_id"))  # esta sigue; las demás se cierran
    db.registrar_actividad(tenant["nombre"], "cambió su clave", tenant["id"])
    _avisar_cuenta(tenant, "Se cambió la clave de tu cuenta", "Se cambió la clave de tu panel y se cerraron las demás sesiones.")
    flash("Listo, cambiaste la clave. Se cerraron las sesiones abiertas en otros dispositivos.")
    return _volver_a_agenda()


@app.post("/admin/clave")
@admin_requerido
def admin_clave():
    admin = db.admin_completo(session["admin_id"])
    error = _validar_cambio_de_clave(admin["pass_hash"], f"admin:{admin['usuario'].casefold()}")
    if not error and len(request.form.get("nueva") or "") < 12:
        error = "La clave de administración tiene que tener al menos 12 caracteres."
    if error:
        flash(error)
        return redirect(url_for("admin_panel"))
    version = (admin.get("sesion_version") or 0) + 1
    db.cambiar_clave("admins", admin["id"], "pass_hash",
                     bcrypt.hashpw(request.form["nueva"].encode(), bcrypt.gensalt()).decode(), version)
    session["admin_version"] = version
    db.registrar_actividad(_actor_admin(), "cambió su clave de administración")
    flash("Listo, cambiaste la clave. Se cerraron tus otras sesiones.")
    return redirect(url_for("admin_panel"))


@app.post("/admin/profesionales/<tenant_id>/cerrar-sesiones")
@admin_requerido
def admin_cerrar_sesiones(tenant_id):
    tenant = db.get_tenant_by_id(tenant_id)
    if not tenant:
        return "No encontrado.", 404
    db.cerrar_sesiones(tenant_id)
    db.registrar_actividad(_actor_admin(), f"cerró todas las sesiones de {tenant['nombre']}", tenant_id)
    flash(f"Se cerraron todas las sesiones de {tenant['nombre']}: tiene que volver a entrar con su clave.")
    return redirect(url_for("admin_panel"))


@app.post("/admin/profesionales/<tenant_id>/link-clave")
@admin_requerido
def admin_link_clave(tenant_id):
    """Link de un solo uso (24 h) para que la profesional ponga clave nueva.
    Se muestra una sola vez: del token sólo se guarda el hash."""
    tenant = db.get_tenant_by_id(tenant_id)
    if not tenant:
        return "No encontrado.", 404
    token = seguridad.nuevo_token()
    db.crear_enlace_clave(seguridad.hash_token(token), tenant_id,
                          (datetime.now(ZoneInfo("UTC")) + timedelta(hours=24)).isoformat())
    db.registrar_actividad(_actor_admin(), f"generó un link para clave nueva de {tenant['nombre']}", tenant_id)
    return render_template("admin_link.html", tenant=tenant, link=url_for("clave_nueva", token=token, _external=True))


def _actor_admin() -> str:
    admin = db.admin_por_id(session.get("admin_id") or "")
    return f"administración ({admin['usuario']})" if admin else "administración"


@app.post("/agenda/sesiones/<sesion_id>/cerrar")
@login_required
def agenda_sesion_cerrar(sesion_id):
    tenant = db.get_tenant_by_id(session["tenant_id"])
    db.cerrar_sesion(tenant["id"], sesion_id)
    db.registrar_actividad(tenant["nombre"], "cerró una de sus sesiones", tenant["id"])
    flash("Se cerró esa sesión.")
    if sesion_id == session.get("sesion_id"):
        session.clear()
        return redirect(url_for("login"))
    return _volver_a_agenda()


@app.post("/agenda/sesiones/cerrar-otras")
@login_required
def agenda_sesiones_cerrar_otras():
    tenant = db.get_tenant_by_id(session["tenant_id"])
    db.cerrar_sesiones(tenant["id"], excepto=session.get("sesion_id"))
    db.registrar_actividad(tenant["nombre"], "cerró sus otras sesiones", tenant["id"])
    flash("Se cerraron las otras sesiones. Esta sigue abierta.")
    return _volver_a_agenda()


@app.route("/agenda/doble-factor", methods=["GET", "POST"])
@login_required
def agenda_doble_factor():
    """Doble factor opcional para la profesional. Mismo esquema que el de
    administración: el secreto queda en la sesión hasta que un código lo confirma."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    if tenant.get("totp_secreto"):
        return redirect(url_for("agenda"))
    secreto = session.get("totp_nuevo") or seguridad.nuevo_secreto()
    session["totp_nuevo"] = secreto
    error = None
    if request.method == "POST":
        paso = seguridad.verificar(secreto, request.form.get("codigo") or "")
        if paso is not None:
            db.guardar_totp_tenant(tenant["id"], {"totp_secreto": secreto, "totp_ultimo_paso": paso})
            session.pop("totp_nuevo", None)
            db.registrar_actividad(tenant["nombre"], "activó el doble factor", tenant["id"])
            _avisar_cuenta(tenant, "Se activó el doble factor en tu cuenta",
                           "Se activó el doble factor: al entrar con la clave, el panel pide además un código del celular.")
            flash("Doble factor activado: desde ahora, al entrar con la clave te va a pedir el código de la app.")
            return redirect(url_for("agenda"))
        error = "Ese código no coincide. Fijate que la hora del celular esté bien y probá con el próximo."
    uri = seguridad.uri(secreto, tenant["panel_user"])
    return render_template("doble_factor.html", cuenta=tenant["panel_user"], clave=seguridad.agrupado(secreto), uri=uri,
                           qr=seguridad.qr_svg(uri), error=error, volver=url_for("agenda"), salir=None)


@app.post("/agenda/doble-factor/desactivar")
@login_required
def agenda_doble_factor_desactivar():
    tenant = db.get_tenant_by_id(session["tenant_id"])
    if not tenant.get("totp_secreto"):
        return _volver_a_agenda()
    claves = _claves_de_intento(f"totp-panel:{tenant['id']}")
    bloqueo = _bloqueo_login(claves)
    paso = None if bloqueo else seguridad.verificar(tenant["totp_secreto"], request.form.get("codigo") or "",
                                                     tenant.get("totp_ultimo_paso"))
    if paso is None:
        if not bloqueo:
            _registrar_fallo(claves)
        flash(bloqueo or "Para desactivarlo hace falta un código válido de la app.")
        return _volver_a_agenda()
    db.guardar_totp_tenant(tenant["id"], {"totp_secreto": None, "totp_ultimo_paso": None})
    db.registrar_actividad(tenant["nombre"], "desactivó el doble factor", tenant["id"])
    _avisar_cuenta(tenant, "Se desactivó el doble factor en tu cuenta",
                   "Se desactivó el doble factor: ahora alcanza con la clave para entrar a tu panel.")
    flash("Doble factor desactivado.")
    return _volver_a_agenda()


# Llaves de la administración: misma verificación, otra tabla. Entrar con
# llave reemplaza clave + código (la llave ya exige huella, cara o PIN).

@app.post("/admin/llaves/opciones")
@admin_requerido
def admin_llave_opciones():
    admin = db.admin_completo(session["admin_id"])
    return _opciones_registro("admin_llave_reto", f"admin:{admin['id']}".encode(), admin["usuario"],
                              f"{admin['usuario']} (administración)", [x["id"] for x in db.admin_llaves_de(admin["id"])])


@app.post("/admin/llaves/registrar")
@admin_requerido
def admin_llave_registrar():
    admin = db.admin_completo(session["admin_id"])
    v = _verificar_registro("admin_llave_reto")
    if not v:
        return jsonify(ok=False, error="No se pudo guardar la llave. Probá de nuevo."), 400
    nombre = seguridad.dispositivo(request.headers.get("User-Agent"))
    db.agregar_admin_llave({"id": bytes_to_base64url(v.credential_id), "admin_id": admin["id"],
                            "clave_publica": bytes_to_base64url(v.credential_public_key), "contador": v.sign_count,
                            "nombre": nombre})
    db.registrar_actividad(_actor_admin(), f"agregó una llave de acceso de administración ({nombre})")
    return jsonify(ok=True)


@app.post("/admin/llaves/<path:llave_id>/borrar")
@admin_requerido
def admin_llave_borrar(llave_id):
    db.borrar_admin_llave(session["admin_id"], llave_id)
    db.registrar_actividad(_actor_admin(), "borró una llave de acceso de administración")
    flash("Se borró la llave.")
    return redirect(url_for("admin_panel"))


@app.post("/admin/login/llave/opciones")
def admin_login_llave_opciones():
    return _opciones_login("admin_llave_reto_login")


@app.post("/admin/login/llave")
def admin_login_llave():
    bloqueo = _bloqueo_login([f"ip:{request.remote_addr or 'desconocida'}"])
    if bloqueo:
        return jsonify(ok=False, error=bloqueo), 429
    r = _verificar_login("admin_llave_reto_login", db.admin_llave)
    if not r:
        return jsonify(ok=False, error=ERROR_LLAVE), 400
    fila, contador = r
    admin = db.admin_completo(fila["admin_id"])
    if not admin:
        return jsonify(ok=False, error=ERROR_LLAVE), 400
    db.usar_admin_llave(fila["id"], contador, datetime.now(ZoneInfo("UTC")).isoformat())
    session.clear()
    session["admin_id"] = admin["id"]
    session["admin_version"] = admin.get("sesion_version") or 0
    db.marcar_ingreso("admins", admin["id"], datetime.now(ZoneInfo("UTC")).isoformat())
    db.registrar_actividad(f"administración ({admin['usuario']})", "entró a administración con llave de acceso")
    return jsonify(ok=True, destino=url_for("admin_panel"))


# QR para entrar a administración: igual que el del panel, pero lo aprueba un
# celular con sesión de ADMINISTRACIÓN y abre una sesión de administración.
# Crear y consultar viven bajo /admin/login (sin sesión, sin CSRF); aprobar
# vive en /admin/qr/<token>, que sí pide sesión de admin y token.

@app.post("/admin/login/qr")
def admin_login_qr_nuevo():
    token, nonce = seguridad.nuevo_token(), seguridad.nuevo_token()
    session["admin_qr_nonce"] = nonce
    vence = datetime.now(ZoneInfo("UTC")) + QR_VIGENCIA
    db.crear_qr_login(seguridad.hash_token(token), seguridad.hash_token(nonce),
                      seguridad.dispositivo(request.headers.get("User-Agent")), vence.isoformat(), tipo="admin")
    link = url_for("admin_qr_aprobar", token=token, _external=True)
    return jsonify(token=token, svg=seguridad.qr_svg(link, escala=4), segundos=int(QR_VIGENCIA.total_seconds()),
                   estado=url_for("admin_login_qr_estado", token=token))


@app.get("/admin/login/qr/<token>/estado")
def admin_login_qr_estado(token):
    q = db.qr_login(seguridad.hash_token(token))
    nonce = session.get("admin_qr_nonce") or ""
    if not q or q.get("tipo") != "admin" or not hmac.compare_digest(q["nonce_hash"], seguridad.hash_token(nonce)):
        return jsonify(estado="invalido"), 404
    if datetime.fromisoformat(q["vence"]) <= datetime.now(ZoneInfo("UTC")) or q.get("usado_at"):
        return jsonify(estado="vencido")
    if not q.get("aprobado_at"):
        return jsonify(estado="esperando")
    admin = db.admin_completo(q["admin_id"]) if q.get("admin_id") else None
    if not admin or not db.usar_qr_login(q["token_hash"], datetime.now(ZoneInfo("UTC")).isoformat()):
        return jsonify(estado="vencido")
    session.clear()
    session["admin_id"] = admin["id"]
    session["admin_version"] = admin.get("sesion_version") or 0
    db.marcar_ingreso("admins", admin["id"], datetime.now(ZoneInfo("UTC")).isoformat())
    db.registrar_actividad(f"administración ({admin['usuario']})", f"entró a administración con QR ({q.get('dispositivo')})")
    return jsonify(estado="listo", destino=url_for("admin_panel"))


@app.route("/admin/qr/<token>", methods=["GET", "POST"])
@admin_requerido
def admin_qr_aprobar(token):
    admin = db.admin_completo(session["admin_id"])
    q = db.qr_login(seguridad.hash_token(token))
    vigente = q and q.get("tipo") == "admin" and not q.get("aprobado_at") \
        and datetime.fromisoformat(q["vence"]) > datetime.now(ZoneInfo("UTC"))
    datos = {"tenant": {"nombre": f"Administración · {admin['usuario']}"}, "q": q, "volver": url_for("admin_panel")}
    if request.method == "POST" and vigente:
        if request.form.get("respuesta") == "aprobar" and \
                db.aprobar_qr_admin(q["token_hash"], admin["id"], datetime.now(ZoneInfo("UTC")).isoformat()):
            db.registrar_actividad(_actor_admin(), f"aprobó entrar a administración con QR en {q.get('dispositivo')}")
            return render_template("login_qr.html", estado="aprobado", **datos)
        return render_template("login_qr.html", estado="rechazado", **datos)
    return render_template("login_qr.html", estado="pregunta" if vigente else "vencido", **datos)


@app.get("/logout")
def logout():
    era_admin = "admin_id" in session or "admin_pendiente" in session
    session.clear()
    return redirect(url_for("admin_login" if era_admin else "login"))


def _tz_de(tenant: dict) -> ZoneInfo:
    return ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")


def _lunes_de(valor: str | None, tz: ZoneInfo) -> datetime:
    """Lunes 00:00 (hora local) de la semana que contiene `valor` (YYYY-MM-DD),
    o de la semana actual si no viene o no se entiende."""
    try:
        dia = datetime.strptime(valor or "", "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError:
        dia = datetime.now(tz)
    dia = dia.replace(hour=0, minute=0, second=0, microsecond=0)
    return dia - timedelta(days=dia.weekday())


def _meses_label(primero: date) -> str:
    return f"{MESES_LABEL[primero.month - 1]} {primero.year}"


def _mes_de(valor: str | None, hoy: date) -> date:
    """Primer día del mes `valor` (YYYY-MM), o del mes actual si no se entiende."""
    try:
        return datetime.strptime(valor or "", "%Y-%m").date()
    except ValueError:
        return hoy.replace(day=1)


def _calendario(tenant: dict, primero: date, tz: ZoneInfo, ahora: datetime, bloqueado) -> list[list[dict]]:
    """Las semanas completas (de lunes a domingo) que tocan el mes, con las
    sesiones de cada día. Trae también los días de los meses vecinos que
    completan la grilla, igual que Google Calendar."""
    siguiente = (primero + timedelta(days=32)).replace(day=1)
    inicio = primero - timedelta(days=primero.weekday())
    fin = siguiente + timedelta(days=(7 - siguiente.weekday()) % 7)
    desde = datetime.combine(inicio, dtime.min, tzinfo=tz).astimezone(ZoneInfo("UTC"))
    hasta = datetime.combine(fin, dtime.min, tzinfo=tz).astimezone(ZoneInfo("UTC"))

    por_dia: dict[date, list[dict]] = {}
    for t in db.agenda_semana(tenant["id"], desde, hasta):
        local = datetime.fromisoformat(t["inicio"]).astimezone(tz)
        por_dia.setdefault(local.date(), []).append({**t, "_hora": local.strftime("%H:%M")})

    semanas = []
    cursor = inicio
    while cursor < fin:
        semana = []
        for _ in range(7):
            turnos = por_dia.get(cursor, [])
            semana.append(
                {
                    "fecha": cursor.strftime("%Y-%m-%d"),
                    "numero": cursor.day,
                    "es_hoy": cursor == ahora.date(),
                    "bloqueado": bloqueado(cursor),
                    "de_otro_mes": cursor.month != primero.month,
                    "sesiones": [t for t in turnos if t["estado"] == "reservado"],
                    "libres": sum(t["estado"] == "libre" for t in turnos),
                    "abiertos": sum(t["estado"] == "libre" and bool(t.get("abierto")) and not t.get("hueco")
                                    for t in turnos),
                    "pasado": cursor < ahora.date(),
                }
            )
            cursor += timedelta(days=1)
        semanas.append(semana)
    return semanas


def _edad(nacimiento: str | None, hoy: date) -> int | None:
    if not nacimiento:
        return None
    n = date.fromisoformat(nacimiento)
    return hoy.year - n.year - ((hoy.month, hoy.day) < (n.month, n.day))


DIAS_FECHA_CERCA = 14
DIAS_DE_ASISTENCIA = 90
AUSENCIAS_PARA_ALERTA = 3


def _falta(dias: int) -> str:
    return {0: "hoy", 1: "mañana"}.get(dias, f"en {dias} días")


def _cumpleanios(nacimiento: str | None, hoy: date) -> str | None:
    if not nacimiento:
        return None
    n = date.fromisoformat(nacimiento)
    cae = proxima_vez(n.month, n.day, hoy)
    return f"{cae.year - n.year} años, {_falta((cae - hoy).days)}"


def _con_cuenta_regresiva(fechas: list[dict], hoy: date) -> list[dict]:
    resultado = []
    for f in fechas:
        cae = proxima_vez(f["mes"], f["dia"], hoy)
        resultado.append(
            {
                **f,
                "_dias": (cae - hoy).days,
                "_falta": _falta((cae - hoy).days),
                "_cuando": f"{f['dia']}/{f['mes']:02d}" + (f"/{f['anio']}" if f.get("anio") else ""),
                "_anios": cae.year - f["anio"] if f.get("anio") else None,
            }
        )
    return sorted(resultado, key=lambda f: f["_dias"])


CAMPOS_ANOTADOS = ("notas", "motivo_consulta", "objetivos", "como_viene", "psiquiatra", "medicacion", "plan_crisis",
                   "escuela", "grado", "docente", "equipo_orientador", "obra_social", "numero_afiliado",
                   "fecha_nacimiento", "estado_fecha")


def _fichas(tenant: dict, tz: ZoneInfo, ahora: datetime) -> list[dict]:
    """Una ficha por paciente (se identifica por teléfono, como en todo el
    sistema). Frecuencia y valor son los de su próxima sesión, o los de la
    última si no tiene ninguna agendada. La deuda se cuenta igual que en
    Pendientes de cobro."""
    por_paciente: dict[str, list[dict]] = {}
    for t in _con_hora_local(db.sesiones_reservadas(tenant["id"]), tz):
        por_paciente.setdefault(db.clave_de(t), []).append(t)
    datos = db.datos_de_pacientes(tenant["id"])
    personas: dict[str, list[dict]] = {}
    for p in db.personas_de_pacientes(tenant["id"]):
        personas.setdefault(db.clave_de(p), []).append(p)
    fechas: dict[str, list[dict]] = {}
    for f in db.fechas_de_pacientes(tenant["id"]):
        fechas.setdefault(db.clave_de(f), []).append(f)
    hoy = ahora.date()
    corte = ahora - timedelta(days=DIAS_DE_ASISTENCIA)
    canceladas: dict[str, int] = {}
    for c in db.cancelaciones_desde(tenant["id"], corte):
        canceladas[db.clave_de(c)] = canceladas.get(db.clave_de(c), 0) + 1

    lista = []
    # Quien se quedó sin sesiones sigue teniendo ficha si hay algo anotado. Si
    # sólo queda el nombre (una ficha abierta por error) no hay nada que mostrar.
    sin_sesiones = [clave for clave, d in datos.items() if clave not in por_paciente
                    and (any(d.get(c) for c in CAMPOS_ANOTADOS) or clave in personas or clave in fechas)]
    for clave in [*por_paciente, *sin_sesiones]:
        sesiones = por_paciente.get(clave, [])
        telefono = db.partir_clave(clave)[0]
        pasadas = [t for t in sesiones if datetime.fromisoformat(t["inicio"]) <= ahora]
        futuras = sesiones[len(pasadas):]
        vigente = futuras[0] if futuras else pasadas[-1] if pasadas else {}
        sin_pagar = [t for t in pasadas if t.get("pago") == "pendiente"]
        estado = datos.get(clave, {}).get("estado") or "activo"
        faltas_recientes = sum(t.get("asistencia") == "no_vino" and datetime.fromisoformat(t["inicio"]) >= corte for t in pasadas)
        canceladas_recientes = canceladas.get(clave, 0)
        alerta = alerta_de_seguimiento(sesiones, ahora) if estado == "activo" and sesiones else None
        if estado == "activo" and not alerta and faltas_recientes + canceladas_recientes >= AUSENCIAS_PARA_ALERTA:
            alerta = f"{faltas_recientes + canceladas_recientes} faltas o cancelaciones en {DIAS_DE_ASISTENCIA} días"
        por_revisar = [t for t in sesiones if t.get("revisar_ficha")]
        if por_revisar:
            alerta = f"Revisar de quién es: {len(por_revisar)} sesión{'es' if len(por_revisar) > 1 else ''}"
        nacimiento = datos.get(clave, {}).get("fecha_nacimiento")
        cerrado = datos.get(clave, {}).get("estado_fecha")
        lista.append(
            {
                "clave": clave,
                "telefono": telefono,
                "nombre": next((t["paciente_nombre"] for t in reversed(sesiones) if t.get("paciente_nombre")),
                               datos.get(clave, {}).get("nombre") or telefono),
                "frecuencia": vigente.get("frecuencia"),
                "valor": vigente.get("valor"),
                "fecha_nacimiento": nacimiento,
                "edad": _edad(nacimiento, hoy),
                "cumpleanios": _cumpleanios(nacimiento, hoy),
                "notas": datos.get(clave, {}).get("notas"),
                "motivo_consulta": datos.get(clave, {}).get("motivo_consulta"),
                "objetivos": datos.get(clave, {}).get("objetivos"),
                "como_viene": datos.get(clave, {}).get("como_viene"),
                "psiquiatra": datos.get(clave, {}).get("psiquiatra"),
                "medicacion": datos.get(clave, {}).get("medicacion"),
                "plan_crisis": datos.get(clave, {}).get("plan_crisis"),
                "escuela": datos.get(clave, {}).get("escuela"),
                "grado": datos.get(clave, {}).get("grado"),
                "docente": datos.get(clave, {}).get("docente"),
                "equipo_orientador": datos.get(clave, {}).get("equipo_orientador"),
                "obra_social": datos.get(clave, {}).get("obra_social"),
                "numero_afiliado": datos.get(clave, {}).get("numero_afiliado"),
                "personas": personas.get(clave, []),
                "fechas": _con_cuenta_regresiva(fechas.get(clave, []), hoy),
                "desde": datetime.fromisoformat(sesiones[0]["inicio"]).astimezone(tz).strftime("%d/%m/%Y") if sesiones else None,
                "proxima": f"{futuras[0]['_dia']} {futuras[0]['_hora']}" if futuras else None,
                "asistio": sum(t.get("asistencia") == "vino" for t in pasadas),
                "falto": sum(t.get("asistencia") == "no_vino" for t in pasadas),
                "sin_pagar": len(sin_pagar),
                "adeudado": sum(t.get("valor") or 0 for t in sin_pagar),
                "historial": pasadas[:-11:-1],
                "proximas": futuras,
                "por_revisar": por_revisar,
                "ultima_nota": next((t for t in reversed(pasadas) if t.get("nota")), None),
                "estado": estado,
                "estado_fecha": date.fromisoformat(cerrado).strftime("%d/%m/%Y") if cerrado else None,
                "estado_motivo": datos.get(clave, {}).get("estado_motivo"),
                "quedan": sesiones_que_quedan(sesiones, ahora) if estado == "activo" and sesiones else None,
                "alerta": alerta,
                "faltas_recientes": faltas_recientes,
                "canceladas_recientes": canceladas_recientes,
                "sesiones": sesiones,
                "sin_sesiones": not sesiones,
                "recordado_hoy": datos.get(clave, {}).get("ultimo_recordatorio_pago") == hoy.isoformat(),
            }
        )
    if rubros.de(tenant)["tests"]:
        tomados: dict[str, list[dict]] = {}
        for x in db.tests_tomados(tenant["id"]):
            x["_fecha"] = date.fromisoformat(x["fecha"]).strftime("%d/%m/%Y")
            tomados.setdefault(db.clave_de(x), []).append(x)
        leidas: dict[str, list[dict]] = {}
        for x in db.lecturas(tenant["id"]):
            x["_fecha"] = date.fromisoformat(x["fecha"]).strftime("%d/%m/%Y")
            leidas.setdefault(db.clave_de(x), []).append(x)
        for f in lista:
            f["tests_tomados"] = tomados.get(f["clave"], [])
            f["tests_por_test"] = resultados_tests.por_test(f["tests_tomados"])
            f["fluidez"] = fluidez.para_ficha(leidas.get(f["clave"], []))
            f["para_repetir"] = (tests_sugeridos.para_repetir(f["tests_tomados"], hoy, tenant.get("plazos_tests"))
                                 if f["estado"] == "activo" else [])
            nacimiento = f["fecha_nacimiento"]
            edad = (hoy - date.fromisoformat(nacimiento)).days / 365.25 if nacimiento else None
            textos = [f["motivo_consulta"], f["objetivos"], f["como_viene"], f["notas"], *[t.get("nota") for t in f["sesiones"]]]
            f["tests_sugeridos"] = tests_sugeridos.sugerir(textos, edad, f["tests_tomados"], hoy,
                                                             tenant.get("plazos_tests"))
    if rubros.de(tenant)["cuestionarios"]:
        por_clave: dict[str, list[dict]] = {}
        for c in db.cuestionarios(tenant["id"]):
            por_clave.setdefault(db.clave_de(c), []).append(c)
        for f in lista:
            f["cuestionarios"] = _cuestionarios_de_ficha(por_clave.get(f["clave"], []), tz, ahora)
    por_clave_ent: dict[str, list[dict]] = {}
    ahora_utc = datetime.now(ZoneInfo("UTC"))
    for e in db.entregas(tenant["id"]):
        e["_creada"] = datetime.fromisoformat(e["created_at"]).astimezone(tz).strftime("%d/%m %H:%M") if "T" in e["created_at"] else e["created_at"]
        e["_abierta"] = datetime.fromisoformat(e["abierta_at"]).astimezone(tz).strftime("%d/%m/%Y %H:%M") if e.get("abierta_at") else None
        e["_vencida"] = not e.get("abierta_at") and datetime.fromisoformat(e["vence"]) <= ahora_utc
        e["link"] = url_for("entrega_publica", token=e["token"], _external=True)
        por_clave_ent.setdefault(db.clave_de(e), []).append(e)
    por_clave_inf: dict[str, list[dict]] = {}
    for x in db.informes(tenant["id"]):
        x["fecha"] = datetime.fromisoformat(x["created_at"]).astimezone(tz).strftime("%d/%m/%Y %H:%M") if "T" in x["created_at"] else x["created_at"]
        por_clave_inf.setdefault(db.clave_de(x), []).append(x)
    for f in lista:
        f["entregas"] = por_clave_ent.get(f["clave"], [])
        f["informes"] = por_clave_inf.get(f["clave"], [])
        for n, x in enumerate(reversed(f["informes"]), start=1):
            x["version"] = n
    lista.sort(key=lambda f: f["nombre"].casefold())
    for i, f in enumerate(lista, start=1):
        f["dom"] = f"ficha-{i}"
        f["hermanos"] = [{"clave": o["clave"], "nombre": o["nombre"], "desde": o["desde"]} for o in lista
                         if o["telefono"] == f["telefono"] and o is not f]
    return lista


def _cobros(fichas: list[dict], primero: date, tz: ZoneInfo, ahora: datetime) -> dict:
    """Las cuentas del mes. Lo adeudado por paciente es histórico, no del mes:
    una deuda de marzo se sigue debiendo en abril."""
    cuentas = {"cobrado": 0, "a_cobrar": 0, "agendado": 0, "sin_valor": 0,
               "sesiones": 0, "ausencias": 0, "pacientes": 0, "nuevos": 0, "cobrado_antes": 0}
    anterior = (primero - timedelta(days=1)).replace(day=1)
    for ficha in fichas:
        atendido = False
        for i, t in enumerate(ficha["sesiones"]):
            inicio = datetime.fromisoformat(t["inicio"]).astimezone(tz)
            if (inicio.year, inicio.month) == (anterior.year, anterior.month) and t.get("pago") == "pagado":
                cuentas["cobrado_antes"] += t.get("valor") or 0
            if (inicio.year, inicio.month) != (primero.year, primero.month):
                continue
            if inicio <= ahora:
                if t.get("asistencia") == "no_vino":
                    cuentas["ausencias"] += 1
                else:
                    cuentas["sesiones"] += 1
                    atendido = True
                    cuentas["nuevos"] += i == 0
            if t.get("pago") == "no_cobra":
                continue
            if t.get("valor") is None:
                cuentas["sin_valor"] += 1
            elif t.get("pago") == "pagado":
                cuentas["cobrado"] += t["valor"]
            elif inicio <= ahora:
                cuentas["a_cobrar"] += t["valor"]
            else:
                cuentas["agendado"] += t["valor"]
        cuentas["pacientes"] += atendido
    return {
        **cuentas,
        "titulo": _meses_label(primero),
        "mes": primero.strftime("%Y-%m"),
        "anterior": MESES_LABEL[anterior.month - 1].lower(),
        "deuda_total": sum(f["adeudado"] for f in fichas),
        "deudores": sorted((f for f in fichas if f["adeudado"]), key=lambda f: -f["adeudado"]),
    }


def _fechas_semana(fichas: list[dict]) -> list[dict]:
    cercanas = [{"ficha": f, "fecha": c} for f in fichas if f["estado"] == "activo"
                for c in f["fechas"] if c["_dias"] <= scheduler.DIAS_AVISO]
    return sorted(cercanas, key=lambda x: x["fecha"]["_dias"])


def _bloqueos(tenant: dict, fichas: list[dict], tz: ZoneInfo, ahora: datetime) -> list[dict]:
    """Bloqueos vigentes y a quién agarran: bloquear no cancela nada, el
    profesional decide qué hacer con cada sesión."""
    lista = []
    for b in db.bloqueos_vigentes(tenant["id"], ahora):
        desde = datetime.fromisoformat(b["desde"])
        hasta = datetime.fromisoformat(b["hasta"])
        ultimo = (hasta.astimezone(tz) - timedelta(days=1)).date()
        d, h = desde.astimezone(tz), hasta.astimezone(tz)
        if d.time() == dtime.min and h.time() == dtime.min:
            texto = d.strftime("%d/%m") + (f" al {ultimo.strftime('%d/%m')}" if ultimo != d.date() else "")
        elif d.date() == h.date():
            texto = f"{DIAS_LABEL[d.weekday()]} {d.strftime('%d/%m')} de {d.strftime('%H:%M')} a {h.strftime('%H:%M')}"
        else:
            texto = f"{d.strftime('%d/%m %H:%M')} al {h.strftime('%d/%m %H:%M')}"
        lista.append({
            **b, "_desde": desde.astimezone(tz).date(), "_hasta": ultimo, "_texto": texto,
            "_afectados": [
                {"ficha": f, "cuando": f"{t['_dia']} {t['_hora']}"}
                for f in fichas for t in f["sesiones"]
                if desde <= datetime.fromisoformat(t["inicio"]) < hasta and datetime.fromisoformat(t["inicio"]) > ahora
            ],
        })
    return lista


def _hoy(tenant: dict, fichas: list[dict], tz: ZoneInfo, ahora: datetime) -> list[dict]:
    """Lo que el profesional quiere repasar antes de cada sesión del día."""
    inicio = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    utc = ZoneInfo("UTC")
    turnos = db.agenda_semana(tenant["id"], inicio.astimezone(utc), (inicio + timedelta(days=1)).astimezone(utc))
    por_clave = {f["clave"]: f for f in fichas}
    filas = []
    for t in _con_hora_local([t for t in turnos if t["estado"] == "reservado"], tz):
        ficha = por_clave.get(db.clave_de(t))
        if ficha:
            cerca = [c for c in ficha["fechas"] if c["_dias"] <= DIAS_FECHA_CERCA]
            filas.append({"hora": t["_hora"], "asistencia": t.get("asistencia"), "ficha": ficha, "fechas": cerca})
    return filas


def _cuestionarios_de_ficha(filas: list[dict], tz: ZoneInfo, ahora: datetime) -> dict:
    """Pendientes (con su link) y respondidos agrupados por instrumento, con
    la evolución del puntaje. filas viene de la más nueva a la más vieja."""
    pendientes, por_inst, abiertos = [], {}, []
    for c in filas:
        d = cuestionarios.definicion(c)
        if not d:
            continue
        c["nombre"] = d["nombre"]
        if c.get("respondido_at"):
            c["_fecha"] = datetime.fromisoformat(c["respondido_at"]).astimezone(tz).strftime("%d/%m/%Y")
            if d["tipo"] == "abierto":
                c["pares"] = [(p, r) for p, r in zip(d["preguntas"], c.get("respuestas") or []) if r]
                abiertos.append(c)
            else:
                # Los propios se agrupan por plantilla: dos plantillas distintas no se comparan.
                grupo = c["instrumento"] if d["tipo"] != "propio" else f"propio:{c['plantilla'].get('id')}"
                por_inst.setdefault(grupo, []).append((c, d))
        elif datetime.fromisoformat(c["vence"]) > ahora:
            c["_vence"] = datetime.fromisoformat(c["vence"]).astimezone(tz).strftime("%d/%m")
            c["link"] = url_for("cuestionario_publico", token=c["token"], _external=True)
            pendientes.append(c)
    grupos = []
    for pares in por_inst.values():
        rs = [c for c, _ in pares]
        inst = pares[0][1]
        for nueva, vieja in zip(rs, rs[1:]):
            nueva["cambio"] = nueva["puntaje"] - vieja["puntaje"]
        plantilla = rs[0].get("plantilla") or {}
        grupos.append({"nombre": inst["nombre"], "que_mide": inst["que_mide"], "respuestas": rs,
                       "maximo": inst["maximo"], "base": plantilla.get("nombre"), "version": plantilla.get("version"),
                       "grafico": fluidez.grafico([r["puntaje"] for r in reversed(rs)])})
    return {"pendientes": pendientes, "grupos": grupos, "abiertos": abiertos,
            "comparaciones": cuestionarios.comparar_versiones(grupos),
            "alerta": any(r.get("alerta") and not r.get("visto") for g in grupos for r in g["respuestas"])}


def _volver_a_agenda():
    """Después de tocar una sesión, volver a la misma semana que se estaba
    mirando (viene como campo oculto del form). Se reconstruye la URL con
    url_for en vez de usar el Referer: así nunca redirige afuera del panel."""
    semana = request.form.get("semana") or ""
    try:
        datetime.strptime(semana, "%Y-%m-%d")
    except ValueError:
        mes = request.form.get("mes") or ""
        try:
            datetime.strptime(mes, "%Y-%m")
        except ValueError:
            return redirect(url_for("agenda"))
        return redirect(url_for("agenda", mes=mes))
    return redirect(url_for("agenda", semana=semana))


def _se_superponen(a: dict, b: dict) -> bool:
    return (
        datetime.fromisoformat(a["inicio"]) < datetime.fromisoformat(b["fin"])
        and datetime.fromisoformat(b["inicio"]) < datetime.fromisoformat(a["fin"])
    )


@app.get("/agenda")
@login_required
def agenda():
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    ahora = datetime.now(tz)
    pendientes = _con_hora_local(db.pendientes_de_pago(tenant["id"], ahora.astimezone(ZoneInfo("UTC"))), tz)
    fichas = _fichas(tenant, tz, ahora)
    bloqueos = _bloqueos(tenant, fichas, tz, ahora)
    bloqueado = lambda dia: next((b for b in bloqueos if b["_desde"] <= dia <= b["_hasta"]), None)
    plantillas = db.plantillas_cuestionario(tenant["id"])
    for p in plantillas:
        c = p["contenido"]
        p["faltan"] = sum(not x.strip() for x in c["items"])
        p["form"] = cuestionarios.a_formulario(c)
        p["titulo"] = c["nombre"] + (f" · {c['version']}" if c.get("version") else "")
    comunes = {
        "tenant": tenant,
        "pendientes": pendientes,
        "por_cobrar": sum(t.get("valor") or 0 for t in pendientes),
        "fichas": fichas,
        "catalogo_tests": [nombre for nombre, *_ in tests_sugeridos.CATALOGO],
        "tipos_resultado": [(k, v[0]) for k, v in resultados_tests.TIPOS.items()],
        "filas_vacias": resultados_tests.FILAS_VACIAS,
        "textos_lectura": [dict(t, palabras=len(fluidez.palabras(t["texto"]))) for t in db.textos_lectura(tenant["id"])]
                          if rubros.de(tenant)["tests"] else [],
        "plazos_tests": [{"nombre": n, "meses": m, "fabrica": f, "propio": n in (tenant.get("plazos_tests") or {})}
                         for (n, m), (_, _, _, f, _) in zip(tests_sugeridos.plazos(tenant.get("plazos_tests")).items(),
                                                            tests_sugeridos.CATALOGO)],
        "hoy_iso": ahora.date().isoformat(),
        "cuestionarios_nuevos": sorted(({"ficha": f, "c": r} for f in fichas
                                        for r in [*(r for g in (f.get("cuestionarios") or {}).get("grupos", []) for r in g["respuestas"]),
                                                  *(f.get("cuestionarios") or {}).get("abiertos", [])]
                                        if not r.get("visto")),
                                       key=lambda x: (not x["c"].get("alerta"), x["c"]["respondido_at"])),
        "instrumentos": [(k, cuestionarios.INSTRUMENTOS.get(k) or cuestionarios.ABIERTOS[k])
                         for k in rubros.de(tenant)["cuestionarios"]]
                        + [(f"propio:{p['id']}", {"nombre": p["titulo"], "que_mide": p["contenido"].get("que_mide") or "propio"})
                           for p in plantillas if cuestionarios.completa(p["contenido"])]
                        + [(f"todas:{base}", {"nombre": f"{nombre} · todas las versiones", "que_mide": ", ".join(vs)})
                           for base, (nombre, vs) in cuestionarios.familias(plantillas).items()],
        "plantillas": plantillas,
        "plantillas_informe": db.plantillas_informe(tenant["id"]),
        "biblioteca": [dict(e, propia=e["autor_tenant_id"] == tenant["id"],
                            titulo=e["contenido"]["nombre"] + (f" · {e['contenido']['version']}" if e["contenido"].get("version") else ""))
                       for e in _biblioteca_visible(tenant)],
        "moderador": bool(tenant.get("moderador")),
        "bloqueo_reciente": _bloqueo_reciente(tenant, tz),
        "mis_llaves": [dict(x, _creada=datetime.fromisoformat(x["creada"]).astimezone(tz).strftime("%d/%m/%Y"),
                            _ultima=datetime.fromisoformat(x["ultima"]).astimezone(tz).strftime("%d/%m %H:%M") if x.get("ultima") else "nunca")
                       for x in db.llaves_de(tenant["id"])],
        "mis_sesiones": [dict(x, actual=x["id"] == session.get("sesion_id"),
                              _desde=datetime.fromisoformat(x["creada"]).astimezone(tz).strftime("%d/%m %H:%M"),
                              _ultima=datetime.fromisoformat(x["ultima"]).astimezone(tz).strftime("%d/%m %H:%M"))
                         for x in db.sesiones_de(tenant["id"])],
        "tests_para_repetir": [{"ficha": f, **t} for f in fichas for t in f.get("para_repetir") or []],
        "ventana_aviso": tests_sugeridos.VENTANA_AVISO,
        "reservas_nuevas": sorted(({"ficha": f, "turno": t} for f in fichas for t in f["sesiones"] if t.get("reserva_nueva")),
                                  key=lambda r: r["turno"]["inicio"]),
        "espera": db.lista_de_espera(tenant["id"]),
        "huecos": _con_hora_local(db.huecos(tenant["id"]), tz),
        "horario_por_dia": {d: [r for r in scheduler.rangos_horario(tenant) if r[0] == d] for d in range(7)},
        "dias_label": DIAS_LABEL,
        "max_sedes": MAX_SEDES, "max_tipos": MAX_TIPOS, "max_franjas": MAX_FRANJAS,
        "estadisticas": _estadisticas_pagina(tenant, tz),
        "pagos_revisar": (revisar := _pagos_para_revisar(tenant, tz)),
        "motivos_rechazo": pagos.MOTIVOS_RECHAZO,
        "mensajes": _mensajes_pacientes(tenant, tz),
        "cobros_web": _cobros_web(tenant, _mes_de(request.args.get("mes") or request.args.get("semana", "")[:7], ahora.date()),
                                  tz, revisar),
        "devolucion_horas": DEVOLUCION_HORAS,
        "mp_disponible": pagos.mp_disponible(),
        "mp_prueba": config.MP_PRUEBA,
        "version_panel": _version_panel(tenant["id"]),
        "cobro_activo": pagos.activo(tenant),
        "bloqueos": bloqueos,
        "hoy_iso": ahora.date().isoformat(),
        "mes_actual": ahora.strftime("%Y-%m"),
        "hoy": _hoy(tenant, fichas, tz, ahora),
        "fechas_semana": _fechas_semana(fichas),
        "cobros": _cobros(fichas, _mes_de(request.args.get("mes") or request.args.get("semana", "")[:7], ahora.date()), tz, ahora),
        "meses": MESES_LABEL,
        "duracion_default": tenant.get("duracion_turno_minutos") or 50,
    }

    # El calendario es la vista de entrada; la semana (con las acciones de
    # cada sesión) se abre al tocar un día.
    if not request.args.get("semana"):
        primero = _mes_de(request.args.get("mes"), ahora.date())
        return render_template(
            "agenda.html",
            rubro=rubros.de(tenant),
            vista="mes",
            semanas=_calendario(tenant, primero, tz, ahora, bloqueado),
            mes_titulo=_meses_label(primero),
            mes_mirado=primero.strftime("%Y-%m"),
            mes_anterior=(primero - timedelta(days=1)).strftime("%Y-%m"),
            mes_siguiente=(primero + timedelta(days=32)).strftime("%Y-%m"),
            semana=_lunes_de(None, tz).strftime("%Y-%m-%d"),
            **comunes,
        )

    desde = _lunes_de(request.args.get("semana"), tz)
    hasta = desde + timedelta(days=7)
    turnos = db.agenda_semana(tenant["id"], desde.astimezone(ZoneInfo("UTC")), hasta.astimezone(ZoneInfo("UTC")))

    por_dia: dict[str, list[dict]] = {}
    reservados = [t for t in turnos if t["estado"] == "reservado"]
    resumen = {"sesiones": 0, "vino": 0, "no_vino": 0, "pagado": 0}
    for t in turnos:
        inicio_local = datetime.fromisoformat(t["inicio"]).astimezone(tz)
        clave = inicio_local.strftime("%Y-%m-%d")
        extra = {"_hora": inicio_local.strftime("%H:%M"), "_paso": inicio_local <= ahora}
        if t.get("reprogramado_desde"):
            antes = datetime.fromisoformat(t["reprogramado_desde"]).astimezone(tz)
            extra["_reprogramado_desde"] = f"{DIAS_LABEL[antes.weekday()]} {antes.day}/{antes.month:02d} {antes:%H:%M}"
        if t["estado"] == "libre":
            extra["_pisado"] = any(_se_superponen(t, r) for r in reservados)
        por_dia.setdefault(clave, []).append({**t, **extra})
        if t["estado"] == "reservado":
            resumen["sesiones"] += 1
            if t.get("asistencia") in ("vino", "no_vino"):
                resumen[t["asistencia"]] += 1
            if t.get("pago") == "pagado":
                resumen["pagado"] += 1

    dias = []
    cursor = desde
    while cursor < hasta:
        clave = cursor.strftime("%Y-%m-%d")
        dias.append(
            {
                "fecha": clave,
                "titulo": f"{DIAS_LABEL[cursor.weekday()]} {cursor.day}/{cursor.month:02d}",
                "es_hoy": clave == ahora.strftime("%Y-%m-%d"),
                "bloqueado": bloqueado(cursor.date()),
                "turnos": por_dia.get(clave, []),
            }
        )
        cursor += timedelta(days=1)

    return render_template(
        "agenda.html",
        rubro=rubros.de(tenant),
        vista="semana",
        dias=dias,
        resumen=resumen,
        semana=desde.strftime("%Y-%m-%d"),
        semana_titulo=f"{desde.day}/{desde.month:02d} – {(hasta - timedelta(days=1)).day}/{(hasta - timedelta(days=1)).month:02d}",
        semana_anterior=(desde - timedelta(days=7)).strftime("%Y-%m-%d"),
        semana_siguiente=hasta.strftime("%Y-%m-%d"),
        **comunes,
    )


@app.post("/agenda/generar")
@login_required
def agenda_generar():
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)

    fecha_desde = datetime.strptime(request.form["fecha_desde"], "%Y-%m-%d").date()
    fecha_hasta = datetime.strptime(request.form["fecha_hasta"], "%Y-%m-%d").date()
    hora_inicio = dtime.fromisoformat(request.form["hora_inicio"])
    hora_fin = dtime.fromisoformat(request.form["hora_fin"])
    duracion = int(request.form.get("duracion_minutos") or tenant.get("duracion_turno_minutos") or 50)
    descanso = int(request.form.get("descanso_minutos") or tenant.get("descanso_minutos") or 0)
    dias_semana = {int(d) for d in request.form.getlist("dias_semana")}

    inicios = []
    cursor = fecha_desde
    while cursor <= fecha_hasta:
        if cursor.weekday() in dias_semana:
            paso = datetime.combine(cursor, hora_inicio, tzinfo=tz)
            fin_del_dia = datetime.combine(cursor, hora_fin, tzinfo=tz)
            while paso + timedelta(minutes=duracion) <= fin_del_dia:
                inicios.append(paso.astimezone(ZoneInfo("UTC")))
                paso += timedelta(minutes=duracion + descanso)
        cursor += timedelta(days=1)

    db.volver_a_ofrecer(tenant["id"], inicios)
    creados = db.crear_turnos_libres(tenant["id"], inicios, duracion, a_mano=True)
    logger.info("Tenant %s generó %s turnos libres", tenant["id"], creados)
    return redirect(url_for("agenda"))


@app.post("/agenda/bloqueo")
@login_required
def agenda_bloqueo():
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    try:
        desde = date.fromisoformat(request.form.get("desde") or "")
        hasta = date.fromisoformat(request.form.get("hasta") or request.form.get("desde") or "")
    except ValueError:
        flash("Elegí desde y hasta cuándo.")
        return _volver_a_agenda()
    if hasta < desde:
        flash("La fecha final no puede ser anterior a la inicial.")
        return _volver_a_agenda()
    utc = ZoneInfo("UTC")
    # Con horas: un rato (ej. martes de 14 a 16). Sin horas: días enteros.
    try:
        hora_desde = dtime.fromisoformat(request.form["hora_desde"]) if request.form.get("hora_desde") else None
        hora_hasta = dtime.fromisoformat(request.form["hora_hasta"]) if request.form.get("hora_hasta") else None
    except ValueError:
        flash("Revisá las horas.")
        return _volver_a_agenda()
    if hora_desde or hora_hasta:
        inicio = datetime.combine(desde, hora_desde or dtime.min, tzinfo=tz)
        fin = datetime.combine(hasta, hora_hasta, tzinfo=tz) if hora_hasta else datetime.combine(hasta + timedelta(days=1), dtime.min, tzinfo=tz)
        if fin <= inicio:
            flash("La hora final tiene que ser posterior a la inicial.")
            return _volver_a_agenda()
        mensaje = "Horario bloqueado. Mirá en la tarjeta si hay sesiones para reprogramar."
    else:
        inicio = datetime.combine(desde, dtime.min, tzinfo=tz)
        fin = datetime.combine(hasta + timedelta(days=1), dtime.min, tzinfo=tz)
        mensaje = "Días bloqueados. Mirá en la tarjeta si hay sesiones para reprogramar."
    db.agregar_bloqueo(tenant["id"], inicio.astimezone(utc), fin.astimezone(utc),
                       (request.form.get("motivo") or "").strip() or None)
    flash(mensaje)
    return _volver_a_agenda()


@app.post("/agenda/bloqueo/<bloqueo_id>/borrar")
@login_required
def agenda_bloqueo_borrar(bloqueo_id):
    db.borrar_bloqueo(session["tenant_id"], bloqueo_id)
    return _volver_a_agenda()


@app.post("/agenda/cancelar/<turno_id>")
@login_required
def agenda_cancelar(turno_id):
    # db.cancelar_turno() filtra por tenant_id, así que un tenant nunca puede
    # tocar un turno que no es suyo aunque adivine el id de otro.
    antes = db.turno_por_id(session["tenant_id"], turno_id)
    db.cancelar_turno(session["tenant_id"], turno_id, "profesional")
    if antes and antes.get("pago_web") == "aprobado":
        try:
            monto, _ = pagos.al_cancelar(db.get_tenant_by_id(session["tenant_id"]), antes, quien="profesional")
        except Exception:
            logger.exception("No se pudo anotar la devolución del turno %s", turno_id)
            monto = 0
        if monto:
            flash(f"Esa sesión estaba pagada por la web: quedó en «Pagos para revisar» para devolverle {_pesos(monto)}.")
    esperando = len(db.lista_de_espera(session["tenant_id"]))
    if esperando:
        flash(f"Se liberó el horario. Tenés {esperando} en lista de espera.")
    return _volver_a_agenda()


FRECUENCIAS = {"semanal": 7, "quincenal": 14}
MAX_SESIONES_FIJAS = 26
SESIONES_AL_RENOVAR = 8
MODALIDADES = ("presencial", "virtual")
MAX_AUMENTO_PORCENTAJE = 300

# Seguimiento de cada sesión: si vino, si pagó, y pasarla a otro horario.
# Igual que cancelar, todo va por db.* con el tenant_id de la sesión de
# login — un id de turno ajeno simplemente no matchea ninguna fila.

@app.post("/agenda/asistencia/<turno_id>")
@login_required
def agenda_asistencia(turno_id):
    valor = request.form.get("asistencia") or None
    if valor not in db.ASISTENCIAS:
        return "asistencia inválida", 400
    db.marcar_asistencia(session["tenant_id"], turno_id, valor)
    return _volver_a_agenda()


@app.post("/agenda/pago/<turno_id>")
@login_required
def agenda_pago(turno_id):
    valor = request.form.get("pago")
    if valor not in db.PAGOS:
        return "pago inválido", 400
    db.marcar_pago(session["tenant_id"], turno_id, valor)
    return _volver_a_agenda()


def _avisar_cambio(tenant: dict, turno: dict, nuevo_inicio: datetime, tz: ZoneInfo) -> bool:
    """Avisa al paciente el horario nuevo. Es aparte del recordatorio: si la
    sesión es en dos semanas, sin esto se enteraría recién la noche anterior."""
    numero = whatsapp.numero_de(tenant)
    if not numero:
        return False
    telefono = turno["paciente_telefono"]
    cuando = scheduler.cuando_de({**turno, "inicio": nuevo_inicio.isoformat()}, tenant, tz)
    if config.REPROGRAMADO_TEMPLATE:
        return whatsapp.enviar_plantilla(
            numero, telefono, config.REPROGRAMADO_TEMPLATE,
            config.RECORDATORIO_TEMPLATE_LANG, [tenant["nombre"], cuando],
        )
    texto = (
        f"Tu turno con *{tenant['nombre']}* cambió de horario: ahora es el {cuando}. "
        "Si no podés ir, respondé *cancelar*."
    )
    return whatsapp.enviar_texto(numero, telefono, texto)


@app.post("/agenda/reprogramar/<turno_id>")
@login_required
def agenda_reprogramar(turno_id):
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    try:
        fecha = datetime.strptime(request.form.get("fecha") or "", "%Y-%m-%d").date()
        hora = dtime.fromisoformat(request.form.get("hora") or "")
    except ValueError:
        flash("Elegí día y hora para pasar la sesión.")
        return _volver_a_agenda()
    nuevo_inicio = datetime.combine(fecha, hora, tzinfo=tz).astimezone(ZoneInfo("UTC"))
    duracion = tenant.get("duracion_turno_minutos") or 50
    original = db.turno_por_id(tenant["id"], turno_id)
    error = db.reprogramar_turno(tenant["id"], turno_id, nuevo_inicio, duracion, ignorar_bloqueo=True)
    if error:
        flash(error)
        return _volver_a_agenda()
    aviso = ""
    if request.form.get("avisar") and nuevo_inicio > datetime.now(ZoneInfo("UTC")):
        if not db.tiene_whatsapp((original or {}).get("paciente_telefono")):
            aviso = " No tiene WhatsApp cargado: avisale vos."
        elif _avisar_cambio(tenant, original, nuevo_inicio, tz):
            aviso = " Le avisamos al paciente."
        else:
            aviso = " No se le pudo avisar al paciente: escribile vos."
    flash(f"Sesión pasada al {DIAS_LABEL[fecha.weekday()]} {fecha.day}/{fecha.month:02d} a las {hora:%H:%M}.{aviso}")
    _avisar_si_bloqueado(tenant, nuevo_inicio.isoformat(), (nuevo_inicio + timedelta(minutes=duracion)).isoformat())
    # Se va a la semana del horario NUEVO: es donde el profesional quiere
    # ver que quedó bien.
    return redirect(url_for("agenda", semana=fecha.strftime("%Y-%m-%d")))


def _valor_del_form() -> int | None:
    digitos = "".join(ch for ch in (request.form.get("valor") or "") if ch.isdigit())
    return int(digitos) if digitos else None


@app.post("/agenda/valor/<turno_id>")
@login_required
def agenda_valor(turno_id):
    db.marcar_valor(session["tenant_id"], turno_id, _valor_del_form())
    return _volver_a_agenda()


def _con_aumento(valor: int | None, modo: str, monto: int) -> int | None:
    """El porcentaje redondea a la centena: nadie cobra $ 27.183."""
    if modo == "valor":
        return monto
    if valor is None:
        return None
    return (valor * (100 + monto) + 5000) // 10000 * 100


@app.post("/agenda/aumento")
@login_required
def agenda_aumento():
    """Sólo sesiones futuras sin pagar: lo ya cobrado o pasado no se toca."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    modo = request.form.get("modo")
    telefonos = set(request.form.getlist("pacientes"))
    monto = _valor_del_form()
    try:
        desde = date.fromisoformat(request.form.get("desde") or "")
    except ValueError:
        desde = None
    hoy = datetime.now(tz).date()
    if modo not in ("porcentaje", "valor") or not monto or not desde:
        flash("Completá el aumento y desde cuándo rige.")
    elif modo == "porcentaje" and monto > MAX_AUMENTO_PORCENTAJE:
        flash(f"El aumento no puede superar el {MAX_AUMENTO_PORCENTAJE} %.")
    elif desde < hoy:
        flash("El aumento no puede regir desde una fecha pasada.")
    elif not telefonos:
        flash("Elegí al menos un paciente.")
    else:
        # Con `desde` = hoy, una sesión de esta mañana ya pasó y no se toca.
        inicio = max(datetime.combine(desde, dtime.min, tzinfo=tz), datetime.now(tz)).astimezone(ZoneInfo("UTC"))
        cambiadas = 0
        for t in db.sesiones_sin_pagar_desde(tenant["id"], inicio):
            nuevo = _con_aumento(t.get("valor"), modo, monto)
            if db.clave_de(t) in telefonos and nuevo != t.get("valor"):
                db.marcar_valor(tenant["id"], t["id"], nuevo)
                cambiadas += 1
        flash(f"Se actualizó el valor de {cambiadas} sesiones desde el {desde:%d/%m/%Y}.")
    return _volver_a_agenda()


@app.post("/agenda/espera")
@login_required
def agenda_espera():
    nombre = (request.form.get("nombre") or "").strip()
    telefono = "".join(ch for ch in (request.form.get("telefono") or "") if ch.isdigit())
    if not nombre or not telefono:
        flash("Para la lista de espera hacen falta nombre y WhatsApp.")
        return _volver_a_agenda()
    if not db.agregar_a_espera(session["tenant_id"], nombre, telefono, (request.form.get("preferencia") or "").strip() or None):
        flash("Ese WhatsApp ya estaba en la lista de espera.")
    return _volver_a_agenda()


@app.post("/agenda/espera/<espera_id>/quitar")
@login_required
def agenda_espera_quitar(espera_id):
    db.quitar_de_espera(session["tenant_id"], espera_id)
    return _volver_a_agenda()


@app.post("/agenda/espera/<espera_id>/ofrecer")
@login_required
def agenda_espera_ofrecer(espera_id):
    """Le ofrece el próximo horario libre. Lo dispara el profesional: él sabe
    a quién le conviene ese horario, y no se manda solo a toda la lista."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    hoy = datetime.now(tz).date().isoformat()
    persona = next((p for p in db.lista_de_espera(tenant["id"]) if p["id"] == espera_id), None)
    libres = db.turnos_libres(tenant["id"], datetime.now(ZoneInfo("UTC")), limite=1)
    if not persona:
        flash("Esa persona ya no está en la lista.")
    elif persona.get("ofrecido_en") == hoy:
        flash(f"A {persona['nombre']} ya se le ofreció un horario hoy.")
    elif not libres:
        flash("No hay ningún horario libre para ofrecer.")
    elif not tenant.get("slug"):
        flash("Falta la página pública de reservas para mandarle el link.")
    else:
        db.marcar_ofrecido(tenant["id"], espera_id, hoy)
        cuando = scheduler._fmt(libres[0]["inicio"], tz)
        link = url_for("publico", slug=tenant["slug"], _external=True, _scheme="https")
        if cancelaciones.ofrecer_horario(tenant, persona["telefono"], cuando, link):
            flash(f"Le ofrecimos a {persona['nombre']} el {cuando}.")
        else:
            flash(f"No se pudo escribirle a {persona['nombre']}.")
    return _volver_a_agenda()


def cuit_valido(cuit: str) -> bool:
    """11 dígitos con el dígito verificador de AFIP/ARCA."""
    if not re.fullmatch(r"\d{11}", cuit):
        return False
    suma = sum(int(d) * f for d, f in zip(cuit[:10], (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)))
    verificador = 11 - suma % 11
    verificador = 0 if verificador == 11 else 9 if verificador == 10 else verificador
    return verificador == int(cuit[10])


@app.post("/agenda/avisos")
@login_required
def agenda_avisos():
    campos = {}
    if "telefono" in request.form:
        campos["aviso_telefono"] = "".join(ch for ch in request.form["telefono"] if ch.isdigit()) or None
    if "matricula" in request.form:
        campos["matricula"] = request.form["matricula"].strip() or None
    if "email" in request.form:
        email = request.form["email"].strip()
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Ese mail no parece válido.")
            return _volver_a_agenda()
        campos["email"] = email or None
    if "resumen_cobros_form" in request.form:
        campos["resumen_cobros_mail"] = request.form.get("resumen_cobros_mail") == "1"
        campos["aviso_cada_pago"] = request.form.get("aviso_cada_pago") == "1"
    if "recibo_cuit" in request.form:
        cuit = "".join(ch for ch in request.form["recibo_cuit"] if ch.isdigit())
        if cuit and not cuit_valido(cuit):
            flash("Revisá el CUIT: son 11 números (ej. 20-12345678-3).")
            return _volver_a_agenda()
        campos["recibo_cuit"] = f"{cuit[:2]}-{cuit[2:10]}-{cuit[10]}" if cuit else None
    if "recibo_domicilio" in request.form:
        campos["recibo_domicilio"] = request.form["recibo_domicilio"].strip()[:150] or None
    if "link_videollamada" in request.form:
        link = request.form["link_videollamada"].strip()
        if link and not link.startswith("https://"):
            flash("El link de videollamada tiene que empezar con https://")
            return _volver_a_agenda()
        campos["link_videollamada"] = link or None
    db.guardar_ajustes(session["tenant_id"], campos)
    flash("Listo, guardado.")
    return _volver_a_agenda()


@app.post("/agenda/paciente")
@login_required
def agenda_paciente():
    telefono = request.form.get("telefono") or ""
    if not telefono:
        return "falta el paciente", 400
    fecha = request.form.get("fecha_nacimiento") or None
    if fecha:
        try:
            date.fromisoformat(fecha)
        except ValueError:
            flash("La fecha de nacimiento no es válida.")
            return _volver_a_agenda()
    # Cada form de la ficha manda sólo sus campos: lo que no viene no se pisa.
    campos = {"fecha_nacimiento": fecha} if "fecha_nacimiento" in request.form else {}
    for campo in ("notas", "obra_social", "numero_afiliado", "motivo_consulta", "objetivos", "como_viene",
                  "psiquiatra", "medicacion", "plan_crisis",
                  "escuela", "grado", "docente", "equipo_orientador"):
        if campo in request.form:
            campos[campo] = request.form[campo].strip() or None
    db.guardar_paciente(session["tenant_id"], telefono, campos)
    return _volver_a_agenda()


@app.post("/agenda/paciente/whatsapp")
@login_required
def agenda_cargar_whatsapp():
    """Para el paciente que se agendó sin WhatsApp: desde acá le llegan los
    recordatorios y avisos como a cualquier otro."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    clave = request.form.get("telefono") or ""
    nuevo = "".join(ch for ch in (request.form.get("whatsapp") or "") if ch.isdigit())
    ficha = next((f for f in _fichas(tenant, tz, datetime.now(tz)) if f["clave"] == clave), None)
    if not ficha or db.tiene_whatsapp(ficha["telefono"]):
        flash("Ese paciente ya tiene WhatsApp.")
    elif len(nuevo) < 8:
        flash("Ese WhatsApp no parece completo: poné el número con código de área.")
    elif db.cargar_whatsapp(tenant["id"], clave, nuevo, ficha["nombre"]):
        flash(f"Listo, a {ficha['nombre']} ya le llegan los recordatorios por WhatsApp.")
    else:
        flash("No se pudo cargar el WhatsApp.")
    return _volver_a_agenda()


@app.post("/agenda/sesion/<turno_id>/ficha")
@login_required
def agenda_mover(turno_id):
    """Corrige una sesión que cayó en la ficha equivocada del mismo teléfono."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    clave = request.form.get("destino") or ""
    destino = next((f for f in _fichas(tenant, tz, datetime.now(tz)) if f["clave"] == clave), None)
    if not destino or not db.mover_sesion(tenant["id"], turno_id, clave, destino["nombre"]):
        flash("No se pudo pasar la sesión a esa ficha.")
    return _volver_a_agenda()


@app.post("/agenda/sesion/<turno_id>/ficha/confirmar")
@login_required
def agenda_confirmar_ficha(turno_id):
    db.confirmar_ficha(session["tenant_id"], turno_id)
    return _volver_a_agenda()


@app.post("/agenda/sesion/<turno_id>/vista")
@login_required
def agenda_reserva_vista(turno_id):
    db.marcar_reserva_vista(session["tenant_id"], turno_id)
    return _volver_a_agenda()


@app.get("/agenda/ayuda")
@login_required
def ayuda():
    tenant = db.get_tenant_by_id(session["tenant_id"])
    return render_template("ayuda.html", tenant=tenant, rubro=rubros.de(tenant))


@app.post("/agenda/paciente/cerrar")
@login_required
def agenda_cerrar():
    """Alta o pausa: la ficha y la deuda quedan; deja de aparecer en avisos."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    telefono = request.form.get("telefono") or ""
    estado = request.form.get("estado")
    if not telefono or estado not in ("pausa", "alta"):
        return "pedido inválido", 400
    ahora = datetime.now(_tz_de(tenant))
    db.guardar_paciente(tenant["id"], telefono, {
        "estado": estado, "estado_fecha": ahora.date().isoformat(),
        "estado_motivo": (request.form.get("motivo") or "").strip() or None,
    })
    canceladas = 0
    if request.form.get("cancelar_futuras"):
        canceladas = db.cancelar_sesiones_futuras(tenant["id"], telefono, ahora.astimezone(ZoneInfo("UTC")))
    aviso = "Tratamiento en pausa." if estado == "pausa" else "Paciente dado de alta."
    flash(f"{aviso} Se liberaron {canceladas} sesiones futuras." if canceladas else aviso)
    return _volver_a_agenda()


@app.post("/agenda/paciente/retomar")
@login_required
def agenda_retomar():
    telefono = request.form.get("telefono") or ""
    if not telefono:
        return "falta el paciente", 400
    db.guardar_paciente(session["tenant_id"], telefono, {"estado": "activo", "estado_fecha": None, "estado_motivo": None})
    return _volver_a_agenda()


ETIQUETAS_EXCEL = {"vino": "Asistió", "no_vino": "No vino", "pendiente": "Sin pagar", "pagado": "Pagó", "no_cobra": "No se cobra"}


def _fila_excel(hoja, valores: list) -> None:
    """Forzar texto: un nombre cargado en la página pública podría empezar con
    `=` y Excel lo correría como fórmula."""
    hoja.append(valores)
    for celda in hoja[hoja.max_row]:
        if isinstance(celda.value, str):
            celda.data_type = "s"


@app.get("/agenda/exportar")
@login_required
def agenda_exportar():
    """Para el contador. Sin nombres salvo que se pidan, y nunca notas."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    ahora = datetime.now(tz)
    primero = _mes_de(request.args.get("mes"), ahora.date())
    con_nombres = request.args.get("nombres") == "1"
    fichas = _fichas(tenant, tz, ahora)

    libro = Workbook()
    hoja = libro.active
    hoja.title = "Sesiones"
    identidad = ["Paciente", "Obra social"] if con_nombres else []
    _fila_excel(hoja, ["Fecha", "Hora", *identidad, "Modalidad", "Asistencia", "Pago", "Valor"])
    filas = []
    for f in fichas:
        for t in f["sesiones"]:
            inicio = datetime.fromisoformat(t["inicio"]).astimezone(tz)
            if (inicio.year, inicio.month) == (primero.year, primero.month):
                filas.append((inicio, f, t))
    for inicio, f, t in sorted(filas, key=lambda x: x[0]):
        _fila_excel(hoja, [
            inicio.date(), inicio.strftime("%H:%M"),
            *([f["nombre"], f["obra_social"]] if con_nombres else []),
            "Videollamada" if t.get("modalidad") == "virtual" else "Presencial",
            ETIQUETAS_EXCEL.get(t.get("asistencia"), ""), ETIQUETAS_EXCEL.get(t.get("pago"), ""), t.get("valor"),
        ])
    cobros = _cobros(fichas, primero, tz, ahora)
    resumen = libro.create_sheet("Resumen")
    for etiqueta, clave in (("Cobrado", "cobrado"), ("A cobrar", "a_cobrar"), ("Agendado", "agendado"),
                            ("Sesiones dadas", "sesiones"), ("Ausencias", "ausencias")):
        _fila_excel(resumen, [etiqueta, cobros[clave]])

    salida = io.BytesIO()
    libro.save(salida)
    salida.seek(0)
    return send_file(salida, as_attachment=True, download_name=f"sesiones-{primero:%Y-%m}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/agenda/pagos-web.xlsx")
@login_required
def agenda_pagos_web_excel():
    """Lo cobrado por la web en un mes, pago por pago, para el contador.
    Sin nombres de pacientes salvo que se pidan; nunca teléfonos ni notas."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    primero = _mes_de(request.args.get("mes"), datetime.now(tz).date())
    siguiente = (primero.replace(day=28) + timedelta(days=4)).replace(day=1)
    con_nombres = request.args.get("nombres") == "1"
    lista = db.pagos_resueltos_entre(tenant["id"], datetime.combine(primero, dtime(0), tz).astimezone(ZoneInfo("UTC")),
                                     datetime.combine(siguiente, dtime(0), tz).astimezone(ZoneInfo("UTC")))
    libro = Workbook()
    hoja = libro.active
    hoja.title = "Pagos web"
    _fila_excel(hoja, ["Recibo", "Fecha", "Hora", *(["Paciente"] if con_nombres else []), "Medio", "Operación Mercado Pago",
                       "Monto", "Devuelto", "Neto"])
    for p in sorted(lista, key=lambda x: str(x.get("resuelto") or "")):
        cuando = datetime.fromisoformat(str(p["resuelto"])).astimezone(tz)
        _fila_excel(hoja, [recibos.datos(tenant, p)["numero"], cuando.date(), cuando.strftime("%H:%M"),
                           *([p.get("paciente_nombre") or ""] if con_nombres else []),
                           "Mercado Pago" if p.get("metodo") == "mercadopago" else "Transferencia",
                           p.get("mp_pago_id") or "", p["monto"], p.get("devuelto") or 0, p["monto"] - (p.get("devuelto") or 0)])
    t = recibos.totales(lista)
    resumen = libro.create_sheet("Resumen")
    for etiqueta, valor in (("Pagos", t["cantidad"]), ("Mercado Pago (neto)", t["mercadopago"]),
                            ("Transferencia (neto)", t["transferencia"]), ("Devuelto", t["devuelto"]), ("Total neto", t["neto"])):
        _fila_excel(resumen, [etiqueta, valor])
    salida = io.BytesIO()
    libro.save(salida)
    salida.seek(0)
    return send_file(salida, as_attachment=True, download_name=f"pagos-web-{primero:%Y-%m}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _misma_obra_social(a: str | None, b: str) -> bool:
    return (a or "").strip().casefold() == b.strip().casefold()


@app.get("/agenda/obra-social")
@login_required
def agenda_obra_social():
    """Planilla mensual para presentar: sólo sesiones marcadas como asistidas.
    Sin teléfonos, montos ni notas."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    ahora = datetime.now(tz)
    primero = _mes_de(request.args.get("mes"), ahora.date())
    obra_social = (request.args.get("obra_social") or "").strip()
    pacientes, sin_marcar = [], 0
    for f in _fichas(tenant, tz, ahora):
        if not _misma_obra_social(f["obra_social"], obra_social):
            continue
        fechas = []
        for t in f["sesiones"]:
            inicio = datetime.fromisoformat(t["inicio"]).astimezone(tz)
            if (inicio.year, inicio.month) != (primero.year, primero.month) or inicio > ahora:
                continue
            if t.get("asistencia") == "vino":
                fechas.append(inicio.strftime("%d/%m"))
            elif not t.get("asistencia"):
                sin_marcar += 1
        if fechas:
            pacientes.append({"nombre": f["nombre"], "afiliado": f["numero_afiliado"], "fechas": fechas})
    return render_template("planilla_obra_social.html", tenant=tenant, obra_social=obra_social, titulo=_meses_label(primero),
                           pacientes=pacientes, sin_marcar=sin_marcar, total=sum(len(p["fechas"]) for p in pacientes))


@app.post("/agenda/paciente/constancia")
@login_required
def agenda_constancia():
    """Por POST para que el teléfono del paciente no quede en la URL."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    ahora = datetime.now(tz)
    telefono = request.form.get("telefono") or ""
    ficha = next((f for f in _fichas(tenant, tz, ahora) if f["clave"] == telefono), None)
    if not ficha:
        return "No encontrado.", 404
    primero = _mes_de(request.form.get("mes"), ahora.date())
    sesiones = [
        t for t in ficha["sesiones"]
        if t.get("pago") == "pagado" and datetime.fromisoformat(t["inicio"]).astimezone(tz).strftime("%Y-%m") == primero.strftime("%Y-%m")
    ]
    return render_template("constancia.html", tenant=tenant, ficha=ficha, sesiones=sesiones,
                           titulo=_meses_label(primero), total=sum(t.get("valor") or 0 for t in sesiones))


@app.post("/agenda/paciente/recordar-pago")
@login_required
def agenda_recordar_pago():
    """Sólo sale cuando el profesional toca el botón: un mensaje de plata a un
    paciente nunca se manda solo. El monto se calcula acá, no viene del form."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    telefono = request.form.get("telefono") or ""
    ficha = next((f for f in _fichas(tenant, tz, datetime.now(tz)) if f["clave"] == telefono), None)
    if not ficha or not ficha["adeudado"]:
        flash("Ese paciente no tiene deuda con valor cargado.")
    elif not db.tiene_whatsapp(ficha["telefono"]):
        flash(f"{ficha['nombre']} no tiene WhatsApp cargado.")
    elif ficha["recordado_hoy"]:
        flash(f"A {ficha['nombre']} ya se le recordó hoy.")
    else:
        db.guardar_paciente(tenant["id"], telefono, {"ultimo_recordatorio_pago": datetime.now(tz).date().isoformat()})
        monto = app.jinja_env.filters["pesos"](ficha["adeudado"])
        if _recordar_pago(tenant, db.partir_clave(telefono)[0], monto):
            flash(f"Le recordamos a {ficha['nombre']} que tiene {monto} pendientes.")
        else:
            flash(f"No se pudo mandar el recordatorio a {ficha['nombre']}.")
    return _volver_a_agenda()


def _recordar_pago(tenant: dict, telefono: str, monto: str) -> bool:
    numero = whatsapp.numero_de(tenant)
    if not numero:
        return False
    if config.RECORDATORIO_PAGO_TEMPLATE:
        return whatsapp.enviar_plantilla(numero, telefono, config.RECORDATORIO_PAGO_TEMPLATE,
                                         config.RECORDATORIO_TEMPLATE_LANG, [tenant["nombre"], monto])
    return whatsapp.enviar_texto(numero, telefono, f"Hola. Te recuerdo que tenés {monto} pendientes de sesiones con *{tenant['nombre']}*.")


@app.post("/agenda/paciente/persona")
@login_required
def agenda_persona():
    telefono = request.form.get("telefono") or ""
    nombre = (request.form.get("nombre") or "").strip()
    if not telefono or not nombre:
        flash("Falta el nombre.")
        return _volver_a_agenda()
    contacto = "".join(ch for ch in (request.form.get("telefono_persona") or "") if ch.isdigit()) or None
    db.agregar_persona(session["tenant_id"], telefono, nombre, (request.form.get("vinculo") or "").strip() or None,
                       contacto, bool(request.form.get("emergencia")))
    return _volver_a_agenda()


@app.post("/agenda/paciente/persona/<persona_id>/emergencia")
@login_required
def agenda_persona_emergencia(persona_id):
    db.marcar_emergencia(session["tenant_id"], persona_id, request.form.get("emergencia") == "1")
    return _volver_a_agenda()


@app.post("/agenda/paciente/persona/<persona_id>/borrar")
@login_required
def agenda_persona_borrar(persona_id):
    db.borrar_persona(session["tenant_id"], persona_id)
    return _volver_a_agenda()


@app.post("/agenda/paciente/fecha")
@login_required
def agenda_fecha():
    telefono = request.form.get("telefono") or ""
    titulo = (request.form.get("titulo") or "").strip()
    try:
        dia, mes = int(request.form.get("dia") or 0), int(request.form.get("mes") or 0)
        anio = int(request.form["anio"]) if request.form.get("anio") else None
        # 2000 fue bisiesto: el 29/2 vale, el 31/4 no.
        date(2000, mes, dia)
        if anio is not None and not 1900 <= anio <= 2100:
            raise ValueError
    except ValueError:
        flash("Esa fecha no es válida.")
        return _volver_a_agenda()
    if not telefono or not titulo:
        flash("Falta decir qué fecha es.")
        return _volver_a_agenda()
    animo = request.form.get("animo")
    db.agregar_fecha(session["tenant_id"], telefono, titulo, dia, mes, anio, animo if animo in ("bien", "mal") else None)
    return _volver_a_agenda()


@app.post("/agenda/paciente/test")
@login_required
def agenda_test():
    telefono = request.form.get("telefono") or ""
    test = (request.form.get("test") or "").strip()
    tenant = db.get_tenant_by_id(session["tenant_id"])
    try:
        fecha = date.fromisoformat(request.form.get("fecha") or "")
    except ValueError:
        flash("Esa fecha no es válida.")
        return _volver_a_agenda()
    if fecha > datetime.now(_tz_de(tenant)).date():
        flash("La fecha del test no puede ser futura.")
        return _volver_a_agenda()
    if not telefono or not test:
        flash("Falta decir qué test se tomó.")
        return _volver_a_agenda()
    db.agregar_test(tenant["id"], telefono, test[:80], fecha.isoformat())
    return _volver_a_agenda()


@app.post("/agenda/plazos-tests")
@login_required
def agenda_plazos_tests():
    """Sólo se guarda lo que difiere del plazo de fábrica: si el de fábrica
    cambia en una versión nueva, quien no lo tocó recibe el nuevo."""
    propios = {}
    for nombre, _, _, fabrica, _ in tests_sugeridos.CATALOGO:
        valor = (request.form.get(f"meses-{nombre}") or "").strip()
        if not valor:
            continue
        try:
            meses = int(valor)
        except ValueError:
            flash(f"El plazo de {nombre} tiene que ser un número de meses.")
            return _volver_a_agenda()
        if not 0 <= meses <= tests_sugeridos.MAX_MESES:
            flash(f"El plazo de {nombre} tiene que estar entre 0 y {tests_sugeridos.MAX_MESES} meses.")
            return _volver_a_agenda()
        if meses != fabrica:
            propios[nombre] = meses
    db.guardar_plazos_tests(session["tenant_id"], propios)
    flash("Se guardaron los plazos para repetir tests.")
    return _volver_a_agenda()


@app.post("/agenda/paciente/test/<test_id>/resultados")
@login_required
def agenda_test_resultados(test_id):
    filas, error = resultados_tests.leer_formulario(request.form)
    if error:
        flash(error)
        return _volver_a_agenda()
    db.guardar_resultados_test(session["tenant_id"], test_id, filas)
    flash("Se guardaron los resultados." if filas else "Se borraron los resultados.")
    return _volver_a_agenda()


@app.post("/agenda/fluidez")
@login_required
def agenda_fluidez():
    """Pantalla de lectura. Por POST para que el teléfono no quede en la URL."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    clave = request.form.get("telefono") or ""
    ficha = next((f for f in _fichas(tenant, tz, datetime.now(tz)) if f["clave"] == clave), None)
    texto = next((t for t in db.textos_lectura(tenant["id"]) if t["id"] == request.form.get("texto_id")), None)
    if not ficha or not texto:
        flash("Elegí un texto para la lectura.")
        return _volver_a_agenda()
    return render_template("fluidez.html", tenant=tenant, ficha=ficha, texto=texto,
                           palabras=fluidez.palabras(texto["texto"]), semana=request.form.get("semana") or "")


@app.post("/agenda/fluidez/guardar")
@login_required
def agenda_fluidez_guardar():
    tenant = db.get_tenant_by_id(session["tenant_id"])
    clave = request.form.get("telefono") or ""
    texto = next((t for t in db.textos_lectura(tenant["id"]) if t["id"] == request.form.get("texto_id")), None)
    if not clave or not texto:
        flash("No se encontró el texto de la lectura.")
        return _volver_a_agenda()
    lista = fluidez.palabras(texto["texto"])
    datos, error = fluidez.validar(len(lista), request.form.get("leidas") or "",
                                   request.form.get("errores") or "", request.form.get("segundos") or "")
    if error:
        flash(error)
        return _volver_a_agenda()
    db.agregar_lectura(tenant["id"], clave, {
        "fecha": datetime.now(_tz_de(tenant)).date().isoformat(),
        "texto_titulo": texto["titulo"],
        "palabras_texto": len(lista),
        "palabras_leidas": datos["leidas"],
        "errores": len(datos["errores"]),
        "errores_palabras": [lista[i] for i in datos["errores"]],
        "segundos": datos["segundos"],
    })
    r = fluidez.calcular(datos["leidas"], len(datos["errores"]), datos["segundos"])
    flash(f"Lectura guardada: {r['pcpm']} palabras correctas por minuto, {r['precision']}% de precisión.")
    return _volver_a_agenda()


@app.post("/agenda/fluidez/<lectura_id>/borrar")
@login_required
def agenda_fluidez_borrar(lectura_id):
    db.borrar_lectura(session["tenant_id"], lectura_id)
    return _volver_a_agenda()


@app.post("/agenda/textos-lectura")
@login_required
def agenda_textos_lectura():
    titulo = (request.form.get("titulo") or "").strip()
    texto = (request.form.get("texto") or "").strip()
    if not titulo or not texto:
        flash("El texto necesita título y contenido.")
    elif len(titulo) > fluidez.MAX_TITULO or len(texto) > fluidez.MAX_TEXTO:
        flash("El texto es demasiado largo.")
    elif not 1 <= len(fluidez.palabras(texto)) <= fluidez.MAX_PALABRAS:
        flash(f"El texto tiene que tener entre 1 y {fluidez.MAX_PALABRAS} palabras.")
    else:
        db.agregar_texto_lectura(session["tenant_id"], titulo, texto)
        flash(f"Se agregó «{titulo}».")
    return _volver_a_agenda()


@app.post("/agenda/textos-lectura/<texto_id>/borrar")
@login_required
def agenda_textos_lectura_borrar(texto_id):
    db.borrar_texto_lectura(session["tenant_id"], texto_id)
    return _volver_a_agenda()


@app.post("/agenda/cuestionario")
@login_required
def agenda_cuestionario():
    tenant = db.get_tenant_by_id(session["tenant_id"])
    clave = request.form.get("telefono") or ""
    instrumento = request.form.get("instrumento") or ""
    plantilla = None
    if instrumento.startswith("todas:"):
        return _cuestionario_todas_las_versiones(tenant, clave, instrumento[6:])
    if instrumento.startswith("propio:"):
        fila = next((p for p in db.plantillas_cuestionario(tenant["id"]) if p["id"] == instrumento[7:]), None)
        if fila and cuestionarios.completa(fila["contenido"]):
            plantilla = {**copy.deepcopy(fila["contenido"]), "id": fila["id"]}
        instrumento = "propio"
    if not clave or (instrumento != "propio" and instrumento not in rubros.de(tenant)["cuestionarios"]) \
            or (instrumento == "propio" and not plantilla):
        flash("Ese cuestionario no está disponible.")
        return _volver_a_agenda()
    vence = datetime.now(ZoneInfo("UTC")) + timedelta(days=cuestionarios.DIAS_VIGENCIA)
    para = (request.form.get("para") or "").strip()[:cuestionarios.MAX_PARA] or None
    db.crear_cuestionario(tenant["id"], clave, instrumento, cuestionarios.nuevo_token(), vence.isoformat(), para, plantilla)
    nombre = plantilla["nombre"] if plantilla else cuestionarios.nombre_de(instrumento)
    flash(f"Listo el link de «{nombre}»: copialo desde la ficha y mandalo.")
    return _volver_a_agenda()


def _cuestionario_todas_las_versiones(tenant: dict, clave: str, base: str):
    """Un link por cada versión completa de un cuestionario propio (p. ej.
    SCARED de padres y del chico) en un solo paso."""
    versiones = [p for p in db.plantillas_cuestionario(tenant["id"])
                 if p["contenido"]["nombre"].casefold() == base and cuestionarios.completa(p["contenido"])]
    if not clave or len(versiones) < 2:
        flash("Ese cuestionario no está disponible.")
        return _volver_a_agenda()
    vence = (datetime.now(ZoneInfo("UTC")) + timedelta(days=cuestionarios.DIAS_VIGENCIA)).isoformat()
    para = (request.form.get("para") or "").strip()[:cuestionarios.MAX_PARA] or None
    for p in versiones:
        db.crear_cuestionario(tenant["id"], clave, "propio", cuestionarios.nuevo_token(), vence, para,
                              {**copy.deepcopy(p["contenido"]), "id": p["id"]})
    nombres = ", ".join(p["contenido"].get("version") or "sin versión" for p in versiones)
    flash(f"Listos {len(versiones)} links de «{versiones[0]['contenido']['nombre']}» ({nombres}): "
          "en la ficha cada uno dice para quién es.")
    return _volver_a_agenda()


@app.post("/agenda/cuestionario/<cuestionario_id>/visto")
@login_required
def agenda_cuestionario_visto(cuestionario_id):
    db.marcar_cuestionario_visto(session["tenant_id"], cuestionario_id)
    return _volver_a_agenda()


@app.post("/agenda/cuestionario/<cuestionario_id>/borrar")
@login_required
def agenda_cuestionario_borrar(cuestionario_id):
    db.borrar_cuestionario(session["tenant_id"], cuestionario_id)
    return _volver_a_agenda()


def _cuestionario_vigente(token: str):
    c = db.cuestionario_por_token(token)
    if not c or not cuestionarios.definicion(c):
        return None, None, "Este link no existe."
    tenant = db.get_tenant_by_id(c["tenant_id"])
    if c.get("respondido_at"):
        return c, tenant, "Este cuestionario ya se respondió. ¡Gracias!"
    if datetime.fromisoformat(c["vence"]) <= datetime.now(ZoneInfo("UTC")):
        return c, tenant, "Este link venció. Pedile uno nuevo a tu profesional."
    return c, tenant, None


@app.get("/cuestionario/<token>")
def cuestionario_publico(token):
    c, tenant, aviso = _cuestionario_vigente(token)
    if aviso:
        return render_template("cuestionario_aviso.html", tenant=tenant, aviso=aviso), (404 if not c else 200)
    return _pantalla_cuestionario(c, tenant, None, {})


def _pantalla_cuestionario(c: dict, tenant: dict, error: str | None, previas):
    inst = cuestionarios.definicion(c)
    abierto = inst["tipo"] == "abierto"
    return render_template("cuestionario.html", tenant=tenant, inst=inst, abierto=abierto, para=c.get("para"),
                           opciones=inst.get("opciones") or [], fuente=inst["fuente"],
                           error=error, previas=previas, max_respuesta=cuestionarios.MAX_RESPUESTA)


@app.post("/cuestionario/<token>")
def cuestionario_responder(token):
    c, tenant, aviso = _cuestionario_vigente(token)
    if aviso:
        return render_template("cuestionario_aviso.html", tenant=tenant, aviso=aviso), (404 if not c else 200)
    if cuestionarios.es_abierto(c["instrumento"]):
        datos, error = cuestionarios.leer_abierto(c["instrumento"], request.form)
    elif c["instrumento"] == "propio":
        datos, error = cuestionarios.corregir_propio(c["plantilla"], request.form)
    else:
        datos, error = cuestionarios.corregir(c["instrumento"], request.form)
    if error:
        return _pantalla_cuestionario(c, tenant, error, request.form), 400
    guardado = db.responder_cuestionario(c["id"], {**datos, "respondido_at": datetime.now(ZoneInfo("UTC")).isoformat()})
    if not guardado:
        return render_template("cuestionario_aviso.html", tenant=tenant, aviso="Este cuestionario ya se respondió. ¡Gracias!")
    return render_template("cuestionario_aviso.html", tenant=tenant, alerta=datos.get("alerta"),
                           aviso=f"Listo, tus respuestas le llegaron a {tenant['nombre']}. ¡Gracias!")


@app.post("/agenda/plantillas")
@login_required
def agenda_plantilla_nueva():
    modelo = request.form.get("modelo") or ""
    if modelo in cuestionarios.VERSIONES_SCARED:
        contenido = {**copy.deepcopy(cuestionarios.MODELO_SCARED), "version": cuestionarios.VERSIONES_SCARED[modelo]}
        flash(f"Se creó el SCARED ({contenido['version'].lower()}) con su corrección. "
              "Falta pegar las 41 preguntas, una por renglón, en orden.")
    else:
        contenido, error = cuestionarios.leer_plantilla(request.form)
        if error:
            flash(error)
            return _volver_a_agenda()
        flash(f"Se creó «{contenido['nombre']}».")
    db.agregar_plantilla(session["tenant_id"], contenido)
    return _volver_a_agenda()


@app.get("/agenda/plantillas/<plantilla_id>/exportar")
@login_required
def agenda_plantilla_exportar(plantilla_id):
    fila = next((p for p in db.plantillas_cuestionario(session["tenant_id"]) if p["id"] == plantilla_id), None)
    if not fila:
        return "No encontrado.", 404
    c = fila["contenido"]
    nombre = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", f"{c['nombre']} {c.get('version') or ''}")
                    .encode("ascii", "ignore").decode().lower()).strip("-") or "cuestionario"
    cuerpo = json.dumps(cuestionarios.exportar(c), ensure_ascii=False, indent=2)
    return app.response_class(cuerpo, mimetype="application/json",
                              headers={"Content-Disposition": f'attachment; filename="{nombre}.json"'})


@app.post("/agenda/plantillas/importar")
@login_required
def agenda_plantilla_importar():
    archivo = request.files.get("archivo")
    if not archivo:
        flash("Elegí el archivo del cuestionario.")
        return _volver_a_agenda()
    contenido, error = cuestionarios.importar(archivo.read(cuestionarios.MAX_IMPORTAR + 1))
    if error:
        flash(error)
        return _volver_a_agenda()
    db.agregar_plantilla(session["tenant_id"], contenido)
    faltan = sum(not x.strip() for x in contenido["items"])
    flash(f"Se importó «{contenido['nombre']}»." + (f" Faltan {faltan} preguntas para poder mandarlo." if faltan else ""))
    return _volver_a_agenda()


@app.post("/agenda/plantillas/<plantilla_id>")
@login_required
def agenda_plantilla_editar(plantilla_id):
    contenido, error = cuestionarios.leer_plantilla(request.form)
    if error:
        flash(error)
        return _volver_a_agenda()
    db.guardar_plantilla(session["tenant_id"], plantilla_id, contenido)
    faltan = sum(not x.strip() for x in contenido["items"])
    flash(f"Se guardó «{contenido['nombre']}»." + (f" Faltan {faltan} preguntas para poder mandarlo." if faltan else ""))
    return _volver_a_agenda()


@app.post("/agenda/plantillas/<plantilla_id>/compartir")
@login_required
def agenda_plantilla_compartir(plantilla_id):
    tenant = db.get_tenant_by_id(session["tenant_id"])
    fila = next((p for p in db.plantillas_cuestionario(tenant["id"]) if p["id"] == plantilla_id), None)
    if not fila:
        flash("No se encontró ese cuestionario.")
        return _volver_a_agenda()
    con_preguntas = request.form.get("con_preguntas") == "1"
    if con_preguntas and request.form.get("declaro") != "1":
        flash("Para compartir las preguntas tenés que confirmar que son tuyas o de uso libre.")
        return _volver_a_agenda()
    if con_preguntas and not cuestionarios.completa(fila["contenido"]):
        flash("Faltan preguntas: compartilo sin preguntas o completalo antes.")
        return _volver_a_agenda()
    db.compartir_en_biblioteca(tenant["id"], tenant["nombre"], cuestionarios.para_biblioteca(fila["contenido"], con_preguntas), con_preguntas)
    flash(f"«{fila['contenido']['nombre']}» quedó en la biblioteca" + (" con sus preguntas." if con_preguntas else ", sin el texto de las preguntas."))
    return _volver_a_agenda()


def _bloqueo_reciente(tenant: dict, tz: ZoneInfo) -> str | None:
    """Fecha del último bloqueo por claves equivocadas, si fue en la última semana."""
    if not tenant.get("ultimo_bloqueo"):
        return None
    cuando = datetime.fromisoformat(tenant["ultimo_bloqueo"])
    if datetime.now(ZoneInfo("UTC")) - cuando > timedelta(days=7):
        return None
    return cuando.astimezone(tz).strftime("%d/%m a las %H:%M")


def _biblioteca_visible(tenant: dict) -> list[dict]:
    """Lo oculto por moderación sólo lo ven los moderadores y la autora."""
    return [e for e in db.biblioteca()
            if not e.get("oculto") or tenant.get("moderador") or e["autor_tenant_id"] == tenant["id"]]


@app.post("/agenda/biblioteca/<entrada_id>/moderar/<accion>")
@login_required
def agenda_biblioteca_moderar(entrada_id, accion):
    tenant = db.get_tenant_by_id(session["tenant_id"])
    if accion not in ("ocultar", "mostrar") or not tenant.get("moderador"):
        return "No autorizado.", 403
    db.moderar_biblioteca(entrada_id, accion == "ocultar", tenant["nombre"] if accion == "ocultar" else None)
    flash("Se ocultó de la biblioteca." if accion == "ocultar" else "Volvió a la biblioteca.")
    return _volver_a_agenda()


@app.post("/agenda/biblioteca/<entrada_id>/agregar")
@login_required
def agenda_biblioteca_agregar(entrada_id):
    tenant = db.get_tenant_by_id(session["tenant_id"])
    entrada = next((e for e in _biblioteca_visible(tenant) if e["id"] == entrada_id), None)
    if not entrada:
        flash("Ese cuestionario ya no está en la biblioteca.")
        return _volver_a_agenda()
    # Pasa por las mismas validaciones que una importación: nunca se copia el JSON tal cual.
    contenido, error = cuestionarios.importar(json.dumps(cuestionarios.exportar(entrada["contenido"])).encode())
    if error:
        flash(error)
        return _volver_a_agenda()
    db.agregar_plantilla(session["tenant_id"], contenido)
    faltan = sum(not x.strip() for x in contenido["items"])
    flash(f"Se agregó «{contenido['nombre']}» a tus cuestionarios." + (f" Falta pegar {faltan} preguntas." if faltan else ""))
    return _volver_a_agenda()


@app.post("/agenda/biblioteca/<entrada_id>/retirar")
@login_required
def agenda_biblioteca_retirar(entrada_id):
    db.retirar_de_biblioteca(session["tenant_id"], entrada_id)
    flash("Se retiró de la biblioteca. Quien ya lo agregó conserva su copia.")
    return _volver_a_agenda()


@app.post("/agenda/plantillas/<plantilla_id>/borrar")
@login_required
def agenda_plantilla_borrar(plantilla_id):
    db.borrar_plantilla(session["tenant_id"], plantilla_id)
    return _volver_a_agenda()


@app.post("/agenda/registro")
@login_required
def agenda_registro():
    """Hoja de registro para tomar un test en persona: cronómetro y conteo
    por subprueba. Guarda por la misma ruta que «Cargar resultados»."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    clave = request.form.get("telefono") or ""
    ficha = next((f for f in _fichas(tenant, tz, datetime.now(tz)) if f["clave"] == clave), None)
    toma = next((x for x in (ficha or {}).get("tests_tomados", []) if x["id"] == request.form.get("test_id")), None)
    if not toma:
        flash("No se encontró ese test.")
        return _volver_a_agenda()
    grupo = next(g for g in ficha["tests_por_test"] if any(t["id"] == toma["id"] for t in g["tomas"]))
    return render_template("registro.html", tenant=tenant, ficha=ficha, toma=toma, escalas=grupo["escalas"],
                           max_filas=resultados_tests.MAX_FILAS, semana=request.form.get("semana") or "")


@app.post("/agenda/paciente/informe")
@login_required
def agenda_informe():
    """Borrador imprimible. Por POST para que el teléfono no quede en la URL."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    ahora = datetime.now(tz)
    clave = request.form.get("telefono") or ""
    ficha = next((f for f in _fichas(tenant, tz, ahora) if f["clave"] == clave), None)
    if not ficha:
        return "No encontrado.", 404
    plantilla = next((p["contenido"] for p in db.plantillas_informe(tenant["id"])
                      if p["id"] == request.form.get("plantilla_id")), None)
    return render_template("informe.html", tenant=tenant, ficha=ficha, hoy=ahora.strftime("%d/%m/%Y"),
                           inf=informe.armar(ficha, rubros.de(tenant), ahora.date(), plantilla), guardado=None)


@app.post("/agenda/plantillas-informe")
@login_required
def agenda_plantilla_informe():
    contenido, error = informe.leer_plantilla(request.form.get("nombre") or "", request.form.get("secciones") or "")
    if error:
        flash(error)
        return _volver_a_agenda()
    db.agregar_plantilla_informe(session["tenant_id"], contenido)
    flash(f"Se guardó la plantilla «{contenido['nombre']}». La elegís al armar el próximo informe.")
    return _volver_a_agenda()


@app.post("/agenda/plantillas-informe/<plantilla_id>/borrar")
@login_required
def agenda_plantilla_informe_borrar(plantilla_id):
    db.borrar_plantilla_informe(session["tenant_id"], plantilla_id)
    return _volver_a_agenda()


FIRMA_MAX = 300_000
FIRMA_TIPOS = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg"}


@app.post("/agenda/firma")
@login_required
def agenda_firma():
    """Imagen de firma y sello para los informes. Se guarda como data URL en
    el tenant: es chica y así el informe no depende de otro almacenamiento.
    El tipo se decide por los primeros bytes, no por lo que diga el navegador."""
    archivo = request.files.get("firma")
    crudo = archivo.read(FIRMA_MAX + 1) if archivo else b""
    tipo = next((t for firma, t in FIRMA_TIPOS.items() if crudo.startswith(firma)), None)
    if not crudo:
        flash("Elegí la imagen de tu firma.")
    elif len(crudo) > FIRMA_MAX:
        flash("La imagen es muy pesada: hasta 300 KB.")
    elif not tipo:
        flash("La firma tiene que ser una imagen PNG o JPG.")
    else:
        db.guardar_ajustes(session["tenant_id"], {"firma_imagen": f"data:{tipo};base64,{base64.b64encode(crudo).decode()}"})
        flash("Se guardó tu firma: aparece en los informes y en la ficha completa.")
    return _volver_a_agenda()


def _estadisticas_pagina(tenant: dict, tz: ZoneInfo) -> dict | None:
    """Este mes en la página de turnos: visitas, reservas, cancelaciones y
    cambios desde la web, anotados en la lista de espera."""
    if not tenant.get("slug"):
        return None
    inicio = datetime.now(tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        return db.eventos_pagina_desde(tenant["id"], inicio)
    except Exception:
        return None


def _avisar_si_bloqueado(tenant: dict, inicio: str, fin: str) -> None:
    """El panel deja agendar en un día bloqueado (es decisión del
    profesional), pero avisa por si tocó el día equivocado."""
    bloqueo = db.bloqueo_en(tenant["id"], inicio, fin)
    if not bloqueo:
        return
    motivo = bloqueo.get("motivo") or ""
    if motivo.startswith(scheduler.PREFIJO_FERIADO):
        flash(f"Ojo: ese día es feriado ({motivo[len(scheduler.PREFIJO_FERIADO):]}). La sesión quedó agendada igual.")
    else:
        flash(f"Ojo: ese día lo tenías bloqueado{f' ({motivo})' if motivo else ''}. La sesión quedó agendada igual.")


@app.post("/agenda/libre/<turno_id>/abrir")
@login_required
def agenda_abrir_libre(turno_id):
    """Ofrece a los pacientes un horario de un día bloqueado, o lo vuelve a cerrar."""
    abrir = request.form.get("abrir") == "1"
    if not db.abrir_libres(session["tenant_id"], [turno_id], abrir):
        flash("Ese horario ya no está libre.")
    return _volver_a_agenda()


@app.post("/agenda/dia/<fecha>/abrir")
@login_required
def agenda_abrir_dia(fecha):
    """«Abrir este día a pacientes»: todos sus horarios libres se ofrecen,
    aunque el día siga bloqueado (el feriado no se borra: vuelve a cargarse
    solo y el año que viene sigue cerrado)."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    try:
        dia = date.fromisoformat(fecha)
    except ValueError:
        return "fecha inválida", 400
    abrir = request.form.get("abrir") == "1"
    # Horario reducido: «abrir sólo de 10 a 14». Sin horas, el día entero.
    try:
        hora_desde = dtime.fromisoformat(request.form["hora_desde"]) if abrir and request.form.get("hora_desde") else None
        hora_hasta = dtime.fromisoformat(request.form["hora_hasta"]) if abrir and request.form.get("hora_hasta") else None
    except ValueError:
        flash("Revisá las horas.")
        return _volver_a_agenda()
    if hora_desde and hora_hasta and hora_hasta <= hora_desde:
        flash("La hora final tiene que ser posterior a la inicial.")
        return _volver_a_agenda()
    desde = datetime.combine(dia, dtime.min, tzinfo=tz)
    libres = []
    for t in db.agenda_semana(tenant["id"], desde.astimezone(ZoneInfo("UTC")),
                              (desde + timedelta(days=1)).astimezone(ZoneInfo("UTC"))):
        if t["estado"] != "libre" or t.get("hueco"):
            continue
        ini = datetime.fromisoformat(t["inicio"]).astimezone(tz)
        fin = datetime.fromisoformat(t["fin"]).astimezone(tz)
        if hora_desde and ini.time() < hora_desde:
            continue
        if hora_hasta and (fin.date() > dia or fin.time() > hora_hasta):
            continue
        libres.append(t)
    cambiados = db.abrir_libres(tenant["id"], [t["id"] for t in libres], abrir)
    fecha = f"{dia.day}/{dia.month:02d}"
    if not abrir:
        flash(f"El {fecha} volvió a estar cerrado a tus pacientes.")
        return _volver_a_agenda()
    if not cambiados:
        flash("Ese día no tiene horarios libres para abrir" + (" en esas horas." if hora_desde or hora_hasta else "."))
        return _volver_a_agenda()
    rango = ""
    if hora_desde or hora_hasta:
        rango = f" de {hora_desde.strftime('%H:%M') if hora_desde else 'la mañana'} a {hora_hasta.strftime('%H:%M') if hora_hasta else 'la noche'}"
    texto = (f"Abriste el {fecha}{rango} a tus pacientes: {cambiados} horario{'s' if cambiados != 1 else ''} "
             f"se ofrece{'n' if cambiados != 1 else ''} en tu página.")
    if request.form.get("avisar_espera") == "1":
        primero = min(libres, key=lambda t: t["inicio"])
        avisados = cancelaciones.ofrecer_a_toda_la_espera(tenant, primero, datetime.now(tz).date())
        texto += (f" Le avisamos a {avisados} de tu lista de espera." if avisados
                  else " Nadie de tu lista de espera quedó para avisar hoy.")
    flash(texto)
    return _volver_a_agenda()


@app.post("/agenda/dia/<fecha>/pasar")
@login_required
def agenda_pasar_dia(fecha):
    """Pasa todas las sesiones de un día (un feriado, un día que se bloqueó) al
    mismo horario de la semana siguiente. La que no entra (ya hay otra sesión,
    o ese día también está bloqueado) queda donde estaba y se avisa."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    try:
        dia = date.fromisoformat(fecha)
    except ValueError:
        return "fecha inválida", 400
    utc = ZoneInfo("UTC")
    ahora = datetime.now(utc)
    desde = datetime.combine(dia, dtime.min, tzinfo=tz)
    sesiones = [t for t in db.agenda_semana(tenant["id"], desde.astimezone(utc), (desde + timedelta(days=1)).astimezone(utc))
                if t["estado"] == "reservado" and datetime.fromisoformat(t["inicio"]) > ahora]
    if not sesiones:
        flash("Ese día no tiene sesiones por venir para pasar.")
        return _volver_a_agenda()
    avisar = request.form.get("avisar") == "1"
    pasadas, quedaron, sin_aviso = 0, [], 0
    for t in sorted(sesiones, key=lambda t: t["inicio"]):
        inicio = datetime.fromisoformat(t["inicio"])
        # Misma hora local una semana después (con cambio de horario incluido).
        nuevo = datetime.combine(inicio.astimezone(tz).date() + timedelta(days=7), inicio.astimezone(tz).time(),
                                 tzinfo=tz).astimezone(utc)
        minutos = int((datetime.fromisoformat(t["fin"]) - inicio).total_seconds() // 60)
        error = db.reprogramar_turno(tenant["id"], t["id"], nuevo, minutos)
        hora = inicio.astimezone(tz).strftime("%H:%M")
        if error:
            quedaron.append(hora)
            continue
        pasadas += 1
        if avisar and not (db.tiene_whatsapp(t.get("paciente_telefono")) and _avisar_cambio(tenant, t, nuevo, tz)):
            sin_aviso += 1
    destino = dia + timedelta(days=7)
    texto = (f"Pasamos {pasadas} sesi{'ones' if pasadas != 1 else 'ón'} al {DIAS_LABEL[destino.weekday()].lower()} "
             f"{destino.day}/{destino.month:02d}, a la misma hora." if pasadas else "No se pudo pasar ninguna sesión.")
    if quedaron:
        texto += (f" Quedaron donde estaban las de las {', '.join(quedaron)}: ese horario de la semana siguiente "
                  f"está ocupado o bloqueado. Pasalas a mano.")
    if avisar and pasadas:
        texto += (f" A {sin_aviso} no se le pudo avisar por WhatsApp: escribiles vos." if sin_aviso
                  else " Les avisamos por WhatsApp.")
    flash(texto)
    return _volver_a_agenda()


@app.post("/agenda/libre/<turno_id>/sacar")
@login_required
def agenda_sacar_libre(turno_id):
    """Saca un horario libre que el profesional no va a usar. No vuelve a
    aparecer aunque esté dentro de su horario de atención."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    turno = db.sacar_libre(tenant["id"], turno_id)
    if turno:
        flash(f"Sacaste el horario del {scheduler._fmt(turno['inicio'], _tz_de(tenant))}.")
    else:
        flash("Ese horario ya no está libre.")
    return _volver_a_agenda()


@app.post("/agenda/hueco/<turno_id>/<accion>")
@login_required
def agenda_hueco(turno_id, accion):
    """El profesional decide sobre un horario que le cancelaron: «liberar»
    (vuelve a ofrecerse, y a la lista de espera si está activado) o «tomar»
    (se lo queda, no se ofrece)."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    turno = db.turno_por_id(tenant["id"], turno_id)
    if not turno or turno["estado"] != "libre" or accion not in ("liberar", "tomar"):
        flash("Ese horario ya no está para decidir.")
        return _volver_a_agenda()
    cuando = scheduler._fmt(turno["inicio"], _tz_de(tenant))
    if accion == "liberar":
        ofrecido = cancelaciones.liberar(tenant, turno)
        flash(f"El {cuando} quedó libre para tus pacientes." + (f" Se lo ofrecimos a {ofrecido}." if ofrecido else ""))
    else:
        cancelaciones.tomar(tenant, turno)
        flash(f"El {cuando} es tuyo: no se le ofrece a nadie.")
    return _volver_a_agenda()


@app.get("/agenda/imprimir")
@login_required
def agenda_imprimir():
    """Hoja de la semana para imprimir: hora, paciente, tipo, modalidad y
    consultorio. Sin notas ni teléfonos (es un papel que queda en el
    escritorio)."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    try:
        lunes = date.fromisoformat(request.args.get("semana") or "")
    except ValueError:
        lunes = datetime.now(tz).date()
    lunes -= timedelta(days=lunes.weekday())
    desde = datetime.combine(lunes, dtime.min, tzinfo=tz)
    hasta = desde + timedelta(days=7)
    sesiones = [t for t in db.sesiones_reservadas(tenant["id"])
                if desde <= datetime.fromisoformat(t["inicio"]) < hasta]
    dias = []
    for i in range(7):
        dia = lunes + timedelta(days=i)
        del_dia = sorted((t for t in sesiones if datetime.fromisoformat(t["inicio"]).astimezone(tz).date() == dia),
                         key=lambda t: t["inicio"])
        if del_dia:
            dias.append({"titulo": f"{DIAS_LABEL[dia.weekday()]} {dia.strftime('%d/%m')}", "sesiones": [
                {**t, "_hora": datetime.fromisoformat(t["inicio"]).astimezone(tz).strftime("%H:%M"),
                 "_fin": datetime.fromisoformat(t["fin"]).astimezone(tz).strftime("%H:%M") if t.get("fin") else ""}
                for t in del_dia]})
    return render_template("imprimir_semana.html", tenant=tenant, dias=dias, total=len(sesiones),
                           titulo=f"{lunes.strftime('%d/%m')} al {(lunes + timedelta(days=6)).strftime('%d/%m/%Y')}")


MAX_SEDES = 8
MAX_TIPOS = 8
MAX_FRANJAS = 4      # por día


@app.post("/agenda/sedes")
@login_required
def agenda_sedes():
    """Hasta MAX_SEDES consultorios (nombre y dirección). Después, en el
    horario se elige en cuál es cada franja."""
    f = request.form
    sedes = []
    for n in range(1, MAX_SEDES + 1):
        nombre = (f.get(f"sede{n}_nombre") or "").strip()[:40]
        if nombre:
            sedes.append({"nombre": nombre, "direccion": (f.get(f"sede{n}_direccion") or "").strip()[:120] or None})
    if len({x["nombre"].casefold() for x in sedes}) != len(sedes):
        flash("Dos consultorios no pueden llamarse igual.")
        return _volver_a_agenda()
    db.guardar_ajustes(session["tenant_id"], {"sedes": sedes or None})
    flash("Guardado. Ahora elegí en tu horario en qué consultorio es cada franja." if len(sedes) > 1 else "Guardado.")
    return _volver_a_agenda()


@app.post("/agenda/tipos")
@login_required
def agenda_tipos():
    """Tipos de turno que ofrece la página (ej. «Primera entrevista» 60 y
    «Sesión» 45). Con uno o ninguno, el paciente no elige."""
    f = request.form
    tipos = []
    for n in range(1, MAX_TIPOS + 1):
        nombre = (f.get(f"tipo{n}_nombre") or "").strip()[:60]
        minutos = f.get(f"tipo{n}_minutos") or ""
        precio = "".join(ch for ch in (f.get(f"tipo{n}_precio") or "") if ch.isdigit())
        if not nombre:
            continue
        if not minutos.isdigit() or not 10 <= int(minutos) <= 240:
            flash(f"«{nombre}»: la duración tiene que ser entre 10 y 240 minutos.")
            return _volver_a_agenda()
        tipos.append({"nombre": nombre, "minutos": int(minutos), **({"precio": int(precio)} if precio and int(precio) > 0 else {})})
    db.guardar_ajustes(session["tenant_id"], {"tipos_turno": tipos or None})
    flash("Guardado: tus pacientes eligen el tipo de turno al reservar." if len(tipos) > 1
          else "Guardado." if tipos else "Sin tipos de turno: se reserva un solo tipo, como siempre.")
    return _volver_a_agenda()


@app.post("/agenda/horario")
@login_required
def agenda_horario():
    """Horario de atención semanal: hasta MAX_FRANJAS por día. Al guardarlo se
    generan enseguida los horarios de las próximas semanas, y después el
    scheduler los va completando solo cada día."""
    f = request.form
    rangos = []
    for d in range(7):
        for n in range(1, MAX_FRANJAS + 1):
            desde, hasta = (f.get(f"dia{d}_desde{n}") or "").strip(), (f.get(f"dia{d}_hasta{n}") or "").strip()
            if not desde and not hasta:
                continue
            try:
                ok = dtime.fromisoformat(desde) < dtime.fromisoformat(hasta)
            except ValueError:
                ok = False
            if not ok:
                flash(f"Revisá el horario del {DIAS_LABEL[d].lower()}: falta una hora o termina antes de empezar.")
                return _volver_a_agenda()
            modalidad = f.get(f"dia{d}_mod{n}")
            sede = (f.get(f"dia{d}_sede{n}") or "").strip()
            if any(r["dia"] == d and r["desde"] < hasta[:5] and desde[:5] < r["hasta"] for r in rangos):
                flash(f"Revisá el horario del {DIAS_LABEL[d].lower()}: dos franjas se pisan.")
                return _volver_a_agenda()
            rangos.append({"dia": d, "desde": desde[:5], "hasta": hasta[:5],
                           **({"modalidad": modalidad} if modalidad in ("presencial", "virtual") else {}),
                           **({"sede": sede} if sede else {})})
    semanas = int(f.get("horario_semanas")) if (f.get("horario_semanas") or "") in {str(x) for x in range(1, 13)} else 4
    campos = {"horario_semanal": rangos or None, "horario_semanas": semanas,
              "feriados": "atender" if f.get("feriados") == "atender" else "bloquear",
              "puentes": "atender" if f.get("puentes") == "atender" else "bloquear"}
    duracion, descanso = f.get("duracion_turno_minutos") or "", f.get("descanso_minutos") or ""
    if duracion:
        if not duracion.isdigit() or not 10 <= int(duracion) <= 240:
            flash("La duración del turno tiene que ser entre 10 y 240 minutos.")
            return _volver_a_agenda()
        campos["duracion_turno_minutos"] = int(duracion)
    if descanso:
        if not descanso.isdigit() or int(descanso) > 60:
            flash("El descanso entre turnos tiene que ser entre 0 y 60 minutos.")
            return _volver_a_agenda()
        campos["descanso_minutos"] = int(descanso)
    db.guardar_ajustes(session["tenant_id"], campos)
    if not rangos:
        flash("Borraste tu horario de atención: los horarios ya generados siguen, pero no se crean nuevos solos.")
        return _volver_a_agenda()
    tenant = db.get_tenant_by_id(session["tenant_id"])
    try:
        scheduler.sincronizar_feriados(tenant, datetime.now(_tz_de(tenant)).date())
    except Exception:
        logger.exception("Fallo sincronizando feriados")
    sacados = scheduler.limpiar_fuera_de_horario(tenant)
    creados = scheduler.generar_desde_horario(tenant)
    flash(f"Guardado. Se crearon {creados} horario{'s' if creados != 1 else ''} libre{'s' if creados != 1 else ''} "
          f"para las próximas {semanas} semanas; desde ahora se van completando solos cada día."
          + (f" Se sacaron {sacados} libre{'s' if sacados != 1 else ''} que ya no entraban en tu horario." if sacados else ""))
    return _volver_a_agenda()


@app.post("/agenda/pausa")
@login_required
def agenda_pausa():
    """Pausar o reactivar la página de turnos (y la reserva por WhatsApp).
    Las sesiones ya reservadas no se tocan; la lista de espera sigue abierta."""
    pausar = request.form.get("pausar") == "1"
    campos = {"pausado": pausar, "pausado_hasta": None}
    hasta = None
    if pausar:
        campos["mensaje_pausa"] = (request.form.get("mensaje_pausa") or "").strip()[:200] or None
        try:
            hasta = date.fromisoformat(request.form.get("pausado_hasta") or "")
        except ValueError:
            hasta = None
        tenant = db.get_tenant_by_id(session["tenant_id"])
        if hasta and hasta <= datetime.now(_tz_de(tenant)).date():
            flash("La fecha de vuelta tiene que ser después de hoy.")
            return _volver_a_agenda()
        campos["pausado_hasta"] = hasta.isoformat() if hasta else None
    db.guardar_ajustes(session["tenant_id"], campos)
    if pausar and hasta:
        flash(f"Pausaste tu página de turnos hasta el {hasta.strftime('%d/%m')}: ese día se reactiva sola y le avisamos a tu lista de espera.")
    else:
        flash("Pausaste tu página de turnos: nadie puede reservar hasta que la reactives." if pausar
              else "Tu página de turnos vuelve a tomar reservas.")
    return _volver_a_agenda()


@app.post("/agenda/perfil")
@login_required
def agenda_perfil():
    """Lo que ven los pacientes en /turnos y arriba del calendario."""
    f = request.form
    modalidades = f.get("modalidades_publicas") if f.get("modalidades_publicas") in MODALIDADES_PUBLICAS else "presencial"
    tenant = db.get_tenant_by_id(session["tenant_id"])
    db.guardar_ajustes(session["tenant_id"], {
        "modalidades_publicas": modalidades,
        "en_directorio": f.get("en_directorio") == "1",
        "al_cancelar": "preguntar" if f.get("al_cancelar") == "preguntar" else "liberar",
        "hueco_vence_horas": int(f.get("hueco_vence_horas")) if (f.get("hueco_vence_horas") or "") in
        {str(h) for h in HORAS_VENCE_HUECO} else (tenant or {}).get("hueco_vence_horas", 0),
        "hueco_por_defecto": "tomar" if f.get("hueco_por_defecto") == "tomar" else "liberar",
        "ofrecer_espera_auto": f.get("ofrecer_espera_auto") == "1",
        "cancelar_hasta_horas": int(f.get("cancelar_hasta_horas")) if (f.get("cancelar_hasta_horas") or "") in
        {str(h) for h in HORAS_CANCELAR} else (tenant or {}).get("cancelar_hasta_horas", 24),
    })
    if modalidades != "presencial" and not (tenant or {}).get("link_videollamada"):
        flash("Guardado. Ojo: ofrecés videollamada pero no cargaste el link en Ajustes; el paciente no lo va a recibir.")
    else:
        flash("Listo, guardado: así te ven tus pacientes.")
    return _volver_a_agenda()


@app.post("/agenda/foto")
@login_required
def agenda_foto():
    """Foto para /turnos y el calendario. Mismo criterio que la firma: PNG o
    JPG por los primeros bytes, hasta 300 KB, guardada en el tenant."""
    archivo = request.files.get("foto")
    crudo = archivo.read(FIRMA_MAX + 1) if archivo else b""
    tipo = next((t for firma, t in FIRMA_TIPOS.items() if crudo.startswith(firma)), None)
    if not crudo:
        flash("Elegí una foto.")
    elif len(crudo) > FIRMA_MAX:
        flash("La foto es muy pesada: hasta 300 KB.")
    elif not tipo:
        flash("La foto tiene que ser PNG o JPG.")
    else:
        db.guardar_ajustes(session["tenant_id"], {"foto": f"data:{tipo};base64,{base64.b64encode(crudo).decode()}"})
        flash("Se guardó tu foto.")
    return _volver_a_agenda()


PORTADA_MAX = 600_000
MAX_ENFOQUES = 8
MAX_FORMACION = 6


def _lista_corta(texto: str, separador: str, maximo: int, largo: int) -> list[str]:
    vistos, salida = set(), []
    for x in (texto or "").split(separador):
        x = " ".join(x.split())[:largo]
        if x and x.lower() not in vistos:
            vistos.add(x.lower())
            salida.append(x)
    return salida[:maximo]


@app.post("/agenda/vista")
@login_required
def agenda_vista():
    """Tema o tamaño de letra elegidos desde el menú «Aa»: quedan en la cuenta."""
    tenant = db.get_tenant_by_id(session["tenant_id"]) or {}
    pref = dict(tenant.get("preferencias_vista") or {})
    if request.form.get("tema") in TEMAS:
        pref["tema"] = request.form["tema"]
    if request.form.get("letra") in LETRAS:
        pref["letra"] = request.form["letra"]
    if _horas_noche(request.form.get("noche")):
        desde, hasta = _horas_noche(request.form["noche"])
        pref["noche"] = f"{desde}-{hasta}"
    db.guardar_ajustes(session["tenant_id"], {"preferencias_vista": pref})
    return jsonify(ok=True)


@app.post("/agenda/presentacion")
@login_required
def agenda_presentacion():
    """Lo que el paciente lee de vos arriba de tus horarios."""
    f = request.form

    def corto(clave, largo):
        return " ".join((f.get(clave) or "").split())[:largo] or None

    def largo(clave, n):
        texto = (f.get(clave) or "").replace("\r\n", "\n").strip()[:n]
        return "\n".join(r.rstrip() for r in texto.split("\n")) or None

    db.guardar_ajustes(session["tenant_id"], {
        "titulo": corto("titulo", 80),
        "ubicacion": corto("ubicacion", 80),
        "descripcion": corto("descripcion", 160),
        "sobre_mi": largo("sobre_mi", 1200),
        "enfoques": _lista_corta(f.get("enfoques", ""), ",", MAX_ENFOQUES, 40),
        "formacion": _lista_corta(f.get("formacion", ""), "\n", MAX_FORMACION, 120),
        "mas_de_mi": largo("mas_de_mi", 400),
        "obras_sociales": corto("obras_sociales", 200),
        "color_pagina": f.get("color_pagina") if f.get("color_pagina") in COLORES_PAGINA else None,
    })
    flash("Listo, guardado: así te presentás en tu página.")
    return redirect(url_for("agenda") + "#mi-pagina")


@app.post("/agenda/portada")
@login_required
def agenda_portada():
    """Imagen de portada de la página. PNG o JPG por los primeros bytes; el
    navegador la achica antes de mandarla."""
    archivo = request.files.get("portada")
    crudo = archivo.read(PORTADA_MAX + 1) if archivo else b""
    tipo = next((t for firma, t in FIRMA_TIPOS.items() if crudo.startswith(firma)), None)
    if not crudo:
        flash("Elegí una imagen.")
    elif len(crudo) > PORTADA_MAX:
        flash("La imagen es muy pesada: hasta 600 KB.")
    elif not tipo:
        flash("La portada tiene que ser PNG o JPG.")
    else:
        tenant = db.get_tenant_by_id(session["tenant_id"])
        db.guardar_portada(session["tenant_id"], tipo, base64.b64encode(crudo).decode(),
                           int(tenant.get("portada_version") or 0) + 1)
        flash("Se guardó tu portada.")
    return redirect(url_for("agenda") + "#mi-pagina")


@app.post("/agenda/portada/borrar")
@login_required
def agenda_portada_borrar():
    db.borrar_portada(session["tenant_id"])
    flash("Se quitó la portada.")
    return redirect(url_for("agenda") + "#mi-pagina")


@app.get("/manifest.webmanifest")
def manifest():
    """Para instalar el panel como app en el celular o la compu."""
    r = jsonify({
        "name": "FiBOT Consultorio", "short_name": "Consultorio", "lang": "es-AR",
        "description": "Tu agenda, tus pacientes y tus cobros.",
        "start_url": "/agenda?app=1", "scope": "/", "display": "standalone",
        "background_color": "#faf6ef", "theme_color": "#2f5233",
        "icons": [
            {"src": url_for("static", filename="icono-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": url_for("static", filename="icono-512.png"), "sizes": "512x512", "type": "image/png"},
            {"src": url_for("static", filename="icono-maskable-512.png"), "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    })
    r.mimetype = "application/manifest+json"
    return r


@app.get("/sw.js")
def service_worker():
    """Service worker mínimo: NO guarda páginas (hay datos de pacientes); sólo
    muestra «Sin conexión» si se cae internet al navegar."""
    js = """
const OFFLINE = '/sin-conexion';
self.addEventListener('install', e => { e.waitUntil(caches.open('v1').then(c => c.add(OFFLINE))); self.skipWaiting(); });
self.addEventListener('activate', e => { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', e => {
  if (e.request.mode !== 'navigate') return;
  e.respondWith(fetch(e.request).catch(() => caches.match(OFFLINE)));
});
"""
    r = app.response_class(js, mimetype="application/javascript")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/sin-conexion")
def sin_conexion():
    return render_template("sin_conexion.html")


@app.get("/<slug>/foto")
def publico_foto(slug):
    """La foto como imagen suelta (para la vista previa al compartir el link)."""
    tenant = db.get_tenant_by_slug(slug)
    foto = (tenant or {}).get("foto") or ""
    m = re.fullmatch(r"data:(image/(?:png|jpeg));base64,([A-Za-z0-9+/=]+)", foto)
    if not m:
        return ("", 404)
    r = app.response_class(base64.b64decode(m.group(2)), mimetype=m.group(1))
    r.headers["Cache-Control"] = "public, max-age=3600"
    r.headers["X-Content-Type-Options"] = "nosniff"
    return r


@app.get("/<slug>/portada")
def publico_portada(slug):
    tenant = db.get_tenant_by_slug(slug)
    fila = db.portada(tenant["id"]) if tenant and tenant.get("portada_version") else None
    if not fila:
        return ("", 404)
    r = app.response_class(base64.b64decode(fila["datos"]), mimetype=fila["tipo"])
    # La URL lleva ?v=<versión>: puede quedar en caché, al cambiarla cambia la URL.
    r.headers["Cache-Control"] = "public, max-age=604800"
    r.headers["X-Content-Type-Options"] = "nosniff"
    return r


@app.post("/agenda/foto/borrar")
@login_required
def agenda_foto_borrar():
    db.guardar_ajustes(session["tenant_id"], {"foto": None})
    flash("Se quitó la foto.")
    return _volver_a_agenda()


@app.post("/agenda/firma/borrar")
@login_required
def agenda_firma_borrar():
    db.guardar_ajustes(session["tenant_id"], {"firma_imagen": None})
    flash("Se quitó la firma.")
    return _volver_a_agenda()


@app.post("/agenda/paciente/ficha-completa")
@login_required
def agenda_ficha_completa():
    """Toda la ficha imprimible, para un cambio de profesional o si el
    paciente pide su historia. Por POST para que el teléfono no quede en la URL."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    contexto = _contexto_ficha_completa(tenant, request.form.get("telefono") or "")
    if not contexto:
        return "No encontrado.", 404
    return render_template("ficha_completa.html", **contexto, entrega=False)


DIAS_ENTREGA = 7


@app.post("/agenda/paciente/ficha-completa/link")
@login_required
def agenda_ficha_link():
    """Copia fija de la ficha completa tal como está ahora, para abrir UNA vez
    desde un link. Se guarda el HTML: lo que se entrega es lo que se revisó."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    clave = request.form.get("telefono") or ""
    contexto = _contexto_ficha_completa(tenant, clave)
    if not contexto:
        flash("No se encontró la ficha.")
        return _volver_a_agenda()
    dni = "".join(ch for ch in request.form.get("dni") or "" if ch.isdigit())
    if dni and not 6 <= len(dni) <= 9:
        flash("El DNI tiene que tener entre 6 y 9 números.")
        return redirect(url_for("agenda"))
    html = render_template("ficha_completa.html", **contexto, entrega=True)
    vence = datetime.now(ZoneInfo("UTC")) + timedelta(days=DIAS_ENTREGA)
    token = cuestionarios.nuevo_token()
    db.crear_entrega(tenant["id"], clave, token, vence.isoformat(), html, _hash_dni(token, dni) if dni else None)
    flash("Listo el link para entregar la ficha: copialo desde la pestaña Sesiones. Se abre una sola vez y vence en 7 días."
          + (" Para abrirlo van a pedir el DNI que cargaste." if dni else ""))
    return redirect(url_for("agenda"))


MAX_INTENTOS_DNI = 5


def _hash_dni(token: str, dni: str) -> str:
    """El token hace de sal: el mismo DNI da otro hash en cada entrega."""
    return hashlib.sha256(f"{token}:{dni}".encode()).hexdigest()


@app.post("/agenda/entrega/<entrega_id>/borrar")
@login_required
def agenda_entrega_borrar(entrega_id):
    db.borrar_entrega(session["tenant_id"], entrega_id)
    flash("Se anuló el link.")
    return _volver_a_agenda()


def _sin_cache(respuesta):
    respuesta.headers["Cache-Control"] = "no-store"
    respuesta.headers["Referrer-Policy"] = "no-referrer"
    respuesta.headers["X-Robots-Tag"] = "noindex"
    return respuesta


@app.get("/entrega/<token>")
def entrega_publica(token):
    """Sólo muestra el aviso: abrir consume el link y los previsualizadores
    de WhatsApp o del mail hacen GET, así que abrir es un POST."""
    e = db.entrega_por_token(token)
    estado = _estado_entrega(e)
    tenant = db.get_tenant_by_id(e["tenant_id"]) if e else None
    return _sin_cache(app.make_response((render_template("entrega.html", tenant=tenant, estado=estado,
                                                         pide_dni=bool(e and e.get("dni_hash")), error=None),
                                         404 if not e else 200)))


@app.post("/entrega/<token>")
def entrega_abrir(token):
    e = db.entrega_por_token(token)
    estado = _estado_entrega(e)
    tenant = db.get_tenant_by_id(e["tenant_id"]) if e else None
    if estado == "lista" and e.get("dni_hash"):
        dni = "".join(ch for ch in request.form.get("dni") or "" if ch.isdigit())
        if not hmac.compare_digest(_hash_dni(token, dni), e["dni_hash"]):
            intentos = (e.get("intentos") or 0) + 1
            db.registrar_intento_entrega(e["id"], intentos)
            estado = "bloqueada" if intentos >= MAX_INTENTOS_DNI else "lista"
            return _sin_cache(app.make_response((render_template(
                "entrega.html", tenant=tenant, estado=estado, pide_dni=True,
                error=None if estado == "bloqueada" else f"El DNI no coincide. Te quedan {MAX_INTENTOS_DNI - intentos} intentos."), 200)))
    if estado != "lista" or not db.abrir_entrega(e["id"], datetime.now(ZoneInfo("UTC")).isoformat()):
        return _sin_cache(app.make_response((render_template("entrega.html", tenant=tenant, estado=estado if estado != "lista" else "abierta"),
                                             404 if not e else 200)))
    return _sin_cache(app.make_response(e["html"]))


def _estado_entrega(e: dict | None) -> str:
    if not e:
        return "no_existe"
    if e.get("abierta_at"):
        return "abierta"
    if (e.get("intentos") or 0) >= MAX_INTENTOS_DNI:
        return "bloqueada"
    if datetime.fromisoformat(e["vence"]) <= datetime.now(ZoneInfo("UTC")):
        return "vencida"
    return "lista"


def _contexto_ficha_completa(tenant: dict, clave: str) -> dict | None:
    tz = _tz_de(tenant)
    ahora = datetime.now(tz)
    rubro = rubros.de(tenant)
    ficha = next((f for f in _fichas(tenant, tz, ahora) if f["clave"] == clave), None)
    if not ficha:
        return None
    datos = [("Nombre", ficha["nombre"]), ("Teléfono", ficha["telefono"] if db.tiene_whatsapp(ficha["telefono"]) else None),
             ("Fecha de nacimiento", date.fromisoformat(ficha["fecha_nacimiento"]).strftime("%d/%m/%Y") if ficha.get("fecha_nacimiento") else None),
             ("Obra social", " · N° ".join(x for x in (ficha.get("obra_social"), ficha.get("numero_afiliado")) if x) or None),
             ("En tratamiento desde", ficha.get("desde")),
             ("Estado", "activo" if ficha["estado"] == "activo" else f"{ficha['estado']} desde {ficha.get('estado_fecha')}"
              + (f" ({ficha['estado_motivo']})" if ficha.get("estado_motivo") else ""))]
    if rubro["escuela"]:
        datos += [("Escuela", ficha.get("escuela")), ("Grado", ficha.get("grado")), ("Docente", ficha.get("docente")),
                  ("Equipo de orientación", ficha.get("equipo_orientador"))]
    textos = [(rubro["motivo"], ficha.get("motivo_consulta")), ("Objetivos", ficha.get("objetivos")),
              ("Cómo viene", ficha.get("como_viene")), ("Notas", ficha.get("notas")),
              (rubro["tratantes"], ficha.get("psiquiatra")), ("Medicación", ficha.get("medicacion")),
              (rubro["indicaciones"], ficha.get("plan_crisis"))]
    asistencia = {"vino": "asistió", "no_vino": "no vino"}
    sesiones = [{"fecha": datetime.fromisoformat(t["inicio"]).astimezone(tz).strftime("%d/%m/%Y %H:%M"),
                 "asistencia": asistencia.get(t.get("asistencia"), "—" if datetime.fromisoformat(t["inicio"]) <= ahora else "próxima"),
                 "nota": t.get("nota") or ""} for t in ficha["sesiones"]]
    return dict(tenant=tenant, ficha=ficha, rubro=rubro, hoy=ahora.strftime("%d/%m/%Y"),
                datos=[(k, v) for k, v in datos if v], textos=[(k, v) for k, v in textos if v], sesiones=sesiones,
                inf=informe.armar(ficha, rubro, ahora.date()))


@app.post("/agenda/paciente/informe/guardar")
@login_required
def agenda_informe_guardar():
    clave = request.form.get("telefono") or ""
    contenido, error = informe.leer_guardado(request.form.get("contenido") or "")
    if not clave or error:
        flash(error or "Falta el paciente.")
        return _volver_a_agenda()
    db.agregar_informe(session["tenant_id"], clave, contenido)
    flash("Se guardó el informe en la ficha.")
    return _volver_a_agenda()


@app.post("/agenda/paciente/informe/<informe_id>")
@login_required
def agenda_informe_ver(informe_id):
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    clave = request.form.get("telefono") or ""
    ficha = next((f for f in _fichas(tenant, tz, datetime.now(tz)) if f["clave"] == clave), None)
    guardado = next((i for i in (ficha or {}).get("informes", []) if i["id"] == informe_id), None)
    if not guardado:
        return "No encontrado.", 404
    c = guardado["contenido"]
    inf = {"titulo": c["titulo"], "datos": c.get("datos") or [], "secciones": c["secciones"]}
    return render_template("informe.html", tenant=tenant, ficha=ficha, hoy=guardado["fecha"], inf=inf, guardado=guardado)


@app.post("/agenda/paciente/informe/<informe_id>/borrar")
@login_required
def agenda_informe_borrar(informe_id):
    db.borrar_informe(session["tenant_id"], informe_id)
    return _volver_a_agenda()


@app.post("/agenda/paciente/test/<test_id>/borrar")
@login_required
def agenda_test_borrar(test_id):
    db.borrar_test(session["tenant_id"], test_id)
    return _volver_a_agenda()


@app.post("/agenda/paciente/fecha/<fecha_id>/borrar")
@login_required
def agenda_fecha_borrar(fecha_id):
    db.borrar_fecha(session["tenant_id"], fecha_id)
    return _volver_a_agenda()


@app.post("/agenda/modalidad/<turno_id>")
@login_required
def agenda_modalidad(turno_id):
    modalidad = request.form.get("modalidad")
    if modalidad not in MODALIDADES:
        return "modalidad inválida", 400
    db.marcar_modalidad(session["tenant_id"], turno_id, modalidad)
    return _volver_a_agenda()


@app.post("/agenda/nota/<turno_id>")
@login_required
def agenda_nota(turno_id):
    db.marcar_nota(session["tenant_id"], turno_id, (request.form.get("nota") or "").strip() or None)
    return _volver_a_agenda()


@app.post("/agenda/agendar/<turno_id>")
@login_required
def agenda_agendar(turno_id):
    """El profesional anota a un paciente en un horario libre — sus pacientes
    fijos no van a entrar por la página pública a reservar cada semana. Es la
    misma reserva atómica que usan WhatsApp y la página pública."""
    nombre = (request.form.get("nombre") or "").strip()
    # Solo dígitos, mismo motivo que en la página pública: el recordatorio
    # sale a este número tal cual.
    telefono = "".join(ch for ch in (request.form.get("telefono") or "") if ch.isdigit())
    if not nombre:
        flash("Para agendar hace falta el nombre del paciente.")
        return _volver_a_agenda()
    tenant = db.get_tenant_by_id(session["tenant_id"])
    otra_persona = bool(request.form.get("otra_persona"))
    if not telefono:
        # El WhatsApp es opcional: sin él no salen recordatorios ni avisos, y
        # se puede cargar después desde la ficha.
        telefono = db.clave_sin_whatsapp(tenant["id"], nombre, otra_persona)
    valor = _valor_del_form()
    frecuencia = request.form.get("frecuencia") or None
    if frecuencia not in FRECUENCIAS:
        frecuencia = None
    modalidad = request.form.get("modalidad")
    if modalidad not in MODALIDADES:
        modalidad = None
    turno = db.turno_por_id(tenant["id"], turno_id)
    if db.tiene_whatsapp(telefono):
        ambiguo = not otra_persona and db.nombre_ambiguo(tenant["id"], telefono, nombre)
        ficha, ficha_nueva = (db.ficha_aparte(tenant["id"], telefono, nombre), False) if otra_persona \
            else db.ficha_para(tenant["id"], telefono, nombre)
    else:
        ambiguo, ficha, ficha_nueva = False, "", False
    if not turno or not db.reservar_turno(tenant["id"], turno_id, telefono, nombre, valor, frecuencia, modalidad, ficha,
                                          ignorar_bloqueo=True):
        flash("Ese horario ya no está libre.")
        return _volver_a_agenda()
    _avisar_si_bloqueado(tenant, turno["inicio"], turno["fin"])
    if ambiguo:
        flash(f"Ese WhatsApp tiene más de una ficha que coincide con {nombre}: quedó en la más reciente. "
              "Si no es esa, pasá la sesión desde la pestaña Sesiones.")
    if ficha_nueva:
        flash(f"{nombre} quedó en una ficha aparte porque ese WhatsApp ya lo usa otro paciente. "
              "Si es la misma persona, pasá la sesión desde la pestaña Sesiones.")
    if not frecuencia:
        return _volver_a_agenda()

    try:
        cantidad = min(int(request.form.get("cantidad") or 1), MAX_SESIONES_FIJAS)
    except ValueError:
        cantidad = 1
    tz = _tz_de(tenant)
    primera = datetime.fromisoformat(turno["inicio"]).astimezone(tz)
    duracion = int((datetime.fromisoformat(turno["fin"]) - primera).total_seconds() // 60)
    salteadas = _agendar_serie(tenant, primera, duracion, range(1, cantidad), telefono, nombre, valor, frecuencia, modalidad, ficha)
    flash(f"Se agendaron {cantidad - len(salteadas)} sesiones ({frecuencia}).")
    if salteadas:
        flash(f"Quedaron sin agendar por estar ocupadas: {', '.join(salteadas)}.")
    return _volver_a_agenda()


def _agendar_serie(tenant: dict, base: datetime, duracion: int, pasos: range, telefono: str,
                   nombre: str, valor: int | None, frecuencia: str, modalidad: str | None = None,
                   ficha: str | None = None) -> list[str]:
    """Cada sesión es su propia fila: si después una se mueve o se cancela, las
    demás no se enteran. Un horario ocupado se saltea y se devuelve, para que
    el profesional decida qué hacer con esa semana."""
    salteadas = []
    for i in pasos:
        siguiente = base + timedelta(days=FRECUENCIAS[frecuencia] * i)
        if not db.reservar_en_horario(tenant["id"], siguiente.astimezone(ZoneInfo("UTC")), duracion,
                                      telefono, nombre, valor, frecuencia, modalidad, ficha):
            salteadas.append(f"{siguiente.day}/{siguiente.month:02d}")
    return salteadas


@app.post("/agenda/paciente/renovar")
@login_required
def agenda_renovar():
    """Sigue la serie desde la última sesión, con el mismo horario y valor. Si
    ya se cortó hace unas semanas, retoma desde la próxima fecha que le toca."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    telefono = request.form.get("telefono") or ""
    sesiones = [t for t in db.sesiones_reservadas(tenant["id"]) if db.clave_de(t) == telefono]
    if not sesiones or sesiones[-1].get("frecuencia") not in FRECUENCIAS:
        flash("Ese paciente no tiene una frecuencia fija.")
        return _volver_a_agenda()
    ultima = sesiones[-1]
    tz = _tz_de(tenant)
    base = datetime.fromisoformat(ultima["inicio"]).astimezone(tz)
    duracion = int((datetime.fromisoformat(ultima["fin"]).astimezone(tz) - base).total_seconds() // 60)
    paso = timedelta(days=FRECUENCIAS[ultima["frecuencia"]])
    ahora = datetime.now(tz)
    while base + paso <= ahora:
        base += paso
    salteadas = _agendar_serie(tenant, base, duracion, range(1, SESIONES_AL_RENOVAR + 1), ultima["paciente_telefono"],
                               ultima.get("paciente_nombre"), ultima.get("valor"), ultima["frecuencia"], ultima.get("modalidad"),
                               ultima.get("ficha") or "")
    flash(f"Se renovaron {SESIONES_AL_RENOVAR - len(salteadas)} sesiones de {ultima.get('paciente_nombre') or ultima['paciente_telefono']}.")
    if salteadas:
        flash(f"Quedaron sin agendar por estar ocupadas: {', '.join(salteadas)}.")
    return _volver_a_agenda()


# ---------- página pública de reserva (sin login) ----------
# consultorio.fibot.ar/<slug> — el link que cada profesional comparte con
# sus pacientes. Solo existe para tenants con `slug` cargado (ver
# scripts/crear_tenant.py); un tenant sin slug simplemente no tiene página
# pública y sigue funcionando como antes (WhatsApp + panel).

def _con_hora_local(turnos: list[dict], tz: ZoneInfo) -> list[dict]:
    resultado = []
    for t in turnos:
        inicio_local = datetime.fromisoformat(t["inicio"]).astimezone(tz)
        resultado.append(
            {
                **t,
                "_dia": f"{DIAS_LABEL[inicio_local.weekday()]} {inicio_local.day}/{inicio_local.month:02d}",
                "_hora": inicio_local.strftime("%H:%M"),
            }
        )
    return resultado


@app.get("/privacidad")
def privacidad():
    """Política de privacidad pública (la pide Meta para publicar la app de WhatsApp)."""
    return render_template("privacidad.html", actualizada="3 de noviembre de 2026",
                           contacto=os.environ.get("CONTACTO_PRIVACIDAD") or "la administración de FiBOT")


def _proximo_con_semana(turnos: list[dict], tz: ZoneInfo) -> dict | None:
    """El primero de la lista con su día LOCAL (para el link a su semana: el
    día en UTC puede ser el siguiente)."""
    if not turnos:
        return None
    t = _con_hora_local(turnos[:1], tz)[0]
    t["_semana"] = datetime.fromisoformat(t["inicio"]).astimezone(tz).date().isoformat()
    return t


@app.get("/agenda/mi-pagina-qr.svg")
@login_required
def agenda_qr_pagina():
    """QR de la página de turnos del profesional, para imprimir o mostrar."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    if not tenant or not tenant.get("slug"):
        return "", 404
    svg = seguridad.qr_svg(url_for("publico", slug=tenant["slug"], _external=True), escala=6)
    return app.response_class(svg, mimetype="image/svg+xml")


DIAS_CORTOS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
PROXIMOS_EN_DIRECTORIO = 4
_proximos_cache: dict[str, tuple[float, list[dict]]] = {}


def _proximos_libres(tenant: dict) -> list[dict]:
    """Los próximos horarios libres de un profesional para el directorio:
    «Jue 6 · 18:00», cada uno con su link. Se guardan 60 segundos en memoria
    (hay un solo worker) para no consultar la base por cada tarjeta en cada
    visita. Con varios tipos de turno el link va a la semana, porque primero
    hay que elegir el tipo. Si la base falla, sin horarios."""
    if tenant.get("pausado"):
        return []
    guardado = _proximos_cache.get(tenant["id"])
    if guardado and time.monotonic() - guardado[0] < 60:
        return guardado[1]
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    try:
        libres = db.turnos_libres(tenant["id"], datetime.now(ZoneInfo("UTC")), limite=PROXIMOS_EN_DIRECTORIO)
    except Exception:
        logger.exception("directorio: no se pudieron leer los horarios de %s", tenant.get("slug"))
        return []
    hoy = datetime.now(tz).date()
    con_tipos = bool(tipos_de(tenant))
    salida = []
    for t in libres:
        local = datetime.fromisoformat(t["inicio"]).astimezone(tz)
        dia = "Hoy" if local.date() == hoy else f"{DIAS_CORTOS[local.weekday()]} {local.day}"
        url = (url_for("publico", slug=tenant["slug"], semana=local.date().isoformat()) if con_tipos
               else url_for("publico_reservar_form", slug=tenant["slug"], turno_id=t["id"]))
        salida.append({"texto": f"{dia} · {local:%H:%M}", "url": url, "inicio": local,
                       "esta_semana": (local.date() - hoy).days < 7})
    _proximos_cache[tenant["id"]] = (time.monotonic(), salida)
    return salida


ESPECIALIDADES = {"psicologia": "Psicología", "psicopedagogia": "Psicopedagogía"}


def _especialidad(tenant: dict) -> str:
    rubro = directorio.normalizar(tenant.get("rubro"))
    return next((k for k in ESPECIALIDADES if rubro.startswith(k)), "")


@app.get("/turnos")
def turnos_directorio():
    """La puerta de entrada para pacientes: todos los profesionales con página
    pública. Cada uno lleva a SU calendario (/<slug>), que es también el link
    que el profesional comparte. Filtros: especialidad, tema, modalidad, obra
    social y «con turno esta semana»; orden por próximo turno o por nombre."""
    tema = request.args.get("tema", "") if request.args.get("tema") in directorio.TEMAS else ""
    modalidad = request.args.get("modalidad", "") if request.args.get("modalidad") in directorio.MODALIDADES else ""
    obra = " ".join((request.args.get("obra") or "").split())[:60]
    esp = request.args.get("especialidad", "") if request.args.get("especialidad") in ESPECIALIDADES else ""
    semana = request.args.get("semana") == "1"
    orden = "nombre" if request.args.get("orden") == "nombre" else "turno"
    todos = sorted(_directorio(), key=lambda t: (directorio.es_demo(t), t["nombre"].casefold()))
    cuenta_esp = {"": len(todos), **{k: sum(1 for t in todos if _especialidad(t) == k) for k in ESPECIALIDADES}}
    profesionales = directorio.filtrar([t for t in todos if not esp or _especialidad(t) == esp], tema, modalidad, obra)
    profesionales = [dict(t, _proximos=_proximos_libres(t), _especialidad=_especialidad(t)) for t in profesionales]
    if semana:
        profesionales = [t for t in profesionales if t["_proximos"] and t["_proximos"][0]["esta_semana"]]
    if orden == "turno":
        # Primero el que tiene el horario libre más cercano; los pausados y los
        # que no tienen horarios, al final (por nombre, el orden de `todos`).
        lejos = datetime.max.replace(tzinfo=ZoneInfo("UTC"))
        profesionales.sort(key=lambda t: (directorio.es_demo(t), t["_proximos"][0]["inicio"] if t["_proximos"] else lejos))
    filtros = {"especialidad": esp or None, "tema": tema or None, "modalidad": modalidad or None,
               "obra": obra or None, "semana": "1" if semana else None, "orden": "nombre" if orden == "nombre" else None}
    temas_esp = (directorio.TEMAS_ADULTOS if esp == "psicologia" else directorio.TEMAS_CHICOS if esp == "psicopedagogia"
                 else directorio.TEMAS_ADULTOS + directorio.TEMAS_CHICOS)
    return render_template("turnos.html", profesionales=profesionales, hay_profesionales=bool(todos),
                           temas=directorio.TEMAS, temas_esp=temas_esp, tema=tema, modalidad=modalidad, obra=obra,
                           especialidad=esp, especialidades=ESPECIALIDADES, cuenta_esp=cuenta_esp,
                           semana=semana, orden=orden, f=filtros,
                           filtrando=bool(tema or modalidad or obra or esp or semana))


def tipos_de(tenant: dict) -> list[dict]:
    """Tipos de turno válidos del profesional ([] = uno solo, el de siempre)."""
    tipos = []
    for t in tenant.get("tipos_turno") or []:
        try:
            nombre, minutos = str(t["nombre"]).strip()[:60], int(t["minutos"])
        except (KeyError, TypeError, ValueError):
            continue
        if nombre and 10 <= minutos <= 240:
            precio = t.get("precio")
            tipos.append({"nombre": nombre, "minutos": minutos,
                          "precio": int(precio) if isinstance(precio, (int, float)) and precio > 0 else None})
    return tipos


def _tipo_elegido(tenant: dict, valor) -> tuple[int | None, dict | None]:
    tipos = tipos_de(tenant)
    if len(tipos) == 1 and valor in (None, ""):
        return 0, tipos[0]
    try:
        i = int(valor)
    except (TypeError, ValueError):
        return None, None
    return (i, tipos[i]) if 0 <= i < len(tipos) else (None, None)


def _cabe(turno: dict, minutos: int, libres_por_inicio: dict, descanso: int = 0) -> bool:
    """¿Entra un turno de `minutos` empezando en `turno`? Si dura más que el
    horario, tiene que seguir en horarios libres consecutivos (así no se pasa
    del horario de atención ni pisa otra sesión). Entre uno y otro puede haber
    a lo sumo el descanso del profesional (con 45 + 15, el de 10:00 sigue en
    el de 10:15)."""
    inicio = datetime.fromisoformat(turno["inicio"])
    fin_pedido = inicio + timedelta(minutes=minutos)
    cursor = datetime.fromisoformat(turno["fin"]) if turno.get("fin") else inicio
    while cursor < fin_pedido:
        siguiente = next((libres_por_inicio[cursor + timedelta(minutes=m)] for m in range(0, descanso + 1)
                          if cursor + timedelta(minutes=m) in libres_por_inicio), None)
        if not siguiente:
            # Si lo que sobra entra en el descanso (ej. 50 min en un horario
            # de 45 con 15 de descanso), entra sin tocar el siguiente.
            return fin_pedido <= cursor + timedelta(minutes=descanso)
        cursor = datetime.fromisoformat(siguiente["fin"])
    return True


def _semana_libre(tenant: dict, semana: str | None, minutos: int | None = None) -> dict:
    """Una semana (lunes a domingo, en la zona del profesional) con sus
    horarios libres. Sin semana elegida: la del próximo horario libre."""
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    ahora = datetime.now(ZoneInfo("UTC"))
    esta_semana = ahora.astimezone(tz).date() - timedelta(days=ahora.astimezone(tz).weekday())
    try:
        lunes = date.fromisoformat(semana or "")
        lunes -= timedelta(days=lunes.weekday())
    except ValueError:
        lunes = None
    if not lunes or lunes < esta_semana:
        proximo = db.turnos_libres(tenant["id"], ahora, limite=1)
        lunes = esta_semana
        if proximo and semana is None:
            dia = datetime.fromisoformat(proximo[0]["inicio"]).astimezone(tz).date()
            lunes = dia - timedelta(days=dia.weekday())
    desde = max(ahora, datetime.combine(lunes, dtime(0), tz))
    hasta = datetime.combine(lunes + timedelta(days=7), dtime(0), tz)
    todos = db.turnos_libres(tenant["id"], desde, limite=300)
    libres = [t for t in todos if desde <= datetime.fromisoformat(t["inicio"]) < hasta]
    if minutos:
        por_inicio = {datetime.fromisoformat(t["inicio"]): t for t in todos if t.get("fin")}
        libres = [t for t in libres if _cabe(t, minutos, por_inicio, tenant.get("descanso_minutos") or 0)]
    por_dia = {lunes + timedelta(days=i): [] for i in range(7)}
    for t in _con_hora_local(libres, tz):
        dia = datetime.fromisoformat(t["inicio"]).astimezone(tz).date()
        if dia in por_dia:
            por_dia[dia].append(t)
    dias = [{"fecha": d, "nombre": DIAS_LABEL[d.weekday()], "numero": f"{d.day}/{d.month:02d}",
             "pasado": d < ahora.astimezone(tz).date(), "turnos": ts} for d, ts in por_dia.items()]
    domingo = lunes + timedelta(days=6)
    return {"dias": dias, "hay": bool(libres),
            "anterior": (lunes - timedelta(days=7)).isoformat() if lunes > esta_semana else None,
            "siguiente": (lunes + timedelta(days=7)).isoformat(),
            "proximo_despues": _proximo_con_semana(db.turnos_libres(tenant["id"], hasta, limite=1), tz),
            "titulo_semana": f"{lunes.day}/{lunes.month:02d} al {domingo.day}/{domingo.month:02d}"}


def _mes_libre(tenant: dict, mes: str | None, minutos: int | None) -> dict:
    """Un mes de calendario (lunes a domingo) con cuántos horarios libres hay
    cada día, para saltar a la semana del día que le sirve."""
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    ahora = datetime.now(ZoneInfo("UTC"))
    hoy = ahora.astimezone(tz).date()
    try:
        primero = date.fromisoformat((mes or "") + "-01")
    except ValueError:
        primero = None
    if not primero or primero < hoy.replace(day=1):
        primero = hoy.replace(day=1)
    siguiente = (primero.replace(day=28) + timedelta(days=4)).replace(day=1)
    desde = max(ahora, datetime.combine(primero, dtime(0), tz))
    hasta = datetime.combine(siguiente, dtime(0), tz)
    todos = db.turnos_libres(tenant["id"], desde, limite=2000)
    libres = [t for t in todos if desde <= datetime.fromisoformat(t["inicio"]) < hasta]
    if minutos:
        por_inicio = {datetime.fromisoformat(t["inicio"]): t for t in todos if t.get("fin")}
        libres = [t for t in libres if _cabe(t, minutos, por_inicio, tenant.get("descanso_minutos") or 0)]
    cuenta: dict = {}
    for t in libres:
        d = datetime.fromisoformat(t["inicio"]).astimezone(tz).date()
        cuenta[d] = cuenta.get(d, 0) + 1
    inicio_grilla = primero - timedelta(days=primero.weekday())
    semanas = []
    d = inicio_grilla
    while d < siguiente:
        semanas.append([{"fecha": d + timedelta(days=i), "numero": (d + timedelta(days=i)).day,
                         "del_mes": (d + timedelta(days=i)).month == primero.month,
                         "libres": cuenta.get(d + timedelta(days=i), 0),
                         "lunes": d.isoformat()} for i in range(7)])
        d += timedelta(days=7)
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
             "noviembre", "diciembre"]
    anterior = (primero - timedelta(days=1)).replace(day=1)
    return {"semanas_mes": semanas, "titulo_mes": f"{meses[primero.month - 1].capitalize()} {primero.year}",
            "mes_anterior": anterior.strftime("%Y-%m") if anterior >= hoy.replace(day=1) else None,
            "mes_siguiente": siguiente.strftime("%Y-%m"), "hay_en_mes": bool(libres)}


@app.get("/<slug>")
def publico(slug):
    """Calendario de UN profesional, de a una semana, sólo con los horarios
    libres."""
    tenant = db.get_tenant_by_slug(slug)
    if not tenant:
        return "No encontrado.", 404
    if "bot" not in (request.user_agent.string or "").lower() and request.args.get("semana") is None:
        _evento(tenant, "visita")
    if tenant.get("pausado"):
        return render_template("publico.html", tenant=tenant, cambiar=None, pausado=True, dias=[], hay=False,
                               anterior=None, siguiente=None, proximo_despues=None, titulo_semana="",
                               hay_directorio=False)
    tipos = tipos_de(tenant)
    i_tipo, tipo = _tipo_elegido(tenant, request.args.get("tipo"))
    if tipos and tipo is None:
        return render_template("publico.html", tenant=tenant, cambiar=None, tipos=tipos, elegir_tipo=True, dias=[], hay=False,
                               anterior=None, siguiente=None, proximo_despues=None, titulo_semana="",
                               hay_directorio=sum(1 for t in db.tenants_activos()
                                                  if t.get("slug") and t.get("en_directorio", True) is not False) > 1)
    if request.args.get("vista") == "mes":
        return render_template("publico.html", tenant=tenant, cambiar=None, tipos=tipos, tipo=tipo, i_tipo=i_tipo,
                               vista_mes=True, dias=[], hay=True, anterior=None, siguiente=None, proximo_despues=None,
                               titulo_semana="", **_mes_libre(tenant, request.args.get("mes"), tipo["minutos"] if tipo else None),
                               hay_directorio=sum(1 for t in db.tenants_activos()
                                                  if t.get("slug") and t.get("en_directorio", True) is not False) > 1)
    return render_template("publico.html", tenant=tenant, cambiar=None, tipos=tipos, tipo=tipo, i_tipo=i_tipo,
                           serie_posible=pagos.activo(tenant),
                           **_semana_libre(tenant, request.args.get("semana"), tipo["minutos"] if tipo else None),
                           hay_directorio=sum(1 for t in db.tenants_activos()
                                              if t.get("slug") and t.get("en_directorio", True) is not False) > 1)


@app.get("/<slug>/reservar/<turno_id>")
def publico_reservar_form(slug, turno_id):
    tenant = db.get_tenant_by_slug(slug)
    if not tenant:
        return "No encontrado.", 404
    turno = db.turno_por_id(tenant["id"], turno_id)
    if tenant.get("pausado") or not turno or turno["estado"] != "libre" or turno.get("hueco"):
        flash("Ese horario ya no está disponible — elegí otro.")
        return redirect(url_for("publico", slug=slug))
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    i_tipo, tipo = _tipo_elegido(tenant, request.args.get("tipo"))
    if tipos_de(tenant) and tipo is None:
        return redirect(url_for("publico", slug=slug))
    turno = _con_hora_local([turno], tz)[0]
    return render_template("publico_reservar.html", tenant=tenant, turno=turno, tipo=tipo, i_tipo=i_tipo,
                           serie_posible=pagos.activo(tenant))


def _parcial() -> bool:
    """Reserva desde la misma página del profesional (/<slug>): el navegador
    pide sólo el pedazo que cambia (revisar, listo o el error), no la página."""
    return request.headers.get("X-Parcial") == "1"


def _error_reserva(mensaje: str, destino: str, otro: bool = False):
    """Un error al reservar: en la misma página vuelve como aviso dentro de la
    tarjeta (`otro`: hay que elegir otro horario); si no, flash y redirect."""
    if _parcial():
        return render_template("_reserva_error.html", mensaje=mensaje, otro=otro), 409
    flash(mensaje)
    return redirect(destino)


@app.post("/<slug>/reservar/<turno_id>")
def publico_reservar(slug, turno_id):
    tenant = db.get_tenant_by_slug(slug)
    if not tenant:
        return "No encontrado.", 404
    nombre = (request.form.get("nombre") or "").strip()
    # Solo dígitos: lo que se manda por WhatsApp (recordatorio, y si el
    # paciente después le escribe al bot) necesita el número en formato
    # internacional sin espacios ni símbolos — se limpia acá para no
    # depender de que el paciente lo tipee bien.
    telefono = "".join(ch for ch in (request.form.get("telefono") or "") if ch.isdigit())
    if not nombre or not telefono:
        return _error_reserva("Completá nombre y teléfono.", url_for("publico_reservar_form", slug=slug, turno_id=turno_id))
    # Email opcional, sólo cuando la reserva se paga: Mercado Pago lo usa
    # para aprobar el pago y se le manda el recibo. No va a la ficha.
    email = (request.form.get("email") or "").strip()[:120] if pagos.activo(tenant) else ""
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return _error_reserva("Revisá el email (o dejalo vacío: es opcional).",
                              url_for("publico_reservar_form", slug=slug, turno_id=turno_id))
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    modalidad = _modalidad_publica(tenant, request.form.get("modalidad"), db.turno_por_id(tenant["id"], turno_id))
    i_tipo, tipo = _tipo_elegido(tenant, request.form.get("tipo"))
    if tipos_de(tenant) and tipo is None:
        return _error_reserva("Elegí qué tipo de turno querés.", url_for("publico", slug=slug))
    if tipo and not _entra(tenant, turno_id, tipo["minutos"]):
        return _error_reserva(f"En ese horario no entra un turno de {tipo['minutos']} minutos — elegí otro.",
                              url_for("publico", slug=slug, tipo=i_tipo))
    # Varias sesiones (mismo día y hora, semanas seguidas): sólo pagando por
    # adelantado. Se reservan las que estén libres; las que no, se avisan.
    sesiones = int(request.form.get("sesiones")) if request.form.get("sesiones") in ("4", "8") and pagos.activo(tenant) else 1
    serie, faltan = _serie(tenant, turno_id, sesiones, tipo["minutos"] if tipo else None) if sesiones > 1 else ([], [])
    if request.form.get("confirmado") != "1":
        # Doble confirmación: primero se muestra todo junto (con quién, cuándo
        # y a qué WhatsApp) y recién el segundo POST reserva. No se toma el
        # horario en este paso: si otro lo reserva mientras tanto, se avisa.
        turno = db.turno_por_id(tenant["id"], turno_id)
        if tenant.get("pausado") or not turno or turno["estado"] != "libre" or turno.get("hueco"):
            return _error_reserva("Ese horario ya no está disponible — elegí otro.", url_for("publico", slug=slug), otro=True)
        if len(telefono) < 10:
            return _error_reserva("Revisá el WhatsApp: tiene que ir completo, con código de país (ej. 5491123456789).",
                                  url_for("publico_reservar_form", slug=slug, turno_id=turno_id))
        return render_template("_reserva_revisar.html" if _parcial() else "publico_revisar.html", tenant=tenant, turno=_con_hora_local([turno], tz)[0],
                               nombre=nombre, telefono=telefono, email=email, modalidad=modalidad, tipo=tipo, i_tipo=i_tipo,
                               a_pagar=pagos.monto_de(tenant, (tipo or {}).get("precio"), 1 + len(serie)) if pagos.activo(tenant) else None,
                               sesiones=sesiones, serie=_con_hora_local(serie, tz), faltan=faltan,
                               confirma_por_whatsapp=bool(config.CONFIRMACION_TEMPLATE))
    previo = db.turno_por_id(tenant["id"], turno_id)
    if tenant.get("pausado") or (previo or {}).get("hueco") or not db.reservar_turno(tenant["id"], turno_id, telefono, nombre):
        return _error_reserva("Ese horario ya no está disponible — elegí otro.", url_for("publico", slug=slug), otro=True)
    _evento(tenant, "reserva")
    token = secrets.token_urlsafe(16)
    campos = {"token_calendario": token, "modalidad": None if modalidad == "presencial" else modalidad}
    turno = db.turno_por_id(tenant["id"], turno_id)
    if tipo and turno:
        campos["tipo"] = tipo["nombre"]
        if tipo.get("precio"):
            campos["valor"] = tipo["precio"]
        campos["fin"] = (datetime.fromisoformat(turno["inicio"]) + timedelta(minutes=tipo["minutos"])).isoformat()
    if turno and not campos.get("valor") and tenant.get("precio_sesion"):
        campos["valor"] = tenant["precio_sesion"]
    if turno and serie:
        campos["frecuencia"] = "semanal"
    monto = pagos.monto_de(tenant, (tipo or {}).get("precio")) if pagos.activo(tenant) else None
    if turno and monto:
        # Se cobra: la reserva queda apartada hasta que pague (no se confirma
        # todavía ni le llega el WhatsApp de confirmación).
        db.actualizar_reserva(tenant["id"], turno_id, campos)
        reservados = [{**turno, **campos}]
        for t in serie:
            if db.reservar_turno(tenant["id"], t["id"], telefono, nombre, ficha=turno.get("ficha")):
                extra = {k: v for k, v in campos.items() if k in ("modalidad", "tipo", "valor", "frecuencia")}
                if tipo:
                    extra["fin"] = (datetime.fromisoformat(t["inicio"]) + timedelta(minutes=tipo["minutos"])).isoformat()
                db.actualizar_reserva(tenant["id"], t["id"], extra)
                reservados.append({**t, **extra})
        pago = pagos.iniciar(tenant, reservados, monto * len(reservados), email or None)
        if _parcial():  # reserva desde la misma página: el navegador va al pago
            return "", 200, {"X-Ir-A": url_for("pago_paciente", token=pago["token"])}
        return redirect(url_for("pago_paciente", token=pago["token"]))
    enviada = scheduler.mandar_confirmacion(tenant, {**turno, **campos}, tz) if turno else False
    if enviada:
        campos["confirmacion_enviada_at"] = datetime.now(ZoneInfo("UTC")).isoformat()
    db.actualizar_reserva(tenant["id"], turno_id, campos)
    turno = _con_hora_local([{**turno, **campos}], tz)[0]
    return render_template("_reserva_lista.html" if _parcial() else "publico_confirmado.html",
                           tenant=tenant, turno=turno, enviada=enviada, token=token, nombre=nombre)


def _serie(tenant: dict, turno_id: str, sesiones: int, minutos: int | None) -> tuple[list[dict], list[str]]:
    """Los horarios libres de las próximas `sesiones - 1` semanas, el mismo
    día y hora que `turno_id` (donde entre el tipo). Devuelve (libres, fechas
    que no están libres)."""
    turno = db.turno_por_id(tenant["id"], turno_id)
    if not turno:
        return [], []
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    inicio = datetime.fromisoformat(turno["inicio"])
    libres, faltan = [], []
    for k in range(1, sesiones):
        cuando = inicio + timedelta(days=7 * k)
        candidato = next((t for t in db.turnos_libres(tenant["id"], cuando, limite=1)
                          if datetime.fromisoformat(t["inicio"]) == cuando), None)
        if candidato and (not minutos or _entra(tenant, candidato["id"], minutos)):
            libres.append(candidato)
        else:
            faltan.append(cuando.astimezone(tz).strftime("%d/%m"))
    return libres, faltan


def _entra(tenant: dict, turno_id: str, minutos: int) -> bool:
    """Chequeo en el servidor (no alcanza con que el calendario no lo
    muestre): el turno de `minutos` entra en ese horario libre."""
    turno = db.turno_por_id(tenant["id"], turno_id)
    if not turno or turno["estado"] != "libre":
        return False
    inicio = datetime.fromisoformat(turno["inicio"])
    libres = db.turnos_libres(tenant["id"], inicio, limite=30)
    return _cabe(turno, minutos, {datetime.fromisoformat(t["inicio"]): t for t in libres if t.get("fin")},
                 tenant.get("descanso_minutos") or 0)


MODALIDADES_PUBLICAS = ("presencial", "virtual", "ambas")
HORAS_CANCELAR = (0, 2, 6, 12, 24, 48, 72)
HORAS_VENCE_HUECO = (0, 1, 2, 4, 8, 12, 24)


def _modalidad_publica(tenant: dict, pedida: str | None, horario: dict | None = None) -> str:
    """La modalidad de una reserva web: la del horario si su franja la fija,
    si no la que ofrece el profesional, o la elegida si ofrece las dos."""
    if horario and horario.get("solo_modalidad"):
        return horario["solo_modalidad"]
    ofrece = tenant.get("modalidades_publicas") or "presencial"
    if ofrece == "ambas":
        return "virtual" if pedida == "virtual" else "presencial"
    return ofrece


def _turno_para_agendar(token: str):
    turno = db.turno_por_token_calendario(token)
    tenant = db.get_tenant_by_id(turno["tenant_id"]) if turno else None
    if not turno or not tenant or datetime.fromisoformat(turno["inicio"]) < datetime.now(ZoneInfo("UTC")) - timedelta(days=1):
        return None, None
    return turno, tenant


def _sin_indexar(r):
    r.headers["X-Robots-Tag"] = "noindex"
    r.headers["Referrer-Policy"] = "no-referrer"
    return r


@app.get("/agendar/<token>")
def agendar(token):
    """Para sumar el turno al calendario del paciente (Google, iPhone,
    Outlook). Sin el nombre del paciente: sólo con quién, cuándo y dónde."""
    turno, tenant = _turno_para_agendar(token)
    if not turno:
        return _sin_indexar(app.make_response((render_template("agendar.html", turno=None, tenant=None), 404)))
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    inicio = datetime.fromisoformat(turno["inicio"]).astimezone(ZoneInfo("UTC"))
    fin = datetime.fromisoformat(turno["fin"]).astimezone(ZoneInfo("UTC")) if turno.get("fin") else inicio + timedelta(minutes=50)
    virtual = turno.get("modalidad") == "virtual"
    detalle = f"Turno con {tenant['nombre']}" + (f". Videollamada: {tenant['link_videollamada']}" if virtual and tenant.get("link_videollamada") else "")
    google = "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE", "text": f"Turno con {tenant['nombre']}",
        "dates": f"{inicio:%Y%m%dT%H%M%SZ}/{fin:%Y%m%dT%H%M%SZ}", "details": detalle})
    return _sin_indexar(app.make_response(render_template(
        "agendar.html", turno=_con_hora_local([turno], tz)[0], tenant=tenant, google=google, virtual=virtual,
        futuro=datetime.fromisoformat(turno["inicio"]) > datetime.now(ZoneInfo("UTC")),
        puede_modificar=_puede_modificar(turno, tenant),
        horas_limite=tenant.get("cancelar_hasta_horas") if tenant.get("cancelar_hasta_horas") is not None else 24,
        cambiado=request.args.get("cambiado") == "1")))


def _evento(tenant: dict, tipo: str) -> None:
    """Para las estadísticas de la página. Si falla, no importa."""
    try:
        db.registrar_evento_pagina(tenant["id"], tipo)
    except Exception:
        logger.warning("No se pudo registrar el evento %s", tipo)


def _puede_modificar(turno: dict, tenant: dict) -> bool:
    return cancelaciones.puede_modificar(turno, tenant)


def _tarde(tenant: dict, turno: dict):
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    return _sin_indexar(app.make_response(render_template(
        "agendar_accion.html", tenant=tenant, accion="tarde", turno=_con_hora_local([turno], tz)[0],
        token=None, horas=tenant.get("cancelar_hasta_horas") if tenant.get("cancelar_hasta_horas") is not None else 24)))


@app.post("/<slug>/lista-espera")
def publico_espera(slug):
    """Anotarse en la lista de espera desde la web (cuando no hay un horario
    que le sirva). El profesional la ve en su panel y ofrece a mano, como
    siempre; acá sólo se le avisa que alguien se anotó."""
    tenant = db.get_tenant_by_slug(slug)
    if not tenant:
        return "No encontrado.", 404
    nombre = (request.form.get("nombre") or "").strip()[:120]
    telefono = "".join(ch for ch in (request.form.get("telefono") or "") if ch.isdigit())
    preferencia = (request.form.get("preferencia") or "").strip()[:200] or None
    if not nombre or len(telefono) < 10:
        flash("Para anotarte hacen falta tu nombre y tu WhatsApp completo, con código de país (ej. 5491123456789).")
        return redirect(url_for("publico", slug=slug) + "#espera")
    nuevo = db.agregar_a_espera(tenant["id"], nombre, telefono, preferencia, origen="web")
    if nuevo:
        _evento(tenant, "espera")
        scheduler.avisar_profesional(tenant, "alguien se anotó en tu lista de espera desde tu página de turnos")
    return render_template("espera_listo.html", tenant=tenant, ya_estaba=not nuevo)


def _minutos_de(turno: dict) -> int | None:
    if not turno.get("fin"):
        return None
    return int((datetime.fromisoformat(turno["fin"]) - datetime.fromisoformat(turno["inicio"])).total_seconds() // 60)


def _turno_futuro_de_token(token: str):
    """Para cancelar o cambiar desde la web: sólo turnos que todavía no
    empezaron."""
    turno, tenant = _turno_para_agendar(token)
    if not turno or datetime.fromisoformat(turno["inicio"]) <= datetime.now(ZoneInfo("UTC")):
        return None, None
    return turno, tenant


def _ya_no_sirve():
    return _sin_indexar(app.make_response((render_template("agendar.html", turno=None, tenant=None), 404)))


@app.route("/agendar/<token>/cancelar", methods=["GET", "POST"])
def agendar_cancelar(token):
    """El paciente cancela desde la web. GET pregunta, POST cancela (queda
    registrada como cancelación del paciente, igual que por WhatsApp)."""
    turno, tenant = _turno_futuro_de_token(token)
    if not turno:
        return _ya_no_sirve()
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    if not _puede_modificar(turno, tenant):
        return _tarde(tenant, turno)
    if request.method == "POST":
        db.cancelar_turno(tenant["id"], turno["id"], "paciente")
        db.registrar_actividad("paciente (web)", f"canceló un turno desde el link de agendar", tenant["id"])
        devolucion = _devolver_al_cancelar(tenant, turno)
        cancelaciones.tras_cancelar(tenant, turno, "la web")
        _evento(tenant, "cancelacion")
        return _sin_indexar(app.make_response(render_template(
            "agendar_listo.html", tenant=tenant, accion="cancelado", turno=_con_hora_local([turno], tz)[0],
            devolucion=devolucion)))
    pago, monto = pagos.cuanto_devolver(tenant, turno)
    return _sin_indexar(app.make_response(render_template(
        "agendar_accion.html", tenant=tenant, accion="cancelar", turno=_con_hora_local([turno], tz)[0], token=token,
        pagado=bool(pago), devolveria=monto, politica=pagos.politica(tenant) if pago else None)))


def _devolver_al_cancelar(tenant: dict, turno: dict) -> dict | None:
    """Aplica la política de devolución; si falla, la cancelación vale igual."""
    try:
        monto, hecho = pagos.al_cancelar(tenant, turno)
    except Exception:
        logger.exception("Falló la devolución al cancelar el turno %s", turno["id"])
        return None
    return {"monto": monto, "hecho": hecho} if monto else None


@app.get("/agendar/<token>/cambiar")
def agendar_cambiar(token):
    """El calendario del profesional para elegir otro horario."""
    turno, tenant = _turno_futuro_de_token(token)
    if not turno:
        return _ya_no_sirve()
    if tenant.get("pausado"):
        flash("Por ahora no se pueden elegir horarios nuevos: escribile directamente.")
        return redirect(url_for("agendar", token=token))
    if not _puede_modificar(turno, tenant):
        return _tarde(tenant, turno)
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    return _sin_indexar(app.make_response(render_template(
        "publico.html", tenant=tenant, cambiar=token, actual=_con_hora_local([turno], tz)[0], hay_directorio=False,
        **_semana_libre(tenant, request.args.get("semana"), _minutos_de(turno)))))


@app.route("/agendar/<token>/cambiar/<turno_id>", methods=["GET", "POST"])
def agendar_cambiar_a(token, turno_id):
    """GET muestra «de tal día a tal otro»; POST lo pasa. Usa el mismo
    reprogramar_turno del panel, pero sólo a horarios que estén libres. El
    turno nuevo se lleva el link de agendar y queda confirmado (lo eligió el
    paciente) y marcado como reserva nueva, para que el profesional lo vea."""
    turno, tenant = _turno_futuro_de_token(token)
    if not turno:
        return _ya_no_sirve()
    if not _puede_modificar(turno, tenant):
        return _tarde(tenant, turno)
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    nuevo = db.turno_por_id(tenant["id"], turno_id)
    if tenant.get("pausado") or not nuevo or nuevo["estado"] != "libre" or nuevo.get("hueco") or datetime.fromisoformat(nuevo["inicio"]) <= datetime.now(ZoneInfo("UTC")):
        flash("Ese horario ya no está disponible — elegí otro.")
        return redirect(url_for("agendar_cambiar", token=token))
    if request.method == "GET":
        return _sin_indexar(app.make_response(render_template(
            "agendar_accion.html", tenant=tenant, accion="cambiar", turno=_con_hora_local([turno], tz)[0],
            nuevo=_con_hora_local([nuevo], tz)[0], token=token)))
    # La duración es la del turno que tenía (ej. una entrevista de 60), no la
    # del horario nuevo; y tiene que entrar ahí (lo revisa _mover_por_paciente).
    error = _mover_por_paciente(tenant, {**turno, "token_calendario": token}, nuevo, "web")
    if error:
        flash(error if error.startswith("En ese horario no entra") else "Ese horario ya no está disponible — elegí otro.")
        return redirect(url_for("agendar_cambiar", token=token))
    return redirect(url_for("agendar", token=token, cambiado=1))


# ---------- pago de las reservas web ----------

def _base() -> str:
    return f"https://{config.DOMINIO_PRINCIPAL}" if config.DOMINIO_PRINCIPAL else request.host_url.rstrip("/")


def _pago_y_tenant(token: str):
    pago = db.pago_por_token(token)
    tenant = db.get_tenant_by_id(pago["tenant_id"]) if pago else None
    return (pago, tenant) if pago and tenant else (None, None)


@app.get("/pago/<token>")
def pago_paciente(token):
    """La página del paciente para pagar su reserva. Al volver de Mercado
    Pago trae `payment_id`: se consulta ahí mismo (además del aviso)."""
    pago, tenant = _pago_y_tenant(token)
    if not pago:
        return _sin_indexar(app.make_response(("Este link ya no sirve.", 404)))
    if pago["estado"] in ("esperando", "vencido") and request.args.get("payment_id"):
        pagos.revisar_pago_mp(tenant, request.args["payment_id"])
        pago = db.pago_por_token(token)
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    turnos = [t for t in (db.turno_por_id(tenant["id"], tid) for tid in pago["turnos"]) if t]
    return _sin_indexar(app.make_response(render_template(
        "pago.html", tenant=tenant, pago=pago, turnos=_con_hora_local(sorted(turnos, key=lambda t: t["inicio"]), tz),
        medios=pagos.metodos(tenant), vuelta_mp=request.args.get("status"), politica=pagos.politica(tenant),
        vence=datetime.fromisoformat(str(pago["vence"])).astimezone(tz).strftime("%H:%M"),
        agendar=next((t.get("token_calendario") for t in turnos if t.get("token_calendario")), None))))


@app.post("/pago/<token>/mercadopago")
def pago_mercadopago(token):
    pago, tenant = _pago_y_tenant(token)
    if not pago or pago["estado"] != "esperando" or "mercadopago" not in pagos.metodos(tenant):
        return redirect(url_for("pago_paciente", token=token))
    link = pagos.preferencia(tenant, pago, f"Turno con {tenant['nombre']}", _base())
    if not link:
        flash("No se pudo abrir Mercado Pago. Probá de nuevo en un momento.")
        return redirect(url_for("pago_paciente", token=token))
    return redirect(link)


@app.post("/pago/<token>/comprobante")
def pago_comprobante(token):
    pago, tenant = _pago_y_tenant(token)
    if not pago or pago["estado"] != "esperando" or "transferencia" not in pagos.metodos(tenant):
        return redirect(url_for("pago_paciente", token=token))
    email = None
    if not pago.get("paciente_email"):
        email = (request.form.get("email") or "").strip()[:120]
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Dejanos tu email: ahí te llega el recibo o el aviso si el comprobante no se puede validar.")
            return redirect(url_for("pago_paciente", token=token))
    archivo = request.files.get("comprobante")
    error = pagos.subir_comprobante(tenant, pago, archivo.read(pagos.COMPROBANTE_MAX + 1) if archivo else b"", email)
    if error:
        flash(error)
    return redirect(url_for("pago_paciente", token=token))


@app.get("/pago/<token>/recibo")
def pago_recibo(token):
    """Recibo del pago web para el paciente (no es factura). El token es la
    credencial, igual que la página de pago. Sin teléfono ni notas."""
    pago, tenant = _pago_y_tenant(token)
    if not pago or pago["estado"] not in ("aprobado", "aprobado_tarde", "devuelto"):
        return _sin_indexar(app.make_response(("Todavía no hay un pago aprobado con este link.", 404)))
    return _sin_indexar(app.make_response(render_template("recibo.html", tenant=tenant, pago=pago, **recibos.datos(tenant, pago))))


@app.get("/pago/<token>/recibo.pdf")
def pago_recibo_pdf(token):
    """El mismo recibo, en PDF para guardar."""
    pago, tenant = _pago_y_tenant(token)
    if not pago or pago["estado"] not in ("aprobado", "aprobado_tarde", "devuelto"):
        return _sin_indexar(app.make_response(("Todavía no hay un pago aprobado con este link.", 404)))
    contenido = recibos.pdf(tenant, pago)
    if not contenido:
        return _sin_indexar(app.make_response(("No se pudo armar el PDF: usá «Imprimir o guardar en PDF».", 503)))
    r = app.response_class(contenido, mimetype="application/pdf")
    r.headers["Content-Disposition"] = f'inline; filename="recibo-{recibos.datos(tenant, pago)["numero"]}.pdf"'
    r.headers["Cache-Control"] = "no-store"
    return _sin_indexar(r)


@app.post("/webhook/mercadopago")
def webhook_mercadopago():
    """Aviso de Mercado Pago. No se confía en el contenido: se consulta el pago
    con el token del profesional (`t` lo identifica) y se compara referencia y
    monto. Siempre 200, si no Mercado Pago reintenta cada 15 minutos."""
    try:
        # `t` viene en la URL del aviso: si falta o no es un id válido, la base
        # rechaza la consulta; igual hay que contestar 200.
        tenant = db.get_tenant_by_id(request.args["t"]) if request.args.get("t") else None
    except Exception:
        tenant = None
    cuerpo = request.get_json(silent=True) or {}
    tipo = cuerpo.get("type") or request.args.get("type") or request.args.get("topic")
    pago_id = (cuerpo.get("data") or {}).get("id") or request.args.get("data.id") or request.args.get("id")
    if tenant and tipo == "payment" and pago_id:
        try:
            pagos.revisar_pago_mp(tenant, str(pago_id))
        except Exception:
            logger.exception("Falló el aviso de Mercado Pago del tenant %s", tenant["id"])
    return "", 200


# ---------- cobro: panel ----------

DEVOLUCION_HORAS = (0, 12, 24, 48, 72)


@app.post("/agenda/cobros")
@login_required
def agenda_cobros():
    f = request.form
    modo = f.get("cobro_web") if f.get("cobro_web") in ("no", "mercadopago", "transferencia", "ambos") else "no"
    numero = lambda clave: int("".join(ch for ch in f.get(clave) or "" if ch.isdigit()) or 0) or None
    campos = {"cobro_web": modo, "cobro_modo": "sena" if f.get("cobro_modo") == "sena" else "total",
              "sena_monto": numero("sena_monto"), "precio_sesion": numero("precio_sesion"),
              "transferencia_datos": (f.get("transferencia_datos") or "").strip()[:300] or None,
              "devolucion_modo": f.get("devolucion_modo") if f.get("devolucion_modo") in ("todo", "menos_sena", "nada") else "todo",
              "devolucion_horas": int(f["devolucion_horas"]) if (f.get("devolucion_horas") or "") in
                                  {str(x) for x in DEVOLUCION_HORAS} else 24}
    db.guardar_ajustes(session["tenant_id"], campos)
    tenant = db.get_tenant_by_id(session["tenant_id"])
    if modo == "no":
        flash("Listo: las reservas web no se cobran.")
    elif not pagos.activo(tenant):
        flash("Guardado, pero todavía no se cobra: " + (
            "conectá Mercado Pago" if modo == "mercadopago" else "cargá tu alias o CBU" if modo == "transferencia"
            else "conectá Mercado Pago o cargá tu alias/CBU") + ".")
    elif not (tenant.get("precio_sesion") or any(t.get("precio") for t in tipos_de(tenant))):
        flash("Guardado, pero falta el precio: cargalo acá o en tus tipos de turno. Sin precio, la reserva no se cobra.")
    else:
        flash("Listo: cada reserva web se acepta cuando se paga.")
    return _volver_a_agenda()


def _tras_conectar_mp(tenant_id: str) -> str:
    """Conectar es un solo clic: si todavía no cobraba con Mercado Pago, se
    activa (sin sacar la transferencia si ya la usaba). Lo único que puede
    faltar es el precio, y se lo dice."""
    tenant = db.get_tenant_by_id(tenant_id)
    modo = tenant.get("cobro_web") or "no"
    nuevo = {"no": "mercadopago", "transferencia": "ambos"}.get(modo, modo)
    if nuevo != modo:
        db.guardar_ajustes(tenant_id, {"cobro_web": nuevo})
    if not (tenant.get("precio_sesion") or any(t.get("precio") for t in tipos_de(tenant))):
        return ("Conectaste Mercado Pago. Falta un paso: cargá el precio de la sesión acá abajo "
                "(o en tus tipos de turno) y las reservas web se empiezan a cobrar.")
    return "Listo, conectaste Mercado Pago: las reservas de tu página se cobran y la plata va a tu cuenta."


def _version_panel(tenant_id: str) -> str:
    try:
        return db.version_panel(tenant_id)
    except Exception:
        logger.warning("No se pudo calcular la versión del panel", exc_info=True)
        return ""


@app.get("/agenda/novedades")
@login_required
def agenda_novedades():
    """El panel abierto pregunta cada tanto si cambió algo (pago, devolución,
    reserva nueva) para ofrecer actualizar. Sólo una firma, sin datos."""
    r = jsonify({"v": _version_panel(session["tenant_id"])})
    r.headers["Cache-Control"] = "no-store"
    return r


@app.get("/agenda/mercadopago/conectar")
@login_required
def agenda_mp_conectar():
    if not pagos.mp_disponible():
        flash("Mercado Pago todavía no está habilitado en la plataforma.")
        return _volver_a_agenda()
    session["mp_state"] = secrets.token_urlsafe(16)
    return redirect(pagos.url_conectar(session["mp_state"], url_for("mp_vuelta", _external=True, _scheme="https")))


@app.get("/mercadopago/vuelta")
@login_required
def mp_vuelta():
    """Vuelta del login de Mercado Pago. El `state` tiene que ser el que se
    guardó en la sesión de ESTE profesional."""
    state = session.pop("mp_state", None)
    volver = redirect(url_for("agenda") + "#cobros")
    motivo = _codigo_mp(request.args.get("error"))
    if motivo:
        # Mercado Pago vuelve con ?error=… cuando la persona cancela o cuando
        # no deja autorizar la cuenta. Nunca se guarda nada de la cuenta.
        db.registrar_actividad("profesional", f"no pudo conectar Mercado Pago ({motivo})", session["tenant_id"])
        flash(MP_CANCELADO if motivo == "access_denied" else MP_NO_DEJO + f" (Mercado Pago dijo: {motivo}.)")
        return volver
    if not state or not hmac.compare_digest(state, request.args.get("state") or ""):
        flash("El link para conectar Mercado Pago venció o se abrió en otro navegador. "
              "Tocá «Conectar Mercado Pago» de nuevo desde este mismo navegador.")
        return volver
    if not request.args.get("code"):
        flash(MP_NO_DEJO)
        return volver
    error = pagos.conectar(session["tenant_id"], request.args["code"], url_for("mp_vuelta", _external=True, _scheme="https"))
    flash(f"{error} {MP_QUE_PROBAR}" if error else _tras_conectar_mp(session["tenant_id"]))
    db.registrar_actividad("profesional", "conectó Mercado Pago" if not error else "intentó conectar Mercado Pago",
                           session["tenant_id"])
    return volver


MP_QUE_PROBAR = ("Probá así: cerrá cualquier otra sesión de Mercado Pago (o usá una ventana privada), entrá con "
                 "TU cuenta de Argentina, con la identidad validada, y tocá «Conectar Mercado Pago» de nuevo. "
                 "Si vuelve a pasar, avisanos y mientras tanto podés cobrar por transferencia.")
MP_NO_DEJO = "Mercado Pago no dejó conectar tu cuenta. " + MP_QUE_PROBAR
MP_CANCELADO = "Cancelaste la conexión en Mercado Pago: no se cambió nada. Podés volver a intentarlo cuando quieras."


def _codigo_mp(valor: str | None) -> str:
    """El código de error que manda Mercado Pago en la vuelta, limpio para
    mostrarlo y anotarlo (viene en la URL: no se confía en él)."""
    return "".join(c for c in (valor or "") if c.isalnum() or c in "_-.")[:40]


@app.post("/agenda/mercadopago/token-prueba")
@login_required
def agenda_mp_token_prueba():
    error = pagos.conectar_con_token(session["tenant_id"], request.form.get("token", ""))
    flash(error or _tras_conectar_mp(session["tenant_id"]))
    if not error:
        db.registrar_actividad("profesional", "conectó Mercado Pago (cuenta de prueba)", session["tenant_id"])
    return redirect(url_for("agenda") + "#cobros")


@app.post("/agenda/mercadopago/desconectar")
@login_required
def agenda_mp_desconectar():
    pagos.desconectar(session["tenant_id"])
    flash("Desconectaste Mercado Pago.")
    return _volver_a_agenda()


@app.post("/agenda/pago-web/<pago_id>/<accion>")
@login_required
def agenda_pago_web(pago_id, accion):
    """Validar un comprobante de transferencia (aceptar / rechazar), o dar por
    resuelto un pago que llegó tarde."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    pago = db.pago_de(tenant["id"], pago_id)
    if not pago:
        return _volver_a_agenda()
    if accion == "aceptar" and pago["estado"] == "comprobante":
        resultado = pagos.aprobar(tenant, pago)
        flash("Aceptada: el turno quedó confirmado y pagado." if resultado == "aprobado"
              else "El turno ya se había liberado: devolvé la transferencia o reprogramalo.")
    elif accion == "rechazar" and pago["estado"] == "comprobante":
        pagos.rechazar(tenant, pago, request.form.get("motivo") or None)
        flash("Rechazaste el comprobante: el horario quedó libre y le avisamos al paciente."
              if pago.get("paciente_email") or config.PAGO_RECHAZADO_TEMPLATE else "Rechazaste el comprobante: el horario quedó libre.")
    elif accion == "resuelto" and pago["estado"] == "aprobado_tarde":
        db.actualizar_pago(pago["id"], {"estado": "aprobado"})
        flash("Listo, lo sacamos de la lista.")
    elif accion == "devolver" and pagos.puede_devolver_mp(pago):
        monto = int(pago.get("a_devolver") or 0) or pagos.devolvible(pago)
        error = pagos.devolver(tenant, pago, monto)
        flash(error or f"Listo: Mercado Pago le devuelve {_pesos(monto)} al paciente (tarda unos días en verse en su cuenta).")
        if not error:
            db.registrar_actividad("profesional", "devolvió un pago web por Mercado Pago", tenant["id"])
    elif accion == "devuelto" and (int(pago.get("a_devolver") or 0) or pago["estado"] == "aprobado_tarde"):
        pagos.anotar_devuelto(pago)
        flash("Anotado como devuelto.")
    return _volver_a_agenda()


@app.get("/agenda/pago-web/<pago_id>/comprobante")
@login_required
def agenda_pago_comprobante(pago_id):
    pago = db.pago_de(session["tenant_id"], pago_id)
    if not pago or not (pago.get("comprobante") or "").startswith("data:"):
        return "No hay comprobante.", 404
    cabecera, datos = pago["comprobante"].split(",", 1)
    r = app.response_class(base64.b64decode(datos), mimetype=cabecera[5:].split(";")[0])
    r.headers["Content-Disposition"] = "inline"
    r.headers["X-Content-Type-Options"] = "nosniff"
    return r


def _cobros_web(tenant: dict, primero: date, tz: ZoneInfo, revisar: list[dict]) -> dict | None:
    """Lo que entró por la web en el mes que se mira: Mercado Pago y
    transferencias por separado, lo devuelto y lo que falta validar. None si
    este profesional nunca cobró por la web."""
    siguiente = (primero.replace(day=28) + timedelta(days=4)).replace(day=1)
    desde = datetime.combine(primero, dtime(0), tz).astimezone(ZoneInfo("UTC"))
    hasta = datetime.combine(siguiente, dtime(0), tz).astimezone(ZoneInfo("UTC"))
    try:
        lista = db.pagos_resueltos_entre(tenant["id"], desde, hasta)
    except Exception:
        logger.warning("No se pudieron leer los cobros web", exc_info=True)
        return None
    if not lista and not revisar and not pagos.activo(tenant):
        return None
    for p in lista:
        p["_cuando"] = datetime.fromisoformat(str(p["resuelto"])).astimezone(tz).strftime("%d/%m %H:%M")
        p["_devolvible"] = pagos.devolvible(p)
        p["_puede_mp"] = pagos.puede_devolver_mp(p)
    suma = lambda metodo: sum(p["monto"] for p in lista if p.get("metodo") == metodo)
    comprobantes = [p for p in revisar if p["estado"] == "comprobante"]
    return {"mp": suma("mercadopago"), "transferencia": suma("transferencia"),
            "devuelto": sum(p.get("devuelto") or 0 for p in lista),
            "neto": sum(p["monto"] - (p.get("devuelto") or 0) for p in lista),
            "por_validar": sum(p["monto"] for p in comprobantes), "por_validar_n": len(comprobantes),
            "pagos": lista}


TIPOS_MENSAJE = {"audio": "Mandó un audio", "imagen": "Mandó una foto", "documento": "Mandó un archivo",
                 "video": "Mandó un video", "otro": "Mandó algo que no es texto"}


def _mensajes_pacientes(tenant: dict, tz: ZoneInfo) -> list[dict]:
    """La bandeja: lo que el bot no supo resolver. Se puede contestar desde el
    panel mientras siga abierta la ventana de 24 h de WhatsApp."""
    try:
        lista = db.mensajes_pacientes(tenant["id"])
    except Exception:
        logger.warning("No se pudo leer la bandeja de mensajes", exc_info=True)
        return []
    ahora = datetime.now(ZoneInfo("UTC"))
    nombres: dict[str, str | None] = {}
    for m in lista:
        if m["telefono"] not in nombres:
            try:
                nombres[m["telefono"]] = next((f.get("paciente_nombre") for f in db.fichas_del_telefono(tenant["id"], m["telefono"])
                                               if f.get("paciente_nombre")), None)
            except Exception:
                nombres[m["telefono"]] = None
        creado = datetime.fromisoformat(str(m["creado"]))
        m["_quien"] = nombres[m["telefono"]] or f"+{m['telefono']}"
        m["_cuando"] = creado.astimezone(tz).strftime("%d/%m %H:%M")
        m["_texto"] = m.get("texto") or TIPOS_MENSAJE.get(m.get("tipo"), "")
        m["_sin_texto"] = not m.get("texto")
        m["_puede_responder"] = not m.get("respuesta") and ahora - creado < timedelta(hours=23, minutes=50)
    return lista


@app.post("/agenda/mensaje/<mensaje_id>/<accion>")
@login_required
def agenda_mensaje(mensaje_id, accion):
    """Marcar leído o contestar (texto libre, dentro de las 24 h) un mensaje de
    la bandeja. La respuesta sale por el mismo número del bot."""
    tenant = db.get_tenant_by_id(session["tenant_id"])
    m = db.mensaje_paciente(tenant["id"], mensaje_id)
    volver = redirect(url_for("agenda") + "#mensajes")
    if not m:
        return volver
    if accion == "leido":
        db.actualizar_mensaje_paciente(tenant["id"], m["id"], {"leido": True})
    elif accion == "responder":
        texto = (request.form.get("respuesta") or "").strip()[:1000]
        creado = datetime.fromisoformat(str(m["creado"]))
        if not texto:
            flash("Escribí la respuesta.")
        elif m.get("respuesta") or datetime.now(ZoneInfo("UTC")) - creado >= timedelta(hours=23, minutes=50):
            flash("Pasaron más de 24 h: WhatsApp no deja contestarle por acá. Escribile desde tu teléfono.")
        elif whatsapp.enviar_texto(whatsapp.numero_de(tenant), m["telefono"], f"*{tenant['nombre']}*: {texto}"):
            db.actualizar_mensaje_paciente(tenant["id"], m["id"], {"respuesta": texto, "leido": True,
                                                                    "respondido": datetime.now(ZoneInfo("UTC")).isoformat()})
            flash("Respuesta enviada por WhatsApp.")
        else:
            flash("WhatsApp no aceptó el mensaje. Probá de nuevo en un rato.")
    return volver


def _pagos_para_revisar(tenant: dict, tz: ZoneInfo) -> list[dict]:
    try:
        lista = db.pagos_para_revisar(tenant["id"])
    except Exception:
        return []
    for p in lista:
        turnos = [t for t in (db.turno_por_id(tenant["id"], tid) for tid in p["turnos"]) if t]
        p["_turnos"] = _con_hora_local(sorted(turnos, key=lambda t: t["inicio"]), tz)
        p["_quien"] = next((t.get("paciente_nombre") for t in turnos if t.get("paciente_nombre")), None) or p.get("paciente_nombre")
        p["_puede_mp"] = pagos.puede_devolver_mp(p)
    return lista


@app.get("/agendar/<token>.ics")
def agendar_ics(token):
    turno, tenant = _turno_para_agendar(token)
    if not turno:
        return "No encontrado.", 404
    inicio = datetime.fromisoformat(turno["inicio"]).astimezone(ZoneInfo("UTC"))
    fin = datetime.fromisoformat(turno["fin"]).astimezone(ZoneInfo("UTC")) if turno.get("fin") else inicio + timedelta(minutes=50)
    escapar = lambda t: t.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    virtual = turno.get("modalidad") == "virtual"
    detalle = f"Turno con {tenant['nombre']}" + (f". Videollamada: {tenant['link_videollamada']}" if virtual and tenant.get("link_videollamada") else "")
    ics = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//FiBOT Consultorio//Turnos//ES", "BEGIN:VEVENT",
        f"UID:{turno['id']}@fibot-consultorio", f"DTSTAMP:{datetime.now(ZoneInfo('UTC')):%Y%m%dT%H%M%SZ}",
        f"DTSTART:{inicio:%Y%m%dT%H%M%SZ}", f"DTEND:{fin:%Y%m%dT%H%M%SZ}",
        f"SUMMARY:{escapar('Turno con ' + tenant['nombre'])}", f"DESCRIPTION:{escapar(detalle)}",
        "BEGIN:VALARM", "TRIGGER:-PT2H", "ACTION:DISPLAY", f"DESCRIPTION:{escapar('Turno con ' + tenant['nombre'])}", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR", ""])
    r = app.response_class(ics, mimetype="text/calendar")
    r.headers["Content-Disposition"] = 'attachment; filename="turno.ics"'
    return _sin_indexar(r)


scheduler.iniciar()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
