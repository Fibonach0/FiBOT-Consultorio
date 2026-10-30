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
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

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
                if msg.get("type") != "text":
                    continue
                telefono = msg.get("from")
                tenant = tenant_del_numero or db.tenant_por_paciente(telefono)
                if not tenant:
                    logger.warning(
                        "Mensaje al número compartido de un teléfono sin turnos: %s", telefono)
                    continue
                texto = msg.get("text", {}).get("body", "")
                try:
                    respuesta = conversation.responder(tenant, telefono, texto)
                except Exception:
                    logger.exception("Fallo procesando mensaje de %s (tenant %s)", telefono, tenant["id"])
                    respuesta = "Uh, tuve un problema procesando eso. Probá de nuevo en un rato."
                whatsapp.enviar_texto(whatsapp.numero_de(tenant), telefono, respuesta)

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


def _volver_a_agenda():
    """Después de tocar una sesión, volver a la misma semana que se estaba
    mirando (viene como campo oculto del form). Se reconstruye la URL con
    url_for en vez de usar el Referer: así nunca redirige afuera del panel."""
    semana = request.form.get("semana") or ""
    try:
        datetime.strptime(semana, "%Y-%m-%d")
    except ValueError:
        return redirect(url_for("agenda"))
    return redirect(url_for("agenda", semana=semana))


@app.get("/agenda")
@login_required
def agenda():
    tenant = db.get_tenant_by_id(session["tenant_id"])
    tz = _tz_de(tenant)
    ahora = datetime.now(tz)
    desde = _lunes_de(request.args.get("semana"), tz)
    hasta = desde + timedelta(days=7)
    turnos = db.agenda_semana(tenant["id"], desde.astimezone(ZoneInfo("UTC")), hasta.astimezone(ZoneInfo("UTC")))

    por_dia: dict[str, list[dict]] = {}
    resumen = {"sesiones": 0, "vino": 0, "no_vino": 0, "pagado": 0}
    for t in turnos:
        inicio_local = datetime.fromisoformat(t["inicio"]).astimezone(tz)
        clave = inicio_local.strftime("%Y-%m-%d")
        extra = {"_hora": inicio_local.strftime("%H:%M"), "_paso": inicio_local <= ahora}
        if t.get("reprogramado_desde"):
            antes = datetime.fromisoformat(t["reprogramado_desde"]).astimezone(tz)
            extra["_reprogramado_desde"] = f"{DIAS_LABEL[antes.weekday()]} {antes.day}/{antes.month:02d} {antes:%H:%M}"
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
                "turnos": por_dia.get(clave, []),
            }
        )
        cursor += timedelta(days=1)

    pendientes = _con_hora_local(db.pendientes_de_pago(tenant["id"], ahora.astimezone(ZoneInfo("UTC"))), tz)

    return render_template(
        "agenda.html",
        tenant=tenant,
        dias=dias,
        resumen=resumen,
        pendientes=pendientes,
        semana=desde.strftime("%Y-%m-%d"),
        semana_titulo=f"{desde.day}/{desde.month:02d} – {(hasta - timedelta(days=1)).day}/{(hasta - timedelta(days=1)).month:02d}",
        semana_anterior=(desde - timedelta(days=7)).strftime("%Y-%m-%d"),
        semana_siguiente=hasta.strftime("%Y-%m-%d"),
        duracion_default=tenant.get("duracion_turno_minutos") or 50,
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
    return _volver_a_agenda()


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
    error = db.reprogramar_turno(tenant["id"], turno_id, nuevo_inicio, duracion)
    if error:
        flash(error)
        return _volver_a_agenda()
    flash(f"Sesión pasada al {DIAS_LABEL[fecha.weekday()]} {fecha.day}/{fecha.month:02d} a las {hora:%H:%M}.")
    # Se va a la semana del horario NUEVO: es donde el profesional quiere
    # ver que quedó bien.
    return redirect(url_for("agenda", semana=fecha.strftime("%Y-%m-%d")))


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
    if not nombre or not telefono:
        flash("Para agendar hacen falta nombre y WhatsApp del paciente.")
        return _volver_a_agenda()
    if not db.reservar_turno(session["tenant_id"], turno_id, telefono, nombre):
        flash("Ese horario ya no está libre.")
    return _volver_a_agenda()


# ---------- página pública de reserva (sin login) ----------
# consultorios.fibot.ar/<slug> — el link que cada profesional comparte con
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


@app.get("/<slug>")
def publico(slug):
    tenant = db.get_tenant_by_slug(slug)
    if not tenant:
        return "No encontrado.", 404
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    ahora = datetime.now(ZoneInfo("UTC"))
    turnos = _con_hora_local(db.turnos_libres(tenant["id"], ahora, limite=20), tz)
    return render_template("publico.html", tenant=tenant, turnos=turnos)


@app.get("/<slug>/reservar/<turno_id>")
def publico_reservar_form(slug, turno_id):
    tenant = db.get_tenant_by_slug(slug)
    if not tenant:
        return "No encontrado.", 404
    turno = db.turno_por_id(tenant["id"], turno_id)
    if not turno or turno["estado"] != "libre":
        flash("Ese horario ya no está disponible — elegí otro.")
        return redirect(url_for("publico", slug=slug))
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    turno = _con_hora_local([turno], tz)[0]
    return render_template("publico_reservar.html", tenant=tenant, turno=turno)


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
        flash("Completá nombre y teléfono.")
        return redirect(url_for("publico_reservar_form", slug=slug, turno_id=turno_id))
    if not db.reservar_turno(tenant["id"], turno_id, telefono, nombre):
        flash("Ese horario ya no está disponible — elegí otro.")
        return redirect(url_for("publico", slug=slug))
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    turno = db.turno_por_id(tenant["id"], turno_id)
    turno = _con_hora_local([turno], tz)[0]
    return render_template("publico_confirmado.html", tenant=tenant, turno=turno)


scheduler.iniciar()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
