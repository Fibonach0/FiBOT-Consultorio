"""FiBOT Consultorio — plataforma.

UN solo deploy sirve a TODOS los profesionales (tenants). El webhook de Meta
es uno solo; cada mensaje entrante trae el phone_number_id del número al que
le escribieron, y eso es lo que resuelve a qué tenant pertenece (ver
db.get_tenant_by_phone_number_id). Nada de esto tiene nada de un cliente en
particular — lo particular vive en la tabla `tenants` y se carga con
scripts/crear_tenant.py, nunca hardcodeado acá."""
import functools
import logging
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import bcrypt
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

import config
import conversation
import db
import scheduler
import whatsapp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY

DIAS_LABEL = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


# ---------- salud ----------

@app.get("/")
@app.get("/health")
def salud():
    return "FiBOT Consultorio activo.", 200


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
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id")
            if not phone_number_id:
                continue
            tenant = db.get_tenant_by_phone_number_id(phone_number_id)
            if not tenant:
                logger.warning("Mensaje a un phone_number_id sin tenant activo: %s", phone_number_id)
                continue
            for msg in value.get("messages", []):
                if msg.get("type") != "text":
                    continue
                telefono = msg.get("from")
                texto = msg.get("text", {}).get("body", "")
                try:
                    respuesta = conversation.responder(tenant, telefono, texto)
                except Exception:
                    logger.exception("Fallo procesando mensaje de %s (tenant %s)", telefono, tenant["id"])
                    respuesta = "Uh, tuve un problema procesando eso. Probá de nuevo en un rato."
                whatsapp.enviar_texto(tenant["whatsapp_phone_number_id"], telefono, respuesta)

    return jsonify(ok=True), 200


# ---------- panel ----------

def login_required(vista):
    @functools.wraps(vista)
    def envoltorio(*args, **kwargs):
        if not session.get("tenant_id"):
            return redirect(url_for("login"))
        return vista(*args, **kwargs)

    return envoltorio


@app.get("/login")
def login():
    return render_template("login.html", error=None)


@app.post("/login")
def login_post():
    usuario = (request.form.get("usuario") or "").strip()
    clave = request.form.get("clave") or ""
    tenant = db.get_tenant_by_panel_user(usuario)
    if not tenant or not bcrypt.checkpw(clave.encode(), tenant["panel_pass_hash"].encode()):
        return render_template("login.html", error="Usuario o clave incorrectos."), 401
    session.clear()
    session["tenant_id"] = tenant["id"]
    return redirect(url_for("agenda"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/agenda")
@login_required
def agenda():
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    hoy = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    desde = hoy
    hasta = hoy + timedelta(days=14)
    turnos = db.agenda_semana(tenant["id"], desde.astimezone(ZoneInfo("UTC")), hasta.astimezone(ZoneInfo("UTC")))

    por_dia: dict[str, list[dict]] = {}
    for t in turnos:
        inicio_local = datetime.fromisoformat(t["inicio"]).astimezone(tz)
        clave = inicio_local.strftime("%Y-%m-%d")
        por_dia.setdefault(clave, []).append({**t, "_hora": inicio_local.strftime("%H:%M")})

    dias = []
    cursor = desde
    while cursor < hasta:
        clave = cursor.strftime("%Y-%m-%d")
        dias.append(
            {
                "fecha": clave,
                "titulo": f"{DIAS_LABEL[cursor.weekday()]} {cursor.day}",
                "turnos": por_dia.get(clave, []),
            }
        )
        cursor += timedelta(days=1)

    return render_template(
        "agenda.html", tenant=tenant, dias=dias, duracion_default=tenant.get("duracion_turno_minutos") or 50
    )


@app.post("/agenda/generar")
@login_required
def agenda_generar():
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")

    fecha_desde = datetime.strptime(request.form["fecha_desde"], "%Y-%m-%d").date()
    fecha_hasta = datetime.strptime(request.form["fecha_hasta"], "%Y-%m-%d").date()
    hora_inicio = dtime.fromisoformat(request.form["hora_inicio"])
    hora_fin = dtime.fromisoformat(request.form["hora_fin"])
    duracion = int(request.form.get("duracion_minutos") or tenant.get("duracion_turno_minutos") or 50)
    dias_semana = {int(d) for d in request.form.getlist("dias_semana")}

    inicios = []
    cursor = fecha_desde
    while cursor <= fecha_hasta:
        if cursor.weekday() in dias_semana:
            paso = datetime.combine(cursor, hora_inicio, tzinfo=tz)
            fin_del_dia = datetime.combine(cursor, hora_fin, tzinfo=tz)
            while paso + timedelta(minutes=duracion) <= fin_del_dia:
                inicios.append(paso.astimezone(ZoneInfo("UTC")))
                paso += timedelta(minutes=duracion)
        cursor += timedelta(days=1)

    creados = db.crear_turnos_libres(tenant["id"], inicios, duracion)
    logger.info("Tenant %s generó %s turnos libres", tenant["id"], creados)
    return redirect(url_for("agenda"))


@app.post("/agenda/cancelar/<turno_id>")
@login_required
def agenda_cancelar(turno_id):
    # db.cancelar_turno() filtra por tenant_id, así que un tenant nunca puede
    # tocar un turno que no es suyo aunque adivine el id de otro.
    db.cancelar_turno(session["tenant_id"], turno_id)
    return redirect(url_for("agenda"))


scheduler.iniciar()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
