"""FiBOT Consultorio — plataforma.

UN solo deploy sirve a TODOS los profesionales (tenants). El webhook de Meta
es uno solo; cada mensaje entrante trae el phone_number_id del número al que
le escribieron, y eso es lo que resuelve a qué tenant pertenece (ver
db.get_tenant_by_phone_number_id). Nada de esto tiene nada de un cliente en
particular — lo particular vive en la tabla `tenants` y se carga con
scripts/crear_tenant.py, nunca hardcodeado acá."""
import functools
import hmac
import logging
import os
import secrets
from datetime import date, datetime, timedelta, time as dtime
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
# Secure sólo en Railway: en local el panel corre por http y no habría login.
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RAILWAY_ENVIRONMENT_NAME")),
)

DIAS_LABEL = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MESES_LABEL = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto",
               "Septiembre", "Octubre", "Noviembre", "Diciembre"]


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

def _csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = _csrf_token
app.jinja_env.filters["pesos"] = lambda v: f"$ {v:,}".replace(",", ".")


@app.before_request
def verificar_csrf():
    """Toda acción del panel tiene que traer el token de la sesión, para que
    otra página no pueda disparar un POST con la cookie del profesional. El
    webhook tiene su firma y la reserva pública no usa sesión."""
    if request.method != "POST" or not request.path.startswith("/agenda"):
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


def _meses_label(primero: date) -> str:
    return f"{MESES_LABEL[primero.month - 1]} {primero.year}"


def _mes_de(valor: str | None, hoy: date) -> date:
    """Primer día del mes `valor` (YYYY-MM), o del mes actual si no se entiende."""
    try:
        return datetime.strptime(valor or "", "%Y-%m").date()
    except ValueError:
        return hoy.replace(day=1)


def _calendario(tenant: dict, primero: date, tz: ZoneInfo, ahora: datetime) -> list[list[dict]]:
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
                    "de_otro_mes": cursor.month != primero.month,
                    "sesiones": [t for t in turnos if t["estado"] == "reservado"],
                    "libres": sum(t["estado"] == "libre" for t in turnos),
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


def _proxima_vez(mes: int, dia: int, hoy: date) -> date:
    """Próxima vez que cae esta fecha, hoy incluido. Un 29/2 se festeja el 28
    en los años que no son bisiestos."""
    for anio in (hoy.year, hoy.year + 1):
        try:
            cae = date(anio, mes, dia)
        except ValueError:
            cae = date(anio, 2, 28)
        if cae >= hoy:
            return cae
    raise AssertionError("una fecha siempre cae dentro del año que viene")


def _falta(dias: int) -> str:
    return {0: "hoy", 1: "mañana"}.get(dias, f"en {dias} días")


def _cumpleanios(nacimiento: str | None, hoy: date) -> str | None:
    if not nacimiento:
        return None
    n = date.fromisoformat(nacimiento)
    cae = _proxima_vez(n.month, n.day, hoy)
    return f"{cae.year - n.year} años, {_falta((cae - hoy).days)}"


def _con_cuenta_regresiva(fechas: list[dict], hoy: date) -> list[dict]:
    resultado = []
    for f in fechas:
        cae = _proxima_vez(f["mes"], f["dia"], hoy)
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


