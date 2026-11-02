"""FiBOT Consultorio — plataforma.

UN solo deploy sirve a TODOS los profesionales (tenants). El webhook de Meta
es uno solo; cada mensaje entrante trae el phone_number_id del número al que
le escribieron, y eso es lo que resuelve a qué tenant pertenece (ver
db.get_tenant_by_phone_number_id). Nada de esto tiene nada de un cliente en
particular — lo particular vive en la tabla `tenants` y se carga con
scripts/crear_tenant.py, nunca hardcodeado acá."""
import copy
import functools
import hmac
import io
import json
import logging
import os
import re
import secrets
import unicodedata
from datetime import date, datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import bcrypt
from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from openpyxl import Workbook

import config
import conversation
import db
import rubros
import cuestionarios
import fluidez
import informe
import resultados_tests
import tests_sugeridos
from fechas import alerta_de_seguimiento, proxima_vez, sesiones_que_quedan
import scheduler
import whatsapp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

app = Flask(__name__)
# Railway termina TLS en su proxy: sin esto, los links absolutos (el del
# cuestionario, el de reservas en la guía) salen con http://.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)
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
    por_clave_inf: dict[str, list[dict]] = {}
    for x in db.informes(tenant["id"]):
        x["fecha"] = datetime.fromisoformat(x["created_at"]).astimezone(tz).strftime("%d/%m/%Y %H:%M") if "T" in x["created_at"] else x["created_at"]
        por_clave_inf.setdefault(db.clave_de(x), []).append(x)
    for f in lista:
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
        lista.append({
            **b, "_desde": desde.astimezone(tz).date(), "_hasta": ultimo,
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
                           for p in plantillas if cuestionarios.completa(p["contenido"])],
        "plantillas": plantillas,
        "tests_para_repetir": [{"ficha": f, **t} for f in fichas for t in f.get("para_repetir") or []],
        "ventana_aviso": tests_sugeridos.VENTANA_AVISO,
        "reservas_nuevas": sorted(({"ficha": f, "turno": t} for f in fichas for t in f["sesiones"] if t.get("reserva_nueva")),
                                  key=lambda r: r["turno"]["inicio"]),
        "espera": db.lista_de_espera(tenant["id"]),
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
    db.agregar_bloqueo(
        tenant["id"],
        datetime.combine(desde, dtime.min, tzinfo=tz).astimezone(utc),
        datetime.combine(hasta + timedelta(days=1), dtime.min, tzinfo=tz).astimezone(utc),
        (request.form.get("motivo") or "").strip() or None,
    )
    flash("Días bloqueados. Mirá en la tarjeta si hay sesiones para reprogramar.")
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
    db.cancelar_turno(session["tenant_id"], turno_id, "profesional")
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
    error = db.reprogramar_turno(tenant["id"], turno_id, nuevo_inicio, duracion)
    if error:
        flash(error)
        return _volver_a_agenda()
    aviso = ""
    if request.form.get("avisar") and nuevo_inicio > datetime.now(ZoneInfo("UTC")):
        if _avisar_cambio(tenant, original, nuevo_inicio, tz):
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
    db.agregar_a_espera(session["tenant_id"], nombre, telefono, (request.form.get("preferencia") or "").strip() or None)
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
        if _ofrecer_horario(tenant, persona["telefono"], cuando, link):
            flash(f"Le ofrecimos a {persona['nombre']} el {cuando}.")
        else:
            flash(f"No se pudo escribirle a {persona['nombre']}.")
    return _volver_a_agenda()


def _ofrecer_horario(tenant: dict, telefono: str, cuando: str, link: str) -> bool:
    numero = whatsapp.numero_de(tenant)
    if not numero:
        return False
    if config.LISTA_ESPERA_TEMPLATE:
        return whatsapp.enviar_plantilla(numero, telefono, config.LISTA_ESPERA_TEMPLATE,
                                         config.RECORDATORIO_TEMPLATE_LANG, [tenant["nombre"], cuando, link])
    return whatsapp.enviar_texto(numero, telefono, f"Hola. Se liberó un horario con *{tenant['nombre']}*: {cuando}. Si lo querés, reservalo acá: {link}")


@app.post("/agenda/avisos")
@login_required
def agenda_avisos():
    campos = {}
    if "telefono" in request.form:
        campos["aviso_telefono"] = "".join(ch for ch in request.form["telefono"] if ch.isdigit()) or None
    if "matricula" in request.form:
        campos["matricula"] = request.form["matricula"].strip() or None
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
    return render_template("informe.html", tenant=tenant, ficha=ficha, hoy=ahora.strftime("%d/%m/%Y"),
                           inf=informe.armar(ficha, rubros.de(tenant), ahora.date()), guardado=None)


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
    if not nombre or not telefono:
        flash("Para agendar hacen falta nombre y WhatsApp del paciente.")
        return _volver_a_agenda()
    tenant = db.get_tenant_by_id(session["tenant_id"])
    valor = _valor_del_form()
    frecuencia = request.form.get("frecuencia") or None
    if frecuencia not in FRECUENCIAS:
        frecuencia = None
    modalidad = request.form.get("modalidad")
    if modalidad not in MODALIDADES:
        modalidad = None
    turno = db.turno_por_id(tenant["id"], turno_id)
    otra_persona = bool(request.form.get("otra_persona"))
    ambiguo = not otra_persona and db.nombre_ambiguo(tenant["id"], telefono, nombre)
    ficha, ficha_nueva = (db.ficha_aparte(tenant["id"], telefono, nombre), False) if otra_persona \
        else db.ficha_para(tenant["id"], telefono, nombre)
    if not turno or not db.reservar_turno(tenant["id"], turno_id, telefono, nombre, valor, frecuencia, modalidad, ficha):
        flash("Ese horario ya no está libre.")
        return _volver_a_agenda()
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