def _fichas(tenant: dict, tz: ZoneInfo, ahora: datetime) -> list[dict]:
    """Una ficha por paciente (se identifica por teléfono, como en todo el
    sistema). Frecuencia y valor son los de su próxima sesión, o los de la
    última si no tiene ninguna agendada. La deuda se cuenta igual que en
    Pendientes de cobro."""
    por_paciente: dict[str, list[dict]] = {}
    for t in _con_hora_local(db.sesiones_reservadas(tenant["id"]), tz):
        por_paciente.setdefault(t["paciente_telefono"], []).append(t)
    datos = db.datos_de_pacientes(tenant["id"])
    personas: dict[str, list[dict]] = {}
    for p in db.personas_de_pacientes(tenant["id"]):
        personas.setdefault(p["paciente_telefono"], []).append(p)
    fechas: dict[str, list[dict]] = {}
    for f in db.fechas_de_pacientes(tenant["id"]):
        fechas.setdefault(f["paciente_telefono"], []).append(f)
    hoy = ahora.date()

    lista = []
    for telefono, sesiones in por_paciente.items():
        pasadas = [t for t in sesiones if datetime.fromisoformat(t["inicio"]) <= ahora]
        futuras = sesiones[len(pasadas):]
        vigente = futuras[0] if futuras else pasadas[-1]
        sin_pagar = [t for t in pasadas if t.get("pago") == "pendiente"]
        nacimiento = datos.get(telefono, {}).get("fecha_nacimiento")
        lista.append(
            {
                "telefono": telefono,
                "nombre": next((t["paciente_nombre"] for t in reversed(sesiones) if t.get("paciente_nombre")), telefono),
                "frecuencia": vigente.get("frecuencia"),
                "valor": vigente.get("valor"),
                "fecha_nacimiento": nacimiento,
                "edad": _edad(nacimiento, hoy),
                "cumpleanios": _cumpleanios(nacimiento, hoy),
                "notas": datos.get(telefono, {}).get("notas"),
                "personas": personas.get(telefono, []),
                "fechas": _con_cuenta_regresiva(fechas.get(telefono, []), hoy),
                "desde": datetime.fromisoformat(sesiones[0]["inicio"]).astimezone(tz).strftime("%d/%m/%Y"),
                "proxima": f"{futuras[0]['_dia']} {futuras[0]['_hora']}" if futuras else None,
                "asistio": sum(t.get("asistencia") == "vino" for t in pasadas),
                "falto": sum(t.get("asistencia") == "no_vino" for t in pasadas),
                "sin_pagar": len(sin_pagar),
                "adeudado": sum(t.get("valor") or 0 for t in sin_pagar),
                "historial": pasadas[:-11:-1],
            }
        )
    return sorted(lista, key=lambda f: f["nombre"].casefold())


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
    comunes = {
        "tenant": tenant,
        "pendientes": pendientes,
        "por_cobrar": sum(t.get("valor") or 0 for t in pendientes),
        "fichas": _fichas(tenant, tz, ahora),
        "meses": MESES_LABEL,
        "duracion_default": tenant.get("duracion_turno_minutos") or 50,
    }

    # El calendario es la vista de entrada; la semana (con las acciones de
    # cada sesión) se abre al tocar un día.
    if not request.args.get("semana"):
        primero = _mes_de(request.args.get("mes"), ahora.date())
        return render_template(
            "agenda.html",
            vista="mes",
            semanas=_calendario(tenant, primero, tz, ahora),
            mes_titulo=_meses_label(primero),
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
                "turnos": por_dia.get(clave, []),
            }
        )
        cursor += timedelta(days=1)

    return render_template(
        "agenda.html",
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


FRECUENCIAS = {"semanal": 7, "quincenal": 14}
MAX_SESIONES_FIJAS = 26

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


def _avisar_cambio(tenant: dict, telefono: str, nuevo_inicio: datetime, tz: ZoneInfo) -> bool:
    """Avisa al paciente el horario nuevo. Es aparte del recordatorio: si la
    sesión es en dos semanas, sin esto se enteraría recién la noche anterior."""
    numero = whatsapp.numero_de(tenant)
    if not numero:
        return False
    cuando = scheduler._fmt(nuevo_inicio.isoformat(), tz)
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
    error = db.reprogramar_turno(tenant["id"], turno_id, nuevo_inicio, duracion)
    if error:
        flash(error)
        return _volver_a_agenda()
    aviso = ""
    if request.form.get("avisar") and nuevo_inicio > datetime.now(ZoneInfo("UTC")):
        if _avisar_cambio(tenant, original["paciente_telefono"], nuevo_inicio, tz):
            aviso = " Le avisamos al paciente."
        else:
            aviso = " No se le pudo avisar al paciente: escribile vos."
    flash(f"Sesión pasada al {DIAS_LABEL[fecha.weekday()]} {fecha.day}/{fecha.month:02d} a las {hora:%H:%M}.{aviso}")
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
    campos = {"fecha_nacimiento": fecha}
    if "notas" in request.form:
        campos["notas"] = request.form["notas"].strip() or None
    db.guardar_paciente(session["tenant_id"], telefono, campos)
    return _volver_a_agenda()


@app.post("/agenda/paciente/persona")
@login_required
def agenda_persona():
    telefono = request.form.get("telefono") or ""
    nombre = (request.form.get("nombre") or "").strip()
    if not telefono or not nombre:
        flash("Falta el nombre.")
        return _volver_a_agenda()
    db.agregar_persona(session["tenant_id"], telefono, nombre, (request.form.get("vinculo") or "").strip() or None)
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


@app.post("/agenda/paciente/fecha/<fecha_id>/borrar")
@login_required
def agenda_fecha_borrar(fecha_id):
    db.borrar_fecha(session["tenant_id"], fecha_id)
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
    if not nombre or not telefono:
        flash("Para agendar hacen falta nombre y WhatsApp del paciente.")
        return _volver_a_agenda()
    tenant = db.get_tenant_by_id(session["tenant_id"])
    valor = _valor_del_form()
    frecuencia = request.form.get("frecuencia") or None
    if frecuencia not in FRECUENCIAS:
        frecuencia = None
    turno = db.turno_por_id(tenant["id"], turno_id)
    if not turno or not db.reservar_turno(tenant["id"], turno_id, telefono, nombre, valor, frecuencia):
        flash("Ese horario ya no está libre.")
        return _volver_a_agenda()
    if not frecuencia:
        return _volver_a_agenda()

    # Cada sesión es su propia fila: si después una se mueve o se cancela, las
    # demás no se enteran. Un horario ocupado se saltea y se avisa, para que el
    # profesional decida qué hacer con esa semana.
    try:
        cantidad = min(int(request.form.get("cantidad") or 1), MAX_SESIONES_FIJAS)
    except ValueError:
        cantidad = 1
    tz = _tz_de(tenant)
    primera = datetime.fromisoformat(turno["inicio"]).astimezone(tz)
    duracion = int((datetime.fromisoformat(turno["fin"]) - primera).total_seconds() // 60)
    salteadas = []
    for i in range(1, cantidad):
        siguiente = primera + timedelta(days=FRECUENCIAS[frecuencia] * i)
        if not db.reservar_en_horario(tenant["id"], siguiente.astimezone(ZoneInfo("UTC")), duracion,
                                      telefono, nombre, valor, frecuencia):
            salteadas.append(f"{siguiente.day}/{siguiente.month:02d}")
    flash(f"Se agendaron {cantidad - len(salteadas)} sesiones ({frecuencia}).")
    if salteadas:
        flash(f"Quedaron sin agendar por estar ocupadas: {', '.join(salteadas)}.")
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
