"""Tests del seguimiento de sesiones: asistencia, pago y reprogramar.

Correrlo SUELTO (`python test_seguimiento.py`), igual que los otros test_*.py
de este repo: reemplaza `db._client` sin restaurarlo.

A diferencia de los otros tests, acá NO se pisan las funciones de `db`: se
pisa el cliente de Supabase por uno en memoria, para que corra la lógica real
de db.py — que es justo donde están las cosas que importan (el orden de
reprogramar, que liberar un turno limpie el pago, que nada cruce de tenant).
"""
import itertools
import re
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault(
    "SUPABASE_SERVICE_ROLE_KEY",
    "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.fake_signature_not_real",
)
os.environ.setdefault("META_ACCESS_TOKEN", "fake")
os.environ.setdefault("META_APP_SECRET", "fake")
os.environ.setdefault("META_VERIFY_TOKEN", "fake")
os.environ.setdefault("FLASK_SECRET_KEY", "fake")

# Ver CLAUDE.md: el scheduler se apaga ANTES de importar app.
import scheduler  # noqa: E402

scheduler.iniciar = lambda: None

import app  # noqa: E402
import db  # noqa: E402
import whatsapp as whatsapp_real  # noqa: E402
whatsapp_real.marcar_leido = lambda pid, mid: True  # nunca a Meta desde los tests

fallos = []



def _panel(c):
    """La agenda y la página de ajustes juntas: los ajustes viven en /ajustes."""
    return c.get("/agenda").data.decode() + c.get("/ajustes").data.decode()


def chequear(nombre, ok, detalle=""):
    print(f"  {nombre:.<60} {'OK' if ok else 'FALLO'}{'  ' + str(detalle) if detalle else ''}")
    if not ok:
        fallos.append(nombre)


# ---------- Supabase en memoria (sólo lo que usa db.py) ----------

class _Res:
    def __init__(self, data):
        self.data = data


def _norm(v):
    # Las fechas se comparan como instantes, no como strings: Postgres
    # tampoco distingue "21:00+00:00" de "18:00-03:00".
    if isinstance(v, str) and "T" in v:
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            pass
    return v


_ALTAS = itertools.count()


class _Query:
    def __init__(self, tabla):
        self.tabla, self.filtros, self.op, self.payload = tabla, [], "select", None
        self._orden, self._limite, self._on_conflict, self._ignore = None, None, "", False
        self._rango = None

    def select(self, *_):
        self.op = "select"
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def upsert(self, filas, on_conflict="", ignore_duplicates=False):
        self.op, self.payload, self._on_conflict, self._ignore = "upsert", filas, on_conflict, ignore_duplicates
        return self

    def insert(self, fila):
        self.op, self.payload = "insert", fila
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, c, v):
        self.filtros.append(lambda f: _norm(f.get(c)) == _norm(v))
        return self

    def is_(self, c, v):
        self.filtros.append(lambda f: f.get(c) is None if v == "null" else f.get(c) == v)
        return self

    def ilike(self, c, patron):
        regex = re.compile("^" + ".*".join(re.escape(p) for p in patron.casefold().split("%")) + "$")
        self.filtros.append(lambda f: bool(regex.match((f.get(c) or "").casefold())))
        return self

    def in_(self, c, valores):
        normales = [_norm(v) for v in valores]
        self.filtros.append(lambda f: _norm(f.get(c)) in normales)
        return self

    def neq(self, c, v):
        self.filtros.append(lambda f: _norm(f.get(c)) != _norm(v))
        return self

    def gt(self, c, v):
        self.filtros.append(lambda f: f.get(c) is not None and _norm(f[c]) > _norm(v))
        return self

    def lt(self, c, v):
        self.filtros.append(lambda f: f.get(c) is not None and _norm(f[c]) < _norm(v))
        return self

    def gte(self, c, v):
        self.filtros.append(lambda f: f.get(c) is not None and _norm(f[c]) >= _norm(v))
        return self

    def lte(self, c, v):
        self.filtros.append(lambda f: f.get(c) is not None and _norm(f[c]) <= _norm(v))
        return self

    def order(self, c, desc=False):
        self._orden = (c, desc)
        return self

    def limit(self, n):
        self._limite = n
        return self

    def range(self, desde, hasta):
        self._rango = (desde, hasta)
        return self

    def execute(self):
        filas = TABLAS["turnos"] if self.tabla == "turnos_disponibles" else TABLAS[self.tabla]
        if self.tabla == "turnos_disponibles":
            reservados = [r for r in filas if r["estado"] == "reservado"]
            _ya = datetime.now(timezone.utc)
            filas = [{**t, "abierto": t.get("abierto", False)} for t in filas if t["estado"] == "libre" and not t.get("hueco")
                     and not (t.get("guardado_hasta") and _norm(t["guardado_hasta"]) > _ya) and not any(
                r["tenant_id"] == t["tenant_id"] and _norm(r["inicio"]) < _norm(t["fin"])
                and _norm(r["fin"]) > _norm(t["inicio"]) for r in reservados)
                and (t.get("abierto") or not any(b["tenant_id"] == t["tenant_id"] and _norm(b["desde"]) < _norm(t["fin"])
                            and _norm(b["hasta"]) > _norm(t["inicio"]) for b in TABLAS["bloqueos"]))]
        if self.op == "upsert":
            claves = self._on_conflict.split(",")
            creadas = []
            for nueva in ([self.payload] if isinstance(self.payload, dict) else self.payload):
                existe = next((f for f in filas if all(_norm(f.get(k)) == _norm(nueva[k]) for k in claves)), None)
                if existe:
                    if not self._ignore:
                        existe.update(nueva)
                        creadas.append(dict(existe))
                    continue
                fila = {"id": f"turno-{len(filas) + 1}", "paciente_telefono": None, "paciente_nombre": None,
                        "recordatorio_enviado": False, "asistencia": None, "pago": "pendiente",
                        "reprogramado_desde": None, "valor": None, "frecuencia": None, "modalidad": None,
                        "nota": None, **nueva}
                filas.append(fila)
                creadas.append(dict(fila))
            return _Res(creadas)
        if self.op == "insert":
            # created_at como contador: ordena por orden de alta, igual que Postgres.
            filas.append({"id": f"{self.tabla}-{len(filas) + 1}", "created_at": f"{next(_ALTAS):08d}", **self.payload})
            return _Res([dict(filas[-1])])
        match = [f for f in filas if all(p(f) for p in self.filtros)]
        if self.op == "delete":
            TABLAS[self.tabla] = [f for f in filas if f not in match]
            return _Res([dict(f) for f in match])
        if self.op == "update":
            for f in match:
                f.update(self.payload)
            return _Res([dict(f) for f in match])
        if self._orden:
            match.sort(key=lambda f: _norm(f[self._orden[0]]), reverse=self._orden[1])
        if self._limite is not None:
            match = match[: self._limite]
        if self._rango:
            match = match[self._rango[0]: self._rango[1] + 1]
        return _Res([dict(f) for f in match])


class _Cliente:
    def table(self, nombre):
        return _Query(nombre)


TZ = "America/Argentina/Buenos_Aires"
TENANT = {"id": "t1", "nombre": "Lic. Marcos", "timezone": TZ, "duracion_turno_minutos": 50,
          "panel_user": "marcos", "activo": True}
OTRO = {"id": "t2", "nombre": "Dra. Otra", "timezone": TZ, "duracion_turno_minutos": 50,
        "panel_user": "otra", "activo": True}

ahora = datetime.now(timezone.utc).replace(second=0, microsecond=0)
ayer = ahora - timedelta(days=1)
manana = ahora + timedelta(days=1)


def turno(id_, tenant_id, inicio, **extra):
    base = {"id": id_, "tenant_id": tenant_id, "inicio": inicio.isoformat(),
            "fin": (inicio + timedelta(minutes=50)).isoformat(), "estado": "libre",
            "paciente_telefono": None, "paciente_nombre": None, "ficha": "", "revisar_ficha": False, "reserva_nueva": False, "reserva_avisada": False, "recordatorio_enviado": False,
            "asistencia": None, "pago": "pendiente", "reprogramado_desde": None,
            "valor": None, "frecuencia": None, "modalidad": None, "nota": None}
    return {**base, **extra}


def _sesion_de(tenant_id):
    """Sesión del lado del servidor para un cliente de prueba."""
    TABLAS.setdefault("sesiones", [])
    sid = f"s-prueba-{len(TABLAS['sesiones']) + 1}"
    ahora_ = datetime.now(timezone.utc).isoformat()
    TABLAS["sesiones"].append({"id": sid, "tenant_id": tenant_id, "dispositivo": "Prueba", "origen": "clave",
                               "creada": ahora_, "ultima": ahora_})
    return sid


def reset():
    TABLAS["tenants"] = [dict(TENANT), dict(OTRO)]
    TABLAS["pacientes"] = []
    TABLAS["horarios_sacados"] = []
    TABLAS["paciente_personas"] = []
    TABLAS["paciente_fechas"] = []
    TABLAS["lista_espera"] = []
    TABLAS["portadas"] = []
    TABLAS["slugs_anteriores"] = []
    TABLAS["cancelaciones"] = []
    TABLAS["bloqueos"] = []
    TABLAS["paciente_tests"] = []
    TABLAS["paciente_bitacora"] = []
    TABLAS["bitacora_fotos"] = []
    TABLAS["textos_lectura"] = []
    TABLAS["lecturas"] = []
    TABLAS["cuestionarios"] = []
    TABLAS["informes"] = []
    TABLAS["plantillas_cuestionario"] = []
    TABLAS["plantillas_informe"] = []
    TABLAS["biblioteca_cuestionarios"] = []
    TABLAS["entregas"] = []
    TABLAS["admins"] = []
    TABLAS["eventos_plataforma"] = []
    TABLAS["mensajes_pacientes"] = []
    TABLAS["elecciones_whatsapp"] = []
    TABLAS["ajustes_plataforma"] = []
    TABLAS["plantillas_meta"] = []
    TABLAS["mensajes_sin_destino"] = []
    TABLAS["eventos_pagina"] = []
    TABLAS["pagos"] = []
    TABLAS["login_intentos"] = []
    TABLAS.setdefault("sesiones", [])  # sobreviven a reset(): los clientes de prueba siguen logueados
    TABLAS["actividad"] = []
    TABLAS["enlaces_clave"] = []
    TABLAS["qr_logins"] = []
    TABLAS["llaves_acceso"] = []
    TABLAS["admin_llaves"] = []
    TABLAS["interesados"] = []
    TABLAS["turnos"] = [
        turno("pasado", "t1", ayer, estado="reservado", paciente_nombre="Juan", paciente_telefono="5491100000001"),
        turno("futuro", "t1", manana, estado="reservado", paciente_nombre="Ana", paciente_telefono="5491100000002",
              pago="pagado", recordatorio_enviado=True),
        turno("libre-manana", "t1", manana + timedelta(hours=2)),
        turno("ajeno", "t2", manana + timedelta(hours=2), estado="reservado", paciente_nombre="De otra",
              paciente_telefono="5491100000009"),
    ]


def fila(id_):
    return next(f for f in TABLAS["turnos"] if f["id"] == id_)


TABLAS = {}
db._client = _Cliente()

# ---------- db.py ----------

print("Asistencia y pago")
reset()
db.marcar_asistencia("t1", "pasado", "vino")
chequear("marcar 'vino'", fila("pasado")["asistencia"] == "vino")
db.marcar_pago("t1", "pasado", "pagado")
chequear("marcar 'pagado'", fila("pasado")["pago"] == "pagado")
db.marcar_pago("t2", "pasado", "no_cobra")
chequear("otro tenant no puede tocar el pago", fila("pasado")["pago"] == "pagado")
db.marcar_asistencia("t1", "libre-manana", "vino")
chequear("un horario libre no registra asistencia", fila("libre-manana")["asistencia"] is None)
try:
    db.marcar_pago("t1", "pasado", "quizas")
    chequear("valor de pago inválido se rechaza", False)
except ValueError:
    chequear("valor de pago inválido se rechaza", True)

print("Pendientes de cobro")
reset()
pend = db.pendientes_de_pago("t1", ahora)
chequear("sólo la sesión pasada sin pagar", [t["id"] for t in pend] == ["pasado"], [t["id"] for t in pend])
db.marcar_pago("t1", "pasado", "no_cobra")
chequear("'no se cobra' la saca de la lista", db.pendientes_de_pago("t1", ahora) == [])

print("Reprogramar a un horario libre existente")
reset()
destino = manana + timedelta(hours=2)
err = db.reprogramar_turno("t1", "futuro", destino, 50)
chequear("sin error", err is None, err)
nuevo = fila("libre-manana")
chequear("el paciente pasa al horario nuevo", nuevo["estado"] == "reservado" and nuevo["paciente_nombre"] == "Ana")
chequear("el pago viaja con la sesión", nuevo["pago"] == "pagado")
chequear("queda anotado de cuándo se pasó", _norm(nuevo["reprogramado_desde"]) == manana)
chequear("el recordatorio se manda de nuevo", nuevo["recordatorio_enviado"] is False)
viejo = fila("futuro")
chequear("el horario viejo queda libre y limpio",
         viejo["estado"] == "libre" and viejo["paciente_nombre"] is None and viejo["pago"] == "pendiente"
         and viejo["recordatorio_enviado"] is False)
chequear("no toca el turno del otro tenant a la misma hora", fila("ajeno")["paciente_nombre"] == "De otra")

print("Reprogramar a una hora sin horario cargado")
reset()
destino = manana + timedelta(days=2, hours=5)
err = db.reprogramar_turno("t1", "pasado", destino, 50)
chequear("sin error", err is None, err)
creado = next(f for f in TABLAS["turnos"] if f["tenant_id"] == "t1" and _norm(f["inicio"]) == destino)
chequear("se crea el horario y queda reservado", creado["estado"] == "reservado" and creado["paciente_nombre"] == "Juan")
chequear("la asistencia NO viaja (la sesión nueva no pasó)", creado["asistencia"] is None)

print("Reprogramar a un horario ocupado")
reset()
err = db.reprogramar_turno("t1", "pasado", manana, 50)
chequear("devuelve el motivo", err is not None and "ocupado" in err, err)
chequear("la sesión original sigue intacta",
         fila("pasado")["estado"] == "reservado" and fila("pasado")["paciente_nombre"] == "Juan")
chequear("la otra sesión sigue intacta", fila("futuro")["paciente_nombre"] == "Ana")
err = db.reprogramar_turno("t2", "pasado", manana + timedelta(days=3), 50)
chequear("no se puede reprogramar un turno de otro tenant", err is not None and fila("pasado")["estado"] == "reservado")

print("Reprogramar a una hora que se pisa con otra sesión")
reset()
cantidad = len(TABLAS["turnos"])
err = db.reprogramar_turno("t1", "pasado", manana + timedelta(minutes=20), 50)
chequear("se rechaza aunque no coincida el inicio", err is not None and "ocupado" in err, err)
chequear("no deja un horario suelto creado", len(TABLAS["turnos"]) == cantidad)
chequear("la sesión original sigue intacta", fila("pasado")["paciente_nombre"] == "Juan")
reset()
err = db.reprogramar_turno("t1", "futuro", manana + timedelta(minutes=20), 50)
chequear("correr una sesión no choca consigo misma", err is None, err)
reset()
err = db.reprogramar_turno("t1", "pasado", manana + timedelta(minutes=50), 50)
chequear("pegada a otra sesión se permite", err is None, err)

print("Horarios libres que se pisan con una sesión")
reset()
libre = manana + timedelta(hours=2)
db.reprogramar_turno("t1", "pasado", libre + timedelta(minutes=30), 50)
ids = [t["id"] for t in db.turnos_libres("t1", ahora)]
chequear("no se ofrece el horario pisado", "libre-manana" not in ids, ids)
chequear("reservarlo igual se rechaza", db.reservar_turno("t1", "libre-manana", "5491100000003", "Eva") is False)
chequear("sigue libre", fila("libre-manana")["estado"] == "libre")
reservada = next(f for f in TABLAS["turnos"] if f["paciente_nombre"] == "Juan")
db.cancelar_turno("t1", reservada["id"])
chequear("vuelve a ofrecerse al cancelar la sesión",
         "libre-manana" in [t["id"] for t in db.turnos_libres("t1", ahora)])
chequear("y se puede reservar de nuevo", db.reservar_turno("t1", "libre-manana", "5491100000003", "Eva") is True)

print("Valor de sesión")
reset()
db.marcar_valor("t1", "futuro", 25000)
chequear("se guarda el valor", fila("futuro")["valor"] == 25000)
db.marcar_valor("t2", "futuro", 1)
chequear("otro tenant no lo toca", fila("futuro")["valor"] == 25000)
db.reprogramar_turno("t1", "futuro", manana + timedelta(hours=2), 50)
chequear("viaja con la sesión", fila("libre-manana")["valor"] == 25000)
chequear("el horario viejo queda sin valor", fila("futuro")["valor"] is None)

print("Generar horarios no pisa reservas")
reset()
creados = db.crear_turnos_libres("t1", [ayer, manana, manana + timedelta(days=5)], 50)
chequear("sólo crea el que no existía", creados == 1, creados)
chequear("la sesión reservada sigue reservada", fila("futuro")["estado"] == "reservado")

# ---------- panel ----------

print("Panel")
reset()
db.get_tenant_by_id = lambda tid: next((dict(t) for t in TABLAS["tenants"] if t["id"] == tid), None)
client = app.app.test_client()
with client.session_transaction() as s:
    s["tenant_id"] = "t1"
    s["sesion_id"] = _sesion_de("t1")
    s["csrf_token"] = "tok"

_post_sin_token = client.post


def _post(url, data=None, **kw):
    return _post_sin_token(url, data={"csrf_token": "tok", **(data or {})}, **kw)


client.post = _post
enviados = []
app.whatsapp.enviar_texto = lambda numero, to, texto: enviados.append((numero, to, texto)) or True
app.config.PLATAFORMA_PHONE_NUMBER_ID = "plataforma"

r = client.get("/agenda")
html = r.data.decode()
chequear("GET /agenda -> 200", r.status_code == 200, r.status_code)
chequear("muestra pendientes de cobro", "Pendientes de cobro" in html and "Juan" in html)
chequear("no muestra pacientes de otro tenant", "De otra" not in html)
chequear("sin pausa ni fuera del directorio: no hay avisos de página", "aviso-pausa" not in html and "aviso-directorio" not in html)
_t1 = next(t for t in TABLAS["tenants"] if t["id"] == "t1")
_t1_antes = dict(_t1)
_t1.update(pausado=True, pausado_hasta=None, slug="marcos", en_directorio=False,
           pausado_desde=(datetime.now(timezone.utc) - timedelta(days=12)).isoformat())
html = client.get("/agenda").data.decode()
chequear("pausado hace días sin fecha: avisa con los días y ofrece volver o poner fecha",
         "Tu página está pausada hace 12 días, sin fecha de vuelta." in html and "Poner fecha de vuelta" in html, html[:0])
chequear("oculta para Google: avisa con botón para mostrarla", "Google no muestra tu página" in html and "Que Google la muestre" in html)
_t1.update(pausado_desde=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
chequear("pausado hace un día: todavía no avisa", "aviso-pausa" not in client.get("/agenda").data.decode())
_t1.update(pausado_desde=None)
chequear("pausado sin saber desde cuándo: avisa sin días", "Tu página está pausada, sin fecha de vuelta." in client.get("/agenda").data.decode())
_t1.update(pausado_hasta="2099-01-10")
chequear("pausado con fecha de vuelta: no avisa", "aviso-pausa" not in client.get("/agenda").data.decode())
_t1.clear(); _t1.update(_t1_antes)

chequear("los forms llevan el token", 'name="csrf_token" value="tok"' in html)
r = _post_sin_token("/agenda/pago/pasado", data={"pago": "pagado"})
chequear("POST sin token -> 403", r.status_code == 403, r.status_code)
r = _post_sin_token("/agenda/pago/pasado", data={"pago": "pagado", "csrf_token": "otro"})
chequear("POST con token ajeno -> 403", r.status_code == 403 and fila("pasado")["pago"] == "pendiente")

r = client.get("/agenda?semana=basura")
chequear("semana inválida cae a la actual", r.status_code == 200)

r = client.post("/agenda/asistencia/pasado", data={"asistencia": "no_vino", "semana": "2026-10-26"})
chequear("POST asistencia redirige a la misma semana",
         r.status_code == 302 and "semana=2026-10-26" in r.headers["Location"], r.headers.get("Location"))
chequear("quedó 'no_vino'", fila("pasado")["asistencia"] == "no_vino")
client.post("/agenda/asistencia/pasado", data={"asistencia": ""})
chequear("se puede desmarcar", fila("pasado")["asistencia"] is None)

r = client.post("/agenda/pago/pasado", data={"pago": "inventado"})
chequear("pago inválido -> 400", r.status_code == 400)
r = client.post("/agenda/pago/pasado", data={"pago": "pagado", "semana": "https://evil.example"})
chequear("'semana' basura no redirige afuera", r.headers["Location"].endswith("/agenda"), r.headers.get("Location"))

local = (manana + timedelta(hours=2)).astimezone(app.ZoneInfo(TZ))
r = client.post("/agenda/reprogramar/futuro", data={"fecha": local.strftime("%Y-%m-%d"), "hora": local.strftime("%H:%M"), "avisar": "1"})
chequear("le avisa el cambio al paciente", len(enviados) == 1 and enviados[0][1] == "5491100000002"
         and "cambió de horario" in enviados[0][2], enviados)
chequear("POST reprogramar -> va a la semana nueva", r.status_code == 302 and "semana=" in r.headers["Location"])
chequear("quedó en el horario nuevo", fila("libre-manana")["paciente_nombre"] == "Ana")
otra = local + timedelta(hours=3)
client.post("/agenda/reprogramar/libre-manana", data={"fecha": otra.strftime("%Y-%m-%d"), "hora": otra.strftime("%H:%M")})
chequear("sin el tilde no avisa", len(enviados) == 1, enviados)
reservada = next(f for f in TABLAS["turnos"] if f["paciente_nombre"] == "Ana")
r = client.post(f"/agenda/reprogramar/{reservada['id']}", data={"fecha": "", "hora": ""}, follow_redirects=True)
chequear("sin fecha -> aviso, no rompe", r.status_code == 200 and "Elegí día y hora" in r.data.decode())

r = client.post("/agenda/agendar/futuro", data={"nombre": "Pedro", "telefono": "+54 9 11 5555-0000"})
chequear("agendar a mano en un horario libre", fila("futuro")["paciente_telefono"] == "5491155550000"
         and fila("futuro")["estado"] == "reservado")
r = client.post("/agenda/agendar/futuro", data={"nombre": "Otro", "telefono": "5491100000000"}, follow_redirects=True)
chequear("agendar sobre uno ocupado avisa", "ya no está libre" in r.data.decode() and fila("futuro")["paciente_nombre"] == "Pedro")

print("Agendar sin WhatsApp")
reset()
TABLAS["turnos"].append(turno("sin-1", "t1", manana + timedelta(days=2, hours=1)))
TABLAS["turnos"].append(turno("sin-2", "t1", manana + timedelta(days=9, hours=1)))
TABLAS["turnos"].append(turno("sin-3", "t1", manana + timedelta(days=10, hours=1)))
TABLAS["turnos"].append(turno("sin-4", "t1", manana + timedelta(days=10, hours=3)))
r = client.post("/agenda/agendar/sin-1", data={"nombre": "Lola Paz", "telefono": ""}, follow_redirects=True)
clave_lola = fila("sin-1")["paciente_telefono"]
chequear("sin WhatsApp: agenda igual", fila("sin-1")["estado"] == "reservado" and clave_lola.startswith(db.SIN_WHATSAPP)
         and not db.tiene_whatsapp(clave_lola))
client.post("/agenda/agendar/sin-2", data={"nombre": "lola  paz"})
chequear("sin WhatsApp: el mismo nombre va a la misma ficha", fila("sin-2")["paciente_telefono"] == clave_lola)
client.post("/agenda/agendar/sin-3", data={"nombre": "Lola Paz", "otra_persona": "1"})
chequear("sin WhatsApp: «otra persona» abre otra ficha", fila("sin-3")["paciente_telefono"] not in (clave_lola, ""))
r = client.post("/agenda/agendar/sin-3", data={"nombre": "", "telefono": "5491100000001"}, follow_redirects=True)
chequear("sin nombre no agenda", "hace falta el nombre" in r.data.decode())
html = client.get("/agenda?semana=" + (manana + timedelta(days=10)).strftime("%Y-%m-%d")).data.decode()
chequear("panel: el WhatsApp es opcional", 'name="telefono" placeholder="54 9 11 2345 6789" required' not in html
         and "WhatsApp <span class=\"tenue\">(opcional)</span>" in html)
chequear("panel: botón Sacar en los libres", "/agenda/libre/sin-4/sacar" in html and ">Sacar</button>" in html)
chequear("ficha: muestra «Sin WhatsApp» y no la clave inventada", "Sin WhatsApp" in html and clave_lola not in html.split("data-buscar")[1][:200]
         and "Cargar su WhatsApp" in html)
chequear("whatsapp.py no manda a una clave sin número", whatsapp_real._sin_numero(clave_lola) and not whatsapp_real._sin_numero("5491100000001"))
enviados.clear()
mover = manana + timedelta(days=3, hours=2)
r = client.post("/agenda/reprogramar/sin-1", data={"fecha": mover.astimezone(app.ZoneInfo(TZ)).strftime("%Y-%m-%d"),
                                                  "hora": mover.astimezone(app.ZoneInfo(TZ)).strftime("%H:%M"), "avisar": "1"},
                follow_redirects=True)
chequear("reprogramar sin WhatsApp: no dice que avisó", "avisale vos" in r.data.decode() and not enviados)
next(p for p in TABLAS["pacientes"] if p["paciente_telefono"] == clave_lola)["notas"] = "nota"
r = client.post("/agenda/paciente/whatsapp", data={"telefono": clave_lola, "whatsapp": "123"}, follow_redirects=True)
chequear("cargar WhatsApp: número corto no", "no parece completo" in r.data.decode())
r = client.post("/agenda/paciente/whatsapp", data={"telefono": clave_lola, "whatsapp": "+54 9 11 4444-0000"}, follow_redirects=True)
chequear("cargar WhatsApp: pasan las sesiones y la ficha", "ya le llegan" in r.data.decode()
         and sum(f.get("paciente_telefono") == "5491144440000" for f in TABLAS["turnos"]) == 2
         and not any(f.get("paciente_telefono") == clave_lola for f in TABLAS["turnos"])
         and fila("sin-3")["paciente_telefono"].startswith(db.SIN_WHATSAPP)
         and any(p["paciente_telefono"] == "5491144440000" and p.get("notas") == "nota" for p in TABLAS["pacientes"]))
r = client.post("/agenda/paciente/whatsapp", data={"telefono": "5491144440000", "whatsapp": "5491155556666"}, follow_redirects=True)
chequear("cargar WhatsApp: no pisa uno que ya tiene", "ya tiene WhatsApp" in r.data.decode()
         and not any(f.get("paciente_telefono") == "5491155556666" for f in TABLAS["turnos"]))

print("Pacientes fijos")
reset()
primero = manana + timedelta(days=30, hours=3)
TABLAS["turnos"].append(turno("fijo-1", "t1", primero))
TABLAS["turnos"].append(turno("ocupado", "t1", primero + timedelta(days=28), estado="reservado",
                              paciente_nombre="Otra", paciente_telefono="5491100000005"))
r = client.post("/agenda/agendar/fijo-1", data={"nombre": "Eva", "telefono": "5491100000003", "valor": "$ 30.000",
                                                "frecuencia": "quincenal", "cantidad": "4"}, follow_redirects=True)
html = r.data.decode()
de_eva = sorted(_norm(f["inicio"]) for f in TABLAS["turnos"] if f["paciente_nombre"] == "Eva")
chequear("agenda cada 14 días y saltea la ocupada",
         de_eva == [primero, primero + timedelta(days=14), primero + timedelta(days=42)], de_eva)
chequear("todas con valor y frecuencia", all(f["valor"] == 30000 and f["frecuencia"] == "quincenal"
                                             for f in TABLAS["turnos"] if f["paciente_nombre"] == "Eva"))
chequear("avisa la que quedó sin agendar", "sin agendar" in html and "3 sesiones" in html)
r = client.post("/agenda/agendar/libre-manana", data={"nombre": "Una", "telefono": "5491100000006", "cantidad": "9"})
chequear("sin frecuencia es una sola", sum(f["paciente_nombre"] == "Una" for f in TABLAS["turnos"]) == 1)

print("Fichas")
reset()
TABLAS["turnos"].append(turno("vieja", "t1", ayer - timedelta(days=7), estado="reservado", paciente_nombre="Ana",
                              paciente_telefono="5491100000002", asistencia="vino", pago="pagado", valor=20000))
TABLAS["turnos"].append(turno("debe", "t1", ayer - timedelta(days=14), estado="reservado", paciente_nombre="Ana",
                              paciente_telefono="5491100000002", asistencia="no_vino", valor=20000))
fila("futuro").update(valor=25000, frecuencia="semanal")
fichas = app._fichas(TENANT, app.ZoneInfo(TZ), ahora.astimezone(app.ZoneInfo(TZ)))
ana = next(f for f in fichas if f["nombre"] == "Ana")
chequear("una ficha por paciente, en orden", [f["nombre"] for f in fichas] == ["Ana", "Juan"], fichas)
chequear("valor y frecuencia son los de la próxima sesión", ana["valor"] == 25000 and ana["frecuencia"] == "semanal")
chequear("cuenta asistencias y faltas", ana["asistio"] == 1 and ana["falto"] == 1)
chequear("la deuda sale de lo que ya pasó", ana["sin_pagar"] == 1 and ana["adeudado"] == 20000)
chequear("el historial arranca por lo último", [h["id"] for h in ana["historial"]] == ["vieja", "debe"])
fecha_hoy = ahora.astimezone(app.ZoneInfo(TZ)).date()
chequear("edad antes y después del cumpleaños",
         app._edad(fecha_hoy.replace(year=fecha_hoy.year - 30, day=1).isoformat(), fecha_hoy.replace(day=2)) == 30
         and app._edad(fecha_hoy.replace(year=fecha_hoy.year - 30, day=2).isoformat(), fecha_hoy.replace(day=1)) == 29)
r = client.post("/agenda/paciente", data={"telefono": "5491100000002", "fecha_nacimiento": "1990-05-20"})
chequear("guarda la fecha de nacimiento", TABLAS["pacientes"][0]["fecha_nacimiento"] == "1990-05-20" and r.status_code == 302)
client.post("/agenda/paciente", data={"telefono": "5491100000002", "fecha_nacimiento": "1991-05-20"})
chequear("guardar de nuevo la pisa", [p["fecha_nacimiento"] for p in TABLAS["pacientes"]] == ["1991-05-20"])
html = client.post("/agenda/paciente", data={"telefono": "5491100000001", "fecha_nacimiento": "basura"},
                   follow_redirects=True).data.decode()
chequear("fecha inválida avisa y no guarda", "no es válida" in html and len(TABLAS["pacientes"]) == 1)
html = _panel(client)
chequear("el panel muestra las fichas", 'class="ficha"' in html and "1991-05-20" in html)
chequear("las fichas no muestran otro tenant", "De otra" not in html)

print("Dentro de la ficha")
reset()
d = app.date
chequear("una fecha de hoy cae hoy", app.proxima_vez(10, 30, d(2026, 10, 30)) == d(2026, 10, 30))
chequear("una que ya pasó cae el año que viene", app.proxima_vez(3, 1, d(2026, 10, 30)) == d(2027, 3, 1))
chequear("el 29/2 cae el 28 si no es bisiesto", app.proxima_vez(2, 29, d(2026, 10, 30)) == d(2027, 2, 28))
chequear("el 29/2 cae el 29 si es bisiesto", app.proxima_vez(2, 29, d(2027, 10, 30)) == d(2028, 2, 29))
chequear("cuenta los días del cumpleaños", app._cumpleanios("1990-11-02", d(2026, 10, 30)) == "36 años, en 3 días")
chequear("el cumpleaños de hoy", app._cumpleanios("1990-10-30", d(2026, 10, 30)) == "36 años, hoy")
tel = "5491100000002"
client.post("/agenda/paciente", data={"telefono": tel, "fecha_nacimiento": "1990-05-20", "notas": "Le cuesta diciembre"})
client.post("/agenda/paciente", data={"telefono": tel, "fecha_nacimiento": "1991-05-20"})
chequear("guardar sin notas no las borra", TABLAS["pacientes"][0]["notas"] == "Le cuesta diciembre")
client.post("/agenda/paciente/persona", data={"telefono": tel, "nombre": "Toby", "vinculo": "perro"})
client.post("/agenda/paciente/fecha", data={"telefono": tel, "titulo": "Navidad", "dia": "25", "mes": "12", "animo": "mal"})
client.post("/agenda/paciente/fecha", data={"telefono": tel, "titulo": "Nació Toby", "dia": "1", "mes": "1", "anio": "2020", "animo": "raro"})
chequear("guarda persona y fecha", TABLAS["paciente_personas"][0]["nombre"] == "Toby" and len(TABLAS["paciente_fechas"]) == 2)
chequear("un ánimo inválido queda sin indicar", TABLAS["paciente_fechas"][1]["animo"] is None)
html = client.post("/agenda/paciente/fecha", data={"telefono": tel, "titulo": "Mal", "dia": "31", "mes": "4"},
                   follow_redirects=True).data.decode()
chequear("el 31/4 se rechaza", "no es válida" in html and len(TABLAS["paciente_fechas"]) == 2)
client.post("/agenda/paciente/fecha", data={"telefono": tel, "titulo": "Bisiesto", "dia": "29", "mes": "2", "anio": "2023"})
chequear("el 29/2 sin importar el año", any(f["titulo"] == "Bisiesto" for f in TABLAS["paciente_fechas"]))
TABLAS["paciente_fechas"].append({"id": "ajena", "tenant_id": "t2", "paciente_telefono": tel, "titulo": "Secreto ajeno",
                                  "dia": 1, "mes": 1, "anio": None, "animo": None})
html = _panel(client)
chequear("el popup trae notas y fechas", "<dialog" in html and "Le cuesta diciembre" in html and "Navidad" in html and "Toby" in html)
chequear("no muestra fechas de otro tenant", "Secreto ajeno" not in html)
html = client.get("/agenda?semana=" + ahora.strftime("%Y-%m-%d")).data.decode()
chequear("desde la semana, la ficha vuelve a la semana", 'name="telefono"' in html
         and html[html.index("<dialog"):].count('name="semana"') > 0)
client.post("/agenda/paciente/fecha/ajena/borrar")
chequear("borrar lo de otro tenant no hace nada", any(f["id"] == "ajena" for f in TABLAS["paciente_fechas"]))
primera = TABLAS["paciente_fechas"][0]["id"]
client.post(f"/agenda/paciente/fecha/{primera}/borrar")
chequear("borra la fecha propia", not any(f["id"] == primera for f in TABLAS["paciente_fechas"]))
client.post(f"/agenda/paciente/persona/{TABLAS['paciente_personas'][0]['id']}/borrar")
chequear("borra la persona propia", TABLAS["paciente_personas"] == [])

print("Hoy")
reset()
zona = app.ZoneInfo(TZ)
local = ahora.astimezone(zona).replace(hour=12, minute=0)
TABLAS["turnos"].append(turno("de-hoy", "t1", local.astimezone(timezone.utc), estado="reservado",
                              paciente_nombre="Hoya", paciente_telefono="5491100000005"))
TABLAS["turnos"].append(turno("libre-hoy", "t1", (local + timedelta(hours=2)).astimezone(timezone.utc)))
TABLAS["pacientes"].append({"tenant_id": "t1", "paciente_telefono": "5491100000005", "fecha_nacimiento": None,
                            "notas": "Retomar lo de la mudanza"})
hoy_local = ahora.astimezone(zona).date()
cerca, lejos = hoy_local + timedelta(days=3), hoy_local + timedelta(days=40)
TABLAS["paciente_fechas"] += [
    {"id": "c", "tenant_id": "t1", "paciente_telefono": "5491100000005", "titulo": "Fecha cerca", "dia": cerca.day,
     "mes": cerca.month, "anio": None, "animo": "mal"},
    {"id": "l", "tenant_id": "t1", "paciente_telefono": "5491100000005", "titulo": "Fecha lejos", "dia": lejos.day,
     "mes": lejos.month, "anio": None, "animo": None}]
fichas = app._fichas(TENANT, zona, ahora.astimezone(zona))
filas = app._hoy(TENANT, fichas, zona, ahora.astimezone(zona))
fila_hoy = next(f for f in filas if f["ficha"]["nombre"] == "Hoya")
chequear("no cuenta horarios libres", all(f["ficha"] for f in filas) and sum(f["ficha"]["nombre"] == "Hoya" for f in filas) == 1)
chequear("sólo las fechas cercanas", [c["titulo"] for c in fila_hoy["fechas"]] == ["Fecha cerca"])
html = _panel(client)
chequear("el panel abre la ficha desde Hoy", "Retomar lo de la mudanza" in html and f'data-ficha="{fila_hoy["ficha"]["dom"]}"' in html
         and f'id="{fila_hoy["ficha"]["dom"]}"' in html)

print("Resumen al profesional")
reset()
enviados.clear()
real_now = ahora.astimezone(zona)
hoy_local = real_now.date()
fecha_cerca = hoy_local + timedelta(days=2)
TABLAS["paciente_fechas"].append({"id": "x", "tenant_id": "t1", "paciente_telefono": "5491100000002", "titulo": "Secreta",
                                  "dia": fecha_cerca.day, "mes": fecha_cerca.month, "anio": None, "animo": "mal"})
client.post("/agenda/avisos", data={"telefono": "54 9 11 7777-0000"})
chequear("guarda el número limpio", TABLAS["tenants"][0]["aviso_telefono"] == "5491177770000")


class _Reloj(app.datetime):
    hora = 9

    @classmethod
    def now(cls, tz=None):
        return real_now.replace(hour=cls.hora)


scheduler.datetime = _Reloj
t = TABLAS["tenants"][0]
_Reloj.hora = 6
scheduler._resumen_diario(t, zona)
chequear("temprano no manda nada", enviados == [])
_Reloj.hora = 9
scheduler._resumen_diario(t, zona)
chequear("manda el resumen al profesional", len(enviados) == 1 and enviados[0][1] == "5491177770000", enviados)
chequear("no dice nada del paciente", "Secreta" not in enviados[0][2] and "1 fecha importante" in enviados[0][2], enviados)
scheduler._resumen_diario(t, zona)
chequear("una sola vez por día", len(enviados) == 1)
html = _panel(client)
chequear("el detalle está en el panel", "Fechas de esta semana" in html and "Secreta" in html)
TABLAS["tenants"][0].update(ultimo_aviso=None)
TABLAS["paciente_fechas"].clear()
scheduler._resumen_diario(t, zona)
chequear("sin nada que contar no manda", len(enviados) == 1)
TABLAS["tenants"][0].update(ultimo_aviso=None)
TABLAS["turnos"].append(turno("serie", "t1", manana, estado="reservado", paciente_nombre="Serie Nombre",
                              paciente_telefono="5491100000008", frecuencia="semanal"))
scheduler._resumen_diario(t, zona)
chequear("cuenta los que hay que renovar", "1 paciente por renovar" in enviados[-1][2] and "Serie Nombre" not in enviados[-1][2], enviados)
import asistente_profesional as _ap
html = _panel(client)
_cod = _ap.codigo_vinculo(TABLAS["tenants"][0])
chequear("panel: sin vincular muestra el código para mandar por WhatsApp", _cod and f"VINCULAR {_cod}" in html and "✓ Vinculado" not in html)
TABLAS["tenants"][0]["aviso_verificado"] = "5491177770000"
html = _panel(client)
chequear("panel: vinculado ya no muestra el código", "✓ Vinculado" in html and f"VINCULAR {_cod}" not in html)
client.post("/agenda/avisos", data={"telefono": "54 9 11 6666-0000"})
html = _panel(client)
chequear("panel: si cambia el número hay que vincular de nuevo", "✓ Vinculado" not in html
         and f"VINCULAR {_ap.codigo_vinculo(TABLAS['tenants'][0])}" in html and _ap.codigo_vinculo(TABLAS["tenants"][0]) != _cod)
_plt = []
_ept, _rt, _at = scheduler.whatsapp.enviar_plantilla, scheduler.config.RESUMEN_TEMPLATE, scheduler.config.AVISO_TEMPLATE
scheduler.whatsapp.enviar_plantilla = lambda n, to, tpl, lang, params: _plt.append((to, tpl, params)) or True
scheduler.config.AVISO_TEMPLATE, scheduler.config.RESUMEN_TEMPLATE = "aviso_profesional", "resumen_manana"
TABLAS["tenants"][0].update(ultimo_aviso=None)
scheduler._resumen_diario(t, zona)
chequear("resumen de la mañana: con RESUMEN_TEMPLATE sale la plantilla de botones", _plt and _plt[-1][1] == "resumen_manana", _plt)
scheduler.config.RESUMEN_TEMPLATE = ""
TABLAS["tenants"][0].update(ultimo_aviso=None)
scheduler._resumen_diario(t, zona)
chequear("resumen de la mañana: sin ella, la de avisos de siempre", len(_plt) == 2 and _plt[-1][1] == "aviso_profesional", _plt)
scheduler.whatsapp.enviar_plantilla, scheduler.config.RESUMEN_TEMPLATE, scheduler.config.AVISO_TEMPLATE = _ept, _rt, _at
client.post("/agenda/avisos", data={"telefono": ""})
chequear("se puede apagar", TABLAS["tenants"][0]["aviso_telefono"] is None)
scheduler.datetime = app.datetime

print("Renovar pacientes fijos")
reset()
semana = timedelta(days=7)
for i in range(-3, 2):
    TABLAS["turnos"].append(turno(f"fijo{i}", "t1", manana + semana * i, estado="reservado", paciente_nombre="Fija",
                                  paciente_telefono="5491100000006", frecuencia="semanal", valor=30000))
fijas = [f for f in TABLAS["turnos"] if f["paciente_nombre"] == "Fija"]
chequear("avisa cuando quedan 2", app.sesiones_que_quedan(fijas, ahora) == 2)
chequear("con varias todavía no", app.sesiones_que_quedan(fijas + [turno("m", "t1", manana + semana * 5, frecuencia="semanal")], ahora) is None)
chequear("una suelta no se renueva", app.sesiones_que_quedan([turno("s", "t1", manana)], ahora) is None)
chequear("el que se fue hace rato no molesta",
         app.sesiones_que_quedan([turno("v", "t1", ahora - timedelta(days=60), frecuencia="semanal")], ahora) is None)
chequear("si se le acabó hace poco, sí", app.sesiones_que_quedan([turno("v", "t1", ayer, frecuencia="semanal")], ahora) == 0)
html = _panel(client)
chequear("el panel lo muestra", "Por renovar" in html and "le quedan 2 sesiones" in html)
html = client.post("/agenda/paciente/renovar", data={"telefono": "5491100000006"}, follow_redirects=True).data.decode()
nuevas = sorted(_norm(f["inicio"]) for f in TABLAS["turnos"] if f["paciente_nombre"] == "Fija")
chequear("renueva 8 más, cada 7 días", len(nuevas) == 13 and nuevas[-1] - nuevas[4] == semana * 8, len(nuevas))
chequear("con el mismo valor", all(f["valor"] == 30000 for f in TABLAS["turnos"] if f["paciente_nombre"] == "Fija"))
chequear("avisa cuántas", "Se renovaron 8 sesiones" in html)
reset()
TABLAS["turnos"].append(turno("cortada", "t1", ahora - timedelta(days=10), estado="reservado", paciente_nombre="Corta",
                              paciente_telefono="5491100000007", frecuencia="semanal"))
client.post("/agenda/paciente/renovar", data={"telefono": "5491100000007"})
cortas = sorted(_norm(f["inicio"]) for f in TABLAS["turnos"] if f["paciente_nombre"] == "Corta")
chequear("si estaba cortada retoma en el futuro", cortas[1] > ahora and cortas[1] - ahora <= semana)
html = client.post("/agenda/paciente/renovar", data={"telefono": "5491100000001"}, follow_redirects=True).data.decode()
chequear("sin frecuencia avisa", "no tiene una frecuencia fija" in html)

print("Cobros")
reset()
enviados.clear()
mes = ahora.astimezone(zona).date().replace(day=15)
def del_mes(hora):
    return app.datetime.combine(mes, app.dtime(hora), tzinfo=zona).astimezone(timezone.utc)
for f in list(TABLAS["turnos"]):
    if f["id"] != "ajeno":
        TABLAS["turnos"].remove(f)
TABLAS["turnos"] += [
    turno("c1", "t1", del_mes(9), estado="reservado", paciente_nombre="Ana", paciente_telefono="1", valor=1000, pago="pagado"),
    turno("c2", "t1", del_mes(10), estado="reservado", paciente_nombre="Ana", paciente_telefono="1", valor=2000),
    turno("c3", "t1", del_mes(11), estado="reservado", paciente_nombre="Beto", paciente_telefono="2", valor=4000, pago="no_cobra"),
    turno("c4", "t1", del_mes(12), estado="reservado", paciente_nombre="Beto", paciente_telefono="2"),
]
reloj = app.datetime.combine(mes, app.dtime(11, 30), tzinfo=zona)
fichas = app._fichas(TENANT, zona, reloj)
c = app._cobros(fichas, mes.replace(day=1), zona, reloj)
chequear("separa cobrado y a cobrar", c["cobrado"] == 1000 and c["a_cobrar"] == 2000)
chequear("lo que no se cobra no cuenta", c["agendado"] == 0)
chequear("marca las sesiones sin valor", c["sin_valor"] == 1)
chequear("lista quién debe", [f["nombre"] for f in c["deudores"]] == ["Ana"])
otro = app._cobros(fichas, (mes.replace(day=1) + timedelta(days=40)).replace(day=1), zona, reloj)
chequear("otro mes no suma este", otro["cobrado"] == 0 and otro["a_cobrar"] == 0)
for f in TABLAS["turnos"]:
    if f["id"] in ("c2",):
        f.update(inicio=ayer.isoformat(), fin=(ayer + timedelta(minutes=50)).isoformat())
html = _panel(client)
chequear("el panel muestra los cobros", "Resumen de" in html and "Quién debe" in html)
html = client.post("/agenda/paciente/recordar-pago", data={"telefono": "1"}, follow_redirects=True).data.decode()
chequear("recuerda el pago con el monto de la base", len(enviados) == 1 and enviados[0][1] == "1" and "$ 2.000" in enviados[0][2], enviados)
html = client.post("/agenda/paciente/recordar-pago", data={"telefono": "1"}, follow_redirects=True).data.decode()
chequear("no lo manda dos veces el mismo día", len(enviados) == 1 and "ya se le recordó" in html)
html = client.post("/agenda/paciente/recordar-pago", data={"telefono": "ajeno"}, follow_redirects=True).data.decode()
chequear("sin deuda no manda nada", len(enviados) == 1 and "no tiene deuda" in html)

print("Para mirar")
a = lambda dias, **k: turno(f"a{dias}", "t1", ahora - timedelta(days=dias), **k)
chequear("dos faltas seguidas", app.alerta_de_seguimiento([a(14, asistencia="no_vino"), a(7, asistencia="no_vino")], ahora) == "faltó 2 veces seguidas")
chequear("una falta suelta no", app.alerta_de_seguimiento([a(14, asistencia="no_vino"), a(7, asistencia="vino")], ahora) is None)
chequear("un mes sin venir ni turno", "45 días" in (app.alerta_de_seguimiento([a(45)], ahora) or ""))
chequear("con turno agendado no molesta", app.alerta_de_seguimiento([a(45), turno("f", "t1", manana)], ahora) is None)
chequear("el que se fue hace meses no aparece", app.alerta_de_seguimiento([a(200)], ahora) is None)
reset()
TABLAS["turnos"].append(turno("lejos", "t1", ahora - timedelta(days=45), estado="reservado", paciente_nombre="Lejana",
                              paciente_telefono="5491100000020"))
chequear("el panel lo muestra", "Para mirar" in _panel(client))

print("Lista de espera")
reset()
enviados.clear()
TABLAS["tenants"][0]["slug"] = "marcos"
client.post("/agenda/espera", data={"nombre": "Eva", "telefono": "54 9 11 1234", "preferencia": "Martes"})
espera = TABLAS["lista_espera"][0]
chequear("agrega con el número limpio", espera["telefono"] == "54911" + "1234")
r = client.post("/agenda/cancelar/futuro", follow_redirects=True).data.decode()
chequear("al cancelar avisa que hay lista", "1 en lista de espera" in r)
html = client.post(f"/agenda/espera/{espera['id']}/ofrecer", follow_redirects=True).data.decode()
chequear("ofrece el próximo libre con el link", len(enviados) == 1 and "/marcos" in enviados[0][2] and enviados[0][1] == espera["telefono"], enviados)
client.post(f"/agenda/espera/{espera['id']}/ofrecer")
chequear("no lo ofrece dos veces el mismo día", len(enviados) == 1)
TABLAS["lista_espera"].append({"id": "ajena", "tenant_id": "t2", "nombre": "De la otra", "telefono": "9", "preferencia": None,
                               "ofrecido_en": None, "created_at": ahora.isoformat()})
client.post("/agenda/espera/ajena/ofrecer")
client.post("/agenda/espera/ajena/quitar")
chequear("no toca la lista de otro tenant", len(enviados) == 1 and any(p["id"] == "ajena" for p in TABLAS["lista_espera"]))
chequear("no muestra la de otro tenant", "De la otra" not in _panel(client))
client.post(f"/agenda/espera/{espera['id']}/quitar")
chequear("se puede quitar", not any(p["id"] == espera["id"] for p in TABLAS["lista_espera"]))

print("Sesiones virtuales")
reset()
enviados.clear()
client.post("/agenda/avisos", data={"link_videollamada": "http://inseguro"})
chequear("rechaza un link sin https", TABLAS["tenants"][0].get("link_videollamada") is None)
client.post("/agenda/avisos", data={"link_videollamada": "https://meet.example/sala"})
chequear("guarda el link", TABLAS["tenants"][0]["link_videollamada"] == "https://meet.example/sala")
client.post("/agenda/avisos", data={"recibo_cuit": "20-12345678-0"})
chequear("CUIT: uno con el verificador mal se rechaza", not TABLAS["tenants"][0].get("recibo_cuit"))
client.post("/agenda/avisos", data={"recibo_cuit": "20 12345678 6", "recibo_domicilio": "  Av. Ficticia 123, CABA  "})
chequear("CUIT y domicilio: se guardan prolijos", TABLAS["tenants"][0].get("recibo_cuit") == "20-12345678-6"
         and TABLAS["tenants"][0].get("recibo_domicilio") == "Av. Ficticia 123, CABA")
chequear("CUIT: el panel lo muestra", 'value="20-12345678-6"' in _panel(client))
client.post("/agenda/avisos", data={"recibo_cuit": "", "recibo_domicilio": ""})
client.post("/agenda/avisos", data={"resumen_cobros_form": "1"})
chequear("resumen: se puede apagar desde el panel", TABLAS["tenants"][0].get("resumen_cobros_mail") is False)
client.post("/agenda/avisos", data={"resumen_cobros_form": "1", "resumen_cobros_mail": "1"})
chequear("resumen: y volver a prender", TABLAS["tenants"][0].get("resumen_cobros_mail") is True)
chequear("aviso de cada pago: se apaga con la misma casilla del formulario", TABLAS["tenants"][0].get("aviso_cada_pago") is False)
client.post("/agenda/avisos", data={"resumen_cobros_form": "1", "aviso_cada_pago": "1"})
chequear("aviso de cada pago: y se prende", TABLAS["tenants"][0].get("aviso_cada_pago") is True
         and TABLAS["tenants"][0].get("resumen_cobros_mail") is False)
client.post("/agenda/avisos", data={"resumen_cobros_form": "1", "resumen_cobros_mail": "1", "aviso_cada_pago": "1"})
client.post("/agenda/avisos", data={"telefono": "54 9 11 7777-0000"})
chequear("resumen: otro formulario no lo toca", TABLAS["tenants"][0].get("resumen_cobros_mail") is True)
chequear("CUIT y domicilio: se pueden borrar", not TABLAS["tenants"][0].get("recibo_cuit") and not TABLAS["tenants"][0].get("recibo_domicilio"))
client.post("/agenda/agendar/libre-manana", data={"nombre": "Vera", "telefono": "5491100000030", "modalidad": "virtual",
                                                  "frecuencia": "semanal", "cantidad": "3"})
veras = [f for f in TABLAS["turnos"] if f["paciente_nombre"] == "Vera"]
chequear("toda la serie queda virtual", len(veras) == 3 and all(f["modalidad"] == "virtual" for f in veras), veras)
turno_virtual = veras[0]
t = TABLAS["tenants"][0]
chequear("el recordatorio lleva el link", "https://meet.example/sala" in scheduler.cuando_de(turno_virtual, t, zona))
chequear("el presencial no", "meet" not in scheduler.cuando_de(fila("futuro"), t, zona))
client.post(f"/agenda/reprogramar/{turno_virtual['id']}", data={"fecha": (ahora + timedelta(days=60)).strftime("%Y-%m-%d"),
                                                                 "hora": "07:00", "avisar": "1"})
chequear("reprogramar conserva la modalidad y avisa con link", "https://meet.example/sala" in enviados[-1][2], enviados)
chequear("cancelar la limpia", db.cancelar_turno("t1", "futuro") is None and fila("futuro")["modalidad"] is None)
client.post("/agenda/modalidad/pasado", data={"modalidad": "virtual"})
chequear("se cambia desde la sesión", fila("pasado")["modalidad"] == "virtual")
chequear("modalidad inválida -> 400", client.post("/agenda/modalidad/pasado", data={"modalidad": "x"}).status_code == 400)

print("Obra social y constancia")
reset()
tel = "5491100000002"
client.post("/agenda/paciente", data={"telefono": tel, "obra_social": "OSDE", "numero_afiliado": "123"})
fila("futuro").update(valor=1500)
chequear("guarda la obra social", TABLAS["pacientes"][0]["obra_social"] == "OSDE")
mes_pago = _norm(fila("futuro")["inicio"]).astimezone(zona).strftime("%Y-%m")
r = client.post("/agenda/paciente/constancia", data={"telefono": tel, "mes": mes_pago})
html = r.data.decode()
chequear("la constancia lista lo pagado", r.status_code == 200 and "OSDE" in html and "$ 1.500" in html and "Ana" in html)
chequear("no trae notas", TABLAS["pacientes"][0].update(notas="Nota secreta") is None
         and "Nota secreta" not in client.post("/agenda/paciente/constancia", data={"telefono": tel, "mes": mes_pago}).data.decode())
html = client.post("/agenda/paciente/constancia", data={"telefono": tel, "mes": "2001-01"}).data.decode()
chequear("un mes sin pagos lo dice", "No hay sesiones pagas" in html)
chequear("otro paciente de otro tenant -> 404",
         client.post("/agenda/paciente/constancia", data={"telefono": "5491100000009", "mes": mes_pago}).status_code == 404)

print("Motivo y objetivos")
reset()
tel = "5491100000002"
client.post("/agenda/paciente", data={"telefono": tel, "fecha_nacimiento": "1990-05-20", "notas": "Nota general"})
client.post("/agenda/paciente", data={"telefono": tel, "motivo_consulta": " Ansiedad ", "objetivos": "Dormir mejor", "como_viene": "Mejor"})
p = TABLAS["pacientes"][0]
chequear("guarda motivo, objetivos y cómo viene", (p["motivo_consulta"], p["objetivos"], p["como_viene"]) == ("Ansiedad", "Dormir mejor", "Mejor"))
chequear("no pisa el nacimiento ni las notas", p["fecha_nacimiento"] == "1990-05-20" and p["notas"] == "Nota general")
html = _panel(client)
chequear("salen arriba de la ficha", html.index("Ansiedad") < html.index("Fechas importantes"))
chequear("el resto de los datos no los pierde", client.post("/agenda/paciente", data={"telefono": tel, "fecha_nacimiento": "1990-05-21"}).status_code == 302
         and p["objetivos"] == "Dormir mejor")

print("Datos de crisis")
reset()
tel = "5491100000002"
client.post("/agenda/paciente", data={"telefono": tel, "fecha_nacimiento": "1990-05-20"})
client.post("/agenda/paciente", data={"telefono": tel, "psiquiatra": "Dra. Paz", "medicacion": "Sertralina", "plan_crisis": "Llamar a la hermana"})
p = TABLAS["pacientes"][0]
chequear("guarda psiquiatra, medicación y plan", (p["psiquiatra"], p["medicacion"], p["plan_crisis"]) == ("Dra. Paz", "Sertralina", "Llamar a la hermana"))
client.post("/agenda/paciente/persona", data={"telefono": tel, "nombre": "Sol", "vinculo": "hermana", "telefono_persona": "+54 9 11 2222-3333", "emergencia": "1"})
client.post("/agenda/paciente/persona", data={"telefono": tel, "nombre": "Toby", "vinculo": "perro"})
sol, toby = TABLAS["paciente_personas"]
chequear("el contacto queda con teléfono limpio", sol["telefono"] == "5491122223333" and sol["emergencia"] is True)
chequear("una persona común no es de emergencia", toby["telefono"] is None and toby["emergencia"] is False)
html = client.get("/agenda").data.decode()
chequear("el contacto queda en la pestaña Crisis", 'href="tel:+5491122223333"' in html and html.index('data-panel="crisis"') < html.index('href="tel:+5491122223333"') < html.index('data-panel="personas"'))
fichas_html = html.count('class="ficha-dialogo"')
chequear("cada ficha trae sus cinco pestañas", fichas_html > 0 and html.count('role="tab" id=') == 5 * fichas_html and html.count('role="tabpanel" id=') == 5 * fichas_html)
chequear("sólo la primera pestaña arranca abierta", html.count('aria-selected="true" tabindex') == fichas_html and html.count('data-panel="terapia">') == fichas_html and html.count('" hidden>') >= 4 * fichas_html)
chequear("notas y motivo van juntos en Terapia", html.index('data-panel="terapia"') < html.index('name="motivo_consulta"') < html.index('name="notas"') < html.index('data-panel="sesiones"'))
client.post(f"/agenda/paciente/persona/{toby['id']}/emergencia", data={"emergencia": "1"})
chequear("se marca después", TABLAS["paciente_personas"][1]["emergencia"] is True)
client.post(f"/agenda/paciente/persona/{toby['id']}/emergencia", data={"emergencia": ""})
chequear("se desmarca", TABLAS["paciente_personas"][1]["emergencia"] is False)
TABLAS["paciente_personas"].append({"id": "ajena", "tenant_id": "t2", "paciente_telefono": tel, "nombre": "X", "emergencia": False})
client.post("/agenda/paciente/persona/ajena/emergencia", data={"emergencia": "1"})
chequear("no toca la persona de otro tenant", TABLAS["paciente_personas"][2]["emergencia"] is False)

print("Alta y pausa")
reset()
tel = "5491100000002"
fila("futuro").update(frecuencia="semanal")
hoy_arg = ahora.astimezone(zona).date()
cercana = hoy_arg + timedelta(days=2)
TABLAS["paciente_fechas"].append({"id": "f", "tenant_id": "t1", "paciente_telefono": tel, "titulo": "Fecha de Ana",
                                  "dia": cercana.day, "mes": cercana.month, "anio": None, "animo": None})
fichas = app._fichas(TENANT, zona, ahora.astimezone(zona))
chequear("un activo aparece por renovar", next(f for f in fichas if f["nombre"] == "Ana")["quedan"] == 1)
html = client.post("/agenda/paciente/cerrar", data={"telefono": tel, "estado": "alta", "motivo": "Cumplió objetivos", "cancelar_futuras": "1"},
                   follow_redirects=True).data.decode()
chequear("libera las sesiones futuras", fila("futuro")["estado"] == "libre" and "Se liberaron 1" in html)
reset()
fila("futuro").update(frecuencia="semanal")
TABLAS["paciente_fechas"].append({"id": "f", "tenant_id": "t1", "paciente_telefono": tel, "titulo": "Fecha de Ana",
                                  "dia": cercana.day, "mes": cercana.month, "anio": None, "animo": None})
client.post("/agenda/paciente/cerrar", data={"telefono": tel, "estado": "pausa", "motivo": "Viaje"})
p = TABLAS["pacientes"][0]
chequear("guarda estado, fecha y motivo", p["estado"] == "pausa" and p["estado_fecha"] == hoy_arg.isoformat() and p["estado_motivo"] == "Viaje")
chequear("sin el tilde deja las sesiones", fila("futuro")["estado"] == "reservado")
ana = next(f for f in app._fichas(TENANT, zona, ahora.astimezone(zona)) if f["nombre"] == "Ana")
chequear("deja de pedir renovar", ana["quedan"] is None and ana["alerta"] is None)
html = _panel(client)
chequear("la ficha sigue, aparte", "Alta o pausa (1)" in html and "Retomar tratamiento" in html and "Ana" in html)
chequear("no aparece en fechas de la semana", "Fechas de esta semana" not in html)
chequear("el resumen no la cuenta", scheduler._partes_del_resumen(TENANT, hoy_arg) == [], scheduler._partes_del_resumen(TENANT, hoy_arg))
chequear("estado inválido -> 400", client.post("/agenda/paciente/cerrar", data={"telefono": tel, "estado": "x"}).status_code == 400)
client.post("/agenda/paciente/retomar", data={"telefono": tel})
chequear("se retoma", p["estado"] == "activo" and p["estado_motivo"] is None)
chequear("vuelve al resumen", len(scheduler._partes_del_resumen(TENANT, hoy_arg)) == 2)
TABLAS["turnos"].append(turno("ajena2", "t2", manana + timedelta(hours=5), estado="reservado", paciente_telefono=tel, paciente_nombre="Ana otra"))
db.cancelar_sesiones_futuras("t1", tel, ahora)
chequear("no libera sesiones de otro tenant con el mismo teléfono", fila("ajena2")["estado"] == "reservado")
reset()
fila("pasado").update(valor=1000)
client.post("/agenda/paciente/cerrar", data={"telefono": "5491100000001", "estado": "alta"})
html = _panel(client)
chequear("la deuda sigue a la vista", "Quién debe" in html)

print("Resumen del mes")
reset()
TABLAS["turnos"] = [t for t in TABLAS["turnos"] if t["tenant_id"] == "t2"]
mes = ahora.astimezone(zona).date().replace(day=15)
def en_mes(dia_mes, hora):
    return app.datetime.combine(dia_mes, app.dtime(hora), tzinfo=zona).astimezone(timezone.utc)
antes_mes = (mes.replace(day=1) - timedelta(days=1)).replace(day=15)
TABLAS["turnos"] += [
    turno("r0", "t1", en_mes(antes_mes, 9), estado="reservado", paciente_nombre="Ana", paciente_telefono="1", valor=500, pago="pagado", asistencia="vino"),
    turno("r1", "t1", en_mes(mes, 9), estado="reservado", paciente_nombre="Ana", paciente_telefono="1", valor=1000, pago="pagado", asistencia="vino"),
    turno("r2", "t1", en_mes(mes, 10), estado="reservado", paciente_nombre="Ana", paciente_telefono="1", valor=1000, asistencia="no_vino", pago="no_cobra"),
    turno("r3", "t1", en_mes(mes, 11), estado="reservado", paciente_nombre="Beto", paciente_telefono="2", valor=2000),
    turno("r4", "t1", en_mes(mes, 20), estado="reservado", paciente_nombre="Cata", paciente_telefono="3", valor=3000),
]
reloj = app.datetime.combine(mes, app.dtime(12), tzinfo=zona)
c = app._cobros(app._fichas(TENANT, zona, reloj), mes.replace(day=1), zona, reloj)
chequear("cuenta sesiones dadas y ausencias", c["sesiones"] == 2 and c["ausencias"] == 1, c)
chequear("una sesión futura no cuenta como dada", c["agendado"] == 3000)
chequear("cuenta pacientes una vez", c["pacientes"] == 2, c["pacientes"])
chequear("nuevos son los que arrancaron este mes", c["nuevos"] == 1, c["nuevos"])
chequear("compara con el mes anterior", c["cobrado_antes"] == 500)
chequear("la deuda total es histórica", c["deuda_total"] == 2000, c["deuda_total"])

print("Ausencias y cancelaciones")
reset()
tel = "5491100000002"
client.post("/agenda/cancelar/futuro")
chequear("cancelar queda registrado", [(c["paciente_telefono"], c["origen"]) for c in TABLAS["cancelaciones"]] == [(tel, "profesional")])
client.post("/agenda/cancelar/futuro")
chequear("cancelar un horario ya libre no suma", len(TABLAS["cancelaciones"]) == 1)
client.post("/agenda/cancelar/ajeno")
chequear("un turno de otro tenant no registra", len(TABLAS["cancelaciones"]) == 1 and fila("ajeno")["estado"] == "reservado")
reset()
db.reprogramar_turno("t1", "futuro", manana + timedelta(hours=2), 50)
chequear("reprogramar no es cancelación", TABLAS["cancelaciones"] == [])
db.cancelar_sesiones_futuras("t1", tel, ahora)
chequear("cerrar un tratamiento tampoco", TABLAS["cancelaciones"] == [])
reset()
db.cancelar_turno("t1", "futuro", "paciente")
TABLAS["turnos"].append(turno("f2", "t1", manana + timedelta(days=9), estado="reservado", paciente_nombre="Ana", paciente_telefono=tel))
for i in (40, 80):
    TABLAS["turnos"].append(turno(f"falta{i}", "t1", ahora - timedelta(days=i), estado="reservado", paciente_nombre="Ana",
                                  paciente_telefono=tel, asistencia="no_vino" ))
    TABLAS["turnos"].append(turno(f"vino{i}", "t1", ahora - timedelta(days=i - 1), estado="reservado", paciente_nombre="Ana",
                                  paciente_telefono=tel, asistencia="vino"))
TABLAS["turnos"].append(turno("vieja-falta", "t1", ahora - timedelta(days=200), estado="reservado", paciente_nombre="Ana",
                              paciente_telefono=tel, asistencia="no_vino"))
TABLAS["cancelaciones"].append({"id": "vieja", "tenant_id": "t1", "paciente_telefono": tel, "inicio": ahora.isoformat(),
                                "origen": "paciente", "created_at": (ahora - timedelta(days=120)).isoformat()})
TABLAS["cancelaciones"].append({"id": "otra", "tenant_id": "t2", "paciente_telefono": tel, "inicio": ahora.isoformat(),
                                "origen": "paciente", "created_at": ahora.isoformat()})
ana = next(f for f in app._fichas(TENANT, zona, ahora.astimezone(zona)) if f["nombre"] == "Ana")
chequear("cuenta sólo los últimos 90 días y este tenant", (ana["faltas_recientes"], ana["canceladas_recientes"]) == (2, 1),
         (ana["faltas_recientes"], ana["canceladas_recientes"]))
chequear("con 3 entre faltas y cancelaciones avisa", ana["alerta"] == "3 faltas o cancelaciones en 90 días", ana["alerta"])
chequear("el panel lo muestra", "Últimos 90 días" in _panel(client))

print("Exportar a Excel")
reset()
TABLAS["pacientes"].append({"tenant_id": "t1", "paciente_telefono": "5491100000001", "obra_social": "OSDE", "notas": "Nota privada"})
fila("pasado").update(valor=1000, paciente_nombre="=HYPERLINK(1)", nota="Nota de sesión", pago="pagado", asistencia="vino")
import io
from openpyxl import load_workbook
mes_x = _norm(fila("pasado")["inicio"]).astimezone(zona).strftime("%Y-%m")
r = client.get(f"/agenda/exportar?mes={mes_x}")
chequear("baja un Excel", r.status_code == 200 and "sesiones-" in r.headers["Content-Disposition"])
libro = load_workbook(io.BytesIO(r.data))
filas_x = list(libro["Sesiones"].values)
plano = " ".join(str(c) for f in filas_x for c in f)
chequear("sin nombres por defecto", "Paciente" not in plano and "HYPERLINK" not in plano and "OSDE" not in plano, plano)
chequear("trae valor, asistencia y pago", "Asistió" in plano and "Pagó" in plano and "1000" in plano, plano)
chequear("no trae teléfonos ni notas", "54911" not in plano and "Nota" not in plano)
chequear("no trae otro tenant", len(filas_x) == 1 + sum(_norm(t["inicio"]).astimezone(zona).strftime("%Y-%m") == mes_x
         for t in TABLAS["turnos"] if t["tenant_id"] == "t1" and t["estado"] == "reservado"), filas_x)
libro = load_workbook(io.BytesIO(client.get(f"/agenda/exportar?mes={mes_x}&nombres=1").data))
celda = next(c for f in libro["Sesiones"].iter_rows() for c in f if c.value == "=HYPERLINK(1)")
chequear("con nombres los trae", any(v == "OSDE" for f in libro["Sesiones"].values for v in f))
chequear("un nombre con = no es fórmula", celda.data_type == "s", celda.data_type)
chequear("resumen con cobrado", dict(libro["Resumen"].values)["Cobrado"] >= 1000)
with client.session_transaction() as s:
    s.pop("tenant_id")
chequear("sin login no baja nada", client.get("/agenda/exportar").status_code == 302)
with client.session_transaction() as s:
    s["tenant_id"] = "t1"
    s["sesion_id"] = _sesion_de("t1")

print("Aumento de honorarios")
chequear("redondea a la centena", app._con_aumento(27180, "porcentaje", 10) == 29900 and app._con_aumento(10050, "porcentaje", 1) == 10200,
         app._con_aumento(27180, "porcentaje", 10))
reset()
t_ana, t_beto = "5491100000002", "5491100000003"
hoy_iso = ahora.astimezone(zona).date().isoformat()
fila("futuro").update(valor=10000, pago="pendiente")
fila("pasado").update(valor=10000)
TABLAS["turnos"] += [
    turno("cobrada", "t1", manana + timedelta(hours=4), estado="reservado", paciente_nombre="Ana", paciente_telefono=t_ana, valor=10000, pago="pagado"),
    turno("sin-valor", "t1", manana + timedelta(hours=6), estado="reservado", paciente_nombre="Ana", paciente_telefono=t_ana),
    turno("beto", "t1", manana + timedelta(hours=8), estado="reservado", paciente_nombre="Beto", paciente_telefono=t_beto, valor=10000),
]
fila("ajeno").update(valor=10000, paciente_telefono=t_ana)
html = client.post("/agenda/aumento", data={"modo": "porcentaje", "valor": "20", "desde": hoy_iso, "pacientes": t_ana}, follow_redirects=True).data.decode()
chequear("sube la sesión futura sin pagar", fila("futuro")["valor"] == 12000 and "1 sesiones" in html, fila("futuro")["valor"])
chequear("no toca lo pasado ni lo pagado", fila("pasado")["valor"] == 10000 and fila("cobrada")["valor"] == 10000)
chequear("no inventa valor donde no había", fila("sin-valor")["valor"] is None)
chequear("respeta a quién se eligió", fila("beto")["valor"] == 10000)
chequear("no toca otro tenant con el mismo teléfono", fila("ajeno")["valor"] == 10000)
client.post("/agenda/aumento", data={"modo": "valor", "valor": "$ 15.000", "desde": hoy_iso, "pacientes": [t_ana, t_beto]})
chequear("con valor fijo pisa también las vacías", fila("sin-valor")["valor"] == 15000 and fila("beto")["valor"] == 15000)
html = client.post("/agenda/aumento", data={"modo": "porcentaje", "valor": "10", "desde": "2001-01-01", "pacientes": t_ana}, follow_redirects=True).data.decode()
chequear("rechaza una fecha pasada", "fecha pasada" in html and fila("futuro")["valor"] == 15000)
lejos_iso = (ahora.astimezone(zona).date() + timedelta(days=30)).isoformat()
client.post("/agenda/aumento", data={"modo": "valor", "valor": "99", "desde": lejos_iso, "pacientes": t_ana})
chequear("sólo rige desde la fecha", fila("futuro")["valor"] == 15000)
html = client.post("/agenda/aumento", data={"modo": "porcentaje", "valor": "9999", "desde": hoy_iso, "pacientes": t_ana}, follow_redirects=True).data.decode()
chequear("frena un porcentaje absurdo", "no puede superar" in html)
html = client.post("/agenda/aumento", data={"modo": "valor", "valor": "1", "desde": hoy_iso}, follow_redirects=True).data.decode()
chequear("pide elegir pacientes", "al menos un paciente" in html)

print("Días bloqueados")
reset()
dia_libre = (manana + timedelta(hours=2)).astimezone(zona).date()
client.post("/agenda/bloqueo", data={"desde": dia_libre.isoformat(), "hasta": dia_libre.isoformat(), "motivo": "Congreso"})
b = TABLAS["bloqueos"][0]
chequear("cubre el día completo en hora local", _norm(b["desde"]).astimezone(zona).hour == 0
         and _norm(b["hasta"]) - _norm(b["desde"]) == timedelta(days=1))
chequear("no se ofrece el horario libre", "libre-manana" not in [t["id"] for t in db.turnos_libres("t1", ahora)])
chequear("reservarlo igual se rechaza", db.reservar_turno("t1", "libre-manana", "5491100000003", "Eva") is False)
chequear("reprogramar ahí se rechaza", db.reprogramar_turno("t1", "pasado", manana + timedelta(hours=2), 50) == "Ese día está bloqueado.")
chequear("el otro tenant no se bloquea", db._esta_bloqueado("t2", fila("libre-manana")["inicio"], fila("libre-manana")["fin"]) is False)
chequear("las sesiones ya reservadas siguen", fila("futuro")["estado"] == "reservado") if _norm(fila("futuro")["inicio"]).astimezone(zona).date() == dia_libre else None
html = _panel(client)
chequear("el panel muestra el bloqueo", "Días bloqueados" in html and "Congreso" in html)
afectados = app._bloqueos(TENANT, app._fichas(TENANT, zona, ahora.astimezone(zona)), zona, ahora.astimezone(zona))
chequear("dice a quién avisarle", [a["ficha"]["nombre"] for a in afectados[0]["_afectados"]] == (["Ana"] if _norm(fila("futuro")["inicio"]).astimezone(zona).date() == dia_libre else []), afectados[0]["_afectados"])
chequear("el mes lo marca", 'bloqueado"' in html or " bloqueado" in html)
html = client.get(f"/agenda?semana={dia_libre.isoformat()}").data.decode()
chequear("la semana lo marca cerrado a pacientes", "bloqueado: Congreso" in html and "cerrado a pacientes" in html
         and "/agenda/agendar/libre-manana" in html)
r_bloq = r = client.post("/agenda/agendar/libre-manana", data={"nombre": "Feriada", "telefono": "5491100000099"}, follow_redirects=True)
chequear("el profesional agenda igual en un día bloqueado", fila("libre-manana")["estado"] == "reservado"
         and fila("libre-manana")["paciente_nombre"] == "Feriada")
chequear("y sigue cerrado para la página", "libre-manana" not in [t["id"] for t in db.turnos_libres("t1", ahora)])
otra_hora = (manana + timedelta(hours=4)).astimezone(zona)
r = client.post("/agenda/reprogramar/pasado", data={"fecha": otra_hora.strftime("%Y-%m-%d"), "hora": otra_hora.strftime("%H:%M")},
                follow_redirects=True)
chequear("el profesional puede pasar una sesión a un día bloqueado", "bloqueado" not in r.data.decode().split("Sesión pasada")[0][-200:]
         and "Sesión pasada" in r.data.decode()) if otra_hora.date() == dia_libre else None
fila("libre-manana").update(estado="libre", paciente_nombre=None, paciente_telefono=None)
chequear("agendar en un día bloqueado avisa", "Ojo: ese día lo tenías bloqueado (Congreso)" in r_bloq.data.decode())
html = client.get(f"/agenda?semana={dia_libre.isoformat()}").data.decode()
chequear("el día bloqueado ofrece abrirlo", "Abrir este día a pacientes" in html and "/agenda/libre/libre-manana/abrir" in html)
client.post("/agenda/libre/libre-manana/abrir", data={"abrir": "1"})
chequear("abrir un horario lo ofrece en la página", "libre-manana" in [t["id"] for t in db.turnos_libres("t1", ahora)])
html = client.get(f"/agenda?semana={dia_libre.isoformat()}").data.decode()
chequear("el panel lo muestra abierto", "abierto a pacientes" in html)
client.post("/agenda/libre/libre-manana/abrir", data={"abrir": "0"})
chequear("cerrarlo lo saca de la página", "libre-manana" not in [t["id"] for t in db.turnos_libres("t1", ahora)])
r = client.post(f"/agenda/dia/{dia_libre.isoformat()}/abrir", data={"abrir": "1"}, follow_redirects=True)
chequear("abrir el día entero", "Abriste el" in r.data.decode() and fila("libre-manana")["abierto"] is True
         and b in TABLAS["bloqueos"])
chequear("abierto, el paciente puede reservarlo", db.reservar_turno("t1", "libre-manana", "5491100000003", "Eva") is True)
fila("libre-manana").update(estado="libre", paciente_nombre=None, paciente_telefono=None)
client.post(f"/agenda/dia/{dia_libre.isoformat()}/abrir", data={"abrir": "0"})
chequear("volver a cerrarlo", fila("libre-manana")["abierto"] is False)
_sesion_bloq = dict(fila("libre-manana"), estado="reservado")
_av = scheduler._bloqueos_con_sesiones(TENANT, dia_libre - timedelta(days=3), [_sesion_bloq])
chequear("resumen: avisa 3 días antes si hay sesiones", len(_av) == 1 and "lo tenés bloqueado" in _av[0] and "1 sesión" in _av[0], _av)
chequear("resumen: 2 días antes no repite", scheduler._bloqueos_con_sesiones(TENANT, dia_libre - timedelta(days=2), [_sesion_bloq]) == [])
chequear("resumen: sin sesiones no avisa", scheduler._bloqueos_con_sesiones(TENANT, dia_libre - timedelta(days=1), []) == [])
_fer = dia_libre + timedelta(days=1)
TABLAS["bloqueos"].append({"id": "fer", "tenant_id": "t1", "motivo": "Feriado: Prueba",
                           "desde": datetime.combine(_fer, datetime.min.time(), tzinfo=zona).isoformat(),
                           "hasta": datetime.combine(_fer + timedelta(days=1), datetime.min.time(), tzinfo=zona).isoformat()})
html = client.get("/agenda?vista=mes&semana=" + _fer.isoformat()).data.decode() + _panel(client)
chequear("el mes pinta distinto los feriados", " bloqueado feriado" in html)
TABLAS["bloqueos"] = [x for x in TABLAS["bloqueos"] if x["id"] != "fer"]
# Abrir con horario reducido y avisando a la lista de espera
_hl = _norm(fila("libre-manana")["inicio"]).astimezone(zona)
_tarde = (_hl + timedelta(minutes=1)).strftime("%H:%M")  # sin pasar de medianoche
r = client.post(f"/agenda/dia/{dia_libre.isoformat()}/abrir", data={"abrir": "1", "hora_desde": _tarde}, follow_redirects=True)
chequear("abrir sólo unas horas deja afuera las de antes", fila("libre-manana")["abierto"] is not True
         and "en esas horas" in r.data.decode(), r.data.decode()[-400:])
r = client.post(f"/agenda/dia/{dia_libre.isoformat()}/abrir", data={"abrir": "1", "hora_desde": "10:00", "hora_hasta": "09:00"},
                follow_redirects=True)
chequear("abrir con horas al revés se rechaza", "posterior a la inicial" in r.data.decode())
TABLAS["tenants"][0]["slug"] = "marcos"
TABLAS["lista_espera"] = [
    {"id": "e1", "tenant_id": "t1", "nombre": "Eva", "telefono": "5491100000070", "preferencia": None, "ofrecido_en": None, "created_at": ahora.isoformat()},
    {"id": "e2", "tenant_id": "t1", "nombre": "Leo", "telefono": "5491100000071", "preferencia": None, "ofrecido_en": None, "created_at": ahora.isoformat()},
    {"id": "e3", "tenant_id": "t2", "nombre": "Ajena", "telefono": "5491100000072", "preferencia": None, "ofrecido_en": None, "created_at": ahora.isoformat()}]
html = client.get(f"/agenda?semana={dia_libre.isoformat()}").data.decode()
chequear("abrir ofrece avisar a la lista de espera", "Avisarle a mi lista de espera (2)" in html)
enviados.clear()
r = client.post(f"/agenda/dia/{dia_libre.isoformat()}/abrir",
                data={"abrir": "1", "hora_desde": _hl.strftime("%H:%M"), "hora_hasta": (_hl + timedelta(minutes=50)).strftime("%H:%M"),
                      "avisar_espera": "1"}, follow_redirects=True)
# (si la sesión de prueba cruza la medianoche no entra en ninguna franja del día: el caso no aplica a esta hora)
chequear("abrir en ese rango y avisar a la lista", fila("libre-manana")["abierto"] is True and "Le avisamos a 2" in r.data.decode()
         and sorted(e[1] for e in enviados) == ["5491100000070", "5491100000071"], (r.data.decode()[-300:], enviados)) \
    if (_hl + timedelta(minutes=50)).date() == _hl.date() else None
client.post(f"/agenda/dia/{dia_libre.isoformat()}/abrir", data={"abrir": "0"})
TABLAS["lista_espera"] = []
TABLAS["tenants"][0].pop("slug", None)
# Pasar todas las sesiones del día a la semana siguiente
_p1 = datetime.combine(dia_libre, datetime.min.time(), tzinfo=zona).replace(hour=15)
_p2 = _p1.replace(hour=17)
TABLAS["turnos"] += [
    turno("pasar-1", "t1", _p1, estado="reservado", paciente_nombre="Paz", paciente_telefono="5491100000080"),
    turno("pasar-2", "t1", _p2, estado="reservado", paciente_nombre="Sol", paciente_telefono="5491100000081"),
    turno("ocupa", "t1", _p2 + timedelta(days=7), estado="reservado", paciente_nombre="Fijo", paciente_telefono="5491100000082")]
html = client.get(f"/agenda?semana={dia_libre.isoformat()}").data.decode()
chequear("el día bloqueado ofrece pasar sus sesiones", "Pasarlas a la semana siguiente" in html and f"/agenda/dia/{dia_libre.isoformat()}/pasar" in html)
enviados.clear()
r = client.post(f"/agenda/dia/{dia_libre.isoformat()}/pasar", data={"avisar": "1"}, follow_redirects=True).data.decode()
_nueva = next((t for t in TABLAS["turnos"] if t["tenant_id"] == "t1" and t["estado"] == "reservado" and t["paciente_nombre"] == "Paz"), None)
chequear("pasar: la sesión va al mismo horario una semana después",
         _nueva and _norm(_nueva["inicio"]) == _norm(_p1.isoformat()) + timedelta(days=7) and "Pasamos " in r, r[-500:])
chequear("pasar: la que choca queda donde estaba y se avisa", fila("pasar-2")["estado"] == "reservado"
         and fila("pasar-2")["paciente_nombre"] == "Sol" and "17:00" in r and "Pasalas a mano" in r)
chequear("pasar: le avisa al paciente que se movió", any(e[1] == "5491100000080" for e in enviados)
         and not any(e[1] == "5491100000081" for e in enviados), enviados)
chequear("pasar: el otro tenant no se toca", fila("ajeno")["estado"] == "reservado")
TABLAS["turnos"] = [t for t in TABLAS["turnos"] if t["id"] not in ("pasar-1", "pasar-2", "ocupa") and t.get("paciente_nombre") != "Paz"]
# Atajos desde el mes
_mes = dia_libre.strftime("%Y-%m")
html = client.get(f"/agenda?mes={_mes}").data.decode()
chequear("mes: el día bloqueado tiene menú para abrirlo o desbloquearlo",
         f'action="/agenda/dia/{dia_libre.isoformat()}/abrir"' in html and f"/agenda/bloqueo/{b['id']}/borrar" in html)
_otro = dia_libre + timedelta(days=2)
chequear("mes: los demás días se pueden bloquear", f'name="desde" value="{_otro.isoformat()}"' in html and "Bloquear este día" in html)
r = client.post("/agenda/bloqueo", data={"desde": _otro.isoformat(), "hasta": _otro.isoformat(), "mes": _mes})
chequear("mes: bloquear vuelve al mismo mes", r.status_code == 302 and f"mes={_mes}" in r.headers["Location"]
         and len(TABLAS["bloqueos"]) == 2)
TABLAS["bloqueos"] = [x for x in TABLAS["bloqueos"] if x["id"] == b["id"]]
client.post(f"/agenda/bloqueo/{b['id']}/borrar")
chequear("se puede quitar", TABLAS["bloqueos"] == [] and "libre-manana" in [t["id"] for t in db.turnos_libres("t1", ahora)])
html = client.post("/agenda/bloqueo", data={"desde": "2030-02-10", "hasta": "2030-02-01"}, follow_redirects=True).data.decode()
chequear("hasta antes que desde se rechaza", "no puede ser anterior" in html and TABLAS["bloqueos"] == [])
TABLAS["bloqueos"].append({"id": "aj", "tenant_id": "t2", "desde": ahora.isoformat(), "hasta": (ahora + timedelta(days=3)).isoformat(), "motivo": "Ajeno"})
client.post("/agenda/bloqueo/aj/borrar")
chequear("no borra el de otro tenant", len(TABLAS["bloqueos"]) == 1 and "Ajeno" not in _panel(client))

print("Planilla para obra social")
reset()
TABLAS["turnos"] = [t for t in TABLAS["turnos"] if t["tenant_id"] == "t2"]
hace = ahora.astimezone(zona).replace(day=1, hour=9, minute=0)
hace = (hace - timedelta(days=1)).replace(day=10)
mes_p = hace.strftime("%Y-%m")
TABLAS["turnos"] += [
    turno("o1", "t1", hace.astimezone(timezone.utc), estado="reservado", paciente_nombre="Ana", paciente_telefono="11", asistencia="vino", valor=777, nota="Nota secreta"),
    turno("o2", "t1", (hace + timedelta(days=7)).astimezone(timezone.utc), estado="reservado", paciente_nombre="Ana", paciente_telefono="11", asistencia="no_vino"),
    turno("o3", "t1", (hace + timedelta(days=8)).astimezone(timezone.utc), estado="reservado", paciente_nombre="Ana", paciente_telefono="11"),
    turno("o4", "t1", (hace + timedelta(days=1)).astimezone(timezone.utc), estado="reservado", paciente_nombre="Beto", paciente_telefono="22", asistencia="vino"),
    turno("o5", "t1", (hace + timedelta(days=2)).astimezone(timezone.utc), estado="reservado", paciente_nombre="Cata", paciente_telefono="33", asistencia="vino"),
]
TABLAS["pacientes"] += [{"tenant_id": "t1", "paciente_telefono": "11", "obra_social": "OSDE", "numero_afiliado": "123"},
                        {"tenant_id": "t1", "paciente_telefono": "22", "obra_social": "osde"},
                        {"tenant_id": "t1", "paciente_telefono": "33", "obra_social": "Swiss"}]
client.post("/agenda/avisos", data={"matricula": " MN 1234 "})
chequear("guarda la matrícula", TABLAS["tenants"][0]["matricula"] == "MN 1234")
html = client.get(f"/agenda/obra-social?mes={mes_p}&obra_social=OSDE").data.decode()
chequear("agrupa por paciente sin distinguir mayúsculas", "Ana" in html and "Beto" in html and "Cata" not in html)
chequear("lleva afiliado y matrícula", "123" in html and "MN 1234" in html)
chequear("sólo las asistidas", hace.strftime("%d/%m") in html and (hace + timedelta(days=7)).strftime("%d/%m") not in html)
chequear("avisa las sin marcar", "1 sesión sin marcar" in html)
chequear("sin teléfonos, montos ni notas", "777" not in html and "Nota secreta" not in html and ">11<" not in html)
html = client.get(f"/agenda/obra-social?mes=2001-01&obra_social=OSDE").data.decode()
chequear("mes vacío lo dice", "No hay sesiones asistidas" in html)
html = _panel(client)
chequear("el panel ofrece las obras sin repetir", html.count("<option>OSDE</option>") + html.count("<option>osde</option>") == 1
         and "<option>Swiss</option>" in html)

print("Nota de cada sesión")
reset()
client.post("/agenda/nota/pasado", data={"nota": "  Habló de la mudanza  "})
chequear("guarda la nota", fila("pasado")["nota"] == "Habló de la mudanza")
client.post("/agenda/nota/ajeno", data={"nota": "pisada"})
chequear("no toca la sesión de otro tenant", fila("ajeno")["nota"] is None)
client.post("/agenda/nota/libre-manana", data={"nota": "x"})
chequear("un horario libre no lleva nota", fila("libre-manana")["nota"] is None)
ficha_juan = next(f for f in app._fichas(TENANT, zona, ahora.astimezone(zona)) if f["nombre"] == "Juan")
chequear("la ficha trae la última nota", ficha_juan["ultima_nota"]["nota"] == "Habló de la mudanza")
TABLAS["turnos"].append(turno("hoy-juan", "t1", ahora.astimezone(zona).replace(hour=12).astimezone(timezone.utc), estado="reservado",
                              paciente_nombre="Juan", paciente_telefono="5491100000001"))
html = _panel(client)
chequear("se ve en el panel", "Habló de la mudanza" in html)
chequear("la constancia no la trae", fila("pasado").update(pago="pagado", valor=1) is None and "mudanza" not in client.post(
    "/agenda/paciente/constancia", data={"telefono": "5491100000001", "mes": _norm(fila("pasado")["inicio"]).astimezone(zona).strftime("%Y-%m")}).data.decode())
reset()
fila("futuro")["nota"] = "Sesión a preparar"
db.reprogramar_turno("t1", "futuro", manana + timedelta(hours=2), 50)
chequear("viaja al reprogramar y no queda en el horario viejo",
         fila("libre-manana")["nota"] == "Sesión a preparar" and fila("futuro")["nota"] is None)
db.cancelar_turno("t1", "libre-manana")
chequear("cancelar la limpia", fila("libre-manana")["nota"] is None)
client.post("/agenda/nota/pasado", data={"nota": "algo"})
client.post("/agenda/nota/pasado", data={"nota": "   "})
chequear("vaciarla la borra", fila("pasado")["nota"] is None)

print("Calendario")
reset()
html = _panel(client)
chequear("entra por el mes", 'class="cal"' in html and "Ana" in html)
chequear("el mes no muestra otro tenant", "De otra" not in html)
chequear("mes inválido cae al actual", client.get("/agenda?mes=basura").status_code == 200)
html = client.get(f"/agenda?semana={manana.astimezone(app.ZoneInfo(TZ)):%Y-%m-%d}").data.decode()
chequear("la semana tiene la cascada con Asistió primero",
         "Acciones" in html and html.index("Asistió") < html.index("Marcar sin pagar"))

with client.session_transaction() as s:
    s.clear()
    s["csrf_token"] = "tok"
r = client.post("/agenda/pago/pasado", data={"pago": "pagado"})
chequear("sin login -> redirige al login", r.status_code == 302 and "/login" in r.headers["Location"])

import rubros  # noqa: E402

chequear("psicopedagogía se reconoce sin acentos", rubros.de({"rubro": " Psicopedagogia "})["escuela"])
chequear("otro rubro usa los textos de siempre", not rubros.de({"rubro": "psicología"})["escuela"] and not rubros.de({})["escuela"])
with client.session_transaction() as s:
    s["tenant_id"] = "t1"
    s["sesion_id"] = _sesion_de("t1")
    s["csrf_token"] = "tok"
reset()
TABLAS["turnos"].append(turno("esc", "t1", manana, estado="reservado", paciente_nombre="Nina", paciente_telefono="549111"))
html = _panel(client)
chequear("psicología no ve la pestaña Escuela", ">Crisis</button>" in html and 'name="escuela"' not in html)
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
html = _panel(client)
chequear("psicopedagogía ve Escuela y Familia", ">Escuela</button>" in html and ">Familia</button>" in html and ">Crisis</button>" not in html and 'name="equipo_orientador"' in html)
client.post("/agenda/paciente", data={"telefono": "549111", "escuela": " Escuela 12 ", "grado": "3°"})
html = _panel(client)
chequear("los datos de la escuela se guardan", 'value="Escuela 12"' in html and 'value="3°"' in html)

# --- Hermanos con el mismo WhatsApp ---
reset()
TEL = "5491144445555"
for i, id_ in enumerate(("h1", "h2", "h3", "h4")):
    TABLAS["turnos"].append(turno(id_, "t1", manana + timedelta(hours=5 + i)))
chequear("primera reserva: ficha de siempre", db.reservar_turno("t1", "h1", TEL, "Nina Acosta"))
db.reservar_turno("t1", "h2", TEL, "Nina")
db.reservar_turno("t1", "h3", TEL, "Lola Acosta")
fila = {t["id"]: t for t in TABLAS["turnos"]}
chequear("mismo nombre, misma ficha", fila["h1"]["ficha"] == "" and fila["h2"]["ficha"] == "")
chequear("otro nombre, ficha aparte", fila["h3"]["ficha"] == "lola")
html = _panel(client)
chequear("salen dos fichas", html.count('class="ficha-dialogo"') == 4 and "Comparte WhatsApp con Lola Acosta" in html and "Comparte WhatsApp con Nina" in html)
clave_lola = f"{TEL}~lola"
client.post("/agenda/paciente", data={"telefono": clave_lola, "notas": "Sólo de Lola"})
client.post("/agenda/paciente", data={"telefono": TEL, "notas": "Sólo de Nina"})
datos = db.datos_de_pacientes("t1")
chequear("cada hermano guarda lo suyo", datos[clave_lola]["notas"] == "Sólo de Lola" and datos[TEL]["notas"] == "Sólo de Nina")
client.post("/agenda/paciente/persona", data={"telefono": clave_lola, "nombre": "Solo Lola"})
filas_p = {p["nombre"]: p for p in TABLAS["paciente_personas"]}
chequear("la persona queda en su ficha", filas_p["Solo Lola"]["ficha"] == "lola" and filas_p["Solo Lola"]["paciente_telefono"] == TEL)
client.post("/agenda/paciente/cerrar", data={"telefono": clave_lola, "estado": "pausa", "cancelar_futuras": "1"})
fila = {t["id"]: t for t in TABLAS["turnos"]}
chequear("cerrar a un hermano no toca al otro", fila["h3"]["estado"] == "libre" and fila["h1"]["estado"] == "reservado" and fila["h3"]["ficha"] == "")
db.reservar_turno("t1", "h3", TEL, "Lola")
db.reservar_turno("t1", "h4", TEL, "Nina")
db.cancelar_turno("t1", "h3", "paciente")
chequear("la cancelación queda en su ficha", TABLAS["cancelaciones"][-1]["ficha"] == "lola")
db.reservar_turno("t1", "h3", TEL, "Lola")
client.post("/agenda/sesion/h4/ficha", data={"destino": clave_lola})
fila = {t["id"]: t for t in TABLAS["turnos"]}
chequear("una sesión se pasa a la otra ficha", fila["h4"]["ficha"] == "lola" and fila["h4"]["paciente_nombre"] == "Lola")
client.post("/agenda/sesion/h4/ficha", data={"destino": "5491100000002"})
chequear("no se pasa a una ficha de otro teléfono", {t["id"]: t for t in TABLAS["turnos"]}["h4"]["paciente_telefono"] == TEL)
TABLAS["turnos"].append(turno("h5", "t2", manana + timedelta(hours=5), estado="reservado", paciente_telefono=TEL, paciente_nombre="De otro"))
chequear("otro profesional no cuenta", len(db.fichas_del_telefono("t1", TEL)) == 2)
import conversation  # noqa: E402

TABLAS["turnos"].append(turno("h6", "t1", manana + timedelta(days=3)))
respuesta = conversation.responder(dict(TENANT), TEL, "1")
chequear("el bot pide el nombre si hay dos", "más de un paciente" in respuesta and {t["id"]: t for t in TABLAS["turnos"]}["h6"]["estado"] == "libre")

motivo = db.reprogramar_turno("t1", "h3", manana + timedelta(hours=13), 50)
movida = [t for t in TABLAS["turnos"] if t.get("reprogramado_desde") and t["paciente_telefono"] == TEL]
chequear("reprogramar no cambia de ficha", motivo is None and len(movida) == 1 and movida[0]["ficha"] == "lola", motivo)
TABLAS["turnos"].append(turno("h7", "t1", manana + timedelta(days=4)))
html = client.post("/agenda/agendar/h7", data={"nombre": "Tomás", "telefono": TEL}, follow_redirects=True).data.decode()
chequear("el panel avisa de la ficha aparte", "ficha aparte" in html and {t["id"]: t for t in TABLAS["turnos"]}["h7"]["ficha"] == "tomas")

# --- Cancelar por WhatsApp con hermanos ---
reset()
TEL2 = "5491155556666"
TABLAS["turnos"] += [
    turno("c1", "t1", manana + timedelta(hours=1), estado="reservado", paciente_telefono=TEL2, paciente_nombre="Nina Acosta"),
    turno("c2", "t1", manana + timedelta(hours=2), estado="reservado", paciente_telefono=TEL2, paciente_nombre="Lola Acosta", ficha="lola"),
    turno("c3", "t1", manana + timedelta(days=7), estado="reservado", paciente_telefono=TEL2, paciente_nombre="Lola Acosta", ficha="lola"),
]
estado_de = lambda id_: {t["id"]: t for t in TABLAS["turnos"]}[id_]["estado"]
reservados = lambda: [t["id"] for t in TABLAS["turnos"] if t["paciente_telefono"] == TEL2 and t["estado"] == "reservado"]
r = conversation.responder(dict(TENANT), TEL2, "cancelar")
chequear("sin nombre pregunta y no cancela", "Nina Acosta" in r and "Lola Acosta" in r and "cancelar Nina" in r and len(reservados()) == 3, r)
r = conversation.responder(dict(TENANT), TEL2, "cancelar turno")
chequear("«cancelar turno» tampoco adivina", len(reservados()) == 3)
r = conversation.responder(dict(TENANT), TEL2, "cancelar Pedro")
chequear("nombre que no está: no cancela", "No encontré" in r and len(reservados()) == 3, r)
r = conversation.responder(dict(TENANT), TEL2, "Cancelar el turno de lola")
chequear("con nombre cancela sólo el suyo", "de Lola Acosta" in r and reservados() == ["c1", "c3"], r)
chequear("registra la cancelación en su ficha", TABLAS["cancelaciones"][-1]["ficha"] == "lola")
r = conversation.responder(dict(TENANT), TEL2, "cancelar")
chequear("sigue preguntando si quedan dos", len(reservados()) == 2, r)
db.cancelar_turno("t1", "c3")
r = conversation.responder(dict(TENANT), TEL2, "cancelar")
chequear("con una sola persona cancela directo", "Cancelado" in r and reservados() == [], r)
r = conversation.responder(dict(TENANT), TEL2, "cancelación?")
chequear("otra palabra no cancela ni rompe", "Cancelado" not in r)

# --- Una ficha que se quedó sin sesiones ---
reset()
TEL3 = "5491166667777"
TABLAS["turnos"] += [turno(f"v{i}", "t1", manana + timedelta(days=i + 1)) for i in range(4)]
db.reservar_turno("t1", "v0", TEL3, "Nina Acosta")
db.guardar_paciente("t1", TEL3, {"notas": "De Nina"})
db.cancelar_turno("t1", "v0")
db.reservar_turno("t1", "v1", TEL3, "Lola Acosta")
fila = {t["id"]: t for t in TABLAS["turnos"]}
chequear("el hermano no hereda la ficha vacía", fila["v1"]["ficha"] == "lola")
chequear("los datos siguen siendo de Nina", db.datos_de_pacientes("t1")[TEL3]["notas"] == "De Nina" and not db.datos_de_pacientes("t1")[f"{TEL3}~lola"].get("notas"))
html = _panel(client)
dialogos = html.split('<dialog class="ficha-dialogo"')[1:]
de_lola = next(d for d in dialogos if "<h4>Lola Acosta</h4>" in d)
de_nina = next(d for d in dialogos if "<h4>Nina Acosta</h4>" in d)
chequear("la ficha sin sesiones se ve con su nombre y sus notas", "De Nina" in de_nina and "Sin sesiones (1)" in html)
chequear("a Lola no le aparecen las notas de Nina", "De Nina" not in de_lola)
chequear("no entra en el aumento", f'name="pacientes" value="{TEL3}"' not in html)
for ruta in ("/agenda?vista=semana", "/agenda/exportar", "/agenda/obra-social"):
    chequear(f"{ruta} no se rompe", client.get(ruta).status_code in (200, 302))
db.reservar_turno("t1", "v2", TEL3, "Nina")
fila = {t["id"]: t for t in TABLAS["turnos"]}
chequear("si vuelve, recupera su ficha", fila["v2"]["ficha"] == "")
db.cancelar_turno("t1", "v2")
db.cancelar_turno("t1", "v1")
chequear("el bot pregunta aunque no tengan sesiones", "más de un paciente" in conversation.responder(dict(TENANT), TEL3, "1"))
TABLAS["pacientes"].append({"tenant_id": "t1", "paciente_telefono": "5491177778888", "ficha": "", "notas": "Sin nombre guardado"})
db.reservar_turno("t1", "v3", "5491177778888", "Bruno")
chequear("ficha vieja sin nombre tampoco se hereda", {t["id"]: t for t in TABLAS["turnos"]}["v3"]["ficha"] == "bruno")

# --- Nombre distinto al volver: se recupera la ficha vieja ---
reset()
TEL4 = "5491188889999"
TABLAS["turnos"] += [turno("m0", "t1", manana + timedelta(days=5)), turno("m1", "t1", manana + timedelta(days=6))]
db.reservar_turno("t1", "m0", TEL4, "Mariana")
db.guardar_paciente("t1", TEL4, {"notas": "Ficha vieja"})
db.cancelar_turno("t1", "m0")
db.reservar_turno("t1", "m1", TEL4, "Mari")
html = _panel(client)
chequear("la ficha vieja se ofrece como destino", f'<option value="{TEL4}">Mariana</option>' in html)
client.post("/agenda/sesion/m1/ficha", data={"destino": TEL4})
html = _panel(client)
chequear("pasar la sesión la recupera", html.count("<h4>Mariana</h4>") == 2 and "Ficha vieja" in html)
chequear("la ficha abierta por error desaparece", "<h4>Mari</h4>" not in html and "Sin sesiones (" not in html)

# --- Mismo nombre y mismo teléfono ---
chequear("«Nina» es «Nina Acosta»", db.mismo_nombre("Nina", "Nina Acosta") and db.mismo_nombre("José Pérez", "jose perez"))
chequear("«Juan Pablo» no es «Juan Ignacio»", not db.mismo_nombre("Juan Pablo", "Juan Ignacio") and not db.mismo_nombre("Juan Pérez", "Juan Gómez"))
reset()
TEL5 = "5491199990000"
TABLAS["turnos"] += [turno(f"n{i}", "t1", manana + timedelta(days=i + 1)) for i in range(9)]
reservar = lambda id_, nombre: db.reservar_turno("t1", id_, TEL5, nombre)
ficha_de = lambda id_: {t["id"]: t for t in TABLAS["turnos"]}[id_]["ficha"]
estado_n = lambda id_: {t["id"]: t for t in TABLAS["turnos"]}[id_]["estado"]
reservar("n0", "Juan Pablo Ruiz")
reservar("n1", "Juan Ignacio Ruiz")
chequear("dos Juan con otro segundo nombre se separan", ficha_de("n0") == "" and ficha_de("n1") == "juan")
chequear("el nombre completo va a su ficha", reservar("n2", "Juan Ignacio Ruiz") and ficha_de("n2") == "juan")
r = conversation.responder(dict(TENANT), TEL5, "cancelar Juan")
chequear("«cancelar Juan» con dos Juan no cancela", "Todavía no se canceló" in r and estado_n("n0") == "reservado" and estado_n("n1") == "reservado", r)
r = conversation.responder(dict(TENANT), TEL5, "1 Juan")
chequear("reservar «Juan» con dos Juan pregunta", "más de un paciente" in r, r)
r = conversation.responder(dict(TENANT), TEL5, "cancelar Juan Ignacio Ruiz")
chequear("con el nombre completo cancela el suyo", "Cancelado" in r and estado_n("n1") == "libre" and estado_n("n0") == "reservado", r)

reset()
TABLAS["turnos"] += [turno(f"n{i}", "t1", manana + timedelta(days=i + 1)) for i in range(9)]
reservar("n0", "Juan Pérez")
html = client.post("/agenda/agendar/n1", data={"nombre": "Juan Pérez", "telefono": TEL5, "otra_persona": "1", "frecuencia": "semanal", "cantidad": "2"},
                   follow_redirects=True).data.decode()
chequear("tildado, el nombre idéntico abre otra ficha", ficha_de("n1") == "juan" and html.count("<h4>Juan Pérez</h4>") == 4)
serie = [t for t in TABLAS["turnos"] if t["paciente_telefono"] == TEL5 and t["id"] != "n0"]
chequear("toda la serie queda en esa ficha", len(serie) == 2 and all(t["ficha"] == "juan" for t in serie), serie)
html = client.post("/agenda/agendar/n2", data={"nombre": "Juan Pérez", "telefono": TEL5}, follow_redirects=True).data.decode()
chequear("sin tildar, el panel avisa", "más de una ficha que coincide" in html)
r = conversation.responder(dict(TENANT), TEL5, "cancelar Juan Pérez")
chequear("idénticos: el bot no cancela y deriva", "Todavía no se canceló" in r and "directamente" in r and len([t for t in TABLAS["turnos"] if t["paciente_telefono"] == TEL5]) == 4, r)
r = conversation.responder(dict(TENANT), TEL5, "1 Juan Pérez")
chequear("idénticos: el bot no reserva", "más de un paciente" in r, r)
client.post("/agenda/paciente", data={"telefono": f"{TEL5}~juan", "notas": "Del hijo"})
chequear("cada Juan guarda lo suyo", not db.datos_de_pacientes("t1")[TEL5].get("notas") and db.datos_de_pacientes("t1")[f"{TEL5}~juan"]["notas"] == "Del hijo")
for t in TABLAS["turnos"]:
    if t["paciente_telefono"] == TEL5 and t["ficha"] == "juan":
        t["frecuencia"] = "semanal"
client.post("/agenda/paciente/renovar", data={"telefono": f"{TEL5}~juan"})
chequear("renovar se queda en la ficha", len([t for t in TABLAS["turnos"] if t["paciente_telefono"] == TEL5 and t["ficha"] == ""]) <= 2
         and len([t for t in TABLAS["turnos"] if t["ficha"] == "juan"]) > 2)

# --- Página pública con nombres idénticos ---
reset()
TEL6 = "5491100001111"
TABLAS["tenants"][0]["slug"] = "marcos"
TABLAS["turnos"] += [turno(f"p{i}", "t1", manana + timedelta(days=i + 1)) for i in range(5)]
fila_p = lambda id_: {t["id"]: t for t in TABLAS["turnos"]}[id_]
db.reservar_turno("t1", "p0", TEL6, "Juan Pérez")
db.reservar_turno("t1", "p1", TEL6, "Juan Pérez", ficha="juan")
chequear("con ficha clara no se marca", not fila_p("p0")["revisar_ficha"] and not fila_p("p1")["revisar_ficha"])
db.reservar_turno("t1", "p2", TEL6, "Juan Pérez")
chequear("con nombre ambiguo reserva y marca", fila_p("p2")["estado"] == "reservado" and fila_p("p2")["revisar_ficha"])
chequear("un nombre claro no se marca", db.reservar_turno("t1", "p3", TEL6, "Lola") and not fila_p("p3")["revisar_ficha"])
html = _panel(client)
chequear("el panel pide revisar", "Revisar de quién es: 1 sesión" in html and "Es de esta ficha" in html)
client.post("/agenda/sesion/p2/ficha/confirmar", data={})
chequear("confirmar saca la marca y no mueve", not fila_p("p2")["revisar_ficha"] and fila_p("p2")["estado"] == "reservado")
html = _panel(client)
chequear("el aviso desaparece", "Revisar de quién es" not in html)
db.cancelar_turno("t1", "p2")
db.reservar_turno("t1", "p2", TEL6, "Juan Pérez")
destino = f"{TEL6}~juan" if fila_p("p2")["ficha"] == "" else TEL6
client.post("/agenda/sesion/p2/ficha", data={"destino": destino})
chequear("pasarla también saca la marca", not fila_p("p2")["revisar_ficha"] and db.clave_de(fila_p("p2")) == destino)
db.reservar_turno("t1", "p4", TEL6, "Juan Pérez")
chequear("marcada antes de reprogramar", fila_p("p4")["revisar_ficha"])
db.reprogramar_turno("t1", "p4", manana + timedelta(days=20), 50)
movida = next(t for t in TABLAS["turnos"] if t.get("reprogramado_desde") and t["paciente_telefono"] == TEL6)
chequear("reprogramar conserva la marca", movida["revisar_ficha"] and not fila_p("p4")["revisar_ficha"])
TABLAS["turnos"].append(turno("p9", "t1", manana + timedelta(days=30)))
respuesta = client.post("/marcos/reservar/p9", data={"nombre": "Juan Pérez", "telefono": TEL6, "confirmado": "1"}).data.decode()
chequear("la página pública no muestra otros pacientes", fila_p("p9")["revisar_ficha"] and "Lola" not in respuesta)

partes = scheduler._partes_del_resumen(dict(TENANT), datetime.now(timezone.utc).date())
chequear("el resumen cuenta las sesiones a revisar", any("2 sesiones para revisar de quién son" in p for p in partes), partes)
chequear("el resumen no lleva nombres", not any("Juan" in p for p in partes))
for t_rev in TABLAS["turnos"]:
    t_rev["revisar_ficha"] = t_rev["id"] == "p9"
partes = scheduler._partes_del_resumen(dict(TENANT), datetime.now(timezone.utc).date())
chequear("una sola sale en singular", any("1 sesión para revisar de quién es" in p for p in partes), partes)
for t_rev in TABLAS["turnos"]:
    t_rev["revisar_ficha"] = False
partes = scheduler._partes_del_resumen(dict(TENANT), datetime.now(timezone.utc).date())
chequear("sin nada para revisar no se menciona", not any("revisar" in p for p in partes), partes)

# --- Reservas que llegan solas ---
reset()
TABLAS["tenants"][0]["slug"] = "marcos"
TABLAS["tenants"][0]["aviso_telefono"] = "5491100009999"
TABLAS["turnos"] += [turno(f"w{i}", "t1", manana + timedelta(days=i + 1)) for i in range(4)]
fila_w = lambda id_: {t["id"]: t for t in TABLAS["turnos"]}[id_]
envios = []
_texto, _numero = scheduler.whatsapp.enviar_texto, scheduler.whatsapp.numero_de
scheduler.whatsapp.enviar_texto = lambda n, tel, texto: envios.append((tel, texto)) or True
scheduler.whatsapp.numero_de = lambda t: "num"
client.post("/marcos/reservar/w0", data={"nombre": "Mora Díaz", "telefono": "54 9 11 2222 3333", "confirmado": "1"})
chequear("la reserva pública entra en la agenda", fila_w("w0")["estado"] == "reservado" and fila_w("w0")["paciente_telefono"] == "5491122223333")
html = _panel(client)
chequear("aparece en Reservas nuevas", "Reservas nuevas" in html and "Mora Díaz" in html[html.index("Reservas nuevas"):html.index("<h3>Hoy")])
semana_w = (manana + timedelta(days=1)).astimezone(zona).date().isoformat()
chequear("aparece en el calendario", "Mora Díaz" in client.get(f"/agenda?semana={semana_w}").data.decode())
tenant_w = db.get_tenant_by_id("t1")
scheduler._avisar_reservas(tenant_w, zona)
chequear("avisa al profesional sin nombres", len(envios) == 1 and envios[0][0] == "5491100009999" and "1 reserva nueva" in envios[0][1] and "Mora" not in envios[0][1], envios)
scheduler._avisar_reservas(tenant_w, zona)
chequear("no avisa dos veces", len(envios) == 1)
client.post("/agenda/sesion/w0/vista", data={})
chequear("Visto la saca de la tarjeta", "Reservas nuevas" not in _panel(client) and fila_w("w0")["estado"] == "reservado")
client.post("/agenda/agendar/w1", data={"nombre": "Del panel", "telefono": "5491133334444"})
chequear("lo agendado desde el panel no avisa", not fila_w("w1")["reserva_nueva"] and scheduler.db.reservas_sin_avisar("t1") == [])
client.post("/marcos/reservar/w2", data={"nombre": "Ida", "telefono": "5491144440000", "confirmado": "1"})
db.cancelar_turno("t1", "w2")
scheduler._avisar_reservas(tenant_w, zona)
chequear("cancelada antes del aviso no se avisa", len(envios) == 1 and not fila_w("w2")["reserva_nueva"])
client.post("/marcos/reservar/w2", data={"nombre": "Ida", "telefono": "5491144440000", "confirmado": "1"})
client.post("/marcos/reservar/w3", data={"nombre": "Eva", "telefono": "5491155550000", "confirmado": "1"})
scheduler._avisar_reservas(tenant_w, zona)
chequear("varias juntas van en un solo mensaje", len(envios) == 2 and "2 reservas nuevas" in envios[1][1], envios)
scheduler.whatsapp.enviar_texto, scheduler.whatsapp.numero_de = _texto, _numero

# --- Tests para considerar ---
import tests_sugeridos  # noqa: E402

fila_area = lambda filas, area: next((f for f in filas if f["area"] == area), None)
nombres = lambda fila: [x["nombre"] for x in fila["tests"]] if fila else []
s = tests_sugeridos.sugerir(["Dificultades en la LECTURA y le cuesta la comprensión"], 9)
chequear("detecta el área sin mayúsculas ni acentos", "PROLEC-R" in nombres(fila_area(s, "Lectura")) and fila_area(s, "Lectura")["palabra"] == "lectura", s)
chequear("filtra por edad", "PROLEC-SE" not in nombres(fila_area(s, "Lectura")))
s = tests_sugeridos.sugerir(["dislexia"], 14)
chequear("a los 14 cambia de batería", "PROLEC-SE" in nombres(fila_area(s, "Lectura")) and "PROLEC-R" not in nombres(fila_area(s, "Lectura")))
chequear("sin edad no filtra", {"PROLEC-R", "PROLEC-SE"} <= set(nombres(fila_area(tests_sugeridos.sugerir(["lectura"], None), "Lectura"))))
chequear("no dispara dentro de otra palabra", tests_sugeridos.sugerir(["Un paseo por la plaza, recuperó su bicicleta"], 9) == [])
chequear("sin texto no sugiere", tests_sugeridos.sugerir([None, ""], 9) == [])
s = tests_sugeridos.sugerir(["se distrae, le cuesta matemática, olvida la tarea"], 10)
chequear("varias áreas juntas", {"Atención", "Matemática", "Memoria"} <= {f["area"] for f in s}, s)
chequear("el catálogo sólo usa áreas que existen", all(set(a) <= set(tests_sugeridos.AREAS) for *_, a in tests_sugeridos.CATALOGO))
chequear("cada área tiene al menos un test", all(any(area in a for *_, a in tests_sugeridos.CATALOGO) for area in tests_sugeridos.AREAS))
chequear("rangos legibles", tests_sugeridos._rango(6, 13) == "6 a 12 años" and tests_sugeridos._rango(2.5, 7.6) == "2 y medio a 7 años" and tests_sugeridos._rango(8, None) == "desde 8 años")
reset()
TABLAS["turnos"].append(turno("t9", "t1", manana, estado="reservado", paciente_nombre="Nina", paciente_telefono="5491112121212", nota="Se distrae mucho"))
TABLAS["pacientes"].append({"tenant_id": "t1", "paciente_telefono": "5491112121212", "ficha": "", "motivo_consulta": "Lectura lenta", "fecha_nacimiento": "2017-05-14"})
chequear("psicología no ve tests", "Tests para considerar" not in _panel(client))
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
html = _panel(client)
chequear("psicopedagogía los ve, también por la nota de sesión", "Tests para considerar" in html and "PROLEC-R" in html and "CARAS-R" in html)
chequear("quedan en la pestaña Trabajo", html.index('data-panel="terapia"') < html.index("Tests para considerar") < html.index('data-panel="sesiones"'))

# --- Cómo se usa ---
reset()
html = _panel(client)
chequear("el panel enlaza la guía", 'href="/agenda/ayuda"' in html)
html = client.get("/agenda/ayuda").data.decode()
chequear("psicología ve sus pestañas", "Terapia:" in html and "Crisis:" in html and "Tests para considerar" not in html)
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
TABLAS["tenants"][0]["slug"] = "marcos"
html = client.get("/agenda/ayuda").data.decode()
chequear("psicopedagogía ve las suyas", "Trabajo:" in html and "Escuela:" in html and "Tests para considerar" in html and "/marcos" in html)
for texto in ("Generar horarios", "Reservas nuevas", "Es de esta ficha"):
    chequear(f"la guía nombra lo que existe: {texto}", texto in _panel(client) + open("templates/agenda.html", encoding="utf8").read())
with client.session_transaction() as s:
    s.pop("tenant_id")
chequear("la guía pide login", client.get("/agenda/ayuda").status_code == 302)
with client.session_transaction() as s:
    s["tenant_id"] = "t1"
    s["sesion_id"] = _sesion_de("t1")

# --- Tests ya tomados ---
from datetime import date as _date  # noqa: E402

hoy_t = _date(2026, 11, 2)
s = tests_sugeridos.sugerir(["lectura"], 9, [{"test": "prolec-r", "fecha": "2026-06-01"}], hoy_t)
chequear("tomado este año no se sugiere", "PROLEC-R" not in nombres(fila_area(s, "Lectura")) and "TEDE" in nombres(fila_area(s, "Lectura")))
s = tests_sugeridos.sugerir(["lectura"], 9, [{"test": "PROLEC-R", "fecha": "2025-06-01"}], hoy_t)
vuelve = next(x for x in fila_area(s, "Lectura")["tests"] if x["nombre"] == "PROLEC-R")
chequear("pasado el plazo vuelve con la fecha", vuelve["ultima"] == _date(2025, 6, 1))
s = tests_sugeridos.sugerir(["lectura"], 9, [{"test": "PROLEC-R", "fecha": "2025-06-01"}, {"test": "PROLEC-R", "fecha": "2026-10-01"}], hoy_t)
chequear("cuenta la última vez", "PROLEC-R" not in nombres(fila_area(s, "Lectura")))
reset()
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
TEL7 = "5491112121212"
TABLAS["turnos"] += [turno("k1", "t1", manana, estado="reservado", paciente_nombre="Nina", paciente_telefono=TEL7),
                     turno("k2", "t1", manana + timedelta(hours=2), estado="reservado", paciente_nombre="Lola", paciente_telefono=TEL7, ficha="lola")]
TABLAS["pacientes"] += [{"tenant_id": "t1", "paciente_telefono": TEL7, "ficha": "", "motivo_consulta": "lectura", "fecha_nacimiento": "2017-05-14"},
                        {"tenant_id": "t1", "paciente_telefono": TEL7, "ficha": "lola", "motivo_consulta": "lectura", "fecha_nacimiento": "2017-05-14"}]
ayer_iso = (datetime.now(zona) - timedelta(days=1)).date().isoformat()
client.post("/agenda/paciente/test", data={"telefono": TEL7, "test": " PROLEC-R ", "fecha": ayer_iso})
chequear("se anota en su ficha", len(TABLAS["paciente_tests"]) == 1 and TABLAS["paciente_tests"][0]["test"] == "PROLEC-R" and TABLAS["paciente_tests"][0]["ficha"] == "")
html = _panel(client)
de_nina = next(d for d in html.split('<dialog class="ficha-dialogo"')[1:] if "<h4>Nina</h4>" in d)
de_lola = next(d for d in html.split('<dialog class="ficha-dialogo"')[1:] if "<h4>Lola</h4>" in d)
sugeridos_nina = de_nina[de_nina.index("Tests para considerar"):de_nina.index("<h5>Tests tomados")]
chequear("deja de sugerirse a Nina y queda en tomados", "PROLEC-R" not in sugeridos_nina and "PROLEC-R" in de_nina[de_nina.index("<h5>Tests tomados"):])
chequear("a la hermana se le sigue sugiriendo", "PROLEC-R" in de_lola[de_lola.index("Tests para considerar"):de_lola.index("<h5>Tests tomados")])
manana_iso = (datetime.now(zona) + timedelta(days=1)).date().isoformat()
html = client.post("/agenda/paciente/test", data={"telefono": TEL7, "test": "TEDE", "fecha": manana_iso}, follow_redirects=True).data.decode()
chequear("no acepta fechas futuras", "no puede ser futura" in html and len(TABLAS["paciente_tests"]) == 1)
client.post("/agenda/paciente/test", data={"telefono": TEL7, "test": "", "fecha": ayer_iso})
chequear("pide el nombre del test", len(TABLAS["paciente_tests"]) == 1)
TABLAS["paciente_tests"].append({"id": "ajeno", "tenant_id": "t2", "paciente_telefono": TEL7, "ficha": "", "test": "D2", "fecha": ayer_iso})
client.post("/agenda/paciente/test/ajeno/borrar", data={})
chequear("no borra tests de otro profesional", any(x["id"] == "ajeno" for x in TABLAS["paciente_tests"]))
client.post(f"/agenda/paciente/test/{TABLAS['paciente_tests'][0]['id']}/borrar", data={})
html = _panel(client)
de_nina = next(d for d in html.split('<dialog class="ficha-dialogo"')[1:] if "<h4>Nina</h4>" in d)
chequear("borrar lo vuelve a sugerir", len(TABLAS["paciente_tests"]) == 1
         and "PROLEC-R" in de_nina[de_nina.index("Tests para considerar"):de_nina.index("<h5>Tests tomados")])

# --- Plazo para repetir cada test ---
nombres_en = lambda s, area: nombres(fila_area(s, area))
tomado_hace = lambda test, fecha: [{"test": test, "fecha": fecha}]
s = tests_sugeridos.sugerir(["lectura"], 9, tomado_hace("PROLEC-R", "2026-04-02"), hoy_t)
chequear("PROLEC-R vuelve a los 6 meses", "PROLEC-R" in nombres_en(s, "Lectura"))
s = tests_sugeridos.sugerir(["lectura"], 9, tomado_hace("PROLEC-R", "2026-05-03"), hoy_t)
chequear("PROLEC-R no vuelve antes", "PROLEC-R" not in nombres_en(s, "Lectura"))
s = tests_sugeridos.sugerir(["intelig"], 9, tomado_hace("WISC-V", "2025-06-01"), hoy_t)
chequear("WISC-V sigue fuera al año y medio", "WISC-V" not in nombres_en(s, "Capacidad intelectual") and "WISC-IV" in nombres_en(s, "Capacidad intelectual"))
s = tests_sugeridos.sugerir(["se distrae"], 9, tomado_hace("D2", "2025-11-02"), hoy_t)
chequear("D2 vuelve al año justo", "D2" in nombres_en(s, "Atención"))
s = tests_sugeridos.sugerir(["intelig"], 9, tomado_hace("WISC-V", "2025-06-01"), hoy_t, {"WISC-V": 12})
chequear("el plazo propio pisa el de fábrica", "WISC-V" in nombres_en(s, "Capacidad intelectual"))
chequear("fin de mes sin fechas inválidas", tests_sugeridos._se_puede_repetir(_date(2025, 8, 31), 6, _date(2026, 2, 28))
         and not tests_sugeridos._se_puede_repetir(_date(2025, 8, 31), 6, _date(2026, 2, 27)))
chequear("todos los tests tienen plazo", all(isinstance(m, int) and 0 < m <= tests_sugeridos.MAX_MESES for _, _, _, m, _ in tests_sugeridos.CATALOGO))
reset()
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
html = _panel(client)
chequear("Ajustes muestra los plazos", "Plazo para repetir cada test" in html and 'name="meses-WISC-V"' in html and 'placeholder="24"' in html)
client.post("/agenda/plazos-tests", data={"meses-WISC-V": "12", "meses-PROLEC-R": "6", "meses-D2": ""})
chequear("guarda sólo lo que cambió", TABLAS["tenants"][0]["plazos_tests"] == {"WISC-V": 12})
html = client.post("/agenda/plazos-tests", data={"meses-D2": "99"}, follow_redirects=True).data.decode()
chequear("rechaza un plazo absurdo", "entre 0 y 60" in html and TABLAS["tenants"][0]["plazos_tests"] == {"WISC-V": 12})
html = client.post("/agenda/plazos-tests", data={"meses-D2": "doce"}, follow_redirects=True).data.decode()
chequear("rechaza texto", "número de meses" in html and TABLAS["tenants"][0]["plazos_tests"] == {"WISC-V": 12})
html = _panel(client)
chequear("muestra el propio y el de fábrica", 'value="12"' in html and "(de fábrica: 24)" in html)
chequear("otro profesional no se entera", not TABLAS["tenants"][1].get("plazos_tests"))
TABLAS["tenants"][0]["rubro"] = "psicología"
chequear("psicología no ve plazos", "Plazo para repetir cada test" not in _panel(client))

# --- Resultados de tests y evolución ---
import resultados_tests  # noqa: E402

leer = resultados_tests.leer_formulario
filas, error = leer({"escala-0": " CI Total ", "tipo-0": "compuesto", "puntaje-0": "98", "nivel-0": "Promedio",
                     "escala-1": "", "puntaje-1": "", "escala-2": "Precisión", "tipo-2": "percentil", "puntaje-2": "12,5"})
chequear("lee las filas e ignora las vacías", error is None and filas == [
    {"escala": "CI Total", "tipo": "compuesto", "puntaje": 98, "nivel": "Promedio"},
    {"escala": "Precisión", "tipo": "percentil", "puntaje": 12.5, "nivel": ""}])
chequear("pide escala", leer({"puntaje-0": "98"})[1] == "Cada resultado necesita el nombre de la escala.")
chequear("rechaza texto como puntaje", "tiene que ser un número" in leer({"escala-0": "CI", "puntaje-0": "noventa"})[1])
chequear("rechaza un CI imposible", "va de 40 a 160" in leer({"escala-0": "CI", "tipo-0": "compuesto", "puntaje-0": "300"})[1])
chequear("rechaza percentil 120", "va de 0 a 100" in leer({"escala-0": "CL", "tipo-0": "percentil", "puntaje-0": "120"})[1])
chequear("rechaza tipo inventado", "tipo de puntaje" in leer({"escala-0": "CI", "tipo-0": "z", "puntaje-0": "1"})[1])
chequear("acepta sólo nivel", leer({"escala-0": "Nombre de letras", "nivel-0": "Normal"})[0][0]["puntaje"] is None)
chequear("pide puntaje o nivel", "le falta el puntaje o el nivel" in leer({"escala-0": "CI"})[1])
chequear("no repite escalas", leer({"escala-0": "CI", "puntaje-0": "90", "escala-1": "ci", "puntaje-1": "95"})[1] == "Hay una escala repetida.")

tomas = [
    {"_fecha": "01/03/2025", "resultados": [{"escala": "CI Total", "tipo": "compuesto", "puntaje": 85, "nivel": "Medio bajo"},
                                           {"escala": "Memoria de Trabajo", "tipo": "compuesto", "puntaje": 90, "nivel": ""},
                                           {"escala": "Velocidad", "tipo": "compuesto", "puntaje": 100, "nivel": ""},
                                           {"escala": "Lectura", "tipo": "bruto", "puntaje": None, "nivel": "Dificultad"}]},
    {"_fecha": "01/03/2026", "resultados": [{"escala": "CI Total", "tipo": "compuesto", "puntaje": 96, "nivel": "Promedio"},
                                           {"escala": "Memoria de Trabajo", "tipo": "compuesto", "puntaje": 87, "nivel": ""},
                                           {"escala": "Velocidad", "tipo": "compuesto", "puntaje": 92, "nivel": ""},
                                           {"escala": "Lectura", "tipo": "bruto", "puntaje": None, "nivel": "Normal"},
                                           {"escala": "Nueva", "tipo": "compuesto", "puntaje": 100, "nivel": ""}]},
]
ev = {e["escala"]: e for e in resultados_tests.evolucion(tomas)}
chequear("sube con el número", ev["CI Total"]["sentido"] == "sube" and ev["CI Total"]["cambio"] == "subió 11")
chequear("cambio chico se mantiene", ev["Memoria de Trabajo"]["sentido"] == "igual" and ev["Memoria de Trabajo"]["cambio"] == "se mantuvo")
chequear("baja", ev["Velocidad"]["cambio"] == "bajó 8")
chequear("muestra el cambio de nivel", ev["CI Total"]["nivel_cambio"] == "Medio bajo → Promedio")
chequear("compara niveles sin puntaje", ev["Lectura"]["cambio"] == "cambió de nivel")
chequear("una sola toma no es evolución", "Nueva" not in ev)
chequear("compara sólo el mismo tipo", resultados_tests.evolucion([
    {"_fecha": "a", "resultados": [{"escala": "CL", "tipo": "percentil", "puntaje": 30}]},
    {"_fecha": "b", "resultados": [{"escala": "CL", "tipo": "t", "puntaje": 45}]}]) == [])

reset()
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
TEL8 = "5491113131313"
TABLAS["turnos"].append(turno("r1", "t1", manana, estado="reservado", paciente_nombre="Juli Ruiz", paciente_telefono=TEL8))
TABLAS["paciente_tests"] += [
    {"id": "w1", "tenant_id": "t1", "paciente_telefono": TEL8, "ficha": "", "test": "WISC-V", "fecha": "2024-10-01"},
    {"id": "w2", "tenant_id": "t1", "paciente_telefono": TEL8, "ficha": "", "test": "WISC-V", "fecha": "2026-10-01"},
    {"id": "otro", "tenant_id": "t2", "paciente_telefono": TEL8, "ficha": "", "test": "WISC-V", "fecha": "2026-10-01"}]
html = _panel(client)
chequear("ofrece cargar resultados", "Cargar resultados del 01/10/2026" in html and "sin resultados" in html)
chequear("sugiere las escalas del WISC-V", '<option value="Razonamiento Fluido">' in html)
client.post("/agenda/paciente/test/w1/resultados", data={"telefono": TEL8, "escala-0": "CI Total", "tipo-0": "compuesto", "puntaje-0": "84", "nivel-0": "Medio bajo"})
html = client.post("/agenda/paciente/test/w2/resultados", data={"telefono": TEL8, "escala-0": "CI Total", "tipo-0": "compuesto", "puntaje-0": "97", "nivel-0": "Promedio"},
                   follow_redirects=True).data.decode()
guardado = next(x for x in TABLAS["paciente_tests"] if x["id"] == "w2")
chequear("guarda los resultados", "Se guardaron los resultados" in html and guardado["resultados"] == [{"escala": "CI Total", "tipo": "compuesto", "puntaje": 97, "nivel": "Promedio"}])
chequear("muestra la evolución", "subió 13" in html and "Medio bajo → Promedio" in html and 'class="cambio-sube"' in html)
chequear("edita con lo cargado", 'Editar resultados del 01/10/2026' in html and 'name="puntaje-0" value="97"' in html)
html = client.post("/agenda/paciente/test/w2/resultados", data={"telefono": TEL8, "escala-0": "CI Total", "puntaje-0": "999"}, follow_redirects=True).data.decode()
chequear("un error no pisa lo guardado", "va de 40 a 160" in html and next(x for x in TABLAS["paciente_tests"] if x["id"] == "w2")["resultados"][0]["puntaje"] == 97)
client.post("/agenda/paciente/test/otro/resultados", data={"escala-0": "CI Total", "puntaje-0": "70"})
chequear("no toca tests de otro profesional", not next(x for x in TABLAS["paciente_tests"] if x["id"] == "otro").get("resultados"))
html = client.post("/agenda/paciente/test/w2/resultados", data={"telefono": TEL8}, follow_redirects=True).data.decode()
chequear("vaciar borra los resultados", "Se borraron los resultados" in html and next(x for x in TABLAS["paciente_tests"] if x["id"] == "w2")["resultados"] == [])

# --- Fluidez lectora ---
import fluidez  # noqa: E402

chequear("cuenta palabras sin signos sueltos", fluidez.palabras("El perro — corre, ¡rápido!  ") == ["El", "perro", "corre,", "¡rápido!"])
chequear("calcula las medidas", fluidez.calcular(60, 3, 60) == {"ppm": 60, "pcpm": 57, "precision": 95})
chequear("con tiempo parcial", fluidez.calcular(40, 0, 30)["pcpm"] == 80)
ok, error = fluidez.validar(50, "30", "3,1,3", "60.04")
chequear("valida y ordena errores", error is None and ok == {"leidas": 30, "errores": [1, 3], "segundos": 60.0})
chequear("pide la última palabra", fluidez.validar(50, "0", "", "60")[1] == "Falta marcar hasta dónde leyó.")
chequear("no pasa del texto", fluidez.validar(50, "51", "", "60")[1] == "Falta marcar hasta dónde leyó.")
chequear("errores después de lo leído", "después de la última" in fluidez.validar(50, "10", "12", "60")[1])
chequear("tiempo inválido", "tiempo" in fluidez.validar(50, "10", "", "0")[1])
chequear("basura no explota", "incompleta" in fluidez.validar(50, "diez", "", "60")[1])
fl = fluidez.para_ficha([{"palabras_leidas": 70, "errores": 2, "segundos": 60}, {"palabras_leidas": 50, "errores": 5, "segundos": 60}])
chequear("cambio contra la anterior", fl["filas"][0]["cambio"] == 23 and "cambio" not in fl["filas"][1])
chequear("gráfico de vieja a nueva", [p["valor"] for p in fl["grafico"]["puntos"]] == [45, 68])
chequear("una sola lectura sin gráfico", fluidez.para_ficha([{"palabras_leidas": 5, "errores": 0, "segundos": 10}])["grafico"] is None)

reset()
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
TEL9 = "5491114141414"
TABLAS["turnos"].append(turno("f1", "t1", manana, estado="reservado", paciente_nombre="Mora Díaz", paciente_telefono=TEL9))
html = _panel(client)
chequear("sin textos pide cargarlos", "cargá un texto en «Ajustes»" in html and "Textos para fluidez lectora (0)" in html)
client.post("/agenda/textos-lectura", data={"titulo": "La plaza", "texto": "Hoy fuimos a la plaza con mi abuela y vimos patos."})
client.post("/agenda/textos-lectura", data={"titulo": "", "texto": "algo"})
chequear("agrega textos con título", [t["titulo"] for t in TABLAS["textos_lectura"]] == ["La plaza"])
TABLAS["textos_lectura"].append({"id": "ajeno", "tenant_id": "t2", "titulo": "Ajeno", "texto": "uno dos"})
html = _panel(client)
chequear("ofrece tomar lectura con sus textos", "Tomar lectura" in html and "La plaza (11 palabras)" in html and "Ajeno" not in html)
texto_id = TABLAS["textos_lectura"][0]["id"]
pantalla = client.post("/agenda/fluidez", data={"telefono": TEL9, "texto_id": texto_id}).data.decode()
chequear("abre la pantalla de lectura", "Fluidez lectora · Mora Díaz" in pantalla and pantalla.count('class="palabra"') == 11 and TEL9 in pantalla)
html = client.post("/agenda/fluidez", data={"telefono": TEL9, "texto_id": "ajeno"}, follow_redirects=True).data.decode()
chequear("no usa textos de otro profesional", "Elegí un texto" in html)
html = client.post("/agenda/fluidez/guardar", data={"telefono": TEL9, "texto_id": texto_id, "leidas": "11", "errores": "2,8", "segundos": "15"},
                   follow_redirects=True).data.decode()
guardada = TABLAS["lecturas"][-1] if TABLAS["lecturas"] else {}
chequear("guarda la lectura", guardada.get("palabras_leidas") == 11 and guardada.get("errores") == 2
         and guardada.get("errores_palabras") == ["a", "y"] and guardada.get("paciente_telefono") == TEL9 and guardada.get("texto_titulo") == "La plaza")
chequear("avisa el resultado", "36 palabras correctas por minuto, 82% de precisión" in html)
client.post("/agenda/fluidez/guardar", data={"telefono": TEL9, "texto_id": texto_id, "leidas": "11", "errores": "", "segundos": "12"})
html = _panel(client)
chequear("muestra la evolución con gráfico", "grafico-fluidez" in html and "↑ 19" in html and "Errores: a, y" in html)
n = len(TABLAS["lecturas"])
client.post("/agenda/fluidez/guardar", data={"telefono": TEL9, "texto_id": texto_id, "leidas": "12", "errores": "", "segundos": "12"})
chequear("rechaza más palabras que el texto", len(TABLAS["lecturas"]) == n)
TABLAS["lecturas"].append({"id": "ajena", "tenant_id": "t2", "paciente_telefono": TEL9, "ficha": "", "fecha": "2026-10-01"})
client.post("/agenda/fluidez/ajena/borrar", data={})
chequear("no borra lecturas de otro", any(x["id"] == "ajena" for x in TABLAS["lecturas"]))
client.post(f"/agenda/textos-lectura/{texto_id}/borrar", data={})
chequear("borrar el texto deja las lecturas", not any(t["id"] == texto_id for t in TABLAS["textos_lectura"]) and len(TABLAS["lecturas"]) == n + 1)
TABLAS["tenants"][0]["rubro"] = "psicología"
chequear("psicología no ve fluidez", "Fluidez lectora" not in _panel(client))

# --- Cuestionarios por link ---
import cuestionarios  # noqa: E402

todo = lambda n, v: {f"item-{i}": str(v) for i in range(n)}
r, e = cuestionarios.corregir("phq9", {**todo(9, 1), "dificultad": "Mucha dificultad"})
chequear("corrige el PHQ-9", e is None and r["puntaje"] == 9 and r["severidad"] == "leve" and r["alerta"] and r["dificultad"] == "Mucha dificultad")
r, _ = cuestionarios.corregir("phq9", {**todo(9, 3), "item-8": "0"})
chequear("24 es severa y sin la 9 no alerta", r["puntaje"] == 24 and r["severidad"] == "severa" and not r["alerta"])
chequear("cortes del PHQ-9", [cuestionarios.severidad("phq9", p) for p in (4, 5, 10, 15, 20)] == ["mínima", "leve", "moderada", "moderadamente severa", "severa"])
chequear("cortes del GAD-7", [cuestionarios.severidad("gad7", p) for p in (0, 9, 14, 21)] == ["mínima", "leve", "moderada", "severa"])
chequear("pide todas las respuestas", cuestionarios.corregir("gad7", todo(6, 1))[1] == "Falta responder alguna pregunta.")
chequear("rechaza valores raros", cuestionarios.corregir("gad7", {**todo(7, 1), "item-3": "7"})[1] is not None)
chequear("GAD-7 no alerta", cuestionarios.corregir("gad7", todo(7, 3))[0] == {"respuestas": [3] * 7, "puntaje": 21, "severidad": "severa", "alerta": False, "dificultad": None})
chequear("tokens distintos y largos", len({cuestionarios.nuevo_token() for _ in range(50)}) == 50 and len(cuestionarios.nuevo_token()) >= 32)

reset()
TELC = "5491115151515"
TABLAS["turnos"].append(turno("q1", "t1", manana, estado="reservado", paciente_nombre="Carla Gómez", paciente_telefono=TELC))
html = _panel(client)
chequear("psicología ofrece cuestionarios", "Generar link" in html and "PHQ-9 (depresión)" in html)
client.post("/agenda/cuestionario", data={"telefono": TELC, "instrumento": "phq9"})
client.post("/agenda/cuestionario", data={"telefono": TELC, "instrumento": "snap"})
chequear("crea el link", len(TABLAS["cuestionarios"]) == 1 and TABLAS["cuestionarios"][0]["instrumento"] == "phq9")
token = TABLAS["cuestionarios"][0]["token"]
html = _panel(client)
chequear("la ficha muestra el link", f"/cuestionario/{token}" in html and "sin responder" in html)
publica = client.get(f"/cuestionario/{token}").data.decode()
chequear("el paciente ve el cuestionario sin login", "Poco interés o placer en hacer cosas" in publica and "Carla" not in publica and TELC not in publica)
chequear("link inventado no existe", client.get("/cuestionario/nada").status_code == 404)
res = client.post(f"/cuestionario/{token}", data=todo(8, 2))
chequear("incompleto vuelve con aviso y conserva", res.status_code == 400 and "Falta responder" in res.data.decode() and 'value="2" required checked' in res.data.decode())
fin = client.post(f"/cuestionario/{token}", data={**todo(9, 2), "item-8": "1"}).data.decode()
guardado = TABLAS["cuestionarios"][0]
chequear("guarda corregido", guardado["puntaje"] == 17 and guardado["severidad"] == "moderadamente severa" and guardado["alerta"] and guardado.get("respondido_at"))
chequear("con la 9 le da teléfonos de ayuda", "135" in fin and "911" in fin)
otra = client.post(f"/cuestionario/{token}", data=todo(9, 0)).data.decode()
chequear("un link sirve una vez", "ya se respondió" in otra and TABLAS["cuestionarios"][0]["puntaje"] == 17)
html = _panel(client)
chequear("aviso en el panel y en la ficha", 'id="bt-cuestionarios"' in html and "Para resolver" in html and "marcó ideas de hacerse daño" in html and "pregunta 9: varios días" in html)
TABLAS["cuestionarios"].append({"id": "viejo", "tenant_id": "t1", "paciente_telefono": TELC, "ficha": "", "instrumento": "phq9", "token": "tv",
                                "vence": "2026-01-01T00:00:00+00:00", "respondido_at": "2026-01-02T12:00:00+00:00", "puntaje": 22,
                                "severidad": "severa", "alerta": False, "respuestas": [2] * 9, "visto": True, "created_at": "00000000"})
html = _panel(client)
chequear("evolución contra la anterior", "↓ 5" in html and "grafico-fluidez" in html)
client.post(f"/agenda/cuestionario/{guardado['id']}/visto", data={})
html = _panel(client)
chequear("visto saca el aviso", "Cuestionarios respondidos" not in html and "sin ver marcó ideas" not in html)
TABLAS["cuestionarios"].append({"id": "venc", "tenant_id": "t1", "paciente_telefono": TELC, "ficha": "", "instrumento": "gad7", "token": "tvenc",
                                "vence": "2026-01-01T00:00:00+00:00", "created_at": "00000001"})
chequear("link vencido avisa", "venció" in client.get("/cuestionario/tvenc").data.decode())
chequear("vencido no se muestra como pendiente", "/cuestionario/tvenc" not in _panel(client))
TABLAS["cuestionarios"].append({"id": "ajeno", "tenant_id": "t2", "paciente_telefono": TELC, "ficha": "", "instrumento": "gad7", "token": "taj",
                                "vence": "2099-01-01T00:00:00+00:00", "created_at": "00000002"})
client.post("/agenda/cuestionario/ajeno/borrar", data={})
client.post("/agenda/cuestionario/ajeno/visto", data={})
chequear("no toca cuestionarios de otro", any(c["id"] == "ajeno" and not c.get("visto") for c in TABLAS["cuestionarios"]))
chequear("ni lo muestra", "/cuestionario/taj" not in _panel(client))
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
html = _panel(client)
chequear("psicopedagogía ofrece entrevista y docente, no PHQ-9", "Entrevista inicial (para la familia)" in html and "Cuestionario para el docente" in html and "PHQ-9 (depresión)" not in html)
n = len(TABLAS["cuestionarios"])
client.post("/agenda/cuestionario", data={"telefono": TELC, "instrumento": "gad7"})
chequear("ni deja crear un GAD-7", len(TABLAS["cuestionarios"]) == n)

html = client.get("/agenda", headers={"X-Forwarded-Proto": "https"}).data.decode()
TABLAS["tenants"][0]["rubro"] = "psicología"
client.post("/agenda/cuestionario", data={"telefono": TELC, "instrumento": "gad7"})
html = client.get("/agenda", headers={"X-Forwarded-Proto": "https"}).data.decode()
chequear("el link sale con https detrás del proxy", "https://localhost/cuestionario/" in html)

# --- Entrevista y docente por link ---
reset()
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
TELE = "5491116161616"
TABLAS["turnos"].append(turno("e1", "t1", manana, estado="reservado", paciente_nombre="Bauti Paz", paciente_telefono=TELE))
client.post("/agenda/cuestionario", data={"telefono": TELE, "instrumento": "docente", "para": "B. P., 4º A"})
doc = TABLAS["cuestionarios"][0]
chequear("crea el link del docente con referencia", doc["instrumento"] == "docente" and doc["para"] == "B. P., 4º A")
pagina = client.get(f"/cuestionario/{doc['token']}").data.decode()
chequear("el docente ve preguntas abiertas y la referencia", "lectura en voz alta" in pagina and "B. P., 4º A" in pagina
         and "<textarea" in pagina and "Bauti" not in pagina and "No es necesario ningún permiso" not in pagina)
res = client.post(f"/cuestionario/{doc['token']}", data={"pregunta-0": " ", "pregunta-1": ""})
chequear("pide al menos una respuesta", res.status_code == 400 and "al menos una" in res.data.decode())
client.post(f"/cuestionario/{doc['token']}", data={"pregunta-1": "Lee silabeando.", "pregunta-7": "Se frustra rápido."})
doc = TABLAS["cuestionarios"][0]
chequear("guarda las respuestas abiertas", doc.get("respondido_at") and doc["respuestas"][1] == "Lee silabeando." and doc.get("puntaje") is None)
html = _panel(client)
chequear("se lee en la ficha con su pregunta", "Lee silabeando." in html and "¿Cómo es su lectura en voz alta" in html and "Cuestionario para el docente" in html)
chequear("y aparece como respondido", "Cuestionario para el docente respondido" in html)
chequear("no repite preguntas sin respuesta", "¿Cómo le va en matemática" not in html.split("Lee silabeando.")[1].split("Borrar respuestas")[0])
TABLAS["tenants"][0]["rubro"] = "psicología"
html = _panel(client)
chequear("psicología ofrece entrevista de adultos", "Antes de la primera sesión (entrevista inicial)" in html)

# --- Hoja de registro ---
reset()
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
TELR = "5491117171717"
TABLAS["turnos"].append(turno("h1", "t1", manana, estado="reservado", paciente_nombre="Sol Ríos", paciente_telefono=TELR))
TABLAS["paciente_tests"].append({"id": "hr", "tenant_id": "t1", "paciente_telefono": TELR, "ficha": "", "test": "WISC-V", "fecha": "2026-10-20",
                                 "resultados": [{"escala": "CI Total", "tipo": "compuesto", "puntaje": 95, "nivel": ""}]})
html = _panel(client)
chequear("ofrece la hoja de registro", "Hoja de registro" in html)
hoja = client.post("/agenda/registro", data={"telefono": TELR, "test_id": "hr"}).data.decode()
chequear("abre la hoja con lo que ya tenía", "Hoja de registro · WISC-V · Sol Ríos" in hoja and '"CI Total"' in hoja
         and '<option value="Razonamiento Fluido">' in hoja and "/agenda/paciente/test/hr/resultados" in hoja)
html = client.post("/agenda/registro", data={"telefono": TELR, "test_id": "otro"}, follow_redirects=True).data.decode()
chequear("test ajeno o inexistente", "No se encontró ese test" in html)
client.post("/agenda/paciente/test/hr/resultados", data={"telefono": TELR, "escala-0": "CI Total", "tipo-0": "compuesto", "puntaje-0": "95",
                                                         "escala-1": "Vocabulario", "tipo-1": "bruto", "puntaje-1": "23", "nivel-1": "4 errores · 3:10"})
chequear("lo de la hoja se guarda como bruto", TABLAS["paciente_tests"][0]["resultados"][1] == {"escala": "Vocabulario", "tipo": "bruto", "puntaje": 23, "nivel": "4 errores · 3:10"})

# --- Gráfico por escala ---
tres = [{"_fecha": f"0{i}/01/2026", "resultados": [{"escala": "CI", "tipo": "compuesto", "puntaje": p, "nivel": ""}]} for i, p in ((1, 85), (2, 92), (3, 97))]
g = resultados_tests.evolucion(tres)[0]["grafico"]
chequear("gráfico por escala ajustado al rango", g and [p["valor"] for p in g["puntos"]] == [85, 92, 97] and g["puntos"][0]["y"] > g["puntos"][-1]["y"]
         and g["puntos"][0]["y"] == g["alto"] - 6)
sin = resultados_tests.evolucion([{"_fecha": "a", "resultados": [{"escala": "L", "tipo": "bruto", "puntaje": None, "nivel": "x"}]},
                                  {"_fecha": "b", "resultados": [{"escala": "L", "tipo": "bruto", "puntaje": None, "nivel": "y"}]}])[0]
chequear("sin números no hay gráfico", sin["grafico"] is None)
chequear("valores iguales no dividen por cero", fluidez.grafico([5, 5], desde_cero=False) is not None)

# --- Borrador de informe ---
import informe  # noqa: E402

reset()
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
TABLAS["tenants"][0]["matricula"] = "MP 1234"
TELI = "5491118181818"
TABLAS["turnos"].append(turno("i1", "t1", manana, estado="reservado", paciente_nombre="Iara Sosa", paciente_telefono=TELI))
TABLAS["pacientes"].append({"tenant_id": "t1", "paciente_telefono": TELI, "ficha": "", "motivo_consulta": "Dificultad en lectura",
                            "fecha_nacimiento": "2017-03-10", "escuela": "Escuela 5", "grado": "3º"})
TABLAS["paciente_tests"] += [
    {"id": "p1", "tenant_id": "t1", "paciente_telefono": TELI, "ficha": "", "test": "PROLEC-R", "fecha": "2026-04-01",
     "resultados": [{"escala": "Comprensión", "tipo": "percentil", "puntaje": 15, "nivel": "Dificultad"}]},
    {"id": "p2", "tenant_id": "t1", "paciente_telefono": TELI, "ficha": "", "test": "PROLEC-R", "fecha": "2026-10-01",
     "resultados": [{"escala": "Comprensión", "tipo": "percentil", "puntaje": 45, "nivel": "Normal"}]}]
TABLAS["lecturas"].append({"id": "l1", "tenant_id": "t1", "paciente_telefono": TELI, "ficha": "", "fecha": "2026-10-02", "texto_titulo": "La plaza",
                           "palabras_texto": 50, "palabras_leidas": 50, "errores": 2, "errores_palabras": [], "segundos": 60, "created_at": "1"})
pagina = client.post("/agenda/paciente/informe", data={"telefono": TELI}).data.decode()
chequear("arma el informe", "Informe psicopedagógico" in pagina and "Iara Sosa" in pagina and "Escuela 5" in pagina and "MP 1234" in pagina)
chequear("con resultados y evolución", "PROLEC-R (01/10/2026): Comprensión: 45 (Normal)." in pagina
         and "En Comprensión subió de 15 a 45 respecto de la toma del 01/04/2026." in pagina)
chequear("con la lectura", "leyó 48 palabras correctas por minuto, con 96% de precisión" in pagina)
chequear("edad calculada", informe._edad("2017-03-10", _date(2026, 11, 2)) == "9 años y 7 meses" and informe._edad("2017-11-02", _date(2026, 11, 2)) == "9 años")
chequear("informe de otro no existe", client.post("/agenda/paciente/informe", data={"telefono": "999"}).status_code == 404)
TABLAS["tenants"][0]["rubro"] = "psicología"
chequear("psicología dice psicológico", "Informe psicológico" in client.post("/agenda/paciente/informe", data={"telefono": TELI}).data.decode())

# --- Aviso de retest ---
pr = tests_sugeridos.para_repetir
tom = [{"test": "PROLEC-R", "fecha": "2026-04-20"}, {"test": "prolec-r", "fecha": "2025-01-01"}, {"test": "WISC-V", "fecha": "2025-11-02"},
       {"test": "Inventado", "fecha": "2020-01-01"}]
avisos = pr(tom, _date(2026, 11, 2))
chequear("avisa sólo lo que se cumplió hace poco", [(a["nombre"], a["vence"]) for a in avisos] == [("PROLEC-R", _date(2026, 10, 20))])
chequear("pasados 30 días deja de avisar", pr(tom, _date(2026, 11, 20)) == [])
chequear("con el plazo propio", [a["nombre"] for a in pr(tom, _date(2026, 11, 2), {"WISC-V": 12})] == ["PROLEC-R", "WISC-V"])
reset()
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
TELT = "5491119191919"
TABLAS["turnos"].append(turno("t9", "t1", manana, estado="reservado", paciente_nombre="Teresa Gil", paciente_telefono=TELT))
hoy_local = datetime.now(zona).date()
fecha_vieja = (hoy_local.replace(day=1) - timedelta(days=200)).isoformat()
TABLAS["paciente_tests"].append({"id": "rt", "tenant_id": "t1", "paciente_telefono": TELT, "ficha": "", "test": "TEDE", "fecha": fecha_vieja})
vence = tests_sugeridos._vence(_date.fromisoformat(fecha_vieja), 6)
html = _panel(client)
en_ventana = 0 <= (hoy_local - vence).days < 30
chequear("tarjeta de tests para repetir", ('id="bt-tests-repetir"' in html) == en_ventana, (fecha_vieja, vence))
TABLAS["paciente_tests"][0]["fecha"] = _date(hoy_local.year - 1, hoy_local.month, min(hoy_local.day, 28)).isoformat() if hoy_local.day <= 28 else TABLAS["paciente_tests"][0]["fecha"]
TABLAS["paciente_tests"][0]["test"] = "D2"  # 12 meses: vence hoy si el día existe
html = _panel(client)
chequear("aparece el día que vence", hoy_local.day > 28 or ('id="bt-tests-repetir"' in html and "Teresa Gil" in html and "D2" in html))
partes = scheduler._partes_del_resumen(TABLAS["tenants"][0], hoy_local)
chequear("el resumen cuenta los de hoy sin nombres", hoy_local.day > 28 or ("1 test que hoy se puede repetir" in ", ".join(partes) and "Teresa" not in ", ".join(partes)))

# --- Informes guardados ---
import json  # noqa: E402

leer_inf = informe.leer_guardado
ok, err = leer_inf(json.dumps({"titulo": " Informe ", "datos": [["Nombre", "Iara"]], "secciones": [{"titulo": "Motivo", "texto": " lee lento "}, {"titulo": "Vacía", "texto": ""}]}))
chequear("lee el informe guardado", err is None and ok == {"titulo": "Informe", "datos": [["Nombre", "Iara"]],
                                                             "secciones": [{"titulo": "Motivo", "texto": "lee lento"}, {"titulo": "Vacía", "texto": ""}]})
chequear("rechaza basura", leer_inf("no es json")[1] and leer_inf(json.dumps({"titulo": "x"}))[1] and leer_inf(json.dumps({"titulo": "x", "secciones": "hola"}))[1])
chequear("rechaza vacío", leer_inf(json.dumps({"titulo": "x", "secciones": [{"titulo": "a", "texto": " "}]}))[1] == "El informe está vacío.")
reset()
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
TELV = "5491110101010"
TABLAS["turnos"].append(turno("v1", "t1", manana, estado="reservado", paciente_nombre="Vera Luna", paciente_telefono=TELV))
borrador = client.post("/agenda/paciente/informe", data={"telefono": TELV}).data.decode()
chequear("el borrador ofrece guardar", "Guardar en la ficha" in borrador and "Conclusiones" in borrador and "/agenda/paciente/informe/guardar" in borrador)
contenido = lambda texto: json.dumps({"titulo": "Informe psicopedagógico", "datos": [["Nombre", "Vera Luna"]],
                                      "secciones": [{"titulo": "Conclusiones", "texto": texto}]})
html = client.post("/agenda/paciente/informe/guardar", data={"telefono": TELV, "contenido": contenido("Primera versión.")}, follow_redirects=True).data.decode()
client.post("/agenda/paciente/informe/guardar", data={"telefono": TELV, "contenido": contenido("Segunda versión.")})
chequear("guarda versiones sin pisar", "Se guardó el informe" in html and [i["contenido"]["secciones"][0]["texto"] for i in TABLAS["informes"]] == ["Primera versión.", "Segunda versión."])
html = _panel(client)
chequear("la ficha lista las versiones", "Versión 1 · Informe psicopedagógico" in html and "Versión 2 · Informe psicopedagógico" in html
         and html.index("Versión 2") < html.index("Versión 1"))
primera = TABLAS["informes"][0]["id"]
abierto = client.post(f"/agenda/paciente/informe/{primera}", data={"telefono": TELV}).data.decode()
chequear("abre la versión guardada", "Primera versión." in abierto and "Segunda versión." not in abierto and "Versión guardada el" in abierto and "Vera Luna" in abierto)
TABLAS["informes"].append({"id": "ajeno", "tenant_id": "t2", "paciente_telefono": TELV, "ficha": "", "created_at": "x",
                           "contenido": {"titulo": "Ajeno", "datos": [], "secciones": [{"titulo": "a", "texto": "secreto"}]}})
chequear("no abre informes de otro", client.post("/agenda/paciente/informe/ajeno", data={"telefono": TELV}).status_code == 404)
client.post("/agenda/paciente/informe/ajeno/borrar", data={})
chequear("ni los borra", any(i["id"] == "ajeno" for i in TABLAS["informes"]))
client.post(f"/agenda/paciente/informe/{primera}/borrar", data={"telefono": TELV})
chequear("borra una versión", [i["id"] for i in TABLAS["informes"] if i["tenant_id"] == "t1"] != [] and not any(i["id"] == primera for i in TABLAS["informes"]))
n = len(TABLAS["informes"])
client.post("/agenda/paciente/informe/guardar", data={"telefono": TELV, "contenido": "{roto"})
chequear("no guarda un informe roto", len(TABLAS["informes"]) == n)

# --- Cuestionarios propios y SCARED ---
M = cuestionarios.MODELO_SCARED
todos_items = sorted(n for x in M["subescalas"] for n in x["items"])
chequear("SCARED: cada pregunta en una sola subescala", todos_items == list(range(1, 42)))
chequear("SCARED: cortes publicados", [x["corte"] for x in M["subescalas"]] == [7, 9, 5, 8, 3] and M["corte_total"] == 25)
chequear("SCARED: sin texto de preguntas", M["items"] == [""] * 41 and not cuestionarios.completa(M))
form = {"nombre": "Mi escala", "que_mide": "ánimo", "items": "Uno\nDos\nTres\n\n", "opciones": "No = 0\nSí = 2",
        "subescalas": "A: 1, 2 | 3\nB: 3", "corte_total": "4", "fuente": "Propia"}
pl, err = cuestionarios.leer_plantilla(form)
chequear("lee la plantilla", err is None and pl["items"] == ["Uno", "Dos", "Tres"] and pl["opciones"] == [{"texto": "No", "valor": 0}, {"texto": "Sí", "valor": 2}]
         and pl["subescalas"] == [{"nombre": "A", "items": [1, 2], "corte": 3}, {"nombre": "B", "items": [3], "corte": None}] and pl["corte_total"] == 4)
chequear("opción sin puntos", "= número" in cuestionarios.leer_plantilla({**form, "opciones": "No\nSí = 1"})[1])
chequear("subescala fuera de rango", "¿Faltan renglones" in cuestionarios.leer_plantilla({**form, "subescalas": "A: 1, 9"})[1])
chequear("subescala mal escrita", "No entiendo" in cuestionarios.leer_plantilla({**form, "subescalas": "A: uno | 2"})[1])
chequear("sin nombre", cuestionarios.leer_plantilla({**form, "nombre": " "})[1] == "El cuestionario necesita un nombre.")
r, _ = cuestionarios.corregir_propio(pl, {"item-0": "1", "item-1": "1", "item-2": "0"})
chequear("corrige con subescalas y cortes", r["puntaje"] == 4 and r["severidad"] == "sobre el corte"
         and r["detalle"] == [{"nombre": "A", "puntaje": 4, "corte": 3, "sobre_corte": True}, {"nombre": "B", "puntaje": 0, "corte": None, "sobre_corte": False}])
chequear("opción inexistente", cuestionarios.corregir_propio(pl, {"item-0": "5", "item-1": "1", "item-2": "0"})[1] is not None)

reset()
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
TELS = "5491112345678"
TABLAS["turnos"].append(turno("s1", "t1", manana, estado="reservado", paciente_nombre="Santi Rey", paciente_telefono=TELS))
html = client.post("/agenda/plantillas", data={"modelo": "scared_chico", "volver": "ajustes", "pestania": "avanzado"},
                   follow_redirects=True).data.decode()
chequear("crea el SCARED vacío", len(TABLAS["plantillas_cuestionario"]) == 1 and "faltan 41: todavía no se puede mandar" in html and "Falta pegar las 41 preguntas" in html)
chequear("no se ofrece incompleto", "SCARED · Chico/a (ansiedad infantil)" not in html)
pid = TABLAS["plantillas_cuestionario"][0]["id"]
chequear("el form trae la corrección", "Ansiedad de separación: 4, 8, 13, 16, 20, 25, 29, 31 | 5" in html and "Seguido = 2" in html)
n = len(TABLAS["cuestionarios"])
client.post("/agenda/cuestionario", data={"telefono": TELS, "instrumento": f"propio:{pid}"})
chequear("no manda un cuestionario incompleto", len(TABLAS["cuestionarios"]) == n)
subes = "\n".join(f"{x['nombre']}: {', '.join(map(str, x['items']))} | {x['corte']}" for x in M["subescalas"])
editar = {"nombre": "SCARED", "version": "Chico/a", "que_mide": "ansiedad infantil", "items": "\n".join(f"Pregunta de prueba {i}" for i in range(1, 42)),
          "opciones": "Nunca = 0\nA veces = 1\nSeguido = 2", "subescalas": subes, "corte_total": "25", "fuente": "Crédito"}
html = client.post(f"/agenda/plantillas/{pid}", data={**editar, "items": "solo una"}, follow_redirects=True).data.decode()
chequear("si faltan renglones avisa y no guarda", "¿Faltan renglones" in html and not cuestionarios.completa(TABLAS["plantillas_cuestionario"][0]["contenido"]))
client.post(f"/agenda/plantillas/{pid}", data=editar)
html = _panel(client)
chequear("completo se ofrece en la ficha", f'value="propio:{pid}"' in html and "SCARED · Chico/a (ansiedad infantil)" in html)
client.post("/agenda/cuestionario", data={"telefono": TELS, "instrumento": f"propio:{pid}"})
q = TABLAS["cuestionarios"][-1]
chequear("manda una copia de la plantilla", q["instrumento"] == "propio" and q["plantilla"]["id"] == pid and len(q["plantilla"]["items"]) == 41)
pub = client.get(f"/cuestionario/{q['token']}").data.decode()
chequear("el link muestra sus preguntas y opciones", "Pregunta de prueba 41" in pub and "Seguido" in pub and "Crédito" in pub and "Santi" not in pub)
respuestas = {f"item-{i}": "2" for i in range(41)}
respuestas.update({f"item-{n - 1}": "0" for n in M["subescalas"][4]["items"]})
client.post(f"/cuestionario/{q['token']}", data=respuestas)
q = TABLAS["cuestionarios"][-1]
chequear("corrige el SCARED", q["puntaje"] == 74 and q["severidad"] == "sobre el corte"
         and [d["sobre_corte"] for d in q["detalle"]] == [True, True, True, True, False])
TABLAS["plantillas_cuestionario"][0]["contenido"]["items"][0] = "Cambiada después"
html = _panel(client)
chequear("la ficha muestra subescalas", "Evitación escolar: 0/3" in html and "Ansiedad social: 14/8" in html and "74</strong> sobre el corte" in html)
chequear("editar la plantilla no toca lo mandado", q["plantilla"]["items"][0] == "Pregunta de prueba 1")
TABLAS["plantillas_cuestionario"].append({"id": "ajena", "tenant_id": "t2", "contenido": dict(editar, items=["a"], opciones=[{"texto": "x", "valor": 0}, {"texto": "y", "valor": 1}], subescalas=[], corte_total=None)})
n = len(TABLAS["cuestionarios"])
client.post("/agenda/cuestionario", data={"telefono": TELS, "instrumento": "propio:ajena"})
chequear("no usa plantillas de otro", len(TABLAS["cuestionarios"]) == n)
client.post("/agenda/plantillas/ajena", data=editar)
client.post("/agenda/plantillas/ajena/borrar", data={})
chequear("ni las edita ni las borra", any(p["id"] == "ajena" and p["contenido"]["items"] == ["a"] for p in TABLAS["plantillas_cuestionario"]))
TABLAS["tenants"][0]["rubro"] = "psicología"
chequear("psicología también tiene propios", "Cuestionarios propios" in _panel(client))

# --- Exportar e importar cuestionarios, versiones lado a lado ---
exp = cuestionarios.exportar({**M, "version": "Padres"})
imp, err = cuestionarios.importar(json.dumps(exp).encode())
chequear("exporta e importa el SCARED vacío sin perder lugares", err is None and len(imp["items"]) == 41 and imp["version"] == "Padres"
         and imp["subescalas"] == M["subescalas"] and imp["corte_total"] == 25)
chequear("rechaza otro JSON", cuestionarios.importar(b'{"hola": 1}')[1] == "Ese archivo no es un cuestionario exportado de FiBOT.")
chequear("rechaza basura", cuestionarios.importar(b"\xff\xfe")[1] is not None and cuestionarios.importar(b"[1,2]")[1] is not None)
chequear("rechaza archivos enormes", cuestionarios.importar(b" " * (cuestionarios.MAX_IMPORTAR + 1))[1] == "El archivo es demasiado grande.")
malo = dict(exp, subescalas=[{"nombre": "X", "items": [99], "corte": 1}])
chequear("valida como el formulario", "¿Faltan renglones" in cuestionarios.importar(json.dumps(malo).encode())[1])

reset()
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
TELX = "5491113579135"
TABLAS["turnos"].append(turno("x1", "t1", manana, estado="reservado", paciente_nombre="Luz Paz", paciente_telefono=TELX))
completo = lambda version: {"nombre": "SCARED", "version": version, "que_mide": "ansiedad infantil",
                            "items": "\n".join(f"Pregunta {i}" for i in range(1, 42)), "opciones": "Nunca = 0\nA veces = 1\nSeguido = 2",
                            "subescalas": "\n".join(f"{x['nombre']}: {', '.join(map(str, x['items']))} | {x['corte']}" for x in M["subescalas"]),
                            "corte_total": "25"}
client.post("/agenda/plantillas", data=completo("Chico/a"))
pid = TABLAS["plantillas_cuestionario"][0]["id"]
r = client.get(f"/agenda/plantillas/{pid}/exportar")
chequear("exporta como archivo", r.status_code == 200 and "attachment" in r.headers["Content-Disposition"] and "scared-chico-a.json" in r.headers["Content-Disposition"]
         and json.loads(r.data)["formato"] == "fibot-cuestionario" and len(json.loads(r.data)["items"]) == 41)
chequear("no exporta de otro", client.get("/agenda/plantillas/nada/exportar").status_code == 404)
datos_imp = json.loads(r.data)
datos_imp["version"] = "Padres"
html = client.post("/agenda/plantillas/importar", data={"archivo": (io.BytesIO(json.dumps(datos_imp).encode()), "scared.json")},
                   content_type="multipart/form-data", follow_redirects=True).data.decode()
chequear("importa", "Se importó «SCARED»" in html and len(TABLAS["plantillas_cuestionario"]) == 2)
html = client.post("/agenda/plantillas/importar", data={"archivo": (io.BytesIO(b"no"), "x.json")},
                   content_type="multipart/form-data", follow_redirects=True).data.decode()
chequear("importar basura avisa", "no es un cuestionario exportado" in html and len(TABLAS["plantillas_cuestionario"]) == 2)
ids = {p["contenido"]["version"]: p["id"] for p in TABLAS["plantillas_cuestionario"]}

def responder(version, valores):
    client.post("/agenda/cuestionario", data={"telefono": TELX, "instrumento": f"propio:{ids[version]}"})
    client.post(f"/cuestionario/{TABLAS['cuestionarios'][-1]['token']}", data={f"item-{i}": str(v) for i, v in enumerate(valores)})

chico = [0] * 41
for n in M["subescalas"][3]["items"] + M["subescalas"][4]["items"]:  # social y escolar altas en el chico
    chico[n - 1] = 2
responder("Chico/a", chico)
responder("Padres", [1] * 41)
html = _panel(client)
chequear("compara las versiones lado a lado", "SCARED: versiones lado a lado" in html and "Chico/a" in html and "Padres" in html)
tabla = html.split("SCARED: versiones lado a lado")[1].split("</table>")[0]
fila_social = tabla.split("Ansiedad social")[1].split("</tr>")[0]
fila_gen = tabla.split("Ansiedad generalizada")[1].split("</tr>")[0]
chequear("marca dónde no coinciden", "no coinciden" in fila_social and "<strong>14</strong>" in fila_social and "<strong>7</strong>" in fila_social)
chequear("generalizada: padres 9 sobre el corte, chico 0", "no coinciden" in fila_gen)
fila_esc = tabla.split("Evitación escolar")[1].split("</tr>")[0]
chequear("los dos sobre el corte no se marcan", "no coinciden" not in fila_esc and fila_esc.count("sobre el corte") == 2)
chequear("total con las dos", "<strong>41</strong>" in tabla.split(">Total")[1] and "<strong>22</strong>" in tabla.split(">Total")[1])

# --- Ficha completa, plantillas de informe y firma ---
reset()
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
TELF = "5491112223334"
TABLAS["turnos"].append(turno("fc1", "t1", ahora - timedelta(days=7), estado="reservado", paciente_nombre="Fede Gil", paciente_telefono=TELF,
                              asistencia="vino", nota="Trabajamos conciencia fonológica."))
TABLAS["turnos"].append(turno("fc2", "t1", manana, estado="reservado", paciente_nombre="Fede Gil", paciente_telefono=TELF))
TABLAS["pacientes"].append({"tenant_id": "t1", "paciente_telefono": TELF, "ficha": "", "motivo_consulta": "Deriva la escuela por lectura.",
                            "notas": "Nota privada de prueba.", "escuela": "Escuela 9", "fecha_nacimiento": "2016-05-01"})
TABLAS["paciente_personas"].append({"tenant_id": "t1", "paciente_telefono": TELF, "ficha": "", "nombre": "Ana", "vinculo": "Madre", "telefono": "11", "emergencia": True})
TABLAS["cuestionarios"].append({"id": "fca", "tenant_id": "t1", "paciente_telefono": TELF, "ficha": "", "instrumento": "docente", "token": "tfc",
                                "vence": "2026-01-01T00:00:00+00:00", "respondido_at": "2026-10-01T12:00:00+00:00",
                                "respuestas": ["", "Lee silabeando."], "created_at": "1"})
TABLAS["informes"].append({"id": "fci", "tenant_id": "t1", "paciente_telefono": TELF, "ficha": "", "created_at": "2",
                           "contenido": {"titulo": "Informe psicopedagógico", "datos": [], "secciones": [{"titulo": "Conclusiones", "texto": "Avanza bien."}]}})
html = _panel(client)
chequear("ofrece la ficha completa", "Ficha completa en PDF" in html)
pagina = client.post("/agenda/paciente/ficha-completa", data={"telefono": TELF}).data.decode()
for texto in ("Ficha de Fede Gil", "Deriva la escuela por lectura.", "Nota privada de prueba.", "Escuela 9", "Ana", "Madre",
              "contacto de emergencia", "Trabajamos conciencia fonológica.", "asistió", "próxima", "Lee silabeando.",
              "Avanza bien.", "Confidencial", "01/05/2016"):
    chequear(f"la ficha completa trae: {texto}", texto in pagina)
chequear("ficha completa de otro no existe", client.post("/agenda/paciente/ficha-completa", data={"telefono": "nada"}).status_code == 404)

html = client.post("/agenda/plantillas-informe", data={"nombre": "Para la escuela", "secciones": json.dumps(["Motivo de derivación", "Lo que observamos", " ", "Objetivos"])},
                   follow_redirects=True).data.decode()
chequear("guarda la plantilla de informe", "Se guardó la plantilla «Para la escuela»" in html
         and TABLAS["plantillas_informe"][0]["contenido"] == {"nombre": "Para la escuela", "secciones": ["Motivo de derivación", "Lo que observamos", "Objetivos"]})
chequear("plantilla sin secciones", "no tiene secciones" in client.post("/agenda/plantillas-informe", data={"nombre": "x", "secciones": "[]"}, follow_redirects=True).data.decode())
html = _panel(client)
chequear("se elige al armar el informe", '<option value="">Estándar</option>' in html and ">Para la escuela</option>" in html)
pid = TABLAS["plantillas_informe"][0]["id"]
borr = client.post("/agenda/paciente/informe", data={"telefono": TELF, "plantilla_id": pid}).data.decode()
chequear("el borrador sigue la plantilla", "Para la escuela" in borr and "Lo que observamos" in borr and "Conclusiones" not in borr
         and "Deriva la escuela por lectura." in borr.split("Motivo de derivación")[1].split("Lo que observamos")[0])
chequear("y ofrece guardarse como plantilla", "Guardar estructura como plantilla" in borr and "Sumar sección" in borr)
TABLAS["plantillas_informe"].append({"id": "pi-ajena", "tenant_id": "t2", "contenido": {"nombre": "Ajena", "secciones": ["Secreta"]}})
borr = client.post("/agenda/paciente/informe", data={"telefono": TELF, "plantilla_id": "pi-ajena"}).data.decode()
chequear("no usa plantillas de otro", "Secreta" not in borr and "Conclusiones" in borr)
client.post("/agenda/plantillas-informe/pi-ajena/borrar", data={})
chequear("ni las borra", any(p["id"] == "pi-ajena" for p in TABLAS["plantillas_informe"]))

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 50
html = client.post("/agenda/firma", data={"firma": (io.BytesIO(PNG), "firma.png")}, content_type="multipart/form-data", follow_redirects=True).data.decode()
chequear("guarda la firma", "Se guardó tu firma" in html and TABLAS["tenants"][0]["firma_imagen"].startswith("data:image/png;base64,"))
pagina = client.post("/agenda/paciente/ficha-completa", data={"telefono": TELF}).data.decode()
chequear("la firma sale en la ficha y el informe", 'alt="Firma"' in pagina and 'alt="Firma"' in client.post("/agenda/paciente/informe", data={"telefono": TELF}).data.decode())
html = client.post("/agenda/firma", data={"firma": (io.BytesIO(b"<svg onload=alert(1)>"), "f.svg")}, content_type="multipart/form-data", follow_redirects=True).data.decode()
chequear("rechaza lo que no es PNG o JPG", "PNG o JPG" in html and TABLAS["tenants"][0]["firma_imagen"].startswith("data:image/png"))
html = client.post("/agenda/firma", data={"firma": (io.BytesIO(PNG + b"0" * 300_000), "f.png")}, content_type="multipart/form-data", follow_redirects=True).data.decode()
chequear("rechaza imágenes pesadas", "muy pesada" in html)
client.post("/agenda/firma/borrar", data={})
chequear("quita la firma", TABLAS["tenants"][0]["firma_imagen"] is None and not TABLAS["tenants"][1].get("firma_imagen"))

# --- Todas las versiones en un paso, toma anterior y biblioteca ---
reset()
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
TELW = "5491114445556"
TABLAS["turnos"].append(turno("w1", "t1", manana, estado="reservado", paciente_nombre="Juana Rey", paciente_telefono=TELW))
for v in ("Chico/a", "Padres"):
    client.post("/agenda/plantillas", data=completo(v))
client.post("/agenda/plantillas", data={"modelo": "scared_padres"})  # una tercera, incompleta: no cuenta
html = _panel(client)
chequear("ofrece todas las versiones juntas", 'value="todas:scared"' in html and "SCARED · todas las versiones (Chico/a, Padres)" in html)
html = client.post("/agenda/cuestionario", data={"telefono": TELW, "instrumento": "todas:scared", "para": "J. R."}, follow_redirects=True).data.decode()
mandados = [c for c in TABLAS["cuestionarios"] if c["instrumento"] == "propio"]
chequear("crea un link por versión completa", sorted(c["plantilla"]["version"] for c in mandados) == ["Chico/a", "Padres"]
         and all(c["para"] == "J. R." for c in mandados) and "Listos 2 links" in html)
chequear("cada link dice de qué versión es", "SCARED · Chico/a" in html and "SCARED · Padres" in html and html.count("sin responder") >= 2)
n = len(TABLAS["cuestionarios"])
client.post("/agenda/cuestionario", data={"telefono": TELW, "instrumento": "todas:inventado"})
chequear("una familia que no existe no crea nada", len(TABLAS["cuestionarios"]) == n)
chequear("familias necesita dos versiones completas", cuestionarios.familias([{"contenido": {"nombre": "X", "items": ["a"]}}]) == {})

def responder_link(c, valores):
    client.post(f"/cuestionario/{c['token']}", data={f"item-{i}": str(v) for i, v in enumerate(valores)})

chico_m = next(c for c in mandados if c["plantilla"]["version"] == "Chico/a")
padres_m = next(c for c in mandados if c["plantilla"]["version"] == "Padres")
responder_link(chico_m, [0] * 41)
responder_link(padres_m, [0] * 41)
client.post("/agenda/cuestionario", data={"telefono": TELW, "instrumento": "todas:scared"})
nuevos = [c for c in TABLAS["cuestionarios"] if c["instrumento"] == "propio" and not c.get("respondido_at")]
alto = [0] * 41
for n_ in M["subescalas"][3]["items"]:
    alto[n_ - 1] = 2
for c in nuevos:
    responder_link(c, alto if c["plantilla"]["version"] == "Chico/a" else [0] * 41)
html = _panel(client)
tabla = html.split("SCARED: versiones lado a lado")[1].split("</table>")[0]
social = tabla.split("Ansiedad social")[1].split("</tr>")[0]
chequear("muestra la toma anterior de cada versión", "antes 0 (↑ 14)" in social and "antes 0" in social and "· antes" in tabla)
chequear("sin cambio no pone flecha", "antes 0</span>" in social)
c_ = cuestionarios.comparar_versiones([
    {"base": "X", "version": "A", "respuestas": [{"_fecha": "b", "puntaje": 3, "detalle": [{"nombre": "S", "puntaje": 3, "corte": 2, "sobre_corte": True}]}]},
    {"base": "X", "version": "B", "respuestas": [{"_fecha": "b", "puntaje": 1, "detalle": [{"nombre": "S", "puntaje": 1, "corte": 2, "sobre_corte": False}]}]}])[0]
chequear("sin toma anterior no inventa", all("antes" not in c for c in c_["filas"][0]["celdas"]) and c_["fechas_antes"] == [None, None])

pid_chico = next(p["id"] for p in TABLAS["plantillas_cuestionario"] if p["contenido"].get("version") == "Chico/a")
html = client.post(f"/agenda/plantillas/{pid_chico}/compartir", data={}, follow_redirects=True).data.decode()
b0 = TABLAS["biblioteca_cuestionarios"][0]
chequear("comparte sin preguntas por defecto", "sin el texto de las preguntas" in html and b0["con_preguntas"] is False
         and b0["contenido"]["items"] == [""] * 41 and b0["contenido"]["subescalas"] and b0["autor"] == "Lic. Marcos")
client.post(f"/agenda/plantillas/{pid_chico}/compartir", data={"con_preguntas": "1"})
chequear("con preguntas pide declarar", len(TABLAS["biblioteca_cuestionarios"]) == 1)
client.post(f"/agenda/plantillas/{pid_chico}/compartir", data={"con_preguntas": "1", "declaro": "1"})
chequear("con declaración comparte el texto", TABLAS["biblioteca_cuestionarios"][1]["contenido"]["items"][0] == "Pregunta 1")
incompleta = next(p["id"] for p in TABLAS["plantillas_cuestionario"] if not cuestionarios.completa(p["contenido"]))
client.post(f"/agenda/plantillas/{incompleta}/compartir", data={"con_preguntas": "1", "declaro": "1"})
chequear("no comparte preguntas incompletas", len(TABLAS["biblioteca_cuestionarios"]) == 2)
client.post("/agenda/plantillas/nada/compartir", data={})
chequear("no comparte lo que no existe", len(TABLAS["biblioteca_cuestionarios"]) == 2)
# otra profesional la ve y la agrega
with client.session_transaction() as ses:
    ses["tenant_id"] = "t2"
    ses["sesion_id"] = _sesion_de("t2")
html = _panel(client)
chequear("la otra profesional ve la biblioteca", "Biblioteca compartida (2)" in html and "compartido por Lic. Marcos" in html and ">Retirar<" not in html)
entrada = TABLAS["biblioteca_cuestionarios"][0]["id"]
antes_t2 = sum(p["tenant_id"] == "t2" for p in TABLAS["plantillas_cuestionario"])
html = client.post(f"/agenda/biblioteca/{entrada}/agregar", data={}, follow_redirects=True).data.decode()
suyas = [p for p in TABLAS["plantillas_cuestionario"] if p["tenant_id"] == "t2"]
chequear("la agrega como copia propia", len(suyas) == antes_t2 + 1 and suyas[-1]["contenido"]["items"] == [""] * 41 and "Falta pegar 41" in html)
client.post(f"/agenda/biblioteca/{entrada}/retirar", data={})
chequear("no puede retirar lo ajeno", any(e["id"] == entrada for e in TABLAS["biblioteca_cuestionarios"]))
with client.session_transaction() as ses:
    ses["tenant_id"] = "t1"
    ses["sesion_id"] = _sesion_de("t1")
client.post(f"/agenda/biblioteca/{entrada}/retirar", data={})
chequear("la autora retira y la copia queda", not any(e["id"] == entrada for e in TABLAS["biblioteca_cuestionarios"]) and len([p for p in TABLAS["plantillas_cuestionario"] if p["tenant_id"] == "t2"]) == antes_t2 + 1)
html = client.post(f"/agenda/biblioteca/{entrada}/agregar", data={}, follow_redirects=True).data.decode()
chequear("lo retirado ya no se agrega", "ya no está en la biblioteca" in html)

# --- Entrega por link, firma en constancias y moderación ---
reset()
TELN = "5491116667778"
TABLAS["turnos"].append(turno("n1", "t1", ahora - timedelta(days=3), estado="reservado", paciente_nombre="Nico Sur", paciente_telefono=TELN,
                              asistencia="vino", nota="Nota de la sesión.", pago="pagado", valor=1000))
html = client.post("/agenda/paciente/ficha-completa/link", data={"telefono": TELN}, follow_redirects=True).data.decode()
ent = TABLAS["entregas"][0] if TABLAS["entregas"] else {}
chequear("crea el link con la ficha guardada", "Listo el link para entregar" in html and "Nota de la sesión." in ent.get("html", "")
         and "Volver al panel" not in ent.get("html", "") and "Generar link" not in ent.get("html", ""))
chequear("la ficha lo lista con su link", f"/entrega/{ent['token']}" in html and "sin abrir" in html)
TABLAS["turnos"][-1]["nota"] = "Cambiada después."
landing = client.get(f"/entrega/{ent['token']}")
chequear("abrir con GET no lo consume", landing.status_code == 200 and "Abrir el documento" in landing.data.decode()
         and "Nota de la sesión." not in landing.data.decode() and not TABLAS["entregas"][0].get("abierta_at")
         and landing.headers.get("Cache-Control") == "no-store")
doc = client.post(f"/entrega/{ent['token']}")
chequear("con POST muestra la copia guardada", "Nota de la sesión." in doc.data.decode() and "Cambiada después." not in doc.data.decode()
         and TABLAS["entregas"][0].get("abierta_at") and doc.headers.get("X-Robots-Tag") == "noindex")
chequear("una sola vez", "ya se abrió" in client.post(f"/entrega/{ent['token']}").data.decode()
         and "ya se abrió" in client.get(f"/entrega/{ent['token']}").data.decode())
html = _panel(client)
chequear("la ficha dice que se abrió", "abierto el" in html)
chequear("link inventado", client.get("/entrega/nada").status_code == 404 and client.post("/entrega/nada").status_code == 404)
TABLAS["entregas"].append({"id": "venc", "tenant_id": "t1", "paciente_telefono": TELN, "ficha": "", "token": "tv2", "html": "secreto",
                           "vence": "2026-01-01T00:00:00+00:00", "created_at": "x"})
chequear("vencido no abre", "venció" in client.post("/entrega/tv2").data.decode() and "secreto" not in client.post("/entrega/tv2").data.decode())
TABLAS["entregas"].append({"id": "ajena", "tenant_id": "t2", "paciente_telefono": TELN, "ficha": "", "token": "ta", "html": "x",
                           "vence": "2099-01-01T00:00:00+00:00", "created_at": "y"})
client.post("/agenda/entrega/ajena/borrar", data={})
chequear("no anula links de otro", any(e["id"] == "ajena" for e in TABLAS["entregas"]))
chequear("ni los muestra", "/entrega/ta\"" not in _panel(client))
client.post("/agenda/entrega/venc/borrar", data={})
chequear("anula los propios", not any(e["id"] == "venc" for e in TABLAS["entregas"]))
chequear("ficha ajena no genera link", "No se encontró la ficha" in client.post("/agenda/paciente/ficha-completa/link", data={"telefono": "nada"}, follow_redirects=True).data.decode())

TABLAS["tenants"][0]["firma_imagen"] = "data:image/png;base64,AAAA"
mes = ahora.strftime("%Y-%m")
const = client.post("/agenda/paciente/constancia", data={"telefono": TELN, "mes": mes}).data.decode()
chequear("la constancia lleva la firma", 'alt="Firma"' in const)
TABLAS["tenants"][0]["firma_imagen"] = None
chequear("sin firma queda la línea", 'alt="Firma"' not in client.post("/agenda/paciente/constancia", data={"telefono": TELN, "mes": mes}).data.decode())

TABLAS["biblioteca_cuestionarios"] += [
    {"id": "b1", "autor_tenant_id": "t2", "autor": "Dra. Otra", "contenido": {**cuestionarios.para_biblioteca({**M, "items": ["x"] * 41}, False)},
     "con_preguntas": False, "oculto": False, "created_at": "1"}]
r = client.post("/agenda/biblioteca/b1/moderar/ocultar", data={})
chequear("sin ser moderador no oculta", r.status_code == 403 and not TABLAS["biblioteca_cuestionarios"][0]["oculto"])
TABLAS["tenants"][0]["moderador"] = True
html = _panel(client)
chequear("el moderador ve el botón", ">Ocultar</button>" in html)
client.post("/agenda/biblioteca/b1/moderar/ocultar", data={})
chequear("oculta y guarda quién", TABLAS["biblioteca_cuestionarios"][0]["oculto"] and TABLAS["biblioteca_cuestionarios"][0]["oculto_por"] == "Lic. Marcos")
html = _panel(client)
chequear("el moderador lo sigue viendo marcado", "oculto por moderación (Lic. Marcos)" in html and ">Mostrar</button>" in html)
TABLAS["tenants"][0]["moderador"] = False
html = _panel(client)
chequear("los demás no lo ven", "Biblioteca compartida (0)" in html)
html = client.post("/agenda/biblioteca/b1/agregar", data={}, follow_redirects=True).data.decode()
chequear("ni lo pueden agregar", "ya no está en la biblioteca" in html)
with client.session_transaction() as ses:
    ses["tenant_id"] = "t2"
    ses["sesion_id"] = _sesion_de("t2")
html = _panel(client)
chequear("la autora lo ve marcado", "oculto por moderación" in html and "(Lic. Marcos)" not in html)
with client.session_transaction() as ses:
    ses["tenant_id"] = "t1"
    ses["sesion_id"] = _sesion_de("t1")
TABLAS["tenants"][0]["moderador"] = True
client.post("/agenda/biblioteca/b1/moderar/mostrar", data={})
chequear("mostrar lo devuelve", not TABLAS["biblioteca_cuestionarios"][0]["oculto"] and TABLAS["biblioteca_cuestionarios"][0]["oculto_por"] is None)
chequear("acción inventada", client.post("/agenda/biblioteca/b1/moderar/borrar", data={}).status_code == 403)
TABLAS["tenants"][0]["moderador"] = False

# --- Administración, aviso de entregas y DNI ---
import bcrypt as _bcrypt  # noqa: E402

reset()
TABLAS["admins"].append({"id": "a1", "usuario": "igna", "pass_hash": _bcrypt.hashpw(b"clave-larga-de-prueba", _bcrypt.gensalt(4)).decode()})
TABLAS["tenants"][1]["panel_user"] = "otra"
TABLAS["turnos"].append(turno("ad1", "t1", ahora - timedelta(days=2), estado="reservado", paciente_nombre="Paciente Secreto", paciente_telefono="5491100000001"))
chequear("el panel de admin pide login", client.get("/admin").status_code == 302 and "/admin/login" in client.get("/admin").headers["Location"])
with client.session_transaction() as ses:
    ses.clear()
r = _post_sin_token("/admin/login", data={"usuario": "igna", "clave": "mala"})
chequear("clave mala no entra", r.status_code == 401)
import seguridad  # noqa: E402

SECRETO = seguridad.nuevo_secreto()
TABLAS["admins"][0]["totp_secreto"] = SECRETO
r = _post_sin_token("/admin/login", data={"usuario": "igna", "clave": "clave-larga-de-prueba"})
chequear("con la clave pide el código", r.status_code == 302 and "/admin/codigo" in r.headers["Location"])
chequear("sin código no hay panel", client.get("/admin").status_code == 302)
with client.session_transaction() as ses:
    ses["csrf_token"] = "tok"
import time as _time  # noqa: E402
r = client.post("/admin/codigo", data={"codigo": seguridad._codigo(SECRETO, int(_time.time() // 30))})
chequear("entra y marca el ingreso", r.status_code == 302 and "/admin" in r.headers["Location"] and TABLAS["admins"][0].get("ultimo_ingreso"))
with client.session_transaction() as ses:
    ses["csrf_token"] = "tok"
    chequear("la sesión de admin no es de un profesional", "tenant_id" not in ses)
html = client.get("/admin").data.decode()
chequear("lista profesionales con su uso", "Lic. Marcos" in html and "Dra. Otra" in html and "Alta de profesional" in html)
chequear("admin: columna de Mercado Pago", "<th>Mercado Pago</th>" in html)
_ahora_adm = datetime.now(timezone.utc).isoformat()
TABLAS["pagos"] += [{"id": "pa1", "tenant_id": "t1", "monto": 30000, "metodo": "mercadopago", "estado": "aprobado", "devuelto": 0, "resuelto": _ahora_adm},
                    {"id": "pa2", "tenant_id": "t1", "monto": 20000, "metodo": "transferencia", "estado": "devuelto", "devuelto": 5000, "resuelto": _ahora_adm},
                    {"id": "pa3", "tenant_id": "t1", "monto": 99000, "metodo": "mercadopago", "estado": "esperando", "devuelto": 0, "resuelto": None},
                    {"id": "pa4", "tenant_id": "t1", "monto": 77000, "metodo": "mercadopago", "estado": "aprobado", "devuelto": 0,
                     "resuelto": "2020-01-05T12:00:00+00:00", "paciente_nombre": "Paciente Secreto"}]
html = client.get("/admin").data.decode()
chequear("admin: cobrado este mes por profesional", "<th>Cobrado este mes</th>" in html and "$ 45.000" in html
         and "MP $ 30.000" in html and "transf. $ 15.000" in html and "devuelto $ 5.000" in html
         and "$ 99.000" not in html and "$ 77.000" not in html, html[html.find("Cobrado"):][:200])
chequear("admin: tablero del mes de toda la plataforma", "Cobrado en la plataforma" in html
         and "<strong style=\"font-size:1.3em\">$ 45.000</strong>" in html and "Profesionales que cobraron" in html
         and "<strong>1</strong> <span class=\"tenue\">de " in html and "Paciente Secreto" not in html, html[html.find("Cobrado en"):][:600])
html = client.get("/admin?cobros=2020-01").data.decode()
chequear("admin: cobrado de otro mes", "<th>Cobrado 2020-01</th>" in html and "$ 77.000" in html and "$ 45.000" not in html
         and "Paciente Secreto" not in html)
chequear("admin: tablero sin mes anterior para comparar", "(el mes anterior: $ 0)" in html)
html = client.get("/admin?cobros=2020-02").data.decode()
chequear("admin: tablero compara contra el mes anterior", "-100% vs. mes anterior" in html, html[html.find("Cobrado en"):][:400])
chequear("admin: gráfico de 6 meses", "últimos 6 meses" in html and "sep 19" in html and "feb 20" in html and "ene 20" in html, html[html.find("6 meses"):][:300])
TABLAS["pagos"] += [{"id": "pd1", "tenant_id": "t1", "monto": 1000, "metodo": "transferencia", "estado": "comprobante",
                     "comprobante_at": "2020-01-01T00:00:00+00:00", "paciente_nombre": "Paciente Secreto"},
                    {"id": "pd2", "tenant_id": "t1", "monto": 1000, "metodo": "transferencia", "estado": "comprobante",
                     "comprobante_at": datetime.now(timezone.utc).isoformat()}]
html = client.get("/admin").data.decode()
chequear("admin: avisa comprobantes sin revisar hace más de 24 h", "sin revisar hace más de 24 h: <strong>Lic. Marcos</strong> (1)" in html
         and "Paciente Secreto" not in html, html[html.find("sin revisar"):][:200])
TABLAS["pagos"] = [p for p in TABLAS["pagos"] if p["id"] not in ("pd1", "pd2")]
chequear("admin: un mes raro cae en el actual", "<th>Cobrado este mes</th>" in client.get("/admin?cobros=nada").data.decode())
_csv = client.get("/admin/uso.csv?mes=2020-01").data.decode("utf-8")
chequear("CSV: trae lo cobrado del mes", "Cobrado web (neto)" in _csv
         and any(l.startswith("Lic. Marcos;") and l.split(";")[9:] == ["1", "77000", "77000", "0", "0"] for l in _csv.splitlines())
         and "Paciente Secreto" not in _csv, _csv[:400])
TABLAS["pagos"] = [p for p in TABLAS["pagos"] if p["id"] not in ("pa1", "pa2", "pa3", "pa4")]
import mail as _mail_adm  # noqa: E402
_mad, _med = _mail_adm.disponible, _mail_adm.enviar_detalle
_mail_adm.disponible = lambda: True
_mails_adm = []
_mail_adm.enviar_detalle = lambda para, asunto, texto: _mails_adm.append(para) or (True, "ok")
chequear("admin: ofrece el mail de prueba", "Mandarme un mail de prueba" in client.get("/admin").data.decode())
html = client.post("/admin/mail/prueba", data={"email": "igna@ejemplo.com"}, follow_redirects=True).data.decode()
chequear("admin: manda el mail de prueba", _mails_adm == ["igna@ejemplo.com"] and "Salió el mail de prueba a ig…@ejemplo.com" in html)
_mail_adm.enviar_detalle = lambda para, asunto, texto: (False, "Resend respondió 403: domain not verified")
html = client.post("/admin/mail/prueba", data={"email": "igna@ejemplo.com"}, follow_redirects=True).data.decode()
chequear("admin: dice por qué no salió el mail", "domain not verified" in html)
html = client.post("/admin/mail/prueba", data={"email": "nada"}, follow_redirects=True).data.decode()
chequear("admin: mail de prueba inválido", "Escribí un mail válido" in html)
_mail_adm.disponible, _mail_adm.enviar_detalle = _mad, _med
chequear("no muestra pacientes", "Paciente Secreto" not in html and "5491100000001" not in html)
chequear("el admin no entra al panel de un profesional", client.get("/agenda").status_code == 302)
_bienvenidas = []
_av_alta = app.scheduler.avisar_profesional
app.scheduler.avisar_profesional = lambda t, texto, **_kw: _bienvenidas.append((t, texto)) or True
html = client.post("/admin/profesionales", data={"nombre": "Lic. Nueva", "rubro": "psicopedagogía", "slug": "nueva", "panel_user": "nueva",
                                                 "clave": "una-clave-larga", "telefono": "11 2345-6789"}, follow_redirects=True).data.decode()
nueva = next((t for t in TABLAS["tenants"] if t.get("panel_user") == "nueva"), None)
chequear("da de alta", "Se creó la cuenta de Lic. Nueva" in html and nueva and nueva["rubro"] == "psicopedagogía"
         and _bcrypt.checkpw(b"una-clave-larga", nueva["panel_pass_hash"].encode()) and "una-clave-larga" not in str(nueva))
chequear("alta: guarda su WhatsApp de avisos y le manda la bienvenida", nueva["aviso_telefono"] == "5491123456789"
         and len(_bienvenidas) == 1 and _bienvenidas[0][0]["aviso_telefono"] == "5491123456789"
         and "panel_pass_hash" not in _bienvenidas[0][0] and "código de vinculación" in _bienvenidas[0][1] and "bienvenida a su WhatsApp" in html)
html = client.post("/admin/profesionales", data={"nombre": "Lic. Sin", "rubro": "psicología", "panel_user": "sinwa",
                                                 "clave": "una-clave-larga", "telefono": "123"}, follow_redirects=True).data.decode()
chequear("alta: sin WhatsApp válido no se crea", "Falta el WhatsApp" in html and not any(t.get("panel_user") == "sinwa" for t in TABLAS["tenants"]))
app.scheduler.avisar_profesional = _av_alta
chequear("alta: el WhatsApp acepta +54 9, 0 11 y 549…", app.whatsapp_de_alta("+54 9 11 2345-6789") == "5491123456789"
         and app.whatsapp_de_alta("011 2345 6789") == "5491123456789" and app.whatsapp_de_alta("5493515550000") == "5493515550000")
for datos, motivo in [({"slug": "admin"}, "ruta del sistema"), ({"slug": "Con Espacios"}, "ruta del sistema"),
                      ({"panel_user": "nueva", "slug": "otra-cosa"}, "Ya existe"), ({"clave": "corta"}, "al menos 10"),
                      ({"rubro": "astrología"}, "obligatorios")]:
    base = {"nombre": "X", "rubro": "psicología", "slug": "", "panel_user": "x" + str(len(TABLAS["tenants"])), "clave": "otra-clave-larga",
            "telefono": "1123456789"}
    html = client.post("/admin/profesionales", data={**base, **datos}, follow_redirects=True).data.decode()
    chequear(f"alta rechazada: {motivo}", motivo in html)
r = _post_sin_token("/admin/profesionales", data={"nombre": "Y", "rubro": "psicología", "panel_user": "y", "clave": "otra-clave-larga"})
chequear("el admin también pide el token", r.status_code == 403)
client.post("/admin/profesionales/t2/desactivar", data={})
chequear("desactiva", TABLAS["tenants"][1]["activo"] is False)
TABLAS["biblioteca_cuestionarios"].append({"id": "bx", "autor_tenant_id": "t1", "autor": "Lic. Marcos", "contenido": {"nombre": "Algo", "items": ["a"]},
                                           "con_preguntas": False, "oculto": False, "created_at": "1"})
client.post("/admin/biblioteca/bx/ocultar", data={})
chequear("el admin modera", TABLAS["biblioteca_cuestionarios"][0]["oculto"] and TABLAS["biblioteca_cuestionarios"][0]["oculto_por"] == "administración (igna)")
r = client.get("/logout")
chequear("salir vuelve al login de admin", "/admin/login" in r.headers["Location"])
# la cuenta desactivada pierde la sesión
with client.session_transaction() as ses:
    ses["tenant_id"] = "t2"
    ses["sesion_id"] = _sesion_de("t2")
    ses["csrf_token"] = "tok"
chequear("cuenta desactivada no entra aunque tenga sesión", client.get("/agenda").status_code == 302)
with client.session_transaction() as ses:
    ses["tenant_id"] = "t1"
    ses["sesion_id"] = _sesion_de("t1")
    ses["csrf_token"] = "tok"
TABLAS["tenants"][1]["activo"] = True

# aviso de entregas abiertas en el resumen
TABLAS["entregas"] = [
    {"id": "e1", "tenant_id": "t1", "paciente_telefono": "1", "ficha": "", "token": "x1", "vence": "2099-01-01T00:00:00+00:00",
     "abierta_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(), "created_at": "1", "html": ""},
    {"id": "e2", "tenant_id": "t1", "paciente_telefono": "2", "ficha": "", "token": "x2", "vence": "2099-01-01T00:00:00+00:00",
     "abierta_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(), "created_at": "2", "html": ""},
    {"id": "e3", "tenant_id": "t2", "paciente_telefono": "3", "ficha": "", "token": "x3", "vence": "2099-01-01T00:00:00+00:00",
     "abierta_at": datetime.now(timezone.utc).isoformat(), "created_at": "3", "html": ""}]
partes = ", ".join(scheduler._partes_del_resumen(TABLAS["tenants"][0], datetime.now(zona).date()))
chequear("el resumen avisa la entrega abierta, sin nombres", "1 ficha entregada se abrió en las últimas 24 horas" in partes)

# DNI como segunda llave
reset()
TELD = "5491119998887"
TABLAS["turnos"].append(turno("d1", "t1", manana, estado="reservado", paciente_nombre="Dani Vera", paciente_telefono=TELD))
html = client.post("/agenda/paciente/ficha-completa/link", data={"telefono": TELD, "dni": "30.123.456"}, follow_redirects=True).data.decode()
e = TABLAS["entregas"][0]
chequear("guarda sólo el hash del DNI", "van a pedir el DNI" in html and e["dni_hash"] and "30123456" not in str(e))
chequear("la ficha dice que pide DNI", "pide DNI" in html)
chequear("el aviso pide el DNI", 'name="dni"' in client.get(f"/entrega/{e['token']}").data.decode())
r = _post_sin_token(f"/entrega/{e['token']}", data={"dni": "11111111"}).data.decode()
chequear("DNI equivocado no abre", "no coincide" in r and "Te quedan 4" in r and "Dani Vera" not in r and not TABLAS["entregas"][0].get("abierta_at"))
r = _post_sin_token(f"/entrega/{e['token']}", data={"dni": "30123456"}).data.decode()
chequear("con el DNI correcto abre", "Ficha de Dani Vera" in r and TABLAS["entregas"][0].get("abierta_at"))
client.post("/agenda/paciente/ficha-completa/link", data={"telefono": TELD, "dni": "301"})
chequear("DNI muy corto no crea link", len(TABLAS["entregas"]) == 1)
client.post("/agenda/paciente/ficha-completa/link", data={"telefono": TELD, "dni": "22333444"})
e2 = TABLAS["entregas"][1]
chequear("el mismo DNI da otro hash en otra entrega", e2["dni_hash"] != app._hash_dni(e["token"], "22333444"))
for _ in range(5):
    r = _post_sin_token(f"/entrega/{e2['token']}", data={"dni": "1"}).data.decode()
chequear("a los 5 intentos se bloquea", "se bloqueó" in r and TABLAS["entregas"][1]["intentos"] == 5)
r = _post_sin_token(f"/entrega/{e2['token']}", data={"dni": "22333444"}).data.decode()
chequear("bloqueado no abre ni con el DNI correcto", "se bloqueó" in r and "Ficha de Dani" not in r)
chequear("la ficha lo muestra bloqueado", "bloqueado por DNI equivocado" in _panel(client))
client.post("/agenda/paciente/ficha-completa/link", data={"telefono": TELD})
chequear("sin DNI abre directo", "Ficha de Dani Vera" in _post_sin_token(f"/entrega/{TABLAS['entregas'][2]['token']}", data={}).data.decode())

# --- Límite de intentos, doble factor y CSV de uso ---
chequear("TOTP: vector de la RFC 6238", seguridad._codigo("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 59 // 30) == "287082"
         and seguridad._codigo("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 1111111109 // 30) == "081804")
sec = seguridad.nuevo_secreto()
paso = seguridad.verificar(sec, seguridad._codigo(sec, 1000), cuando=1000 * 30 + 5)
chequear("TOTP: acepta el código actual", paso == 1000)
chequear("TOTP: un paso de margen", seguridad.verificar(sec, seguridad._codigo(sec, 999), cuando=1000 * 30 + 5) == 999)
chequear("TOTP: no acepta viejos", seguridad.verificar(sec, seguridad._codigo(sec, 990), cuando=1000 * 30 + 5) is None)
chequear("TOTP: no se reusa", seguridad.verificar(sec, seguridad._codigo(sec, 1000), ultimo_paso=1000, cuando=1000 * 30 + 5) is None)
chequear("TOTP: basura", seguridad.verificar(sec, "abc") is None and seguridad.verificar(sec, "") is None)
from datetime import datetime as _dt, timezone as _tz  # noqa: E402
t0 = _dt(2026, 11, 2, 12, 0, tzinfo=_tz.utc)
fila = None
for i in range(4):
    fila = seguridad.tras_fallo(fila, t0)
chequear("4 fallos no bloquean", fila["fallos"] == 4 and not fila["bloqueado_hasta"])
fila = seguridad.tras_fallo(fila, t0)
chequear("el quinto bloquea 15 minutos", fila["bloqueado_hasta"] == (t0 + seguridad.BLOQUEO).isoformat())
chequear("fallos viejos no cuentan", seguridad.tras_fallo({"fallos": 4, "ultimo": (t0 - timedelta(minutes=20)).isoformat()}, t0)["fallos"] == 1)

reset()
db.get_tenant_by_panel_user = lambda u: next((dict(t) for t in TABLAS["tenants"] if t.get("panel_user") == u), None)
TABLAS["tenants"][0]["panel_user"] = "marcos"
TABLAS["tenants"][0]["panel_pass_hash"] = _bcrypt.hashpw(b"clave-buena", _bcrypt.gensalt(4)).decode()
with client.session_transaction() as ses:
    ses.clear()
for _ in range(5):
    r = _post_sin_token("/login", data={"usuario": "marcos", "clave": "mala"})
chequear("5 fallos dejan la cuenta bloqueada", r.status_code == 401
         and any(f["clave"] == "panel:marcos" and f.get("bloqueado_hasta") for f in TABLAS["login_intentos"]))
r = _post_sin_token("/login", data={"usuario": "marcos", "clave": "clave-buena"})
chequear("bloqueada no entra ni con la clave buena", r.status_code == 429 and "Demasiados intentos" in r.data.decode())
r = _post_sin_token("/login", data={"usuario": "otro", "clave": "x"})
chequear("otro usuario desde la misma IP todavía puede", r.status_code == 401)
for _ in range(15):
    r = _post_sin_token("/login", data={"usuario": f"u{_}", "clave": "x"})
chequear("a los 20 fallos se bloquea la IP", _post_sin_token("/login", data={"usuario": "otro2", "clave": "x"}).status_code == 429)
TABLAS["login_intentos"] = []
r = _post_sin_token("/login", data={"usuario": "marcos", "clave": "mala"})
r = _post_sin_token("/login", data={"usuario": "MARCOS", "clave": "clave-buena"})
chequear("mayúsculas cuentan como el mismo usuario", any(f["clave"] == "panel:marcos" for f in TABLAS["login_intentos"]) or r.status_code == 302)
TABLAS["login_intentos"] = []
_post_sin_token("/login", data={"usuario": "marcos", "clave": "mala"})
r = _post_sin_token("/login", data={"usuario": "marcos", "clave": "clave-buena"})
chequear("entrar bien limpia los fallos del usuario", r.status_code == 302 and not any(f["clave"] == "panel:marcos" for f in TABLAS["login_intentos"])
         and TABLAS["tenants"][0].get("ultimo_ingreso"))

# admin nuevo: la clave sola lleva a configurar el doble factor
TABLAS["login_intentos"] = []
TABLAS["admins"] = [{"id": "a2", "usuario": "nuevo", "pass_hash": _bcrypt.hashpw(b"otra-clave-larga", _bcrypt.gensalt(4)).decode()}]
with client.session_transaction() as ses:
    ses.clear()
    ses["csrf_token"] = "tok"
r = _post_sin_token("/admin/login", data={"usuario": "nuevo", "clave": "otra-clave-larga"})
chequear("admin sin doble factor va a configurarlo", "/admin/doble-factor" in r.headers["Location"])
with client.session_transaction() as ses:
    ses["csrf_token"] = "tok"
chequear("sin configurarlo no ve el panel", "/admin/doble-factor" in client.get("/admin").headers["Location"])
pagina = client.get("/admin/doble-factor").data.decode()
with client.session_transaction() as ses:
    nuevo_sec = ses["totp_nuevo"]
chequear("muestra la clave para la app", seguridad.agrupado(nuevo_sec) in pagina and "otpauth://totp/" in pagina)
chequear("todavía no la guarda", not TABLAS["admins"][0].get("totp_secreto"))
html = client.post("/admin/doble-factor", data={"codigo": "000000" if seguridad._codigo(nuevo_sec, int(_time.time() // 30)) != "000000" else "111111"}).data.decode()
chequear("código malo no activa", "no coincide" in html and not TABLAS["admins"][0].get("totp_secreto"))
r = client.post("/admin/doble-factor", data={"codigo": seguridad._codigo(nuevo_sec, int(_time.time() // 30))})
chequear("código bueno activa", TABLAS["admins"][0].get("totp_secreto") == nuevo_sec and client.get("/admin").status_code == 200)
mes = datetime.now(zona).strftime("%Y-%m")
TABLAS["turnos"].append(turno("csv1", "t1", ahora - timedelta(hours=5), estado="reservado", paciente_nombre="Nadie", paciente_telefono="5491100000009"))
r = client.get(f"/admin/uso.csv?mes={mes}")
cuerpo = r.data.decode("utf-8")
chequear("baja el CSV del mes", r.status_code == 200 and f"uso-{mes}.csv" in r.headers["Content-Disposition"] and cuerpo.startswith("\ufeffProfesional;"))
chequear("con el uso de cada cuenta y sin pacientes", "Lic. Marcos;marcos" in cuerpo and "Nadie" not in cuerpo and "5491100000009" not in cuerpo
         and any(l.startswith("Lic. Marcos;") and l.split(";")[5] != "0" for l in cuerpo.splitlines()))
chequear("mes raro cae en el actual", f"uso-{mes}.csv" in client.get("/admin/uso.csv?mes=cualquiera").headers["Content-Disposition"])
with client.session_transaction() as ses:
    ses.clear()
    ses["tenant_id"] = "t1"
    ses["sesion_id"] = _sesion_de("t1")
    ses["csrf_token"] = "tok"
chequear("un profesional no baja el CSV", client.get("/admin/uso.csv").status_code == 302)
# código de admin: reuso y límite
TABLAS["admins"][0]["totp_ultimo_paso"] = None
with client.session_transaction() as ses:
    ses.clear()
    ses["csrf_token"] = "tok"
_post_sin_token("/admin/login", data={"usuario": "nuevo", "clave": "otra-clave-larga"})
with client.session_transaction() as ses:
    ses["csrf_token"] = "tok"
codigo_ya = seguridad._codigo(nuevo_sec, int(_time.time() // 30))
r = client.post("/admin/codigo", data={"codigo": codigo_ya})
chequear("el código entra", r.status_code == 302 and TABLAS["admins"][0]["totp_ultimo_paso"])
with client.session_transaction() as ses:
    ses.clear()
    ses["csrf_token"] = "tok"
_post_sin_token("/admin/login", data={"usuario": "nuevo", "clave": "otra-clave-larga"})
with client.session_transaction() as ses:
    ses["csrf_token"] = "tok"
r = client.post("/admin/codigo", data={"codigo": codigo_ya})
chequear("el mismo código no sirve dos veces", r.status_code == 401 and "ya usado" in r.data.decode())
for _ in range(5):
    r = client.post("/admin/codigo", data={"codigo": "123456"})
chequear("el código también tiene límite", client.post("/admin/codigo", data={"codigo": "123456"}).status_code == 429)

# --- Cambio de clave, aviso de bloqueo y cerrar sesiones ---
reset()
TABLAS["login_intentos"] = []
TABLAS["tenants"][0]["panel_user"] = "marcos"
TABLAS["tenants"][0]["panel_pass_hash"] = _bcrypt.hashpw(b"clave-vieja-1", _bcrypt.gensalt(4)).decode()
otro_cliente = app.app.test_client()
with otro_cliente.session_transaction() as ses:
    ses["tenant_id"] = "t1"
    ses["sesion_id"] = _sesion_de("t1")
with client.session_transaction() as ses:
    ses.clear()
    ses["tenant_id"] = "t1"
    ses["sesion_id"] = _sesion_de("t1")
    ses["csrf_token"] = "tok"
html = _panel(client)
chequear("Ajustes ofrece cambiar la clave", "Cambiar la clave" in html)
for datos, motivo in [({"actual": "mala", "nueva": "clave-nueva-1", "repetir": "clave-nueva-1"}, "no es correcta"),
                      ({"actual": "clave-vieja-1", "nueva": "corta", "repetir": "corta"}, "al menos 10"),
                      ({"actual": "clave-vieja-1", "nueva": "clave-nueva-1", "repetir": "otra-cosa-1"}, "no coinciden"),
                      ({"actual": "clave-vieja-1", "nueva": "clave-vieja-1", "repetir": "clave-vieja-1"}, "distinta")]:
    html = client.post("/agenda/clave", data=datos, follow_redirects=True).data.decode()
    chequear(f"cambio de clave rechazado: {motivo}", motivo in html and _bcrypt.checkpw(b"clave-vieja-1", TABLAS["tenants"][0]["panel_pass_hash"].encode()))
html = client.post("/agenda/clave", data={"actual": "clave-vieja-1", "nueva": "clave-nueva-1", "repetir": "clave-nueva-1"}, follow_redirects=True).data.decode()
chequear("cambia la clave", "cambiaste la clave" in html and _bcrypt.checkpw(b"clave-nueva-1", TABLAS["tenants"][0]["panel_pass_hash"].encode()))
chequear("esta sesión sigue", client.get("/agenda").status_code == 200)
chequear("las otras se cierran", otro_cliente.get("/agenda").status_code == 302)
TABLAS["login_intentos"] = []
for _ in range(5):
    client.post("/agenda/clave", data={"actual": "mala", "nueva": "clave-nueva-2", "repetir": "clave-nueva-2"})
html = client.post("/agenda/clave", data={"actual": "clave-nueva-1", "nueva": "clave-nueva-2", "repetir": "clave-nueva-2"}, follow_redirects=True).data.decode()
chequear("la clave actual tiene el mismo límite de intentos", "Demasiados intentos" in html)
chequear("y el bloqueo se avisa", TABLAS["tenants"][0].get("ultimo_bloqueo"))
html = _panel(client)
chequear("aviso de bloqueo en el panel", "tu cuenta se bloqueó 15 minutos" in html)
partes = ", ".join(scheduler._partes_del_resumen(TABLAS["tenants"][0], datetime.now(zona).date()))
chequear("y en el resumen, sin IP ni detalles", "tu cuenta se bloqueó por claves equivocadas" in partes)
TABLAS["tenants"][0]["ultimo_bloqueo"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
chequear("un bloqueo viejo ya no se muestra", "tu cuenta se bloqueó" not in _panel(client))
TABLAS["login_intentos"] = []
for _ in range(5):
    _post_sin_token("/login", data={"usuario": "marcos", "clave": "mala"})
chequear("el login también marca el bloqueo", datetime.fromisoformat(TABLAS["tenants"][0]["ultimo_bloqueo"]) > datetime.now(timezone.utc) - timedelta(minutes=1))

# cerrar sesiones desde admin
TABLAS["login_intentos"] = []
SEC2 = seguridad.nuevo_secreto()
TABLAS["admins"] = [{"id": "a3", "usuario": "jefe", "pass_hash": _bcrypt.hashpw(b"clave-admin-larga", _bcrypt.gensalt(4)).decode(), "totp_secreto": SEC2}]
profesional = app.app.test_client()
with profesional.session_transaction() as ses:
    ses["tenant_id"] = "t2"
    ses["sesion_id"] = _sesion_de("t2")
chequear("la profesional está adentro", profesional.get("/agenda").status_code == 200)
with client.session_transaction() as ses:
    ses.clear()
    ses["csrf_token"] = "tok"
_post_sin_token("/admin/login", data={"usuario": "jefe", "clave": "clave-admin-larga"})
with client.session_transaction() as ses:
    ses["csrf_token"] = "tok"
client.post("/admin/codigo", data={"codigo": seguridad._codigo(SEC2, int(_time.time() // 30))})
with client.session_transaction() as ses:
    ses["csrf_token"] = "tok"
html = client.post("/admin/profesionales/t2/cerrar-sesiones", data={}, follow_redirects=True).data.decode()
chequear("el admin cierra sus sesiones", "Se cerraron todas las sesiones de Dra. Otra" in html
         and not any(x["tenant_id"] == "t2" for x in TABLAS["sesiones"]))
chequear("la profesional queda afuera", profesional.get("/agenda").status_code == 302)
chequear("cuenta inexistente", client.post("/admin/profesionales/nada/cerrar-sesiones", data={}).status_code == 404)
admin2 = app.app.test_client()
with admin2.session_transaction() as ses:
    ses["admin_id"] = "a3"
html = client.post("/admin/clave", data={"actual": "clave-admin-larga", "nueva": "solo-diez-c", "repetir": "solo-diez-c"}, follow_redirects=True).data.decode()
chequear("la clave de admin pide 12", "al menos 12" in html)
html = client.post("/admin/clave", data={"actual": "clave-admin-larga", "nueva": "clave-admin-nueva", "repetir": "clave-admin-nueva"}, follow_redirects=True).data.decode()
chequear("el admin cambia su clave", "cambiaste la clave" in html and _bcrypt.checkpw(b"clave-admin-nueva", TABLAS["admins"][0]["pass_hash"].encode()))
chequear("y sigue adentro", client.get("/admin").status_code == 200)
chequear("su otra sesión se cierra", admin2.get("/admin").status_code == 302)

# --- Sesiones visibles, link de clave, actividad, doble factor con QR y entrar con QR ---
chequear("dispositivo legible", seguridad.dispositivo("Mozilla/5.0 (Windows NT 10.0) AppleWebKit Chrome/120 Safari/537") == "Chrome en Windows"
         and seguridad.dispositivo("Mozilla/5.0 (iPhone; CPU iPhone OS 17) Version/17 Safari/604") == "Safari en iPhone"
         and seguridad.dispositivo(None) == "Navegador")
svg = seguridad.qr_svg("otpauth://totp/x?secret=ABC")
chequear("el QR sale como SVG en la página", svg.startswith("<svg") and "http" not in svg.split(">")[0].replace("http://www.w3.org", ""))

reset()
TABLAS["login_intentos"] = []
TABLAS["sesiones"] = []
TABLAS["tenants"][0]["panel_user"] = "marcos"
TABLAS["tenants"][0]["panel_pass_hash"] = _bcrypt.hashpw(b"clave-de-marcos", _bcrypt.gensalt(4)).decode()
compu = app.app.test_client()
celu = app.app.test_client()
r = compu.post("/login", data={"usuario": "marcos", "clave": "clave-de-marcos"}, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/120"})
r2 = celu.post("/login", data={"usuario": "marcos", "clave": "clave-de-marcos"}, headers={"User-Agent": "Mozilla/5.0 (iPhone) Version/17 Safari/604"})
chequear("cada login abre una sesión registrada", r.status_code == 302 and r2.status_code == 302 and len(TABLAS["sesiones"]) == 2
         and {x["dispositivo"] for x in TABLAS["sesiones"]} == {"Chrome en Windows", "Safari en iPhone"})
chequear("y queda en la actividad", sum("entró al panel" in a["accion"] for a in TABLAS["actividad"]) == 2)
with compu.session_transaction() as ses:
    ses["csrf_token"] = "tok"
    sid_compu = ses["sesion_id"]
html = _panel(compu)
chequear("Ajustes lista las sesiones", "Tus sesiones abiertas (2)" in html and "Safari en iPhone" in html and "Chrome en Windows" in html and ">esta<" in html)
sid_celu = next(x["id"] for x in TABLAS["sesiones"] if x["id"] != sid_compu)
compu.post(f"/agenda/sesiones/{sid_celu}/cerrar", data={"csrf_token": "tok"})
chequear("cerrar una sesión la saca", celu.get("/agenda").status_code == 302 and compu.get("/agenda").status_code == 200)
TABLAS["sesiones"].append({"id": "ajena", "tenant_id": "t2", "dispositivo": "x", "origen": "clave", "creada": "2026-11-02T00:00:00+00:00", "ultima": "2026-11-02T00:00:00+00:00"})
compu.post("/agenda/sesiones/ajena/cerrar", data={"csrf_token": "tok"})
chequear("no cierra sesiones de otro", any(x["id"] == "ajena" for x in TABLAS["sesiones"]))
celu.post("/login", data={"usuario": "marcos", "clave": "clave-de-marcos"})
compu.post("/agenda/sesiones/cerrar-otras", data={"csrf_token": "tok"})
chequear("cerrar las otras deja ésta", compu.get("/agenda").status_code == 200 and celu.get("/agenda").status_code == 302)
r = compu.post(f"/agenda/sesiones/{sid_compu}/cerrar", data={"csrf_token": "tok"})
chequear("cerrar la propia es salir", "/login" in r.headers["Location"] and compu.get("/agenda").status_code == 302)
viejo = app.app.test_client()
with viejo.session_transaction() as ses:
    ses["tenant_id"] = "t1"  # sesión anterior al cambio, sin fila
chequear("una sesión sin registrar no entra", viejo.get("/agenda").status_code == 302)

# entrar con QR
compu = app.app.test_client()
celu = app.app.test_client()
celu.post("/login", data={"usuario": "marcos", "clave": "clave-de-marcos"})
with celu.session_transaction() as ses:
    ses["csrf_token"] = "tok"
d = compu.post("/login/qr", headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/120"}).get_json()
chequear("la compu recibe un QR", d["svg"].startswith("<svg") and d["token"] and d["segundos"] == 120)
q = TABLAS["qr_logins"][-1]
chequear("del token sólo se guarda el hash", q["token_hash"] != d["token"] and d["token"] not in str(q))
chequear("todavía esperando", compu.get(f"/login/qr/{d['token']}/estado").get_json()["estado"] == "esperando")
intruso = app.app.test_client()
chequear("otro navegador con el mismo link no puede", intruso.get(f"/login/qr/{d['token']}/estado").status_code == 404)
anonimo = app.app.test_client()
r = anonimo.get(f"/login/qr/{d['token']}")
chequear("abrirlo sin sesión pide entrar y vuelve", r.status_code == 302 and "siguiente=/login/qr/" in r.headers["Location"])
pregunta = celu.get(f"/login/qr/{d['token']}").data.decode()
chequear("el celular pregunta y muestra qué pidió", "¿Entrar a tu panel en otra pantalla?" in pregunta and "Chrome en Windows" in pregunta)
chequear("aprobar sin token no vale", _post_sin_token.__self__ is not None and celu.post(f"/login/qr/{d['token']}", data={"respuesta": "aprobar"}).status_code == 403)
html = celu.post(f"/login/qr/{d['token']}", data={"respuesta": "aprobar", "csrf_token": "tok"}).data.decode()
chequear("el celular aprueba", "Listo" in html and TABLAS["qr_logins"][-1]["tenant_id"] == "t1")
e = compu.get(f"/login/qr/{d['token']}/estado").get_json()
chequear("la compu entra", e["estado"] == "listo" and compu.get("/agenda").status_code == 200
         and any(x["origen"] == "QR" and x["dispositivo"] == "Chrome en Windows" for x in TABLAS["sesiones"]))
chequear("el QR no sirve dos veces", compu.get(f"/login/qr/{d['token']}/estado").get_json()["estado"] in ("vencido", "invalido")
         and "ya no sirve" in celu.get(f"/login/qr/{d['token']}").data.decode())
compu2 = app.app.test_client()
d2 = compu2.post("/login/qr").get_json()
celu.post(f"/login/qr/{d2['token']}", data={"respuesta": "rechazar", "csrf_token": "tok"})
chequear("rechazar no deja entrar", compu2.get(f"/login/qr/{d2['token']}/estado").get_json()["estado"] == "esperando"
         and compu2.get("/agenda").status_code == 302)
TABLAS["qr_logins"][-1]["vence"] = "2026-01-01T00:00:00+00:00"
chequear("vencido no se aprueba", "ya no sirve" in celu.get(f"/login/qr/{d2['token']}").data.decode())

# el destino después del login es sólo propio
anon = app.app.test_client()
r = anon.post("/login", data={"usuario": "marcos", "clave": "clave-de-marcos", "siguiente": "https://malo.com"})
chequear("no redirige afuera", r.headers["Location"].endswith("/agenda"))
anon = app.app.test_client()
r = anon.post("/login", data={"usuario": "marcos", "clave": "clave-de-marcos", "siguiente": "//malo.com/x"})
chequear("ni con doble barra", r.headers["Location"].endswith("/agenda"))

# doble factor de la profesional con QR
dfa = app.app.test_client()
dfa.post("/login", data={"usuario": "marcos", "clave": "clave-de-marcos"})
with dfa.session_transaction() as ses:
    ses["csrf_token"] = "tok"
pagina = dfa.get("/agenda/doble-factor").data.decode()
with dfa.session_transaction() as ses:
    sec_m = ses["totp_nuevo"]
chequear("el doble factor muestra un QR", "<svg" in pagina and "escaneá este código" in pagina and seguridad.agrupado(sec_m) in pagina)
dfa.post("/agenda/doble-factor", data={"csrf_token": "tok", "codigo": seguridad._codigo(sec_m, int(_time.time() // 30))})
chequear("se activa con el código", TABLAS["tenants"][0].get("totp_secreto") == sec_m)
nuevo_ingreso = app.app.test_client()
r = nuevo_ingreso.post("/login", data={"usuario": "marcos", "clave": "clave-de-marcos"})
chequear("ahora la clave sola pide código", "/login/codigo" in r.headers["Location"] and nuevo_ingreso.get("/agenda").status_code == 302)
r = nuevo_ingreso.post("/login/codigo", data={"codigo": "000000" if seguridad._codigo(sec_m, int(_time.time() // 30)) != "000000" else "111111"})
chequear("código malo no entra", r.status_code == 401)
TABLAS["tenants"][0]["totp_ultimo_paso"] = None
r = nuevo_ingreso.post("/login/codigo", data={"codigo": seguridad._codigo(sec_m, int(_time.time() // 30))})
chequear("con el código entra", r.status_code == 302 and nuevo_ingreso.get("/agenda").status_code == 200)
html = dfa.post("/agenda/doble-factor/desactivar", data={"csrf_token": "tok", "codigo": "123456"}, follow_redirects=True).data.decode()
chequear("desactivar pide un código válido", "hace falta un código válido" in html and TABLAS["tenants"][0].get("totp_secreto"))
TABLAS["tenants"][0]["totp_ultimo_paso"] = None
dfa.post("/agenda/doble-factor/desactivar", data={"csrf_token": "tok", "codigo": seguridad._codigo(sec_m, int(_time.time() // 30))})
chequear("se desactiva con el código", not TABLAS["tenants"][0].get("totp_secreto"))

# link de clave nueva desde admin
TABLAS["login_intentos"] = []
SEC3 = seguridad.nuevo_secreto()
TABLAS["admins"] = [{"id": "a9", "usuario": "jefa", "pass_hash": _bcrypt.hashpw(b"clave-admin-larga", _bcrypt.gensalt(4)).decode(), "totp_secreto": SEC3}]
adm = app.app.test_client()
adm.post("/admin/login", data={"usuario": "jefa", "clave": "clave-admin-larga"})
with adm.session_transaction() as ses:
    ses["csrf_token"] = "tok"
adm.post("/admin/codigo", data={"csrf_token": "tok", "codigo": seguridad._codigo(SEC3, int(_time.time() // 30))})
with adm.session_transaction() as ses:
    ses["csrf_token"] = "tok"
pagina = adm.post("/admin/profesionales/t1/link-clave", data={"csrf_token": "tok"}).data.decode()
link = re.search(r'value="(https?://[^"]+/clave-nueva/[^"]+)"', pagina).group(1)
token_link = link.rsplit("/", 1)[1]
chequear("el admin genera el link y sólo se guarda el hash", TABLAS["enlaces_clave"] and token_link not in str(TABLAS["enlaces_clave"]))
anon = app.app.test_client()
chequear("abrir el link no lo gasta", "Poné tu clave nueva" in anon.get(f"/clave-nueva/{token_link}").data.decode() and not TABLAS["enlaces_clave"][0].get("usado_at"))
chequear("valida la clave", "al menos 10" in anon.post(f"/clave-nueva/{token_link}", data={"nueva": "corta", "repetir": "corta"}).data.decode())
html = anon.post(f"/clave-nueva/{token_link}", data={"nueva": "clave-recuperada", "repetir": "clave-recuperada"}).data.decode()
chequear("pone la clave nueva y cierra sesiones", "Ya podés entrar" in html and _bcrypt.checkpw(b"clave-recuperada", TABLAS["tenants"][0]["panel_pass_hash"].encode())
         and not any(x["tenant_id"] == "t1" for x in TABLAS["sesiones"]))
chequear("el link sirve una vez", "ya no sirve" in anon.get(f"/clave-nueva/{token_link}").data.decode())
chequear("link inventado", "ya no sirve" in anon.get("/clave-nueva/inventado").data.decode())
chequear("la página de olvido explica qué hacer", "te manda un link" in anon.get("/olvide-la-clave").data.decode()
         and "¿Te olvidaste la clave?" in anon.get("/login").data.decode())

# actividad en admin
html = adm.get("/admin").data.decode()
for texto in ("generó un link para clave nueva de Lic. Marcos", "puso una clave nueva con el link", "aprobó entrar con QR",
              "activó el doble factor", "cerró una de sus sesiones", "entró a administración"):
    chequear(f"actividad: {texto}", texto in html)
html = adm.get("/admin?actividad_de=t2").data.decode()
chequear("filtra por cuenta", "Lic. Marcos" not in html.split("<th>Qué</th>")[1].split("</table>")[0])
chequear("la actividad no tiene datos de pacientes", all("Paciente" not in a["accion"] for a in TABLAS["actividad"]))

# --- Recuperar la clave por mail, limpieza diaria y llaves de acceso ---
import mail as _mail  # noqa: E402

reset()
TABLAS["login_intentos"] = []
TABLAS["tenants"][0].update({"panel_user": "marcos", "email": "marcos@ejemplo.com", "activo": True,
                             "panel_pass_hash": _bcrypt.hashpw(b"clave-de-marcos", _bcrypt.gensalt(4)).decode()})
db.get_tenant_by_panel_user = lambda u: next((dict(t) for t in TABLAS["tenants"] if t.get("panel_user") == u and t.get("activo") is not False), None)
anon = app.app.test_client()
chequear("sin mail configurado, se pide a la administración", "Escribile a quien te dio la cuenta" in anon.get("/olvide-la-clave").data.decode())
mails = []
app.config.RESEND_API_KEY, app.config.MAIL_REMITENTE = "re_prueba", "FiBOT <avisos@ejemplo.com>"
_mail.enviar = lambda para, asunto, texto: mails.append((para, asunto, texto)) or True
chequear("con mail configurado ofrece el formulario", "Mandarme un link" in anon.get("/olvide-la-clave").data.decode())
r1 = anon.post("/olvide-la-clave", data={"dato": "marcos"}).data.decode()
r2 = anon.post("/olvide-la-clave", data={"dato": "nadie"}).data.decode()
chequear("misma respuesta exista o no", "Si hay una cuenta con ese dato" in r1 and "Si hay una cuenta con ese dato" in r2)
chequear("manda un solo mail, a su dirección", len(mails) == 1 and mails[0][0] == "marcos@ejemplo.com" and "/clave-nueva/" in mails[0][2])
anon.post("/olvide-la-clave", data={"dato": "MARCOS@ejemplo.com"})
chequear("también por mail, sin mayúsculas", len(mails) == 2)
tok = mails[0][2].split("/clave-nueva/")[1].split()[0]
chequear("el link del mail funciona", "Poné tu clave nueva" in anon.get(f"/clave-nueva/{tok}").data.decode())
chequear("del token sólo se guarda el hash", tok not in str(TABLAS["enlaces_clave"]))
chequear("el mail no lleva datos de pacientes ni la clave", "Paciente" not in mails[0][2] and "clave-de-marcos" not in mails[0][2])
for _ in range(5):
    anon.post("/olvide-la-clave", data={"dato": "marcos"})
chequear("los pedidos tienen límite", anon.post("/olvide-la-clave", data={"dato": "marcos"}).status_code == 429)
app.config.RESEND_API_KEY = ""
with client.session_transaction() as ses:
    ses["tenant_id"] = "t1"
    ses["sesion_id"] = _sesion_de("t1")
    ses["csrf_token"] = "tok"
html = client.post("/agenda/avisos", data={"email": "no-es-un-mail"}, follow_redirects=True).data.decode()
chequear("valida el mail en Ajustes", "no parece válido" in html and TABLAS["tenants"][0]["email"] == "marcos@ejemplo.com")
client.post("/agenda/avisos", data={"email": "nuevo@ejemplo.com"})
chequear("guarda el mail", TABLAS["tenants"][0]["email"] == "nuevo@ejemplo.com")

# limpieza diaria
viejo, nuevo_ = "2025-01-01T00:00:00+00:00", datetime.now(timezone.utc).isoformat()
TABLAS["qr_logins"] = [{"token_hash": "a", "vence": viejo}, {"token_hash": "b", "vence": "2099-01-01T00:00:00+00:00"}]
TABLAS["enlaces_clave"] = [{"token_hash": "a", "vence": viejo}]
TABLAS["sesiones"] = [x for x in TABLAS["sesiones"]] + [{"id": "vieja", "tenant_id": "t1", "ultima": viejo, "creada": viejo}]
TABLAS["entregas"] = [{"id": "ev", "tenant_id": "t1", "vence": viejo, "html": "copia de la ficha"},
                      {"id": "en", "tenant_id": "t1", "vence": "2099-01-01T00:00:00+00:00", "html": "copia nueva"}]
TABLAS["cuestionarios"] = [{"id": "cv", "tenant_id": "t1", "vence": viejo, "respondido_at": None},
                           {"id": "cr", "tenant_id": "t1", "vence": viejo, "respondido_at": viejo}]
TABLAS["actividad"] = [{"id": 1, "cuando": viejo, "actor": "x", "accion": "vieja"}, {"id": 2, "cuando": nuevo_, "actor": "x", "accion": "nueva"}]
scheduler._ultima_limpieza = None
scheduler._limpiar()
chequear("limpia QR, links y sesiones vencidas", [q["token_hash"] for q in TABLAS["qr_logins"]] == ["b"] and not TABLAS["enlaces_clave"]
         and not any(x["id"] == "vieja" for x in TABLAS["sesiones"]))
chequear("las entregas viejas pierden la copia pero quedan", [e["html"] for e in TABLAS["entregas"]] == ["", "copia nueva"])
chequear("borra links de cuestionario sin responder, nunca respuestas", [c["id"] for c in TABLAS["cuestionarios"]] == ["cr"])
chequear("la actividad de más de un año se va", [a["accion"] for a in TABLAS["actividad"]] == ["nueva"])
TABLAS["qr_logins"].append({"token_hash": "c", "vence": viejo})
scheduler._limpiar()
chequear("una sola vez por día", any(q["token_hash"] == "c" for q in TABLAS["qr_logins"]))

# llaves de acceso (la criptografía la verifica webauthn; acá, el flujo)
from types import SimpleNamespace  # noqa: E402

reset()
TABLAS["tenants"][0].update({"panel_user": "marcos", "activo": True})
celu = app.app.test_client()
with celu.session_transaction() as ses:
    ses["tenant_id"] = "t1"
    ses["sesion_id"] = _sesion_de("t1")
    ses["csrf_token"] = "tok"
o = celu.post("/agenda/llaves/opciones", data={"csrf_token": "tok"}).get_json()
chequear("opciones de registro con verificación del usuario", o["rp"]["id"] == "localhost" and o["authenticatorSelection"]["userVerification"] == "required"
         and o["authenticatorSelection"]["residentKey"] == "required" and o["user"]["name"] == "marcos")
chequear("registrar pide sesión", app.app.test_client().post("/agenda/llaves/opciones").status_code in (302, 403))
recibido = {}
def _verif_reg(**k):
    recibido.update(k)
    return SimpleNamespace(credential_id=b"llave-1", credential_public_key=b"clave-publica", sign_count=0)
app.webauthn.verify_registration_response = _verif_reg
r = celu.post("/agenda/llaves/registrar", data={"csrf_token": "tok", "credencial": "{}"},
              headers={"User-Agent": "Mozilla/5.0 (iPhone) Version/17 Safari/604"}).get_json()
chequear("guarda la llave pública", r["ok"] and TABLAS["llaves_acceso"][0]["tenant_id"] == "t1" and TABLAS["llaves_acceso"][0]["nombre"] == "Safari en iPhone"
         and recibido["require_user_verification"] and recibido["expected_origin"] == "http://localhost")
chequear("el reto se usa una vez", celu.post("/agenda/llaves/registrar", data={"csrf_token": "tok", "credencial": "{}"}).status_code == 400
         or len(TABLAS["llaves_acceso"]) == 1)
app.webauthn.verify_registration_response = lambda **k: (_ for _ in ()).throw(ValueError("firma mala"))
celu.post("/agenda/llaves/opciones", data={"csrf_token": "tok"})
chequear("registro inválido no guarda", celu.post("/agenda/llaves/registrar", data={"csrf_token": "tok", "credencial": "{}"}).status_code == 400
         and len(TABLAS["llaves_acceso"]) == 1)
chequear("Ajustes lista la llave", "Llaves de acceso (1)" in _panel(celu))
llave_id = TABLAS["llaves_acceso"][0]["id"]
compu = app.app.test_client()
o = compu.post("/login/llave/opciones").get_json()
chequear("opciones de login sin pedir usuario", o["userVerification"] == "required" and not o.get("allowCredentials"))
usadas = {}
def _verif_auth(**k):
    usadas.update(k)
    return SimpleNamespace(new_sign_count=5)
app.webauthn.verify_authentication_response = _verif_auth
r = compu.post("/login/llave", data={"credencial": json.dumps({"id": llave_id})}, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/120"}).get_json()
chequear("entra con la llave", r["ok"] and compu.get("/agenda").status_code == 200 and usadas["credential_current_sign_count"] == 0
         and TABLAS["llaves_acceso"][0]["contador"] == 5 and any(x["origen"] == "llave de acceso" for x in TABLAS["sesiones"]))
otro = app.app.test_client()
chequear("sin reto no entra", otro.post("/login/llave", data={"credencial": json.dumps({"id": llave_id})}).status_code == 400)
otro.post("/login/llave/opciones")
chequear("llave desconocida no entra", otro.post("/login/llave", data={"credencial": json.dumps({"id": "inventada"})}).status_code == 400)
otro.post("/login/llave/opciones")
chequear("basura no entra", otro.post("/login/llave", data={"credencial": "no es json"}).status_code == 400)
app.webauthn.verify_authentication_response = lambda **k: (_ for _ in ()).throw(ValueError("firma mala"))
otro.post("/login/llave/opciones")
chequear("firma mala no entra", otro.post("/login/llave", data={"credencial": json.dumps({"id": llave_id})}).status_code == 400
         and otro.get("/agenda").status_code == 302)
app.webauthn.verify_authentication_response = _verif_auth
TABLAS["tenants"][0]["activo"] = False
otro.post("/login/llave/opciones")
chequear("cuenta desactivada no entra con llave", otro.post("/login/llave", data={"credencial": json.dumps({"id": llave_id})}).status_code == 400)
TABLAS["tenants"][0]["activo"] = True
TABLAS["llaves_acceso"].append({"id": "ajena", "tenant_id": "t2", "clave_publica": "x", "contador": 0})
celu.post("/agenda/llaves/ajena/borrar", data={"csrf_token": "tok"})
chequear("no borra llaves de otro", any(x["id"] == "ajena" for x in TABLAS["llaves_acceso"]))
celu.post(f"/agenda/llaves/{llave_id}/borrar", data={"csrf_token": "tok"})
chequear("borra la propia", not any(x["id"] == llave_id for x in TABLAS["llaves_acceso"]))
chequear("la actividad registra las llaves", any("agregó una llave de acceso" in a["accion"] for a in TABLAS["actividad"])
         and any("entró al panel (llave de acceso" in a["accion"] for a in TABLAS["actividad"]))

# --- Llaves de administración, avisos por mail y dominio principal ---
reset()
TABLAS["login_intentos"] = []
SEC4 = seguridad.nuevo_secreto()
TABLAS["admins"] = [{"id": "a7", "usuario": "igna", "pass_hash": _bcrypt.hashpw(b"clave-admin-larga", _bcrypt.gensalt(4)).decode(), "totp_secreto": SEC4}]
adm = app.app.test_client()
adm.post("/admin/login", data={"usuario": "igna", "clave": "clave-admin-larga"})
with adm.session_transaction() as ses:
    ses["csrf_token"] = "tok"
adm.post("/admin/codigo", data={"csrf_token": "tok", "codigo": seguridad._codigo(SEC4, int(_time.time() // 30))})
with adm.session_transaction() as ses:
    ses["csrf_token"] = "tok"
html = adm.get("/admin").data.decode()
chequear("admin ofrece agregar llave", "llaves de acceso" in html and "/admin/llaves/opciones" in html)
chequear("la llave está arriba, antes de la lista de profesionales", html.index("Agregar llave en este dispositivo") < html.index("<h3>Profesionales"))
o = adm.post("/admin/llaves/opciones", data={"csrf_token": "tok"}).get_json()
chequear("opciones de llave para admin", o["user"]["name"] == "igna" and o["authenticatorSelection"]["userVerification"] == "required")
app.webauthn.verify_registration_response = lambda **k: SimpleNamespace(credential_id=b"llave-admin", credential_public_key=b"pub", sign_count=0)
r = adm.post("/admin/llaves/registrar", data={"csrf_token": "tok", "credencial": "{}"}).get_json()
chequear("guarda la llave del admin aparte", r["ok"] and TABLAS["admin_llaves"][0]["admin_id"] == "a7" and not TABLAS["llaves_acceso"])
lid = TABLAS["admin_llaves"][0]["id"]
chequear("una profesional no registra llaves de admin", app.app.test_client().post("/admin/llaves/opciones").status_code in (302, 403))
nuevo_adm = app.app.test_client()
chequear("el login de admin ofrece la llave", "/admin/login/llave/opciones" in nuevo_adm.get("/admin/login").data.decode())
nuevo_adm.post("/admin/login/llave/opciones")
app.webauthn.verify_authentication_response = lambda **k: SimpleNamespace(new_sign_count=3)
r = nuevo_adm.post("/admin/login/llave", data={"credencial": json.dumps({"id": lid})}).get_json()
chequear("entra a admin con la llave, sin código", r["ok"] and r["destino"].endswith("/admin") and nuevo_adm.get("/admin").status_code == 200
         and TABLAS["admin_llaves"][0]["contador"] == 3)
TABLAS["llaves_acceso"].append({"id": "llave-de-profesional", "tenant_id": "t1", "clave_publica": "x", "contador": 0, "creada": "2026-11-01T00:00:00+00:00"})
otro_adm = app.app.test_client()
otro_adm.post("/admin/login/llave/opciones")
chequear("la llave de una profesional no entra a admin", otro_adm.post("/admin/login/llave", data={"credencial": json.dumps({"id": "llave-de-profesional"})}).status_code == 400
         and otro_adm.get("/admin").status_code == 302)
pro = app.app.test_client()
pro.post("/login/llave/opciones")
chequear("ni la de admin entra al panel de profesional", pro.post("/login/llave", data={"credencial": json.dumps({"id": lid})}).status_code == 400)
adm.post(f"/admin/llaves/{lid}/borrar", data={"csrf_token": "tok"})
chequear("borra la llave de admin", not TABLAS["admin_llaves"])

# avisos por mail a la profesional
mails = []
_mail.enviar = lambda para, asunto, texto: mails.append((para, asunto, texto)) or True
app.config.RESEND_API_KEY, app.config.MAIL_REMITENTE = "re_prueba", "FiBOT <avisos@ejemplo.com>"
TABLAS["tenants"][0].update({"email": "marcos@ejemplo.com", "panel_user": "marcos",
                             "panel_pass_hash": _bcrypt.hashpw(b"clave-de-marcos", _bcrypt.gensalt(4)).decode()})
pc = app.app.test_client()
with pc.session_transaction() as ses:
    ses["tenant_id"] = "t1"
    ses["sesion_id"] = _sesion_de("t1")
    ses["csrf_token"] = "tok"
pc.post("/agenda/llaves/opciones", data={"csrf_token": "tok"})
pc.post("/agenda/llaves/registrar", data={"csrf_token": "tok", "credencial": "{}"}, headers={"User-Agent": "Mozilla/5.0 (iPhone) Version/17 Safari/604"})
chequear("avisa por mail la llave nueva", mails and mails[-1][0] == "marcos@ejemplo.com" and "llave de acceso" in mails[-1][1] and "Safari en iPhone" in mails[-1][2]
         and "Si no fuiste vos" in mails[-1][2])
pc.post("/agenda/clave", data={"csrf_token": "tok", "actual": "clave-de-marcos", "nueva": "clave-nueva-larga", "repetir": "clave-nueva-larga"})
chequear("avisa el cambio de clave", "Se cambió la clave" in mails[-1][1] or "se cambió la clave" in mails[-1][1])
pagina = pc.get("/agenda/doble-factor")
with pc.session_transaction() as ses:
    s5 = ses["totp_nuevo"]
pc.post("/agenda/doble-factor", data={"csrf_token": "tok", "codigo": seguridad._codigo(s5, int(_time.time() // 30))})
chequear("avisa el doble factor", "doble factor" in mails[-1][1])
chequear("los mails no llevan clave ni datos de pacientes", not any("clave-nueva-larga" in m[2] or "Paciente" in m[2] for m in mails))
n = len(mails)
TABLAS["tenants"][0]["email"] = None
pc.post("/agenda/llaves/opciones", data={"csrf_token": "tok"})
pc.post("/agenda/llaves/registrar", data={"csrf_token": "tok", "credencial": "{}"})
chequear("sin mail cargado no manda nada", len(mails) == n)
app.config.RESEND_API_KEY = ""

# dominio principal
app.config.DOMINIO_PRINCIPAL = "consultorio.fibot.ar"
r = app.app.test_client().get("/login?x=1", headers={"Host": "fibot-consultorio-production.up.railway.app"})
chequear("la dirección vieja redirige al dominio", r.status_code == 308 and r.headers["Location"] == "https://consultorio.fibot.ar/login?x=1")
r = app.app.test_client().post("/login", headers={"Host": "otra.cosa"})
chequear("un POST también (308)", r.status_code == 308)
r = app.app.test_client().get("/webhook?hub.mode=subscribe", headers={"Host": "fibot-consultorio-production.up.railway.app"})
chequear("el webhook de Meta no se redirige", r.status_code != 308)
r = app.app.test_client().get("/login", headers={"Host": "consultorio.fibot.ar"})
chequear("en el dominio principal no redirige", r.status_code == 200)
app.config.DOMINIO_PRINCIPAL = ""
chequear("sin dominio cargado no redirige", app.app.test_client().get("/login").status_code == 200)

# --- QR para entrar a administración y página de estado ---
reset()
TABLAS["login_intentos"] = []
SEC6 = seguridad.nuevo_secreto()
TABLAS["admins"] = [{"id": "a8", "usuario": "igna", "pass_hash": _bcrypt.hashpw(b"clave-admin-larga", _bcrypt.gensalt(4)).decode(), "totp_secreto": SEC6}]
celu_adm = app.app.test_client()
with celu_adm.session_transaction() as ses:
    ses["admin_id"] = "a8"
    ses["admin_version"] = 0
    ses["csrf_token"] = "tok"
compu_adm = app.app.test_client()
chequear("el login de admin ofrece el QR", "Entrar con el celular" in compu_adm.get("/admin/login").data.decode())
d = compu_adm.post("/admin/login/qr", headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/120"}).get_json()
chequear("la compu recibe un QR de admin", d["svg"].startswith("<svg") and "/admin/login/qr/" in d["estado"] and TABLAS["qr_logins"][-1]["tipo"] == "admin")
chequear("esperando", compu_adm.get(d["estado"]).get_json()["estado"] == "esperando")
chequear("otro navegador no puede", app.app.test_client().get(d["estado"]).status_code == 404)
anon = app.app.test_client()
r = anon.get(f"/admin/qr/{d['token']}")
chequear("sin sesión de admin pide entrar y vuelve", r.status_code == 302 and "siguiente=/admin/qr/" in r.headers["Location"])
pro = app.app.test_client()
with pro.session_transaction() as ses:
    ses["tenant_id"] = "t1"
    ses["sesion_id"] = _sesion_de("t1")
    ses["csrf_token"] = "tok"
chequear("una profesional no aprueba un QR de admin", "/admin/login" in pro.get(f"/admin/qr/{d['token']}").headers.get("Location", ""))
pro.post(f"/login/qr/{d['token']}", data={"csrf_token": "tok", "respuesta": "aprobar"})
chequear("ni por la puerta del panel", not TABLAS["qr_logins"][-1].get("aprobado_at"))
pregunta = celu_adm.get(f"/admin/qr/{d['token']}").data.decode()
chequear("el celular de admin pregunta", "¿Entrar a administración en otra pantalla?" in pregunta and "Chrome en Windows" in pregunta)
chequear("aprobar pide token", celu_adm.post(f"/admin/qr/{d['token']}", data={"respuesta": "aprobar"}).status_code == 403)
celu_adm.post(f"/admin/qr/{d['token']}", data={"csrf_token": "tok", "respuesta": "aprobar"})
e = compu_adm.get(d["estado"]).get_json()
chequear("la compu entra a admin", e["estado"] == "listo" and e["destino"].endswith("/admin") and compu_adm.get("/admin").status_code == 200)
chequear("no sirve dos veces", compu_adm.get(d["estado"]).get_json()["estado"] in ("vencido", "invalido"))
compu_panel = app.app.test_client()
dp = compu_panel.post("/login/qr").get_json()
chequear("un QR del panel no se aprueba desde admin", "ya no sirve" in celu_adm.get(f"/admin/qr/{dp['token']}").data.decode())
chequear("ni se consulta como admin", compu_panel.get(f"/admin/login/qr/{dp['token']}/estado").status_code == 404)
# volver a aprobar después de entrar con clave y código
anon2 = app.app.test_client()
d2 = app.app.test_client().post("/admin/login/qr").get_json()
anon2.post(f"/admin/login?siguiente=/admin/qr/{d2['token']}", data={"usuario": "igna", "clave": "clave-admin-larga", "siguiente": f"/admin/qr/{d2['token']}"})
with anon2.session_transaction() as ses:
    ses["csrf_token"] = "tok"
r = anon2.post("/admin/codigo", data={"csrf_token": "tok", "codigo": seguridad._codigo(SEC6, int(_time.time() // 30))})
chequear("después del código vuelve al QR", r.headers["Location"].endswith(f"/admin/qr/{d2['token']}"))
anon3 = app.app.test_client()
anon3.post("/admin/login", data={"usuario": "igna", "clave": "clave-admin-larga", "siguiente": "https://malo.com"})
with anon3.session_transaction() as ses:
    ses["csrf_token"] = "tok"
TABLAS["admins"][0]["totp_ultimo_paso"] = None
r = anon3.post("/admin/codigo", data={"csrf_token": "tok", "codigo": seguridad._codigo(SEC6, int(_time.time() // 30) + 1)})
chequear("nunca vuelve a un sitio de afuera", r.headers["Location"].endswith("/admin"))

# página de estado
html = celu_adm.get("/admin").data.decode()
estado_html = html.split("<h3>Estado</h3>")[1].split("</div>\n\n")[0] if "<h3>Estado</h3>" in html else ""
for texto in ("Dominio", "Falta cargar DOMINIO_PRINCIPAL", "Mail", "Sin proveedor", "WhatsApp", "Recordatorios y limpieza",
              "Respaldo", "Supabase", "Profesionales", "sin mail cargado"):
    chequear(f"estado: {texto}", texto in html)
chequear("el estado nunca muestra valores de variables", "re_prueba" not in html and "fake_signature" not in html)
app.config.DOMINIO_PRINCIPAL = "localhost"
chequear("con el dominio cargado lo marca bien", "Dominio principal: localhost" in celu_adm.get("/admin").data.decode())
app.config.DOMINIO_PRINCIPAL = ""

# --- WhatsApp en administración: último mensaje, lista de pasos y prueba ---
import whatsapp as _wa  # noqa: E402

TABLAS["eventos_plataforma"] = []
html = celu_adm.get("/admin").data.decode()
chequear("estado: último mensaje nunca", "Último mensaje recibido: nunca" in html and "Revisar WhatsApp" in html)
db.marcar_evento("mensaje", "PN1")
db.marcar_evento("mensaje", "PN1")
chequear("un solo registro por clave", len(TABLAS["eventos_plataforma"]) == 1)
chequear("estado: último mensaje hace poco", "Último mensaje recibido: hace 0 min" in celu_adm.get("/admin").data.decode())


class _R:
    def __init__(self, codigo, datos):
        self.status_code, self._d, self.text = codigo, datos, str(datos)

    def json(self):
        return self._d


def _get_meta(url, headers=None, params=None, timeout=None):
    if url.endswith("/subscribed_apps"):
        return _R(200, {"data": [{"whatsapp_business_api_data": {"id": "999", "name": "WA DevX Webhook Events 1P App"}}]})
    if url.endswith("/app"):
        return _R(200, {"id": "123", "name": "FiBOT - Consultorio"})
    if url.endswith("/message_templates"):
        return _R(200, {"data": [{"name": "recordatorio_turno", "status": "APPROVED", "language": "es_AR"},
                                 {"name": "turno_reprogramado", "status": "PENDING", "language": "es_AR"}]})
    return _R(200, {"display_phone_number": "+1 555 000", "verified_name": "FiBOT", "quality_rating": "GREEN"})


enviados = []
_wa.requests.get = _get_meta
_wa.requests.post = lambda url, headers=None, json=None, timeout=None: enviados.append(json) or _R(200, {})
_cfg = _wa.config
viejos = (_cfg.META_ACCESS_TOKEN, _cfg.PLATAFORMA_PHONE_NUMBER_ID, _cfg.META_WABA_ID, _cfg.RECORDATORIO_TEMPLATE,
          _cfg.REPROGRAMADO_TEMPLATE, _cfg.WHATSAPP_SOLO_A)
_cfg.META_ACCESS_TOKEN, _cfg.PLATAFORMA_PHONE_NUMBER_ID, _cfg.META_WABA_ID = "token-real-de-prueba", "PN1", "WABA1"
_cfg.RECORDATORIO_TEMPLATE, _cfg.REPROGRAMADO_TEMPLATE, _cfg.WHATSAPP_SOLO_A = "recordatorio_turno", "turno_reprogramado", ["5491122223333"]
html = celu_adm.get("/admin/whatsapp").data.decode()
chequear("pasos: número", "+1 555 000" in html and "calidad alta" in html)
chequear("pasos: sin suscripción y cómo arreglarlo", "«FiBOT - Consultorio» NO está suscripta" in html and "Tocá «Suscribir la app»" in html)
chequear("pasos: plantillas", "Recordatorio (recordatorio_turno): aprobada" in html and "Reprogramado (turno_reprogramado): en revisión" in html
         and "Sólo falta que Meta las apruebe" in html)
chequear("pasos: freno", "terminados en …3333" in html)
chequear("la página nunca muestra el token", "token-real-de-prueba" not in html)
chequear("la prueba pide CSRF", celu_adm.post("/admin/whatsapp/prueba", data={"telefono": "5491122223333"}).status_code in (400, 403))
celu_adm.post("/admin/whatsapp/prueba", data={"csrf_token": "tok", "telefono": "+54 9 11 9999-0000"})
html = celu_adm.get("/admin/whatsapp").data.decode()
chequear("prueba frenada", "no está en el freno de envíos" in html and not enviados)
celu_adm.post("/admin/whatsapp/prueba", data={"csrf_token": "tok", "telefono": "5491122223333"})
html = celu_adm.get("/admin/whatsapp").data.decode()
chequear("admin whatsapp: cómo crear cada plantilla, con botones y URL dinámica",
         "Cómo crear cada plantilla" in html and "PAGO_RECHAZADO_TEMPLATE" in html and "Sí, confirmo" in html
         and "https://consultorio.fibot.ar/pago/{{1}}" in html and "PAGO_PENDIENTE_BOTON=1" in html.replace("</code>", "").replace("<code>", ""))
chequear("prueba aceptada", "Meta aceptó «hello_world»" in html and enviados[-1]["template"] == {"name": "hello_world", "language": {"code": "en_US"}})
celu_adm.post("/admin/whatsapp/prueba", data={"csrf_token": "tok", "telefono": "5491122223333", "plantilla": "recordatorio_turno"})
chequear("prueba con el recordatorio lleva parámetros", len(enviados[-1]["template"]["components"][0]["parameters"]) == 2)
_wa.requests.post = lambda url, headers=None, json=None, timeout=None: _R(400, {"error": {"message": "x", "code": 131030, "error_data": {"details": "No autorizado"}}})
celu_adm.post("/admin/whatsapp/prueba", data={"csrf_token": "tok", "telefono": "5491122223333", "plantilla": "otra_cualquiera"})
html = celu_adm.get("/admin/whatsapp").data.decode()
chequear("rechazo de Meta con detalle", "Meta lo rechazó: No autorizado (código 131030)" in html)
chequear("la prueba queda en la actividad", any("WhatsApp de prueba" in a["accion"] for a in TABLAS["actividad"]))
# suscribir la app
import hashlib  # noqa: E402
import hmac  # noqa: E402

suscripciones = []
_wa.requests.post = lambda url, headers=None, json=None, timeout=None: suscripciones.append(url) or _R(200, {"success": True})
chequear("suscribir pide CSRF", celu_adm.post("/admin/whatsapp/suscribir").status_code in (400, 403) and not suscripciones)
celu_adm.post("/admin/whatsapp/suscribir", data={"csrf_token": "tok"})
chequear("suscribe a la cuenta", suscripciones == [f"https://graph.facebook.com/{_cfg.META_GRAPH_VERSION}/WABA1/subscribed_apps"]
         and "Meta confirmó la suscripción" in celu_adm.get("/admin/whatsapp").data.decode())
_wa.requests.post = lambda url, headers=None, json=None, timeout=None: _R(400, {"error": {"message": "x", "code": 100}})
celu_adm.post("/admin/whatsapp/suscribir", data={"csrf_token": "tok"})
chequear("si Meta rechaza, lo dice", "Meta no la suscribió: x (código 100)" in celu_adm.get("/admin/whatsapp").data.decode())

# ajustes desde administración
TABLAS["ajustes_plataforma"] = []
_wa._DE_RAILWAY.update({"phone_number_id": "PN_RAILWAY", "waba_id": "", "solo_a": "", "aviso_admin": ""})
celu_adm.post("/admin/whatsapp/ajustes", data={"csrf_token": "tok", "phone_number_id": "PN 777", "waba_id": "888",
                                               "solo_a": "+54 9 11 2222-3333, 5491144445555", "aviso_admin": "5491122223333"})
chequear("ajustes: se aplican sin deploy", _cfg.PLATAFORMA_PHONE_NUMBER_ID == "777" and _cfg.META_WABA_ID == "888"
         and _cfg.WHATSAPP_SOLO_A == ["5491122223333", "5491144445555"] and _wa.aviso_admin == "5491122223333")
chequear("ajustes: quedan guardados", {f["clave"]: f["valor"] for f in TABLAS["ajustes_plataforma"]}["phone_number_id"] == "777")
html = celu_adm.get("/admin/whatsapp").data.decode()
chequear("ajustes: dice de dónde viene cada uno", "hoy viene de administración" in html)
_cfg.PLATAFORMA_PHONE_NUMBER_ID = "otro"
_wa.cargar_ajustes()
chequear("ajustes: se cargan al arrancar", _cfg.PLATAFORMA_PHONE_NUMBER_ID == "777")
celu_adm.post("/admin/whatsapp/ajustes", data={"csrf_token": "tok", "solo_a": "123"})
chequear("ajustes: teléfono incompleto no se guarda", "Los teléfonos van completos" in celu_adm.get("/admin/whatsapp").data.decode()
         and _cfg.PLATAFORMA_PHONE_NUMBER_ID == "777")
celu_adm.post("/admin/whatsapp/ajustes", data={"csrf_token": "tok"})
chequear("ajustes: vacío vuelve a Railway", _cfg.PLATAFORMA_PHONE_NUMBER_ID == "PN_RAILWAY" and _cfg.WHATSAPP_SOLO_A == []
         and _wa.origen("phone_number_id") == "Railway" and _wa.origen("waba_id") == "")
chequear("ajustes: quedan en la actividad", any("cambió los ajustes de WhatsApp" in a["accion"] for a in TABLAS["actividad"]))

# aviso de Meta: plantilla aprobada o rechazada
TABLAS["plantillas_meta"] = []
avisos_wa = []
_wa.aplicar_ajustes({"phone_number_id": "PN1", "aviso_admin": "5491122223333"})
_wa.requests.post = lambda url, headers=None, json=None, timeout=None: avisos_wa.append(json) or _R(200, {})
# enviar_texto está parcheado más arriba en este archivo: acá se captura igual.
_wa.enviar_texto = lambda n, to, texto: avisos_wa.append({"to": to, "texto": texto}) or True


def _webhook(cuerpo):
    datos = json.dumps(cuerpo).encode()
    firma = "sha256=" + hmac.new(_cfg.META_APP_SECRET.encode(), datos, hashlib.sha256).hexdigest()
    return app.app.test_client().post("/webhook", data=datos, content_type="application/json",
                                      headers={"X-Hub-Signature-256": firma})


r = _webhook({"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [
    {"field": "message_template_status_update", "value": {"event": "REJECTED", "message_template_name": "recordatorio_turno",
                                                          "message_template_language": "es_AR", "reason": "INCORRECT_CATEGORY"}}]}]})
chequear("aviso de plantilla: 200", r.status_code == 200)
chequear("aviso de plantilla: se guarda", TABLAS["plantillas_meta"][0]["estado"] == "rechazada"
         and TABLAS["plantillas_meta"][0]["motivo"] == "incorrect category")
chequear("aviso de plantilla: le llega a administración", avisos_wa and avisos_wa[-1]["to"] == "5491122223333"
         and "recordatorio_turno" in json.dumps(avisos_wa[-1], ensure_ascii=False) and "rechazada" in json.dumps(avisos_wa[-1], ensure_ascii=False))
_webhook({"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [
    {"field": "message_template_status_update", "value": {"event": "APPROVED", "message_template_name": "recordatorio_turno",
                                                          "message_template_language": "es_AR", "reason": "NONE"}}]}]})
chequear("aviso de plantilla: un registro por plantilla", len(TABLAS["plantillas_meta"]) == 1 and TABLAS["plantillas_meta"][0]["estado"] == "aprobada"
         and TABLAS["plantillas_meta"][0]["motivo"] is None)
_webhook({"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [
    {"field": "template_category_update", "value": {"message_template_name": "resumen_consultorio", "message_template_language": "es_AR",
                                                    "previous_category": "UTILITY", "new_category": "MARKETING"}}]}]})
html = celu_adm.get("/admin/whatsapp").data.decode()
chequear("aviso de categoría: en la página", "resumen_consultorio" in html and "pasó a marketing" in html and "antes era utility" in html)
chequear("avisos: quedan en la actividad", any(a["actor"] == "Meta" and "aprobada" in a["accion"] for a in TABLAS["actividad"]))
# alguien sin turno le escribe al número compartido
TABLAS["mensajes_sin_destino"] = []
avisos_wa.clear()
_viejo_por_numero, _viejo_por_paciente = db.get_tenant_by_phone_number_id, db.tenant_por_paciente
db.get_tenant_by_phone_number_id = lambda pid: None
db.tenant_por_paciente = lambda tel: None
_msg = lambda: {"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [{"field": "messages", "value": {
    "metadata": {"phone_number_id": "PN1"}, "messages": [{"from": "5491177778888", "type": "text", "text": {"body": "hola"}}]}}]}]}
_webhook(_msg())
_webhook(_msg())
chequear("sin turno: se le contesta una sola vez", len(avisos_wa) == 1 and avisos_wa[0]["to"] == "5491177778888"
         and "No encontramos un turno" in avisos_wa[0]["texto"])
chequear("sin turno: queda en la bandeja sin el número entero", len(TABLAS["mensajes_sin_destino"]) == 2
         and all(m["final"] == "8888" and "77778888" not in json.dumps(m) for m in TABLAS["mensajes_sin_destino"]))
html = celu_adm.get("/admin/whatsapp").data.decode()
chequear("sin turno: se ve en administración", "Escribieron sin turno" in html and "…8888" in html and "5491177778888" not in html
         and "ya se le había contestado hoy" in html)

# link a la página pública y texto editable
for t in TABLAS["tenants"]:
    t["activo"] = True
TABLAS["tenants"][0]["slug"], TABLAS["tenants"][1]["slug"] = "marcos", None
TABLAS["mensajes_sin_destino"] = []
avisos_wa.clear()
db.get_tenant_by_phone_number_id = lambda pid: None
db.tenant_por_paciente = lambda tel: None
_webhook(_msg())
chequear("sin turno: con un solo profesional en el número, va el link", avisos_wa and "/marcos" in avisos_wa[-1]["texto"])
TABLAS["tenants"][1]["slug"] = "otra"
TABLAS["mensajes_sin_destino"] = []
_webhook(_msg())
chequear("sin turno: con dos, «la página que te pasó tu profesional» (no hay lista)", "/marcos" not in avisos_wa[-1]["texto"]
         and "/turnos" not in avisos_wa[-1]["texto"] and "la página que te pasó tu profesional" in avisos_wa[-1]["texto"])
celu_adm.post("/admin/whatsapp/respuesta", data={"csrf_token": "tok", "texto": "Hola! Reservá en {link} 🙂"})
TABLAS["mensajes_sin_destino"] = []
TABLAS["tenants"][1]["slug"] = None
_webhook(_msg())
chequear("sin turno: usa el texto guardado con el link", avisos_wa[-1]["texto"].startswith("Hola! Reservá en http") and "/marcos" in avisos_wa[-1]["texto"])
html = celu_adm.get("/admin/whatsapp").data.decode()
chequear("sin turno: el texto se ve y se edita", "Hola! Reservá en {link}" in html and "/marcos 🙂" in html)
celu_adm.post("/admin/whatsapp/respuesta", data={"csrf_token": "tok", "texto": "algo", "fabrica": "1"})
chequear("sin turno: volver al de fábrica", "No encontramos un turno" in celu_adm.get("/admin/whatsapp").data.decode()
         and any("respuesta de fábrica" in a["accion"] for a in TABLAS["actividad"]))
# la página de turnos en el panel
panel_t1 = app.app.test_client()
with panel_t1.session_transaction() as ses:
    ses["tenant_id"] = "t1"
    ses["sesion_id"] = _sesion_de("t1")
    ses["csrf_token"] = "tok"
html = _panel(panel_t1)
chequear("panel: muestra su página de turnos para compartir", "Tu página de turnos" in html and "/marcos" in html and "wa.me" in html)
r = panel_t1.get("/agenda/mi-pagina-qr.svg")
chequear("panel: QR de su página", r.status_code == 200 and r.mimetype == "image/svg+xml" and b"<svg" in r.data)
TABLAS["tenants"][0]["slug"] = None
chequear("panel: sin página, no hay tarjeta ni QR", "Tu página de turnos" not in _panel(panel_t1)
         and panel_t1.get("/agenda/mi-pagina-qr.svg").status_code == 404)
db.get_tenant_by_phone_number_id, db.tenant_por_paciente = _viejo_por_numero, _viejo_por_paciente

# simulador
chequear("simulador pide CSRF", celu_adm.post("/admin/whatsapp/simular", data={"tenant_id": "t1", "texto": "hola"}).status_code in (400, 403))
avisos_wa.clear()
celu_adm.post("/admin/whatsapp/simular", data={"csrf_token": "tok", "tenant_id": "t1", "texto": "hola"})
html = celu_adm.get("/admin/whatsapp").data.decode()
chequear("simulador: muestra la charla", "Charla con el bot de" in html and 'burbuja paciente">hola' in html and 'burbuja bot' in html)
chequear("simulador: no manda WhatsApp", not avisos_wa)
celu_adm.post("/admin/whatsapp/simular", data={"csrf_token": "tok", "tenant_id": "t1", "texto": "cancelar"})
chequear("simulador: sigue la charla", celu_adm.get("/admin/whatsapp").data.decode().count('burbuja paciente') == 2)
chequear("simulador: queda en la actividad", any("con el simulador" in a["accion"] for a in TABLAS["actividad"]))
_cancelados = []
_viejo_prox, _viejo_cancelar = db.proximos_turnos_de, db.cancelar_turno
_pendientes = [[{"id": "x1"}, {"id": "x2"}], []]
db.proximos_turnos_de = lambda tid, tel: _pendientes.pop(0) if tel == app.SIMULADO and tid == "t1" else []
db.cancelar_turno = lambda tid, turno_id, origen=None: _cancelados.append((tid, turno_id, origen))
celu_adm.post("/admin/whatsapp/simular/deshacer", data={"csrf_token": "tok"})
html = celu_adm.get("/admin/whatsapp").data.decode()
chequear("deshacer: libera sus turnos sin contarlo como cancelación", _cancelados == [("t1", "x1", None), ("t1", "x2", None)]
         and "2 turno(s) del simulador liberados" in html and "Charla con el bot de" not in html)
db.proximos_turnos_de, db.cancelar_turno = _viejo_prox, _viejo_cancelar

# como si fuera otro día
_mañana10 = (datetime.now(timezone.utc) + timedelta(days=1)).astimezone(app.ZoneInfo(TZ)).replace(hour=10, minute=0, second=0, microsecond=0)
TABLAS["turnos"] = [turno("r1", "t1", _mañana10 + timedelta(hours=3), estado="reservado", paciente_telefono="5491155556666",
                          paciente_nombre="Paciente Secreta"),
                    turno("r2", "t1", _mañana10 + timedelta(hours=30), estado="reservado", paciente_telefono="5491100001111")]
TABLAS["tenants"][0]["aviso_telefono"] = "5491122223333"
avisos_wa.clear()
celu_adm.post("/admin/whatsapp/vista-previa", data={"csrf_token": "tok", "tenant_id": "t1", "momento": _mañana10.strftime("%Y-%m-%dT%H:%M")})
html = celu_adm.get("/admin/whatsapp").data.decode()
chequear("vista previa: recordatorio dentro de la ventana", "1 saldrían" in html and "…6666" in html and "…1111" not in html)
chequear("vista previa: sin nombres de pacientes", "Paciente Secreta" not in html and "5491155556666" not in html)
chequear("vista previa: no manda ni marca nada", not avisos_wa and not any(t["recordatorio_enviado"] for t in TABLAS["turnos"]))
celu_adm.post("/admin/whatsapp/vista-previa", data={"csrf_token": "tok", "tenant_id": "t1", "momento": _mañana10.replace(hour=6).strftime("%Y-%m-%dT%H:%M")})
chequear("vista previa: antes de las 8 no sale el resumen", "Todavía no son las 8" in celu_adm.get("/admin/whatsapp").data.decode())
celu_adm.post("/admin/whatsapp/vista-previa", data={"csrf_token": "tok", "tenant_id": "t1", "momento": "cualquiera"})
chequear("vista previa: fecha mala", "Elegí una cuenta, un día y una hora" in celu_adm.get("/admin/whatsapp").data.decode())

_wa.aplicar_ajustes({})

(_cfg.META_ACCESS_TOKEN, _cfg.PLATAFORMA_PHONE_NUMBER_ID, _cfg.META_WABA_ID, _cfg.RECORDATORIO_TEMPLATE,
 _cfg.REPROGRAMADO_TEMPLATE, _cfg.WHATSAPP_SOLO_A) = viejos

# --- Confirmación por WhatsApp de una reserva web y perfil público ---
import conversation as _conv  # noqa: E402

reset()
_futuro = (datetime.now(timezone.utc) + timedelta(days=2)).replace(second=0, microsecond=0)
TABLAS["turnos"] = [turno("c1", "t1", _futuro, estado="reservado", paciente_telefono="5491166667777", paciente_nombre="Lola",
                          confirmacion_enviada_at=ahora.isoformat(), confirmado_at=None, token_calendario="tok-c1"),
                    turno("c2", "t1", _futuro + timedelta(days=7), estado="reservado", paciente_telefono="5491166667777",
                          paciente_nombre="Lola", confirmacion_enviada_at=None, confirmado_at=None)]
resp = _conv.responder(dict(TENANT), "5491166667777", "Sí, confirmo")
chequear("confirmar: responde con el link para agendar", "Quedó confirmado" in resp and "/agendar/tok-c1" in resp)
chequear("confirmar: marca sólo la que esperaba confirmación", TABLAS["turnos"][0]["confirmado_at"] and not TABLAS["turnos"][1]["confirmado_at"])
resp = _conv.responder(dict(TENANT), "5491166667777", "si")
chequear("confirmar: sin nada pendiente, sigue el menú", "Quedó confirmado" not in resp)
html = None
panel_c = app.app.test_client()
with panel_c.session_transaction() as ses:
    ses["tenant_id"] = "t1"
    ses["sesion_id"] = _sesion_de("t1")
    ses["csrf_token"] = "tok"
html = panel_c.get(f"/agenda?semana={_futuro.astimezone(app.ZoneInfo(TZ)).date().isoformat()}").data.decode()
chequear("panel: marca la sesión confirmada", "✓ confirmó" in html)

# bandeja de mensajes de pacientes
_hace = lambda **k: (datetime.now(timezone.utc) - timedelta(**k)).isoformat()
TABLAS["mensajes_pacientes"] = [
    {"id": "mp1", "tenant_id": "t1", "telefono": "5491166667777", "tipo": "texto", "texto": "Llego 10 minutos tarde",
     "creado": _hace(minutes=5), "leido": False},
    {"id": "mp2", "tenant_id": "t1", "telefono": "5491100009999", "tipo": "audio", "texto": None,
     "creado": _hace(hours=30), "leido": False},
    {"id": "mp3", "tenant_id": "t2", "telefono": "5491100008888", "tipo": "texto", "texto": "Mensaje de otra cuenta",
     "creado": _hace(minutes=1), "leido": False}]
html = _panel(panel_c)
chequear("bandeja: el panel muestra los mensajes de esta cuenta", 'id="mensajes"' in html and "Llego 10 minutos tarde" in html
         and "Mandó un audio" in html and "Mensaje de otra cuenta" not in html)
chequear("bandeja: sólo se contesta dentro de las 24 h", html.count('name="respuesta"') == 1)
_bandeja_wa = []
_bw = app.whatsapp.enviar_texto
app.whatsapp.enviar_texto = lambda n, to, texto: _bandeja_wa.append((to, texto)) or True
panel_c.post("/agenda/mensaje/mp1/responder", data={"csrf_token": "tok", "respuesta": "No hay problema, te espero"})
_mp1 = TABLAS["mensajes_pacientes"][0]
chequear("bandeja: la respuesta sale por WhatsApp con el nombre del profesional",
         _bandeja_wa == [("5491166667777", "*Lic. Marcos*: No hay problema, te espero")]
         and _mp1["respuesta"] == "No hay problema, te espero" and _mp1["leido"] is True, _bandeja_wa)
panel_c.post("/agenda/mensaje/mp2/responder", data={"csrf_token": "tok", "respuesta": "hola"})
chequear("bandeja: pasadas las 24 h no manda nada", len(_bandeja_wa) == 1 and not TABLAS["mensajes_pacientes"][1].get("respuesta"))
panel_c.post("/agenda/mensaje/mp2/leido", data={"csrf_token": "tok"})
panel_c.post("/agenda/mensaje/mp3/leido", data={"csrf_token": "tok"})
chequear("bandeja: marcar leído, y nunca uno de otra cuenta", TABLAS["mensajes_pacientes"][1]["leido"] is True
         and TABLAS["mensajes_pacientes"][2]["leido"] is False)
app.whatsapp.enviar_texto = _bw
TABLAS["mensajes_pacientes"] = []

# el botón de la plantilla llega como type=button
TABLAS["turnos"][0]["confirmado_at"] = None
_resp_bot = []
app.whatsapp.enviar_texto = lambda n, to, texto: _resp_bot.append(texto) or True
_por_num = db.get_tenant_by_phone_number_id
db.get_tenant_by_phone_number_id = lambda pid: dict(TENANT) if pid == "PN_MARCOS" else None
_cuerpo = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
    "metadata": {"phone_number_id": "PN_MARCOS"},
    "messages": [{"from": "5491166667777", "type": "button", "button": {"text": "Sí, confirmo", "payload": "Sí, confirmo"}}]}}]}]}).encode()
import hashlib as _hl, hmac as _hm  # noqa: E401,E402
_firma = "sha256=" + _hm.new(app.config.META_APP_SECRET.encode(), _cuerpo, _hl.sha256).hexdigest()
app.app.test_client().post("/webhook", data=_cuerpo, content_type="application/json", headers={"X-Hub-Signature-256": _firma})
chequear("confirmar: el botón de la plantilla confirma", _resp_bot and "Quedó confirmado" in _resp_bot[-1] and TABLAS["turnos"][0]["confirmado_at"])
db.get_tenant_by_phone_number_id = _por_num

# liberar borra la confirmación
db.cancelar_turno("t1", "c1")
chequear("liberar borra confirmación y token", TABLAS["turnos"][0]["confirmado_at"] is None and TABLAS["turnos"][0]["token_calendario"] is None)

# perfil público desde el panel
import io as _io_p  # noqa: E402
panel_c.post("/agenda/perfil", data={"csrf_token": "tok", "modalidades_publicas": "ambas"})
t1 = TABLAS["tenants"][0]
panel_c.post("/agenda/presentacion", data={
    "csrf_token": "tok", "descripcion": "Adultos · Caballito", "titulo": "  Lic. en   Psicología (UBA) ",
    "ubicacion": "Caballito, CABA", "sobre_mi": "Trabajo desde la TCC.\r\nPrimera sesión: nos conocemos.",
    "enfoques": "Ansiedad, duelo, Ansiedad, , Adolescentes," + ",".join(f"t{i}" for i in range(20)),
    "formacion": "Lic. en Psicología — UBA\r\n\r\nPosgrado TCC", "mas_de_mi": "Ando en bici y tengo dos gatos.",
    "obras_sociales": "Particular, con factura para reintegro"})
chequear("perfil: guarda descripción, modalidad y sale del directorio sin tildar",
         t1["descripcion"] == "Adultos · Caballito" and t1["modalidades_publicas"] == "ambas" and t1["en_directorio"] is False)
chequear("presentación: limpia espacios, saca repetidos y respeta los máximos",
         t1["titulo"] == "Lic. en Psicología (UBA)" and t1["enfoques"][:3] == ["Ansiedad", "duelo", "Adolescentes"]
         and len(t1["enfoques"]) == app.MAX_ENFOQUES and t1["formacion"] == ["Lic. en Psicología — UBA", "Posgrado TCC"]
         and t1["sobre_mi"] == "Trabajo desde la TCC.\nPrimera sesión: nos conocemos.")
panel_c.post("/agenda/perfil", data={"csrf_token": "tok", "modalidades_publicas": "ambas"})
chequear("presentación: guardar la configuración de reservas no la borra", t1["descripcion"] == "Adultos · Caballito")
t1["slug"] = t1.get("slug") or "marcos"
_pub = app.app.test_client().get(f"/{t1['slug']}").data.decode()
chequear("página: muestra la presentación", all(x in _pub for x in (
    "Lic. en Psicología (UBA)", "Caballito, CABA", "Sobre mí", "Temas que trabajo", "Adolescentes", "Formación",
    "Posgrado TCC", "Un poco más de mí", "dos gatos", "Obras sociales", 'href="#horarios"', "presencial o por videollamada")))
chequear("página: sin foto muestra la inicial", "perfil-iniciales" in _pub)
chequear("tema: sin elegir sigue al dispositivo y trae el menú de vista", 'data-tema="auto"' in _pub and 'id="vista-boton"' in _pub
         and 'data-letra="normal"' in _pub and 'name="csrf"' not in _pub)
_cli_osc = app.app.test_client()
_cli_osc.set_cookie("tema", "oscuro")
_cli_raro = app.app.test_client()
_cli_raro.set_cookie("tema", '"><script>')
chequear("tema: respeta la cookie y descarta valores raros",
         'data-tema="oscuro"' in _cli_osc.get(f"/{t1['slug']}").data.decode()
         and 'data-tema="auto"' in _cli_raro.get(f"/{t1['slug']}").data.decode())
panel_c.post("/agenda/portada", data={"csrf_token": "tok", "portada": (_io_p.BytesIO(b"\xff\xd8\xff" + b"1" * 50), "p.jpg")},
             content_type="multipart/form-data")
_pt = app.app.test_client().get(f"/{t1['slug']}/portada")
chequear("portada: se sube y se sirve como imagen", t1.get("portada_version") == 1 and _pt.status_code == 200
         and _pt.mimetype == "image/jpeg" and _pt.data.startswith(b"\xff\xd8\xff")
         and f"/{t1['slug']}/portada?v=1" in app.app.test_client().get(f"/{t1['slug']}").data.decode())
panel_c.post("/agenda/portada", data={"csrf_token": "tok", "portada": (_io_p.BytesIO(b"<svg onload=alert(1)>"), "x.svg")},
             content_type="multipart/form-data")
chequear("portada: rechaza lo que no es PNG/JPG", t1.get("portada_version") == 1)
panel_c.post("/agenda/portada", data={"csrf_token": "tok", "portada": (_io_p.BytesIO(b"\xff\xd8\xff" + b"1" * app.PORTADA_MAX), "g.jpg")},
             content_type="multipart/form-data")
chequear("portada: rechaza la muy pesada", t1.get("portada_version") == 1)
panel_c.post("/agenda/portada/borrar", data={"csrf_token": "tok"})
chequear("portada: se quita", t1.get("portada_version") == 0
         and app.app.test_client().get(f"/{t1['slug']}/portada").status_code == 404)
chequear("perfil: avisa si ofrece videollamada sin link", "no cargaste el link" in _panel(panel_c))
chequear("panel: elegir foto y portada sube sola, sin botón aparte", 'class="btn chico secundario elegir-archivo"' in _panel(panel_c))
# vista guardada en la cuenta y color de la página
_ck = app.app.test_client()
_ck.set_cookie("tema", "claro"); _ck.set_cookie("letra", "enorme")
_html_ck = _ck.get(f"/{t1['slug']}").data.decode()
chequear("vista: sin sesión usa las cookies", 'data-tema="claro"' in _html_ck and 'data-modo="claro"' in _html_ck and 'data-letra="enorme"' in _html_ck)
panel_c.post("/agenda/vista", data={"csrf_token": "tok", "tema": "noche"})
panel_c.post("/agenda/vista", data={"csrf_token": "tok", "letra": "grande"})
panel_c.post("/agenda/vista", data={"csrf_token": "tok", "tema": "rosa", "letra": "gigante"})
chequear("vista: el panel la guarda en la cuenta y descarta valores raros", t1.get("preferencias_vista") == {"tema": "noche", "letra": "grande"})
panel_c.set_cookie("tema", "claro")
_html_p = _panel(panel_c)
chequear("vista: con sesión manda la de la cuenta (te sigue a otro dispositivo)",
         'data-tema="noche"' in _html_p and 'data-letra="grande"' in _html_p and 'name="csrf"' in _html_p)
chequear("vista: sin CSRF no se guarda", panel_c.post("/agenda/vista", data={"tema": "oscuro"}).status_code in (400, 403)
         and t1["preferencias_vista"]["tema"] == "noche")
panel_c.post("/agenda/presentacion", data={"csrf_token": "tok", "color_pagina": "terracota", "descripcion": "Adultos · Caballito"})
_pub_c = app.app.test_client().get(f"/{t1['slug']}").data.decode()
chequear("color: la página usa el color elegido, en claro y en oscuro", t1["color_pagina"] == "terracota"
         and "--green:#9a4a2c" in _pub_c and '[data-modo="oscuro"] { --green:#e6a284' in _pub_c)
panel_c.post("/agenda/presentacion", data={"csrf_token": "tok", "color_pagina": "</style><script>", "descripcion": "Adultos · Caballito"})
chequear("color: un valor raro vuelve al verde de siempre", t1["color_pagina"] is None
         and "--green:#9a4a2c" not in app.app.test_client().get(f"/{t1['slug']}").data.decode())
# horario de «oscuro de noche» a elección
panel_c.post("/agenda/vista", data={"csrf_token": "tok", "noche": "19-7"})
panel_c.post("/agenda/vista", data={"csrf_token": "tok", "noche": "8-8"})
panel_c.post("/agenda/vista", data={"csrf_token": "tok", "noche": "25-3"})
chequear("noche: guarda el horario elegido y descarta los inválidos", t1["preferencias_vista"].get("noche") == "19-7"
         and 'data-noche="19-7"' in _panel(panel_c))
_cn = app.app.test_client(); _cn.set_cookie("noche", "x-y")
chequear("noche: sin elegir va de 20 a 8", 'data-noche="20-8"' in _cn.get(f"/{t1['slug']}").data.decode())
# vista previa al compartir
t1["foto"] = "data:image/png;base64," + __import__("base64").b64encode(b"\x89PNG\r\n\x1a\nxx").decode()
_og = app.app.test_client().get(f"/{t1['slug']}").data.decode()
chequear("compartir: la página trae título, descripción y la foto para la vista previa",
         'property="og:title"' in _og and f"content=\"{t1['nombre']}" in _og and 'property="og:description"' in _og
         and f"/{t1['slug']}/foto" in _og and 'property="og:image"' in _og)
_ft = app.app.test_client().get(f"/{t1['slug']}/foto")
chequear("compartir: una sola vista previa (sin etiquetas repetidas)", _og.count('property="og:title"') == 1
         and _og.count('property="og:image"') == 1 and _og.count('name="description"') == 1 and 'rel="canonical"' in _og)
chequear("compartir: la foto se sirve como imagen", _ft.status_code == 200 and _ft.mimetype == "image/png" and _ft.data.startswith(b"\x89PNG"))
t1["foto"] = "data:text/html;base64,PHNjcmlwdD4="
chequear("compartir: una foto que no es PNG/JPG no se sirve", app.app.test_client().get(f"/{t1['slug']}/foto").status_code == 404)
t1["foto"] = None
# instalar como app
_mf = app.app.test_client().get("/manifest.webmanifest")
chequear("app: manifest con nombre, inicio en el panel e íconos", _mf.status_code == 200 and _mf.mimetype == "application/manifest+json"
         and _mf.get_json()["start_url"].startswith("/agenda") and len(_mf.get_json()["icons"]) == 3)
_sw = app.app.test_client().get("/sw.js").data.decode()
chequear("app: el service worker no guarda páginas, sólo la de sin conexión", "caches.match(OFFLINE)" in _sw and "cache.put" not in _sw
         and "csrf" not in app.app.test_client().get("/sin-conexion").data.decode())
chequear("app: íconos servidos", app.app.test_client().get("/static/icono-512.png").status_code == 200)
chequear("app: el panel ofrece instalarlo y todas las páginas traen el manifest",
         'id="instalar-boton"' in _panel(panel_c) and 'rel="manifest"' in _og)
_hp = _panel(panel_c)
chequear("panel: recorte de foto y colores para elegir", all(x in _hp for x in ('id="recorte"', 'name="color_pagina" value="azul"', "Usar este recorte"))
         and _hp.index('id="recorte"') < _hp.index("se elige el recorte"))

panel_c.post("/agenda/perfil", data={"csrf_token": "tok", "modalidades_publicas": "cualquiera", "en_directorio": "1"})
chequear("perfil: modalidad inválida vuelve a presencial", t1["modalidades_publicas"] == "presencial" and t1["en_directorio"] is True)
import io as _io  # noqa: E402
panel_c.post("/agenda/foto", data={"csrf_token": "tok", "foto": (_io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 20), "yo.png")},
             content_type="multipart/form-data")
chequear("perfil: sube foto PNG", (t1.get("foto") or "").startswith("data:image/png;base64,"))
panel_c.post("/agenda/foto", data={"csrf_token": "tok", "foto": (_io.BytesIO(b"<svg onload=alert(1)>"), "x.svg")},
             content_type="multipart/form-data")
chequear("perfil: rechaza lo que no es PNG/JPG", t1["foto"].startswith("data:image/png"))
panel_c.post("/agenda/foto/borrar", data={"csrf_token": "tok"})
chequear("perfil: quita la foto", t1["foto"] is None)

# --- Segundo aviso de confirmación, cancelar y cambiar desde la web ---
reset()
TZ_AR = app.ZoneInfo(TZ)
_hoy10 = datetime.now(TZ_AR).replace(hour=10, minute=0, second=0, microsecond=0)
_en3dias = (_hoy10 + timedelta(days=3)).astimezone(timezone.utc)
TABLAS["turnos"] = [
    turno("w1", "t1", _en3dias, estado="reservado", paciente_telefono="5491166667777", paciente_nombre="Lola",
          confirmacion_enviada_at=(_hoy10 - timedelta(hours=7)).isoformat(), token_calendario="tok-w1"),
    turno("w2", "t1", _en3dias + timedelta(hours=1), estado="reservado", paciente_telefono="5491188889999",
          confirmacion_enviada_at=(_hoy10 - timedelta(hours=2)).isoformat()),
    turno("l1", "t1", _en3dias + timedelta(days=1)),
]
_plantillas = []
_viejo_tpl = scheduler.whatsapp.enviar_plantilla
scheduler.whatsapp.enviar_plantilla = lambda n, to, tpl, lang, params: _plantillas.append((to, tpl, params)) or True
_viejo_conf = scheduler.config.CONFIRMACION_TEMPLATE
scheduler.config.CONFIRMACION_TEMPLATE = "confirmar_turno"
_viejo_pn = scheduler.config.PLATAFORMA_PHONE_NUMBER_ID
scheduler.config.PLATAFORMA_PHONE_NUMBER_ID = "PN1"
scheduler._recordar_confirmaciones(dict(TENANT), TZ_AR, _hoy10.astimezone(timezone.utc))
chequear("segundo aviso: sólo a quien pasaron 6 h sin confirmar", [p[0] for p in _plantillas] == ["5491166667777"]
         and TABLAS["turnos"][0]["confirmacion_recordada_at"] and not TABLAS["turnos"][1].get("confirmacion_recordada_at"))
scheduler._recordar_confirmaciones(dict(TENANT), TZ_AR, (_hoy10 + timedelta(hours=1)).astimezone(timezone.utc))
chequear("segundo aviso: una sola vez", len(_plantillas) == 1)
TABLAS["turnos"][1]["confirmacion_enviada_at"] = (_hoy10 - timedelta(hours=12)).isoformat()
scheduler._recordar_confirmaciones(dict(TENANT), TZ_AR, _hoy10.replace(hour=23).astimezone(timezone.utc))
chequear("segundo aviso: de noche no", len(_plantillas) == 1)
chequear("resumen: cuenta las reservas sin confirmar", "2 reservas web sin confirmar por WhatsApp" in scheduler._partes_del_resumen(dict(TENANT), _hoy10.date()))
scheduler.whatsapp.enviar_plantilla, scheduler.config.CONFIRMACION_TEMPLATE = _viejo_tpl, _viejo_conf
scheduler.config.PLATAFORMA_PHONE_NUMBER_ID = _viejo_pn

web = app.app.test_client()
_por_id = db.get_tenant_by_id
db.get_tenant_by_id = lambda tid: dict(TENANT, slug="marcos") if tid == "t1" else None
html = web.get("/agendar/tok-w1").data.decode()
chequear("csrf: el panel lo sigue pidiendo", web.post("/agenda/perfil", data={}).status_code in (302, 403))
chequear("agendar: ofrece cambiar y cancelar", "Cambiar el horario" in html and "Cancelar el turno" in html)
html = web.get("/agendar/tok-w1/cambiar").data.decode()
chequear("cambiar: muestra el calendario con su turno actual", "Tu turno de ahora es el" in html and "/agendar/tok-w1/cambiar/l1" in html
         and "reservar/l1" not in html)
html = web.get("/agendar/tok-w1/cambiar/l1").data.decode()
chequear("cambiar: pregunta antes", "¿Cambiás el horario?" in html and TABLAS["turnos"][0]["estado"] == "reservado")
r = web.post("/agendar/tok-w1/cambiar/l1")
nuevo = next(t for t in TABLAS["turnos"] if t["id"] == "l1")
viejo = next(t for t in TABLAS["turnos"] if t["id"] == "w1")
chequear("cambiar: pasa el turno y se lleva el link", nuevo["estado"] == "reservado" and nuevo["paciente_telefono"] == "5491166667777"
         and nuevo["token_calendario"] == "tok-w1" and nuevo["confirmado_at"] and nuevo["reserva_nueva"]
         and viejo["estado"] == "libre" and viejo["token_calendario"] is None)
chequear("cambiar: vuelve a agendar con aviso", "cambiado=1" in r.headers.get("Location", "")
         and "Listo, cambiaste el horario" in web.get("/agendar/tok-w1?cambiado=1").data.decode())
chequear("cambiar: un horario ocupado no", web.get("/agendar/tok-w1/cambiar/w2").status_code == 302)
html = web.get("/agendar/tok-w1/cancelar").data.decode()
chequear("cancelar: pregunta antes", "¿Cancelás tu turno?" in html and nuevo["estado"] == "reservado")
_cancel_antes = len(TABLAS.get("cancelaciones", []))
html = web.post("/agendar/tok-w1/cancelar").data.decode()
chequear("cancelar: libera y lo registra como del paciente", "Listo, se canceló" in html and nuevo["estado"] == "libre"
         and len(TABLAS.get("cancelaciones", [])) == _cancel_antes + 1 and TABLAS["cancelaciones"][-1]["origen"] == "paciente")
chequear("cancelar: el link ya no sirve", web.get("/agendar/tok-w1").status_code == 404
         and web.post("/agendar/tok-w1/cancelar").status_code == 404)
db.get_tenant_by_id = _por_id

# --- Aviso al profesional, lista de espera web y política de cancelación ---
reset()
TABLAS["lista_espera"] = []
_t1 = TABLAS["tenants"][0]
_t1.update({"slug": "marcos", "aviso_telefono": "5491122223333", "cancelar_hasta_horas": 24})
_avisos_prof = []
_viejo_avisar = scheduler.avisar_profesional
app.scheduler.avisar_profesional = lambda tenant, texto, **_kw: _avisos_prof.append(texto) or True
_por_slug, _por_id2 = db.get_tenant_by_slug, db.get_tenant_by_id
db.get_tenant_by_slug = lambda slug: dict(_t1) if slug == "marcos" else None
db.get_tenant_by_id = lambda tid: dict(_t1) if tid == "t1" else None
web2 = app.app.test_client()

html = web2.post("/marcos/lista-espera", data={"nombre": "Nora", "telefono": "+54 9 11 4444-5555", "preferencia": "martes tarde"}).data.decode()
chequear("espera web: se anota y avisa sin nombre", "te anotamos" in html and TABLAS["lista_espera"][0]["origen"] == "web"
         and TABLAS["lista_espera"][0]["telefono"] == "5491144445555" and _avisos_prof and "Nora" not in _avisos_prof[-1])
html = web2.post("/marcos/lista-espera", data={"nombre": "Nora", "telefono": "5491144445555"}).data.decode()
chequear("espera web: no se duplica", "Ya estabas anotado" in html and len(TABLAS["lista_espera"]) == 1 and len(_avisos_prof) == 1)
r = web2.post("/marcos/lista-espera", data={"nombre": "X", "telefono": "123"})
chequear("espera web: teléfono incompleto vuelve", r.status_code == 302 and len(TABLAS["lista_espera"]) == 1)
chequear("espera web: el panel la marca", "se anotó en la web" in _panel(panel_c))

_ahora = datetime.now(timezone.utc).replace(second=0, microsecond=0)
TABLAS["turnos"] = [turno("p1", "t1", _ahora + timedelta(hours=5), estado="reservado", paciente_telefono="5491166667777",
                          token_calendario="tok-p1"),
                    turno("p2", "t1", _ahora + timedelta(days=4), estado="reservado", paciente_telefono="5491166667777",
                          token_calendario="tok-p2")]
html = web2.get("/agendar/tok-p1").data.decode()
chequear("política: con menos de 24 h no ofrece cancelar", "Faltan menos de 24 horas" in html and "Cancelar el turno" not in html)
html = web2.post("/agendar/tok-p1/cancelar").data.decode()
chequear("política: tampoco por POST directo", "Ya no se puede desde acá" in html and TABLAS["turnos"][0]["estado"] == "reservado")
chequear("política: ni cambiar", "Ya no se puede desde acá" in web2.get("/agendar/tok-p1/cambiar").data.decode())
_t1["cancelar_hasta_horas"] = 0
chequear("política: 0 = hasta que empieza", "Cancelar el turno" in web2.get("/agendar/tok-p1").data.decode())
_t1["cancelar_hasta_horas"] = 24
_avisos_prof.clear()
web2.post("/agendar/tok-p2/cancelar")
chequear("cancelar web: avisa al profesional con la lista de espera", _avisos_prof and "canceló por la web" in _avisos_prof[-1]
         and "1 en lista de espera" in _avisos_prof[-1])

panel_c.post("/agenda/perfil", data={"csrf_token": "tok", "modalidades_publicas": "presencial", "en_directorio": "1",
                                     "cancelar_hasta_horas": "48"})
chequear("panel: guarda la política", TABLAS["tenants"][0]["cancelar_hasta_horas"] == 48)
panel_c.post("/agenda/perfil", data={"csrf_token": "tok", "modalidades_publicas": "presencial", "cancelar_hasta_horas": "999"})
chequear("panel: política inválida no cambia", TABLAS["tenants"][0]["cancelar_hasta_horas"] == 48)
app.scheduler.avisar_profesional = _viejo_avisar
db.get_tenant_by_slug, db.get_tenant_by_id = _por_slug, _por_id2

# --- «Preguntarme antes de liberar», lista de espera automática y estadísticas ---
import cancelaciones as _canc  # noqa: E402

reset()
TABLAS["lista_espera"] = []
_t1 = TABLAS["tenants"][0]
_t1.update({"slug": "marcos", "aviso_telefono": "5491122223333", "cancelar_hasta_horas": 24, "al_cancelar": "preguntar"})
_avisos_p, _ofrecidos = [], []
_viejo_av = scheduler.avisar_profesional
scheduler.avisar_profesional = lambda tenant, texto, **_kw: _avisos_p.append(texto) or True
_viejo_of = _canc.ofrecer_horario
_canc.ofrecer_horario = lambda tenant, tel, cuando, link: _ofrecidos.append((tel, link)) or True
db.get_tenant_by_slug = lambda slug: dict(_t1) if slug == "marcos" else None
db.get_tenant_by_id = lambda tid: dict(_t1) if tid == "t1" else None
_a = datetime.now(timezone.utc).replace(second=0, microsecond=0)
TABLAS["turnos"] = [turno("h1", "t1", _a + timedelta(days=3), estado="reservado", paciente_telefono="5491166667777",
                          token_calendario="tok-h1"),
                    turno("h2", "t1", _a + timedelta(days=5), estado="reservado", paciente_telefono="5491188889999",
                          token_calendario="tok-h2")]
web3 = app.app.test_client()
web3.post("/agendar/tok-h1/cancelar")
h1 = TABLAS["turnos"][0]
chequear("preguntar: el horario cancelado queda retenido", h1["estado"] == "libre" and h1["hueco"] == "pendiente")
chequear("preguntar: nadie lo ve", not any(t["id"] == "h1" for t in db.turnos_libres("t1", _a, limite=50)))
chequear("preguntar: tampoco reservándolo por id", web3.post("/marcos/reservar/h1", data={
    "nombre": "X", "telefono": "5491100002222", "confirmado": "1"}).status_code == 302 and h1["estado"] == "libre")
chequear("preguntar: le avisa al profesional cómo decidir", _avisos_p and "LIBERAR" in _avisos_p[-1] and "ME LO TOMO" in _avisos_p[-1])
html = _panel(panel_c)
chequear("preguntar: el panel lo muestra para decidir", 'id="bt-huecos"' in html and "Horarios cancelados" in html and "esperando que decidas" in html)

# el profesional contesta por WhatsApp: sólo con el número vinculado
import asistente_profesional as _apv
r = _canc.respuesta_del_profesional("5491122223333", "Me lo tomo")
chequear("whatsapp profesional: sin vincular, LIBERAR / ME LO TOMO sólo dice cómo vincular",
         r == _apv.sin_vincular() and h1["hueco"] == "pendiente", r)
_t1["aviso_verificado"] = "5491122223333"
r = _canc.respuesta_del_profesional("5491122223333", "Me lo tomo")
chequear("whatsapp profesional: me lo tomo", "es tuyo" in r and h1["hueco"] == "propio")
chequear("whatsapp profesional: sin retenidos lo dice", "No tenés ningún horario retenido" in _canc.respuesta_del_profesional("5491122223333", "liberar"))
chequear("whatsapp profesional: un paciente que escribe «liberar» sigue normal", _canc.respuesta_del_profesional("5491166667777", "liberar") is None)
chequear("whatsapp profesional: otro texto sigue normal", _canc.respuesta_del_profesional("5491122223333", "hola") is None)

# liberar desde el panel, con lista de espera automática
_t1["ofrecer_espera_auto"] = True
TABLAS["lista_espera"] = [{"id": "e1", "tenant_id": "t1", "nombre": "Nora", "telefono": "5491144445555", "preferencia": None,
                           "ofrecido_en": None, "origen": "web", "created_at": _a.isoformat()}]
panel_c.post("/agenda/hueco/h1/liberar", data={"csrf_token": "tok"})
chequear("liberar: vuelve a ofrecerse y va a la lista de espera", h1["hueco"] is None and _ofrecidos
         and _ofrecidos[-1][0] == "5491144445555" and _ofrecidos[-1][1].endswith("/marcos")
         and TABLAS["lista_espera"][0]["ofrecido_en"])
chequear("liberar: aparece de nuevo", any(t["id"] == "h1" for t in db.turnos_libres("t1", _a, limite=50)))

# modo liberar: va directo a la lista (pero no al mismo dos veces el mismo día)
_t1["al_cancelar"] = "liberar"
_ofrecidos.clear()
web3.post("/agendar/tok-h2/cancelar")
chequear("liberar enseguida: no retiene", TABLAS["turnos"][1]["hueco"] is None)
chequear("lista automática: no le ofrece dos veces el mismo día", not _ofrecidos and "1 en lista de espera" in _avisos_p[-1])

# estadísticas
_t1["al_cancelar"] = "liberar"
TABLAS["eventos_pagina"] = []
web3.get("/marcos")
web3.get("/marcos", headers={"User-Agent": "Googlebot/2.1"})
web3.post("/marcos/lista-espera", data={"nombre": "Ana", "telefono": "5491177776666"})
html = _panel(panel_c)
chequear("estadísticas: cuenta visitas (sin bots) y anotados", "</strong> 1 visita\n" in html and "1 a lista de espera" in html)
panel_c.post("/agenda/perfil", data={"csrf_token": "tok", "modalidades_publicas": "presencial", "al_cancelar": "preguntar",
                                     "ofrecer_espera_auto": "1", "cancelar_hasta_horas": "24"})
chequear("panel: guarda qué hacer al cancelar", TABLAS["tenants"][0]["al_cancelar"] == "preguntar"
         and TABLAS["tenants"][0]["ofrecer_espera_auto"] is True)
scheduler.avisar_profesional, _canc.ofrecer_horario = _viejo_av, _viejo_of

# --- Vencimiento de horarios retenidos, bloqueo por horas y pausa ---
import cancelaciones as _canc2  # noqa: E402

reset()
TABLAS["lista_espera"] = []
_t1 = TABLAS["tenants"][0]
_t1.update({"slug": "marcos", "aviso_telefono": "5491122223333", "al_cancelar": "preguntar",
            "hueco_vence_horas": 2, "hueco_por_defecto": "liberar", "cancelar_hasta_horas": 0})
_av = []
_viejo_av2 = scheduler.avisar_profesional
scheduler.avisar_profesional = lambda tenant, texto, **_kw: _av.append(texto) or True
db.get_tenant_by_slug = lambda slug: dict(_t1) if slug == "marcos" else None
db.get_tenant_by_id = lambda tid: dict(_t1) if tid == "t1" else None
_b = datetime.now(timezone.utc).replace(second=0, microsecond=0)
TABLAS["turnos"] = [turno("v1", "t1", _b + timedelta(days=2), hueco="pendiente", hueco_desde=(_b - timedelta(hours=3)).isoformat()),
                    turno("v2", "t1", _b + timedelta(days=3), hueco="pendiente", hueco_desde=(_b - timedelta(minutes=30)).isoformat())]
chequear("vence: resuelve sólo los que pasaron el plazo", _canc2.vencer_huecos(dict(_t1), _b) == 1
         and TABLAS["turnos"][0]["hueco"] is None and TABLAS["turnos"][1]["hueco"] == "pendiente")
chequear("vence: le avisa qué hizo", _av and "lo liberamos" in _av[-1])
_t1["hueco_por_defecto"] = "tomar"
_canc2.vencer_huecos(dict(_t1), _b + timedelta(hours=2))
chequear("vence: por defecto quedárselo", TABLAS["turnos"][1]["hueco"] == "propio" and "te lo dejamos a vos" in _av[-1])
_t1["hueco_vence_horas"] = 0
TABLAS["turnos"][0].update({"hueco": "pendiente", "hueco_desde": (_b - timedelta(days=5)).isoformat()})
chequear("vence: con 0 no vence nunca", _canc2.vencer_huecos(dict(_t1), _b) == 0 and TABLAS["turnos"][0]["hueco"] == "pendiente")
scheduler.avisar_profesional = _viejo_av2

# bloqueo por horas
reset()
_tz_ar = app.ZoneInfo(TZ)
_dia = (datetime.now(_tz_ar) + timedelta(days=7)).date()
_a14 = datetime.combine(_dia, app.dtime(14, 0), tzinfo=_tz_ar)
TABLAS["turnos"] = [turno("b13", "t1", (_a14 - timedelta(hours=1)).astimezone(timezone.utc)),
                    turno("b14", "t1", _a14.astimezone(timezone.utc)),
                    turno("b15", "t1", (_a14 + timedelta(hours=1)).astimezone(timezone.utc)),
                    turno("b16", "t1", (_a14 + timedelta(hours=2)).astimezone(timezone.utc))]
panel_c.post("/agenda/bloqueo", data={"csrf_token": "tok", "desde": _dia.isoformat(), "hora_desde": "14:00", "hora_hasta": "16:00",
                                      "motivo": "trámite"})
_libres = {t["id"] for t in db.turnos_libres("t1", datetime.now(timezone.utc), limite=50)}
chequear("bloqueo por horas: saca sólo ese rato", _libres == {"b13", "b16"})
html = _panel(panel_c)
chequear("bloqueo por horas: se ve con las horas", "de 14:00 a 16:00" in html)
panel_c.post("/agenda/bloqueo", data={"csrf_token": "tok", "desde": _dia.isoformat(), "hora_desde": "16:00", "hora_hasta": "15:00"})
chequear("bloqueo por horas: al revés no", len(TABLAS["bloqueos"]) == 1)

# pausa
reset()
_t1 = TABLAS["tenants"][0]
_t1.update({"slug": "marcos", "activo": True})
db.get_tenant_by_slug = lambda slug: dict(_t1) if slug == "marcos" else None
db.get_tenant_by_id = lambda tid: dict(_t1) if tid == "t1" else None
TABLAS["turnos"] = [turno("z1", "t1", datetime.now(timezone.utc) + timedelta(days=2))]
panel_c.post("/agenda/pausa", data={"csrf_token": "tok", "pausar": "1", "mensaje_pausa": "Vuelvo el 2 de marzo"})
chequear("pausa: se guarda", _t1["pausado"] is True and _t1["mensaje_pausa"] == "Vuelvo el 2 de marzo")
web4 = app.app.test_client()
html = web4.get("/marcos").data.decode()
chequear("pausa: la página no ofrece horarios y deja anotarse", "no toma turnos nuevos" in html and "Vuelvo el 2 de marzo" in html
         and "reservar/z1" not in html and "Anotarme" in html)
r = web4.post("/marcos/reservar/z1", data={"nombre": "X", "telefono": "5491100002222", "confirmado": "1"})
chequear("pausa: no se puede reservar por id", r.status_code == 302 and TABLAS["turnos"][0]["estado"] == "libre")
_conv_resp = _conv.responder(dict(_t1), "5491100002222", "turnos")
chequear("pausa: el bot tampoco ofrece", "no está tomando turnos nuevos" in _conv_resp and "Vuelvo el 2 de marzo" in _conv_resp)
chequear("pausa: /turnos ya no existe (va a la portada)", web4.get("/turnos").status_code == 301)
panel_c.post("/agenda/pausa", data={"csrf_token": "tok", "pausar": "0"})
chequear("pausa: se reactiva", _t1["pausado"] is False and "reservar/z1" in web4.get("/marcos").data.decode())

# --- Pausa con fecha, horario semanal y tipos de turno ---
import cancelaciones as _canc3  # noqa: E402
_canc3._programar = lambda *a: None  # sin temporizadores de verdad: se llama pasar_vencidos a mano

reset()
TABLAS["lista_espera"] = []
_t1 = TABLAS["tenants"][0]
_t1.update({"slug": "marcos", "activo": True, "aviso_telefono": "5491122223333"})
db.get_tenant_by_slug = lambda slug: dict(_t1) if slug == "marcos" else None
db.get_tenant_by_id = lambda tid: dict(_t1) if tid == "t1" else None
_hoy_ar = datetime.now(app.ZoneInfo(TZ)).date()
panel_c.post("/agenda/pausa", data={"csrf_token": "tok", "pausar": "1", "pausado_hasta": _hoy_ar.isoformat()})
chequear("pausa con fecha: hoy o antes no", not _t1.get("pausado"))
panel_c.post("/agenda/pausa", data={"csrf_token": "tok", "pausar": "1", "pausado_hasta": (_hoy_ar + timedelta(days=10)).isoformat()})
chequear("pausa con fecha: se guarda", _t1["pausado"] and _t1["pausado_hasta"] == (_hoy_ar + timedelta(days=10)).isoformat())
_pag = app.app.test_client().get("/marcos").data.decode()
_v10 = _hoy_ar + timedelta(days=10)
chequear("pausa con fecha: la página dice cuándo vuelve, con cuenta regresiva",
         f"Vuelve el {app.DIAS_LARGOS[_v10.weekday()]} {_v10.day}/{_v10.month}" in _pag and 'class="pf-cuenta"' in _pag
         and '<b data-c="d">10</b>' in _pag and "dos días antes de que vuelva te avisamos" in _pag, _pag[:0])
_t1["pausado_hasta"] = None
_pag = app.app.test_client().get("/marcos").data.decode()
chequear("pausa sin fecha: «Vuelve a atender pronto», sin cuenta", "Vuelve a atender pronto" in _pag and 'class="pf-cuenta"' not in _pag)
_t1["pausado_hasta"] = _v10.isoformat()
TABLAS["turnos"] = [turno("r1", "t1", datetime.now(timezone.utc) + timedelta(days=12))]
TABLAS["lista_espera"] = [{"id": "e1", "tenant_id": "t1", "nombre": "Nora", "telefono": "5491144445555", "ofrecido_en": None,
                           "origen": "web", "created_at": _b.isoformat()}]
_ofr, _avs = [], []
_vo, _va = _canc3.ofrecer_horario, scheduler.avisar_profesional
_canc3.ofrecer_horario = lambda tenant, tel, cuando, link: _ofr.append(tel) or True
scheduler.avisar_profesional = lambda tenant, texto, **_kw: _avs.append(texto) or True
_men = []
_ve, _vt = _canc3.whatsapp.enviar_texto, _canc3.whatsapp.enviar_plantilla
_canc3.whatsapp.enviar_texto = lambda numero, to, texto: _men.append((to, texto)) or True
_z = app.ZoneInfo(TZ)
_a10 = lambda d: datetime.combine(_hoy_ar + timedelta(days=d), datetime.min.time(), _z).replace(hour=10)
chequear("vuelta: 3 días antes todavía no avisa", _canc3.avisar_vuelta_si_toca(dict(_t1), _a10(7)) == 0 and not _men)
chequear("vuelta: 2 días antes, antes de las 10 no", _canc3.avisar_vuelta_si_toca(dict(_t1), _a10(8).replace(hour=8)) == 0)
chequear("vuelta: 2 días antes le avisa a la lista y al profesional",
         _canc3.avisar_vuelta_si_toca(dict(_t1), _a10(8)) == 1 and _men[0][0] == "5491144445555"
         and f"vuelvo a tomar turnos el {app.DIAS_LARGOS[_v10.weekday()]} {_v10.day}/{_v10.month}" in _men[0][1]
         and "/marcos" in _men[0][1] and _t1["vuelta_avisada"] == _v10.isoformat()
         and "le avisamos a 1 de tu lista de espera" in _avs[-1], (_men, _avs))
chequear("vuelta: una sola vez", _canc3.avisar_vuelta_si_toca(dict(_t1), _a10(9)) == 0 and len(_men) == 1)
_canc3.whatsapp.enviar_texto = lambda numero, to, texto: _men.append((to, texto)) or True
_t1.update(mensaje_vuelta="Vuelvo renovada después del congreso.", vuelta_avisada=None)
chequear("vuelta: con mensaje propio va ese texto y el link",
         _canc3.avisar_vuelta_si_toca(dict(_t1), _a10(8)) == 1
         and _men[-1][1].endswith("*Lic. Marcos*: Vuelvo renovada después del congreso. Mi página: /marcos"), _men[-1:])
_canc3.whatsapp.enviar_texto, _canc3.whatsapp.enviar_plantilla = _ve, _vt
_r = panel_c.post("/agenda/pausa/mensaje-vuelta", data={"csrf_token": "tok", "mensaje_vuelta": "  Nos vemos\n pronto  "})
chequear("mensaje de vuelta: se guarda en una línea", _r.status_code == 302 and _t1["mensaje_vuelta"] == "Nos vemos pronto")
panel_c.post("/agenda/pausa/mensaje-vuelta", data={"csrf_token": "tok", "mensaje_vuelta": " "})
chequear("mensaje de vuelta: vacío vuelve al de fábrica", _t1["mensaje_vuelta"] is None)
_pag = app.app.test_client().get("/marcos").data.decode()
chequear("cuenta regresiva: links para agregar al calendario",
         "calendar.google.com" in _pag and f"{_v10:%Y%m%d}" in _pag and "/marcos/vuelta.ics" in _pag)
_ics = app.app.test_client().get("/marcos/vuelta.ics")
chequear("vuelta.ics: evento de todo el día", _ics.status_code == 200 and f"DTSTART;VALUE=DATE:{_v10:%Y%m%d}" in _ics.data.decode()
         and "Vuelve a atender Lic. Marcos" in _ics.data.decode())
chequear("pausa con fecha: antes no se reactiva", not _canc3.reactivar_si_toca(dict(_t1), _hoy_ar + timedelta(days=9)))
TABLAS["turnos"] = [turno("r1", "t1", datetime.now(timezone.utc) + timedelta(days=12)),
                    turno("r2", "t1", datetime.now(timezone.utc) + timedelta(days=13))]
TABLAS["lista_espera"] += [{"id": f"e{i}", "tenant_id": "t1", "nombre": f"P{i}", "telefono": f"54911000000{i}", "ofrecido_en": None,
                            "origen": "web", "created_at": (_b + timedelta(minutes=i)).isoformat()} for i in (2, 3)]
_canc3.ofrecer_horario = lambda tenant, tel, cuando, link: _ofr.append((tel, cuando)) or True
chequear("pausa con fecha: ese día se reactiva", _canc3.reactivar_si_toca(dict(_t1), _hoy_ar + timedelta(days=10))
         and _t1["pausado"] is False and _t1["pausado_hasta"] is None)
chequear("pausa con fecha: a cada uno un horario distinto; si faltan, el resto queda en cola",
         [t for t, _ in _ofr] == ["5491144445555", "549110000002"]
         and _ofr[0][1] != _ofr[1][1] and "le avisamos a 2" in _avs[-1]
         and not next(p for p in TABLAS["lista_espera"] if p["id"] == "e3").get("ofrecido_en"), _ofr)
chequear("vuelta.ics: sin pausa no hay evento", app.app.test_client().get("/marcos/vuelta.ics").status_code == 404)
# Si alcanzan los horarios, cada uno queda guardado para esa persona y el profesional sabe a quién se le ofreció qué.
_t1.update(pausado=True, pausado_hasta=_hoy_ar.isoformat())
_en = datetime.now(timezone.utc)
TABLAS["turnos"] = [turno(f"g{i}", "t1", _en + timedelta(days=1, hours=i)) for i in (1, 2, 3)]
TABLAS["lista_espera"] = [{"id": f"w{i}", "tenant_id": "t1", "nombre": n, "telefono": tel, "ofrecido_en": None, "origen": "web",
                           "created_at": (_b + timedelta(minutes=i)).isoformat()}
                          for i, (n, tel) in enumerate([("Nora", "5491144445555"), ("Pía", "5491166667777")])]
_ofr, _avk = [], []
_canc3.ofrecer_horario = lambda tenant, tel, cuando, link: _ofr.append((tel, cuando, link)) or True
scheduler.avisar_profesional = lambda tenant, texto, **kw: _avk.append((texto, kw)) or True
chequear("reparto: reactiva", _canc3.reactivar_si_toca(dict(_t1), _hoy_ar))
_g = {t["id"]: t for t in TABLAS["turnos"]}
chequear("reparto: cada horario queda guardado para su persona, con link directo",
         _g["g1"]["guardado_tel"] == "5491144445555" and _g["g2"]["guardado_tel"] == "5491166667777" and not _g["g3"].get("guardado_tel")
         and f"/marcos/reservar/g1?g={_canc3.token_guardado('g1', '5491144445555')}" in _ofr[0][2]
         and "te lo guardamos hasta las" in _ofr[0][1] and "pedís otro" in _ofr[0][1], _ofr)
chequear("reparto: al profesional, a quién se le ofreció qué",
         "Nora:" in _avk[-1][1]["con_nombre"] and "Pía:" in _avk[-1][1]["con_nombre"]
         and "guardado 60 min" in _avk[-1][1]["con_nombre"] and "le avisamos a 2" in _avk[-1][0], _avk)
chequear("reparto: lo guardado no se ofrece a los demás",
         [t["id"] for t in db.turnos_libres("t1", _en)] == ["g3"])
chequear("reparto: otro teléfono no lo puede reservar", not db.reservar_turno("t1", "g1", "5491100009999", "Otro"))
_w = app.app.test_client()
_r = _w.post("/marcos/reservar/g1", data={"nombre": "Otro", "telefono": "5491100009999"}, headers={"X-Parcial": "1"})
chequear("reparto: la web explica que está guardado", _r.status_code == 409 and "guardado para otra persona" in _r.data.decode())
chequear("reparto: la página del horario dice hasta cuándo está guardado",
         "Este horario está guardado hasta las" in _w.get("/marcos/reservar/g1").data.decode())
chequear("reparto: la persona lo reserva aunque escriba el número distinto",
         db.reservar_turno("t1", "g1", "+54 9 11 4444-5555", "Nora") and _g["g1"]["guardado_hasta"] is None)
_g["g2"]["guardado_hasta"] = (_en - timedelta(minutes=1)).isoformat()
chequear("reparto: vencido el plazo, lo puede tomar cualquiera", db.reservar_turno("t1", "g2", "5491100009999", "Otro"))
_g["g3"].update(guardado_tel="5491100001111", guardado_hasta=(_en + timedelta(hours=1)).isoformat())
chequear("reparto: el panel puede agendar igual", db.reservar_turno("t1", "g3", "5491100008888", "Fijo", ignorar_bloqueo=True))

# Cola: más personas que horarios. Al vencer uno sin reservar, pasa al siguiente; «quiero otro» desde el link.
_t1.update(pausado=True, pausado_hasta=_hoy_ar.isoformat())
TABLAS["turnos"] = [turno(f"c{i}", "t1", _en + timedelta(days=2, hours=i)) for i in (1, 2)]
TABLAS["lista_espera"] = [{"id": f"q{i}", "tenant_id": "t1", "nombre": n, "telefono": tel, "ofrecido_en": None, "origen": "web",
                           "created_at": (_b + timedelta(minutes=i)).isoformat()}
                          for i, (n, tel) in enumerate([("Ana", "5491150000001"), ("Beto", "5491150000002"), ("Cata", "5491150000003")])]
_ofr, _avk = [], []
_canc3.reactivar_si_toca(dict(_t1), _hoy_ar)
_q = {p["id"]: p for p in TABLAS["lista_espera"]}
_c = {t["id"]: t for t in TABLAS["turnos"]}
chequear("cola: los dos primeros con su horario guardado, la tercera espera",
         [t for t, *_ in _ofr] == ["5491150000001", "5491150000002"] and _q["q0"]["ofrecido_estado"] == "esperando"
         and _q["q0"]["ofrecido_turno"] == "c1" and not _q["q2"].get("ofrecido_estado")
         and "1 más quedan en cola" in _avk[-1][1]["con_nombre"], (_ofr, _avk))
chequear("cola: antes de vencer no pasa nada", _canc3.pasar_vencidos(dict(_t1), _en + timedelta(minutes=30)) == 0)
_ahora_v = _en + timedelta(minutes=61)
db.reservar_turno("t1", "c2", "5491150000002", "Beto")
chequear("cola: vencido sin reservar, pasa a la siguiente apenas vence",
         _canc3.pasar_vencidos(dict(_t1), _ahora_v) == 1 and _q["q0"]["ofrecido_estado"] == "vencio"
         and _q["q2"]["ofrecido_turno"] == "c1" and _c["c1"]["guardado_tel"] == "5491150000003"
         and _ofr[-1][0] == "5491150000003" and "Ana se le venció" in _avk[-1][1]["con_nombre"]
         and "a Cata" in _avk[-1][1]["con_nombre"], (_ofr[-1:], _avk[-1:]))
chequear("cola: quien reservó queda «lo reservó» y no se pasa", _q["q1"]["ofrecido_estado"] == "reservo")
# El panel muestra qué se le ofreció a cada uno y cómo terminó
_esp = {p["id"]: p.get("_oferta") for p in app._espera_con_oferta(dict(_t1), app.ZoneInfo(TZ), _ahora_v)}
chequear("panel: lista de espera con el horario ofrecido y su estado",
         _esp["q0"]["estado"] == "vencio" and _esp["q1"]["estado"] == "reservo" and _esp["q2"]["estado"] == "esperando"
         and _esp["q2"]["hasta"], _esp)
_t1.update(pausado=False, pausado_hasta=None)
_html = panel_c.get("/agenda").data.decode()
chequear("panel: se ve en la agenda", "Le ofrecimos" in _html and "lo reservó" in _html and "se le venció" in _html)
# «Quiero otro»: Cata no quiere c1. Hay un libre nuevo c3; c1 vuelve a la cola (no queda nadie sin ofrecer hoy).
TABLAS["turnos"].append(turno("c3", "t1", _en + timedelta(days=3)))
_c = {t["id"]: t for t in TABLAS["turnos"]}
_c["c1"]["guardado_hasta"] = (datetime.now(timezone.utc) + timedelta(minutes=50)).isoformat()
_w2 = app.app.test_client()
_tok = _canc3.token_guardado("c1", "5491150000003")
chequear("quiero otro: la página del horario muestra el botón sólo con el token",
         "quiero otro horario" in _w2.get(f"/marcos/reservar/c1?g={_tok}").data.decode()
         and "quiero otro horario" not in _w2.get("/marcos/reservar/c1?g=falso").data.decode())
_r = _w2.post("/marcos/reservar/c1/otro", data={"g": "falso"})
chequear("quiero otro: sin token no hace nada", _r.status_code == 302 and _c["c1"]["guardado_tel"] == "5491150000003")
_r = _w2.post("/marcos/reservar/c1/otro", data={"g": _tok})
chequear("quiero otro: suelta el suyo y le guarda el siguiente",
         _r.status_code == 302 and "/marcos/reservar/c3?g=" in _r.headers["Location"]
         and _c["c1"]["guardado_tel"] is None and _c["c3"]["guardado_tel"] == "5491150000003"
         and _q["q2"]["ofrecido_turno"] == "c3", _r.headers.get("Location"))
chequear("quiero otro: el horario nuevo dice hasta cuándo está guardado y deja pedir otro",
         "Este horario está guardado hasta las" in _w2.get(_r.headers["Location"]).data.decode())
# Quien reserva sale solo de la lista de espera, y el profesional se entera.
chequear("resumen: cuántos esperan horario en la lista (sólo el número; no cuenta guardados ni reservados)",
         "1 persona en lista de espera esperando horario" in scheduler._partes_del_resumen(dict(_t1), _hoy_ar),
         scheduler._partes_del_resumen(dict(_t1), _hoy_ar))
TABLAS["lista_espera"].append({"id": "q9", "tenant_id": "t1", "nombre": "Eva", "telefono": "5491150000009", "ofrecido_en": None,
                               "origen": "web", "created_at": (_b + timedelta(minutes=9)).isoformat()})
chequear("resumen: «2 personas en lista de espera esperando horario»",
         "2 personas en lista de espera esperando horario" in scheduler._partes_del_resumen(dict(_t1), _hoy_ar),
         scheduler._partes_del_resumen(dict(_t1), _hoy_ar))
_r = _w2.post("/marcos/reservar/c3", data={"nombre": "Cata", "telefono": "5491150000003", "confirmado": "1"})
chequear("al reservar sale de la lista de espera y se le avisa al profesional",
         _c["c3"]["estado"] == "reservado" and not any(p["id"] == "q2" for p in TABLAS["lista_espera"])
         and "Cata reservó" in _avk[-1][1]["con_nombre"] and "lo sacamos de tu lista de espera" in _avk[-1][1]["con_nombre"], _avk[-1:])
TABLAS["lista_espera"].append({"id": "q8", "tenant_id": "t1", "nombre": "Lola", "telefono": "5491150000009", "ofrecido_en": None,
                               "origen": "web", "created_at": (_b + timedelta(minutes=10)).isoformat()})
chequear("mismo WhatsApp con otro nombre: no saca a la otra persona",
         _canc3.sacar_de_espera_al_reservar(dict(_t1), None, "5491150000009", "Eva", avisar=False) == ["Eva"]
         and any(p["id"] == "q8" for p in TABLAS["lista_espera"]))
# Si reserva otro horario teniendo uno guardado, el guardado pasa al siguiente.
TABLAS["turnos"] += [turno("x1", "t1", _en + timedelta(days=4)), turno("y1", "t1", _en + timedelta(days=5))]
_c = {t["id"]: t for t in TABLAS["turnos"]}
TABLAS["lista_espera"] = [
    {"id": "d1", "tenant_id": "t1", "nombre": "Dani", "telefono": "5491150000011", "ofrecido_en": _hoy_ar.isoformat(), "origen": "web",
     "ofrecido_turno": "x1", "ofrecido_estado": "esperando", "ofrecido_hasta": (_en + timedelta(minutes=40)).isoformat(),
     "created_at": _b.isoformat()},
    {"id": "d2", "tenant_id": "t1", "nombre": "Fede", "telefono": "5491150000012", "ofrecido_en": None, "origen": "web",
     "created_at": (_b + timedelta(minutes=1)).isoformat()}]
_c["x1"].update(guardado_tel="5491150000011", guardado_hasta=(_en + timedelta(minutes=40)).isoformat())
db.reservar_turno("t1", "y1", "5491150000011", "Dani")
_canc3.sacar_de_espera_al_reservar(dict(_t1), _c["y1"], "5491150000011", "Dani")
chequear("reservó otro: su horario guardado pasa al siguiente",
         [p["id"] for p in TABLAS["lista_espera"]] == ["d2"] and _c["x1"]["guardado_tel"] == "5491150000012"
         and TABLAS["lista_espera"][0]["ofrecido_turno"] == "x1", _c["x1"])
# «Ofrecer horario» del panel: entra en la misma cola (guardado 1 h, link directo).
TABLAS["lista_espera"].append({"id": "d3", "tenant_id": "t1", "nombre": "Gabi", "telefono": "5491150000013", "ofrecido_en": None,
                               "origen": "panel", "created_at": (_b + timedelta(minutes=2)).isoformat()})
_ofr.clear()
panel_c.post("/agenda/espera/d3/ofrecer", data={"csrf_token": "tok"})
_d3 = next(p for p in TABLAS["lista_espera"] if p["id"] == "d3")
chequear("ofrecer desde el panel: queda guardado una hora con link directo",
         _d3.get("ofrecido_estado") == "esperando" and _c[_d3["ofrecido_turno"]]["guardado_tel"] == "5491150000013"
         and "?g=" in _ofr[-1][2], (_d3, _ofr))
# Un guardado de las 23:30 vence a las 00:30: no vuelve a la misma persona aunque ya sea «otro día».
_noche = datetime(2026, 11, 12, 23, 30, tzinfo=app.ZoneInfo(TZ))
_lista_vieja = TABLAS["lista_espera"]
TABLAS["lista_espera"] = [
    {"id": "n1", "tenant_id": "t1", "nombre": "Noe", "telefono": "5491150000021", "origen": "web", "ofrecido_en": "2026-11-12",
     "ofrecido_turno": "x1", "ofrecido_estado": "vencio", "ofrecido_hasta": (_noche + timedelta(minutes=60)).isoformat(),
     "created_at": _b.isoformat()},
    {"id": "n2", "tenant_id": "t1", "nombre": "Ori", "telefono": "5491150000022", "origen": "web", "ofrecido_en": None,
     "created_at": (_b + timedelta(minutes=1)).isoformat()}]
_pasada = _noche + timedelta(minutes=61)
chequear("cola: pasada la medianoche, el vencido va al siguiente y no vuelve a quien lo dejó vencer",
         _canc3._siguiente(dict(_t1), _pasada)["id"] == "n2")
TABLAS["lista_espera"].pop()
chequear("cola: al otro día sí le puede volver a tocar",
         _canc3._siguiente(dict(_t1), _pasada) is None and _canc3._siguiente(dict(_t1), _noche + timedelta(hours=25))["id"] == "n1")
TABLAS["lista_espera"] = _lista_vieja
_canc3.ofrecer_horario, scheduler.avisar_profesional = _vo, _va

# horario semanal
reset()
_t1 = TABLAS["tenants"][0]
db.get_tenant_by_id = lambda tid: dict(_t1) if tid == "t1" else None
panel_c.post("/agenda/horario", data={"csrf_token": "tok", "dia0_desde1": "09:00", "dia0_hasta1": "11:00",
                                      "dia2_desde1": "15:00", "dia2_hasta1": "14:00"})
chequear("horario: rango al revés no se guarda", not _t1.get("horario_semanal"))
_previos = {t["id"] for t in TABLAS["turnos"]}
panel_c.post("/agenda/horario", data={"csrf_token": "tok", "dia0_desde1": "09:00", "dia0_hasta1": "11:00",
                                      "dia0_desde2": "15:00", "dia0_hasta2": "16:00", "horario_semanas": "2"})
_lunes = [t for t in TABLAS["turnos"] if t["id"] not in _previos]
_horas = sorted({datetime.fromisoformat(t["inicio"]).astimezone(app.ZoneInfo(TZ)).strftime("%H:%M") for t in _lunes})
# 9 a 11 con turnos de 50: 9:00 y 9:50 (el de 10:40 no entra); 15 a 16: 15:00. Dos lunes (o tres si hoy es lunes temprano).
chequear("horario: genera las franjas de los lunes", _horas == ["09:00", "09:50", "15:00"]
         and all(datetime.fromisoformat(t["inicio"]).astimezone(app.ZoneInfo(TZ)).weekday() == 0 for t in _lunes)
         and len(_lunes) in (5, 6, 7, 8, 9))
_antes = len(TABLAS["turnos"])
chequear("horario: generar de nuevo no duplica", scheduler.generar_desde_horario(dict(_t1)) == 0 and len(TABLAS["turnos"]) == _antes)
chequear("horario: el panel lo muestra", 'value="09:00"' in _panel(panel_c))
_tz_ar = app.ZoneInfo(TZ)
_hora = lambda t: datetime.fromisoformat(t["inicio"]).astimezone(_tz_ar).strftime("%H:%M")
_lunes = sorted([t for t in TABLAS["turnos"] if t["id"] not in _previos], key=lambda t: _norm(t["inicio"]))
_uno = next(t for t in _lunes if _hora(t) == "09:50")
r = panel_c.post(f"/agenda/libre/{_uno['id']}/sacar", data={"csrf_token": "tok"}, follow_redirects=True)
chequear("sacar: borra el horario libre", "Sacaste el horario" in r.data.decode()
         and not any(t["id"] == _uno["id"] for t in TABLAS["turnos"]) and len(TABLAS["horarios_sacados"]) == 1)
scheduler.generar_desde_horario(dict(_t1))
chequear("sacar: el scheduler no lo vuelve a crear", not any(_norm(t["inicio"]) == _norm(_uno["inicio"]) for t in TABLAS["turnos"]))
_res = next(t for t in _lunes if _hora(t) == "15:00")
_res.update(estado="reservado", paciente_nombre="Fija", paciente_telefono="5491100000077")
r = panel_c.post(f"/agenda/libre/{_res['id']}/sacar", data={"csrf_token": "tok"}, follow_redirects=True)
chequear("sacar: una sesión agendada no", "ya no está libre" in r.data.decode() and _res in TABLAS["turnos"])
_otros_15 = [t for t in _lunes if _hora(t) == "15:00" and t is not _res]
_otros_15[0]["hueco"] = "pendiente"
_mano_dia = (datetime.now(_tz_ar) + timedelta(days=3)).date()
if _mano_dia.weekday() == 0:   # el lunes ya tiene libres a las 15 del horario: no se crearía nada nuevo
    _mano_dia += timedelta(days=1)
panel_c.post("/agenda/generar", data={"csrf_token": "tok", "fecha_desde": _mano_dia.isoformat(), "fecha_hasta": _mano_dia.isoformat(),
                                      "hora_inicio": "15:00", "hora_fin": "16:00", "duracion_minutos": "50",
                                      "dias_semana": str(_mano_dia.weekday())})
_mano = [t for t in TABLAS["turnos"] if t.get("a_mano")]
chequear("generar a mano: queda marcado", len(_mano) == 1)
TABLAS["turnos"].append(turno("viejo-15", "t1", datetime.fromisoformat(_res["inicio"]) + timedelta(days=7)))
r = panel_c.post("/agenda/horario", data={"csrf_token": "tok", "dia0_desde1": "09:00", "dia0_hasta1": "11:00",
                                          "horario_semanas": "2"}, follow_redirects=True)
_quedan_15 = [t for t in TABLAS["turnos"] if t["id"] not in _previos and _hora(t) == "15:00"]
chequear("cambiar horario: saca los libres que ya no entran", "que ya no entraban" in r.data.decode()
         and _res in _quedan_15 and _otros_15[0] in _quedan_15 and _mano[0] in _quedan_15
         and not any(t.get("estado") == "libre" and not t.get("hueco") and not t.get("a_mano") for t in _quedan_15))
chequear("cambiar horario: los que entran siguen y el sacado no vuelve",
         any(_hora(t) == "09:00" for t in TABLAS["turnos"] if t["id"] not in _previos)
         and not any(_norm(t["inicio"]) == _norm(_uno["inicio"]) for t in TABLAS["turnos"]))
_d = datetime.fromisoformat(_uno["inicio"]).astimezone(_tz_ar).date()
panel_c.post("/agenda/generar", data={"csrf_token": "tok", "fecha_desde": _d.isoformat(), "fecha_hasta": _d.isoformat(),
                                      "hora_inicio": "09:50", "hora_fin": "10:40", "duracion_minutos": "50",
                                      "dias_semana": str(_d.weekday())})
chequear("generar a mano: vuelve a habilitar el sacado", not TABLAS["horarios_sacados"]
         and any(_norm(t["inicio"]) == _norm(_uno["inicio"]) for t in TABLAS["turnos"]))

# tipos de turno
reset()
_t1 = TABLAS["tenants"][0]
_t1.update({"slug": "marcos", "activo": True})
db.get_tenant_by_slug = lambda slug: dict(_t1) if slug == "marcos" else None
db.get_tenant_by_id = lambda tid: dict(_t1) if tid == "t1" else None
panel_c.post("/agenda/tipos", data={"csrf_token": "tok", "tipo1_nombre": "Primera entrevista", "tipo1_minutos": "100",
                                    "tipo2_nombre": "Sesión", "tipo2_minutos": "50"})
chequear("tipos: se guardan", [x["minutos"] for x in _t1["tipos_turno"]] == [100, 50])
# A media tarde (13 UTC): de noche, k3 (200 min después) cruzaba la medianoche y caía en otra semana.
_c = datetime.now(timezone.utc).replace(hour=13, minute=0, second=0, microsecond=0) + timedelta(days=3)
TABLAS["turnos"] = [turno("k1", "t1", _c), turno("k2", "t1", _c + timedelta(minutes=50)),
                    turno("k3", "t1", _c + timedelta(minutes=200))]
web5 = app.app.test_client()
html = web5.get("/marcos").data.decode()
chequear("tipos: primero pide elegir", "¿Qué turno querés?" in html and "reservar/k1" not in html)
html = web5.get("/marcos?tipo=0").data.decode()
chequear("tipos: el largo sólo donde entra", "reservar/k1" in html and "reservar/k2" not in html and "reservar/k3" not in html)
html = web5.get("/marcos?tipo=1").data.decode()
chequear("tipos: el corto en todos", all(f"reservar/{k}" in html for k in ("k1", "k2", "k3")))
r = web5.post("/marcos/reservar/k2", data={"nombre": "Ana", "telefono": "5491100003333", "tipo": "0", "confirmado": "1"})
chequear("tipos: el servidor no deja reservar el largo donde no entra", r.status_code == 302 and TABLAS["turnos"][1]["estado"] == "libre")
web5.post("/marcos/reservar/k1", data={"nombre": "Ana", "telefono": "5491100003333", "tipo": "0", "confirmado": "1"})
k1 = TABLAS["turnos"][0]
chequear("tipos: reserva con su duración", k1["estado"] == "reservado" and k1["tipo"] == "Primera entrevista"
         and datetime.fromisoformat(k1["fin"]) - datetime.fromisoformat(k1["inicio"]) == timedelta(minutes=100))
chequear("tipos: tapa el horario siguiente", "reservar/k2" not in web5.get("/marcos?tipo=1").data.decode())
chequear("tipos: el panel lo muestra", "Primera entrevista" in panel_c.get(f"/agenda?semana={_c.date().isoformat()}").data.decode())

# --- Descanso entre turnos (el caso de Marcos), modalidad por franja, precio, recordatorio y mes ---
reset()
_t1 = TABLAS["tenants"][0]
_t1.update({"slug": "marcos", "activo": True, "modalidades_publicas": "ambas", "link_videollamada": "https://meet.example/x"})
db.get_tenant_by_slug = lambda slug: dict(_t1) if slug == "marcos" else None
db.get_tenant_by_id = lambda tid: dict(_t1) if tid == "t1" else None
_previos = {t["id"] for t in TABLAS["turnos"]}
panel_c.post("/agenda/horario", data={"csrf_token": "tok", "duracion_turno_minutos": "45", "descanso_minutos": "15",
                                      "dia1_desde1": "09:15", "dia1_hasta1": "13:00",
                                      "dia1_desde2": "15:15", "dia1_hasta2": "17:00", "dia1_mod2": "virtual",
                                      "horario_semanas": "2"})
_nuevos = [t for t in TABLAS["turnos"] if t["id"] not in _previos]
_loc = lambda t: datetime.fromisoformat(t["inicio"]).astimezone(app.ZoneInfo(TZ))
_horas = sorted({_loc(t).strftime("%H:%M") for t in _nuevos})
chequear("Marcos: 45 + 15 desde y cuarto", _horas == ["09:15", "10:15", "11:15", "12:15", "15:15", "16:15"], _horas)
chequear("Marcos: cada uno termina en punto", all(
    datetime.fromisoformat(t["fin"]).astimezone(app.ZoneInfo(TZ)).strftime("%M") == "00" for t in _nuevos))
chequear("modalidad por franja: la tarde es por videollamada", all(
    (t.get("solo_modalidad") == "virtual") == (_loc(t).hour >= 15) for t in _nuevos))
_tarde = next((t for t in _nuevos if _loc(t).hour >= 15), None)
_maniana = next((t for t in _nuevos if _loc(t).hour < 13), None)
web6 = app.app.test_client()
if _tarde:
    html = web6.get(f"/marcos?semana={_loc(_tarde).date().isoformat()}").data.decode()
    chequear("modalidad por franja: el calendario lo marca", "<small>video</small>" in html)
    chequear("modalidad por franja: el formulario no deja elegir", "Este horario es por videollamada" in
             web6.get(f"/marcos/reservar/{_tarde['id']}").data.decode())
    web6.post(f"/marcos/reservar/{_tarde['id']}", data={"nombre": "Ana", "telefono": "5491100003333", "modalidad": "presencial",
                                                        "confirmado": "1"})
    chequear("modalidad por franja: se reserva como la franja", _tarde["modalidad"] == "virtual")
if _maniana:
    # una entrevista de 100 min arranca a las 9:15, sigue en el de 10:15 (salteando el descanso) y entra
    _t1["tipos_turno"] = [{"nombre": "Primera entrevista", "minutos": 100, "precio": 30000}, {"nombre": "Sesión", "minutos": 45}]
    _9 = next(t for t in _nuevos if _loc(t).strftime("%H:%M") == "09:15")
    _12 = next((t for t in _nuevos if _loc(t).strftime("%H:%M") == "12:15" and _loc(t).date() == _loc(_9).date()), None)
    html = web6.get(f"/marcos?tipo=0&semana={_loc(_9).date().isoformat()}").data.decode()
    chequear("descanso: el turno largo salta el descanso", f"reservar/{_9['id']}" in html)
    if _12:
        chequear("descanso: pero no después de la última franja", f"reservar/{_12['id']}" not in html)
    web6.post(f"/marcos/reservar/{_9['id']}", data={"nombre": "Bea", "telefono": "5491100004444", "tipo": "0", "confirmado": "1"})
    chequear("precio por tipo: la sesión queda con su valor", _9["valor"] == 30000 and _9["tipo"] == "Primera entrevista")
    chequear("recordatorio: dice el tipo y la duración", "(Primera entrevista de 100 min)" in
             scheduler.cuando_de(_9, dict(_t1), app.ZoneInfo(TZ)))
html = web6.get("/marcos?vista=mes&tipo=1").data.decode()
chequear("mes: muestra el mes con los días que tienen lugar", "Lu" in html and "libre" in html and "semana=" in html)
chequear("mes: se puede volver a la semana", 'vista=mes' in web6.get("/marcos?tipo=1").data.decode())

# --- Cobro de las reservas web ---
import pagos as _pg  # noqa: E402

reset()
_t1 = TABLAS["tenants"][0]
_t1.update({"slug": "marcos", "activo": True, "cobro_web": "transferencia", "precio_sesion": 25000,
            "transferencia_datos": "Alias: marcos.mp", "aviso_telefono": "5491122223333"})
db.get_tenant_by_slug = lambda slug: dict(_t1) if slug == "marcos" else None
db.get_tenant_by_id = lambda tid: dict(_t1) if tid == "t1" else None
_avp = []
_va3 = scheduler.avisar_profesional
scheduler.avisar_profesional = lambda tenant, texto, **_kw: _avp.append(texto) or True
_d = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(days=3)
TABLAS["turnos"] = [turno("p1", "t1", _d), turno("p2", "t1", _d + timedelta(hours=1)), turno("p3", "t1", _d + timedelta(hours=2))]
w = app.app.test_client()
chequear("cobro: el resumen avisa cuánto hay que pagar", "hay que pagar" in w.post("/marcos/reservar/p1", data={
    "nombre": "Ana", "telefono": "5491100003333"}).data.decode())
r = w.post("/marcos/reservar/p1", data={"nombre": "Ana", "telefono": "5491100003333", "confirmado": "1"})
p1 = TABLAS["turnos"][0]
pago = TABLAS["pagos"][-1]
chequear("cobro: la reserva queda apartada esperando el pago", r.status_code == 302 and "/pago/" in r.headers["Location"]
         and p1["estado"] == "reservado" and p1["pago_web"] == "esperando" and pago["monto"] == 25000)
r = w.post("/marcos/reservar/p2", data={"nombre": "Bea", "telefono": "5491100004444", "confirmado": "1"}, headers={"X-Parcial": "1"})
chequear("cobro desde la misma página: el navegador va al pago", r.status_code == 200 and "/pago/" in (r.headers.get("X-Ir-A") or "")
         and TABLAS["turnos"][1]["pago_web"] == "esperando")
html = w.get(f"/pago/{pago['token']}").data.decode()
chequear("cobro: la página muestra el alias y el monto", "Alias: marcos.mp" in html and "$ 25.000" in html and "Enviar comprobante" in html)
chequear("cobro: no se recuerda mientras no paga", "p1" not in [t["id"] for t in [x for x in db.turnos_para_recordar("t1", 24 * 5)
                                                                   if x.get("pago_web") not in ("esperando", "comprobante")]])
import io as _io2  # noqa: E402
w.post(f"/pago/{pago['token']}/comprobante", data={"comprobante": (_io2.BytesIO(b"<html>x"), "x.html")}, content_type="multipart/form-data")
chequear("cobro: rechaza archivos que no son foto o PDF", pago["estado"] == "esperando")
chequear("cobro: sin email pide el email", 'name="email"' in w.get(f"/pago/{pago['token']}").data.decode())
w.post(f"/pago/{pago['token']}/comprobante", data={"comprobante": (_io2.BytesIO(b"%PDF-1.4 ..."), "c.pdf")}, content_type="multipart/form-data")
chequear("cobro: sin email no se recibe el comprobante", pago["estado"] == "esperando")
w.post(f"/pago/{pago['token']}/comprobante", data={"comprobante": (_io2.BytesIO(b"%PDF-1.4 ..."), "c.pdf"), "email": "pepa@ejemplo.com"},
       content_type="multipart/form-data")
chequear("cobro: guarda el email y cuándo subió el comprobante", pago.get("paciente_email") == "pepa@ejemplo.com"
         and pago.get("comprobante_at"))
chequear("cobro: recibe el comprobante y avisa", pago["estado"] == "comprobante" and p1["pago_web"] == "comprobante"
         and "comprobante" in _avp[-1])
html = _panel(panel_c)
chequear("cobro: el panel lo muestra para validar", "Pagos para revisar" in html and "Ver comprobante" in html)
r = panel_c.get(f"/agenda/pago-web/{pago['id']}/comprobante")
chequear("cobro: el comprobante se ve", r.status_code == 200 and r.mimetype == "application/pdf")
panel_c.post(f"/agenda/pago-web/{pago['id']}/aceptar", data={"csrf_token": "tok"})
chequear("cobro: aceptar confirma y marca pagado", pago["estado"] == "aprobado" and p1["pago_web"] == "aprobado"
         and p1["pago"] == "pagado" and p1["confirmado_at"] and pago["comprobante"] is None)

# rechazar
w.post("/marcos/reservar/p2", data={"nombre": "Beto", "telefono": "5491100004444", "confirmado": "1"})
pago2 = TABLAS["pagos"][-1]
w.post(f"/pago/{pago2['token']}/comprobante", data={"comprobante": (_io2.BytesIO(b"\x89PNG\r\n\x1a\n00"), "c.png"), "email": "beto@ejemplo.com"},
       content_type="multipart/form-data")
chequear("cobro: el panel ofrece motivos de rechazo", "El monto no coincide" in _panel(panel_c))
_rz_llamadas = []
_rz_ar, _rz_aw = _pg.avisar_rechazo, _pg.avisar_rechazo_whatsapp
_pg.avisar_rechazo = lambda tenant, pago, motivo=None: _rz_llamadas.append(("mail", pago.get("paciente_email"), motivo)) or True
_pg.avisar_rechazo_whatsapp = lambda tenant, telefono: _rz_llamadas.append(("wa", telefono)) or True
panel_c.post(f"/agenda/pago-web/{pago2['id']}/rechazar", data={"csrf_token": "tok", "motivo": "el monto no coincide"})
_pg.avisar_rechazo, _pg.avisar_rechazo_whatsapp = _rz_ar, _rz_aw
chequear("rechazo: avisa al paciente por mail con el motivo y por WhatsApp a su teléfono",
         ("mail", "beto@ejemplo.com", "el monto no coincide") in _rz_llamadas and ("wa", "5491100004444") in _rz_llamadas, _rz_llamadas)
import mail as _mail_rz  # noqa: E402
import whatsapp as _wa_rz  # noqa: E402
_rz_mails, _rz_wa = [], []
_rz_prev = (_mail_rz.disponible, _mail_rz.enviar, _pg.config.DOMINIO_PRINCIPAL, _pg.config.PAGO_RECHAZADO_TEMPLATE)
_rz_ep, _rz_nd = _wa_rz.enviar_plantilla, _wa_rz.numero_de
_mail_rz.disponible = lambda: True
_mail_rz.enviar = lambda para, asunto, texto, adjuntos=None: _rz_mails.append((para, asunto, texto)) or True
_pg.config.DOMINIO_PRINCIPAL = "consultorio.fibot.ar"
_pg.config.PAGO_RECHAZADO_TEMPLATE = "pago_rechazado"
_wa_rz.enviar_plantilla = lambda numero, to, plantilla, lang, params: _rz_wa.append((to, plantilla, params)) or True
_wa_rz.numero_de = lambda tenant: "PNID"
_pg.rechazar(dict(_t1, slug="marcos"), {"id": "x", "turnos": [], "paciente_email": "beto@ejemplo.com"}, "el monto no coincide")
chequear("rechazo: el mail lleva el motivo", _rz_mails and "Motivo: el monto no coincide." in _rz_mails[-1][2], _rz_mails)
_pg.rechazar(dict(_t1, slug="marcos"), {"id": "x", "turnos": [], "paciente_email": "beto@ejemplo.com"}, "<b>inventado</b>")
chequear("rechazo: un motivo inventado no viaja", "Motivo" not in _rz_mails[-1][2] and "inventado" not in _rz_mails[-1][2])
chequear("rechazo: WhatsApp con la plantilla, profesional y link", _pg.avisar_rechazo_whatsapp(dict(_t1, slug="marcos"), "5491100004444")
         and _rz_wa[-1] == ("5491100004444", "pago_rechazado", [_t1["nombre"], "https://consultorio.fibot.ar/marcos"]), _rz_wa)
_wa_rz.enviar_plantilla = lambda numero, to, plantilla, lang, params, boton_url=None: _rz_wa.append((to, plantilla, params, boton_url)) or True
_pg.config.PAGO_RECHAZADO_BOTON = True
chequear("rechazo: con botón de link, el slug va en el botón y no en el texto",
         _pg.avisar_rechazo_whatsapp(dict(_t1, slug="marcos"), "5491100004444")
         and _rz_wa[-1] == ("5491100004444", "pago_rechazado", [_t1["nombre"]], "marcos"), _rz_wa)
_pg.config.PAGO_RECHAZADO_BOTON = False
_pg.config.PAGO_RECHAZADO_TEMPLATE = ""
chequear("rechazo: sin plantilla no hay WhatsApp", _pg.avisar_rechazo_whatsapp(dict(_t1, slug="marcos"), "5491100004444") is False)
_mail_rz.disponible, _mail_rz.enviar, _pg.config.DOMINIO_PRINCIPAL, _pg.config.PAGO_RECHAZADO_TEMPLATE = _rz_prev
_wa_rz.enviar_plantilla, _wa_rz.numero_de = _rz_ep, _rz_nd
chequear("cobro: rechazar libera el horario", pago2["estado"] == "rechazado" and TABLAS["turnos"][1]["estado"] == "libre")

# vencimiento
w.post("/marcos/reservar/p3", data={"nombre": "Ceci", "telefono": "5491100005555", "confirmado": "1"})
pago3 = TABLAS["pagos"][-1]
_pg.vencer(dict(_t1), datetime.now(timezone.utc) + timedelta(hours=1))
chequear("cobro: sin pagar a tiempo, se libera", pago3["estado"] == "esperando")
_pg.vencer(dict(_t1), datetime.now(timezone.utc) + timedelta(hours=3))
chequear("cobro: vencido se libera", pago3["estado"] == "vencido" and TABLAS["turnos"][2]["estado"] == "libre")

# Mercado Pago
_t1.update({"cobro_web": "mercadopago", "mp_token_cifrado": _pg.cifrar("TEST-token-del-profesional"),
            "mp_vence": (datetime.now(timezone.utc) + timedelta(days=100)).isoformat()})
chequear("cifrado: ida y vuelta, y no queda en claro", _pg.descifrar(_t1["mp_token_cifrado"]) == "TEST-token-del-profesional"
         and "token-del" not in _t1["mp_token_cifrado"])
_vc = (_pg.config.MP_CLIENT_ID, _pg.config.MP_CLIENT_SECRET)
_pg.config.MP_CLIENT_ID, _pg.config.MP_CLIENT_SECRET = "app", "secreto"
_mp = {"pagos": {}}


class _RMP:
    def __init__(self, codigo, datos):
        self.status_code, self._d, self.text = codigo, datos, str(datos)

    def json(self):
        return self._d


def _mp_post(url, json=None, headers=None, timeout=None):
    _mp["pref"] = (url, json, headers)
    return _RMP(201, {"id": "pref1", "init_point": "https://mp.example/pagar"})


_pg.requests.post = _mp_post
_pg.requests.get = lambda url, headers=None, timeout=None: _RMP(200, _mp["pagos"][url.rsplit("/", 1)[1]])
TABLAS["turnos"][0].update(db._LIBERADO)
w.post("/marcos/reservar/p1", data={"nombre": "Dani", "telefono": "5491100006666", "confirmado": "1"})
pago4 = TABLAS["pagos"][-1]
r = w.post(f"/pago/{pago4['token']}/mercadopago")
chequear("MP: abre el checkout con la cuenta del profesional", r.headers.get("Location") == "https://mp.example/pagar"
         and _mp["pref"][2]["Authorization"] == "Bearer TEST-token-del-profesional"
         and _mp["pref"][1]["external_reference"] == pago4["token"] and _mp["pref"][1]["items"][0]["unit_price"] == 25000.0
         and "t=t1" in _mp["pref"][1]["notification_url"])
chequear("MP: manda los datos que pide la calidad de Mercado Pago",
         _mp["pref"][1]["statement_descriptor"] == _pg.descriptor_resumen(TABLAS["tenants"][0])
         and _mp["pref"][1]["payer"] == {"name": "Dani"} and _mp["pref"][1]["items"][0]["category_id"] == "services"
         and _mp["pref"][1]["items"][0]["description"] and _mp["pref"][2]["X-Idempotency-Key"] == f"pref-{pago4['token']}",
         _mp["pref"][1])
chequear("MP: el resumen de la tarjeta sin tildes ni símbolos y hasta 22",
         _pg.descriptor_resumen({"nombre": "Lic. María José Pérez-Núñez (Psicóloga)"}) == "Lic Maria Jose Perez N"
         and _pg.descriptor_resumen({"nombre": "¿?"}) == "Consultorio")
chequear("MP: nombre y apellido del paciente", _pg._comprador("Ana  María Gómez") == {"name": "Ana", "surname": "María Gómez"}
         and _pg._comprador("") is None)
_mp["pagos"]["111"] = {"status": "approved", "external_reference": pago4["token"], "transaction_amount": 100}
app.app.test_client().post("/webhook/mercadopago?t=t1", json={"type": "payment", "data": {"id": "111"}})
chequear("MP: con monto menor no aprueba", pago4["estado"] == "esperando")
_mp["pagos"]["112"] = {"status": "approved", "external_reference": pago4["token"], "transaction_amount": 25000}
r = app.app.test_client().post("/webhook/mercadopago?t=t1", json={"type": "payment", "data": {"id": "112"}})
chequear("MP: el aviso aprueba la reserva", r.status_code == 200 and pago4["estado"] == "aprobado"
         and TABLAS["turnos"][0]["pago_web"] == "aprobado" and pago4["mp_pago_id"] == "112")
app.app.test_client().post("/webhook/mercadopago?t=t1", json={"type": "payment", "data": {"id": "112"}})
chequear("MP: avisos repetidos no rompen nada", pago4["estado"] == "aprobado")
_ver = db.get_tenant_by_id
db.get_tenant_by_id = lambda tid: (_ for _ in ()).throw(Exception("invalid input syntax for type uuid"))
chequear("MP: aviso sin profesional o con id inválido contesta 200",
         app.app.test_client().post("/webhook/mercadopago", json={}).status_code == 200
         and app.app.test_client().post("/webhook/mercadopago?t=cualquiera", json={"type": "payment", "data": {"id": "1"}}).status_code == 200)
db.get_tenant_by_id = _ver
chequear("MP: la página dice confirmado", "tu turno está confirmado" in w.get(f"/pago/{pago4['token']}").data.decode())
# pago tardío
TABLAS["turnos"][1].update(db._LIBERADO)
w.post("/marcos/reservar/p2", data={"nombre": "Eli", "telefono": "5491100007777", "confirmado": "1"})
pago5 = TABLAS["pagos"][-1]
_pg.vencer(dict(_t1), datetime.now(timezone.utc) + timedelta(hours=1))
_mp["pagos"]["113"] = {"status": "approved", "external_reference": pago5["token"], "transaction_amount": 25000}
w.get(f"/pago/{pago5['token']}?payment_id=113&status=approved")
chequear("MP: pago tardío queda para devolver y avisa", pago5["estado"] == "vencido" or pago5["estado"] == "aprobado_tarde")
_pg.revisar_pago_mp(dict(_t1), "113")
chequear("MP: pago tardío se marca y se avisa", pago5["estado"] == "aprobado_tarde" and "ya se había liberado" in _avp[-1])
chequear("MP: un pago de otro profesional no se toca", _pg.revisar_pago_mp(dict(_t1, id="otro"), "113") is None)

# devoluciones hechas desde Mercado Pago (por fuera del panel)
_p4 = {k: pago4.get(k) for k in ("estado", "devuelto", "a_devolver", "devuelto_at")}
_mp["pagos"]["112"] = {"status": "approved", "external_reference": pago4["token"], "transaction_amount": 25000,
                       "transaction_amount_refunded": 10000}
app.app.test_client().post("/webhook/mercadopago?t=t1", json={"type": "payment", "data": {"id": "112"}})
chequear("MP afuera: devolución parcial se anota", pago4["devuelto"] == 10000 and pago4["estado"] == "aprobado")
app.app.test_client().post("/webhook/mercadopago?t=t1", json={"type": "payment", "data": {"id": "112"}})
chequear("MP afuera: el aviso repetido no suma de nuevo", pago4["devuelto"] == 10000)
_mp["pagos"]["112"] = {"status": "refunded", "external_reference": pago4["token"], "transaction_amount": 25000,
                       "transaction_amount_refunded": 25000}
app.app.test_client().post("/webhook/mercadopago?t=t1", json={"type": "payment", "data": {"id": "112"}})
chequear("MP afuera: devolución total deja el pago devuelto", pago4["devuelto"] == 25000 and pago4["estado"] == "devuelto")
chequear("MP afuera: la sesión sigue reservada pero deja de figurar paga",
         TABLAS["turnos"][0]["estado"] == "reservado" and TABLAS["turnos"][0]["pago"] == "pendiente"
         and TABLAS["turnos"][0]["pago_web"] == "devuelto")
TABLAS["turnos"][0].update({"pago": "pagado", "pago_web": "aprobado"})
_mp["pagos"]["119"] = {"status": "refunded", "external_reference": pago4["token"], "transaction_amount": 25000}
pago4.update(_p4)
_pg.revisar_pago_mp(dict(_t1), "119")
chequear("MP afuera: otro pago con la misma referencia no toca este", pago4["devuelto"] == _p4["devuelto"])
_pp = _pg.requests.post
_pg.requests.post = lambda *a, **k: _RMP(401, {"message": "Unauthorized use of live credentials"})
_e = _pg.devolver(dict(_t1), pago4)
chequear("MP: sin permiso para devolver lo dice claro y no anota", "no permite devolver" in (_e or "")
         and "saldo" not in _e and pago4["devuelto"] == _p4["devuelto"])
_pg.requests.post = _pp
_mp["pagos"]["112"] = {"status": "approved", "external_reference": pago4["token"], "transaction_amount": 25000}

# el panel abierto se entera de las novedades
with client.session_transaction() as s:
    s["tenant_id"], s["sesion_id"], s["csrf_token"] = "t1", _sesion_de("t1"), "tok"
_v1 = client.get("/agenda/novedades").get_json()["v"]
_v2 = client.get("/agenda/novedades").get_json()["v"]
pago4["devuelto"] = (pago4.get("devuelto") or 0) + 1
_v3 = client.get("/agenda/novedades").get_json()["v"]
pago4["devuelto"] -= 1
chequear("novedades: misma firma si no cambió nada y otra si cambió un pago", _v1 and _v1 == _v2 and _v3 != _v1)
_html = _panel(client)
chequear("novedades: el panel trae el aviso y su versión", 'id="novedades"' in _html and _v1 in _html)
chequear("novedades: sin sesión no responde", app.app.test_client().get("/agenda/novedades").status_code in (302, 401, 403))

# email opcional al reservar con cobro, recibo por mail y conexión caída
TABLAS["turnos"][2].update(db._LIBERADO)
chequear("email: el form lo ofrece cuando se cobra", 'name="email"' in w.get("/marcos/reservar/p3").data.decode())
html = w.post("/marcos/reservar/p3", data={"nombre": "Fede Paz", "telefono": "5491100008888", "email": "no-es-mail"},
              follow_redirects=True).data.decode()
chequear("email: uno mal escrito se rechaza", "Revisá el email" in html, html[-300:])
html = w.post("/marcos/reservar/p3", data={"nombre": "Fede Paz", "telefono": "5491100008888", "email": "fede@ejemplo.com"}).data.decode()
chequear("email: se muestra al revisar", "fede@ejemplo.com" in html and 'name="email"' in html)
w.post("/marcos/reservar/p3", data={"nombre": "Fede Paz", "telefono": "5491100008888", "email": "fede@ejemplo.com", "confirmado": "1"})
pago6 = TABLAS["pagos"][-1]
chequear("email: queda sólo en el pago", pago6.get("paciente_email") == "fede@ejemplo.com"
         and not any("fede@ejemplo.com" in str(f) for f in TABLAS["pacientes"]))
_pg.requests.post = _mp_post
w.post(f"/pago/{pago6['token']}/mercadopago")
chequear("email: va a Mercado Pago con nombre y apellido", _mp["pref"][1]["payer"] == {"name": "Fede", "surname": "Paz", "email": "fede@ejemplo.com"})
import mail as _mail
_mails = []
_md, _me, _dom = _mail.disponible, _mail.enviar, _pg.config.DOMINIO_PRINCIPAL
_mail.disponible = lambda: True
_mail.enviar = lambda para, asunto, texto, adjuntos=None: _mails.append((para, asunto, texto, adjuntos)) or True
_pg.config.DOMINIO_PRINCIPAL = "consultorio.fibot.ar"
_mp["pagos"]["114"] = {"status": "approved", "external_reference": pago6["token"], "transaction_amount": 25000}
app.app.test_client().post("/webhook/mercadopago?t=t1", json={"type": "payment", "data": {"id": "114"}})
chequear("recibo: al aprobar le llega el link por mail", pago6["estado"] == "aprobado" and len(_mails) == 1
         and _mails[0][0] == "fede@ejemplo.com" and f"/pago/{pago6['token']}/recibo" in _mails[0][2]
         and "Fede" not in _mails[0][2], _mails)
chequear("recibo: va el PDF adjunto", _mails and _mails[0][3] and _mails[0][3][0][0].startswith("recibo-")
         and _mails[0][3][0][1].startswith(b"%PDF") and "adjuntamos el recibo en PDF" in _mails[0][2])
chequear("recibo: sin email no manda nada", _pg.mandar_recibo(_t1, {**pago6, "paciente_email": None}) is False)
_n_mails = len(_mails)
chequear("rechazo: al paciente le llega por mail con el link para reservar de nuevo",
         _pg.avisar_rechazo({**_t1, "slug": "marcos"}, {**pago6, "paciente_email": "fede@ejemplo.com"})
         and _mails[-1][0] == "fede@ejemplo.com" and "no se confirmó" in _mails[-1][1]
         and "https://consultorio.fibot.ar/marcos" in _mails[-1][2] and "Fede" not in _mails[-1][2], _mails[-1:])
chequear("rechazo: sin email no manda nada", _pg.avisar_rechazo(_t1, {**pago6, "paciente_email": None}) is False
         and len(_mails) == _n_mails + 1)
_mail.disponible, _mail.enviar, _pg.config.DOMINIO_PRINCIPAL = _md, _me, _dom
_avs_mp = []
_va_mp = scheduler.avisar_profesional
scheduler.avisar_profesional = lambda tenant, texto, **_kw: _avs_mp.append(texto) or True
_avs_adm = []
_vadm = whatsapp_real.avisar_admin
whatsapp_real.avisar_admin = lambda texto: _avs_adm.append(texto) or True
_pg._pausa = lambda segundos: None
_get_mp_prev = _pg.requests.get
_me_resp = [200]
_pg.requests.get = lambda url, headers=None, timeout=None: (_RMP(_me_resp.pop(0) if len(_me_resp) > 1 else _me_resp[0], {"id": 99})
                                                            if url.endswith("/users/me") else _get_mp_prev(url, headers=headers, timeout=timeout))
_pg.requests.post = lambda url, json=None, headers=None, timeout=None: _RMP(401, {"message": "invalid access token"})
chequear("caída: un rechazo pasajero no corta la conexión", _pg.preferencia(_t1, pago6, "x", "http://x") is None
         and not _t1.get("mp_caido_at") and not _avs_mp)
_me_resp[:] = [401, 200]
_pg.preferencia(_t1, pago6, "x", "http://x")
chequear("caída: si al reintentar acepta, tampoco se corta", not _t1.get("mp_caido_at") and not _avs_mp)
_me_resp[:] = [401]
chequear("caída: si Mercado Pago rechaza el permiso no hay link", _pg.preferencia(_t1, pago6, "x", "http://x") is None)
_pg.preferencia(_t1, pago6, "x", "http://x")
chequear("caída: se marca y avisa una sola vez", _t1.get("mp_caido_at") and len(_avs_mp) == 1 and "Conectar Mercado Pago" in _avs_mp[0])
chequear("caída: administración también se entera", len(_avs_adm) == 1 and _t1["nombre"] in _avs_adm[0])
chequear("caída: deja de ofrecer Mercado Pago", "mercadopago" not in _pg.metodos(_t1))
chequear("caída: el panel pide reconectar", "Se cortó la conexión con Mercado Pago" in _panel(panel_c))
_pg._guardar_tokens("t1", {"access_token": "TEST-token-del-profesional", "user_id": 99})
chequear("caída: reconectar la limpia", not _t1.get("mp_caido_at") and "mercadopago" in _pg.metodos(_t1))
_get_prev2 = _pg.requests.get
_pg.requests.get = lambda url, headers=None, timeout=None: _RMP(401, {})
_pg.verificar_conexion(_t1, datetime.now(timezone.utc).date())
chequear("caída: el chequeo diario la detecta", bool(_t1.get("mp_caido_at")) and len(_avs_mp) == 2)
_pg._guardar_tokens("t1", {"access_token": "TEST-token-del-profesional", "user_id": 99})
_pg.verificar_conexion(_t1, datetime.now(timezone.utc).date())
chequear("caída: el chequeo es una vez por día", not _t1.get("mp_caido_at"))
_pg.requests.get = _get_prev2
_t1.update({"mp_vence": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(), "mp_refresh_cifrado": _pg.cifrar("TG-refresh")})
_renov = [_RMP(400, {"error": "invalid_grant"}), _RMP(200, {"access_token": "TEST-renovado", "user_id": 99, "expires_in": 15552000})]
_pg.requests.post = lambda url, json=None, headers=None, timeout=None: _renov.pop(0)
chequear("caída: si renovar falla una vez, reintenta y sigue", _pg._token(_t1) == "TEST-renovado" and not _t1.get("mp_caido_at"))
_t1.update({"mp_vence": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(), "mp_refresh_cifrado": _pg.cifrar("TG-refresh")})
_pg.requests.post = lambda url, json=None, headers=None, timeout=None: _RMP(400, {"error": "invalid_grant"})
chequear("caída: si no se puede renovar el permiso", _pg._token(_t1) is None and bool(_t1.get("mp_caido_at")))
_pg._guardar_tokens("t1", {"access_token": "TEST-token-del-profesional", "user_id": 99})
_t1.update({"mp_vence": (datetime.now(timezone.utc) + timedelta(days=100)).isoformat(), "mp_refresh_cifrado": None})
_pg.requests.post = _mp_post
scheduler.avisar_profesional = _va_mp
whatsapp_real.avisar_admin = _vadm
_pg.requests.get = _get_mp_prev

# token de prueba pegado a mano (solo con MP_PRUEBA)
_guardado = {k: _t1.get(k) for k in ("mp_user_id", "mp_token_cifrado", "mp_refresh_cifrado", "mp_vence", "cobro_web")}
_get_prev, _prueba_prev = _pg.requests.get, _pg.config.MP_PRUEBA
_pg.requests.get = lambda url, headers=None, timeout=None: (
    _RMP(200, {"id": 3815332524}) if headers and headers.get("Authorization") == "Bearer APP_USR-bueno" else _RMP(401, {}))
_pg.config.MP_PRUEBA = False
chequear("token de prueba: sin MP_PRUEBA no se acepta", _pg.conectar_con_token("t1", "APP_USR-bueno") is not None
         and _t1["mp_token_cifrado"] == _guardado["mp_token_cifrado"])
_pg.config.MP_PRUEBA = True
chequear("token de prueba: rechaza lo que no es un token", _pg.conectar_con_token("t1", "hola que tal") is not None)
chequear("token de prueba: rechaza uno que Mercado Pago no reconoce", _pg.conectar_con_token("t1", "APP_USR-malo") is not None)
with client.session_transaction() as s:
    s["tenant_id"], s["sesion_id"], s["csrf_token"] = "t1", _sesion_de("t1"), "tok"
_t1["cobro_web"] = "transferencia"
r = client.post("/agenda/mercadopago/token-prueba", data={"token": " APP_USR-bueno "})
chequear("conectar MP activa el cobro sin sacar la transferencia", _t1["cobro_web"] == "ambos")
_t1["cobro_web"] = "no"
client.post("/agenda/mercadopago/token-prueba", data={"token": "APP_USR-bueno"})
chequear("conectar MP con el cobro apagado lo prende con Mercado Pago", _t1["cobro_web"] == "mercadopago")
chequear("token de prueba: lo valida, lo guarda cifrado y sin refresh",
         _pg.descifrar(_t1["mp_token_cifrado"]) == "APP_USR-bueno" and "APP_USR-bueno" not in _t1["mp_token_cifrado"]
         and _t1["mp_user_id"] == "3815332524" and not _t1.get("mp_refresh_cifrado"))
_pg.requests.get, _pg.config.MP_PRUEBA = _get_prev, _prueba_prev
_t1.update(_guardado)

# vuelta de Mercado Pago con error: mensaje claro y nada guardado
def _vuelta(qs, state="st-ok"):
    with client.session_transaction() as s_:
        s_["mp_state"] = state
    return client.get("/mercadopago/vuelta?" + qs, follow_redirects=True).data.decode()
TABLAS["actividad"] = []
_html = _vuelta("error=access_denied&state=st-ok")
chequear("vuelta MP: cancelar se explica", "Cancelaste la conexión" in _html and _t1["mp_token_cifrado"] == _guardado["mp_token_cifrado"])
_html = _vuelta("error=invalid_client%3Cb%3E&state=st-ok")
chequear("vuelta MP: otro error dice qué probar y el código limpio", "no dejó conectar tu cuenta" in _html
         and "ventana privada" in _html and "invalid_clientb" in _html and "<b>" not in _html.split("Mercado Pago dijo")[1][:40])
chequear("vuelta MP: el intento queda en la actividad", any("no pudo conectar Mercado Pago" in (a.get("accion") or a.get("detalle") or str(a))
                                                            for a in TABLAS["actividad"]), TABLAS["actividad"])
_html = _vuelta("code=abc&state=otro")
chequear("vuelta MP: state que no coincide explica que vuelva a tocar", "venció o se abrió en otro navegador" in _html)
_html = _vuelta("state=st-ok")
chequear("vuelta MP: sin código dice qué probar", "no dejó conectar tu cuenta" in _html)
_post_prev = _pg.requests.post
_pg.requests.post = lambda url, json=None, headers=None, timeout=None: _RMP(400, {"error": "invalid_grant"})
_html = _vuelta("code=abc&state=st-ok")
chequear("vuelta MP: el canje rechazado muestra el motivo y qué probar", "no aceptó la conexión (invalid_grant)" in _html
         and "ventana privada" in _html)
_pg.requests.post = _post_prev
_t1.update(_guardado)

# seña
chequear("seña: cobra la seña por sesión", _pg.monto_de({"cobro_modo": "sena", "sena_monto": 10000}, 25000, 3) == 30000
         and _pg.monto_de({"cobro_modo": "total"}, 25000, 2) == 50000 and _pg.monto_de({"cobro_modo": "total"}, None) is None)
_pg.config.MP_CLIENT_ID, _pg.config.MP_CLIENT_SECRET = _vc
scheduler.avisar_profesional = _va3

# --- Varias sesiones pagas, feriados, consultorios e imprimir ---
import feriados as _fer  # noqa: E402

reset()
TABLAS["pagos"] = []
_t1 = TABLAS["tenants"][0]
_t1.update({"slug": "marcos", "activo": True, "cobro_web": "transferencia", "precio_sesion": 20000,
            "transferencia_datos": "Alias: x", "aviso_telefono": "5491122223333"})
db.get_tenant_by_slug = lambda slug: dict(_t1) if slug == "marcos" else None
db.get_tenant_by_id = lambda tid: dict(_t1) if tid == "t1" else None
_va4 = scheduler.avisar_profesional
scheduler.avisar_profesional = lambda tenant, texto, **_kw: True
_s = datetime.now(timezone.utc).replace(second=0, microsecond=0, minute=0) + timedelta(days=2)
TABLAS["turnos"] = [turno(f"s{k}", "t1", _s + timedelta(days=7 * k)) for k in range(4)]
TABLAS["turnos"][2]["estado"] = "reservado"
TABLAS["turnos"][2]["paciente_telefono"] = "549110000999"
w7 = app.app.test_client()
chequear("serie: el formulario la ofrece sólo si se cobra", "4 semanas seguidas" in w7.get("/marcos/reservar/s0").data.decode())
html = w7.post("/marcos/reservar/s0", data={"nombre": "Ana", "telefono": "5491100003333", "sesiones": "4"}).data.decode()
chequear("serie: el resumen dice cuáles faltan y el total", "No están libres" in html and "$ 60.000" in html)
w7.post("/marcos/reservar/s0", data={"nombre": "Ana", "telefono": "5491100003333", "sesiones": "4", "confirmado": "1"})
_pago_s = TABLAS["pagos"][-1]
chequear("serie: reserva las libres en un solo pago", sorted(_pago_s["turnos"]) == ["s0", "s1", "s3"] and _pago_s["monto"] == 60000
         and all(t["pago_web"] == "esperando" and t["frecuencia"] == "semanal" for t in TABLAS["turnos"] if t["id"] in ("s0", "s1", "s3")))
_t1["cobro_web"] = "no"
chequear("serie: sin cobro no se ofrece", "4 semanas seguidas" not in w7.get("/marcos/reservar/s1").data.decode())
scheduler.avisar_profesional = _va4

# feriados
reset()
TABLAS["bloqueos"] = []
_t1 = TABLAS["tenants"][0]
_hoy = _date(2026, 11, 3)
_viejo_fer = dict(_fer._actualizados)
_fer._actualizados.clear()
scheduler.sincronizar_feriados(dict(_t1), _hoy)
_motivos = sorted(b["motivo"] for b in TABLAS["bloqueos"])
chequear("feriados: bloquea los próximos", "Feriado: Visita del papa León XIV" in _motivos and "Feriado: Navidad" in _motivos
         and not any("Año nuevo" in m and "2026" in m for m in _motivos))
_n = len(TABLAS["bloqueos"])
scheduler.sincronizar_feriados(dict(_t1), _hoy)
chequear("feriados: no duplica", len(TABLAS["bloqueos"]) == _n)
scheduler.sincronizar_feriados(dict(_t1, feriados="atender"), _hoy)
chequear("feriados: «atiendo igual» los saca", not any((b["motivo"] or "").startswith("Feriado") for b in TABLAS["bloqueos"]))
scheduler.sincronizar_feriados(dict(_t1), _hoy)
chequear("puentes: por defecto se bloquean", any("Puente" in b["motivo"] for b in TABLAS["bloqueos"]))
scheduler.sincronizar_feriados(dict(_t1, puentes="atender"), _hoy)
_motivos = [b["motivo"] for b in TABLAS["bloqueos"]]
chequear("puentes: «atiendo» saca sólo los puentes", not any("Puente" in m for m in _motivos) and "Feriado: Navidad" in _motivos, _motivos)
_fer._tipos["2026-11-23"] = "puente"
scheduler.sincronizar_feriados(dict(_t1, puentes="atender"), _hoy)
chequear("puentes: respeta el tipo que manda la API", not any("Soberan" in b["motivo"] for b in TABLAS["bloqueos"]))
_fer._tipos.clear()
_fer._actualizados.update(_viejo_fer)

# consultorios
reset()
_t1 = TABLAS["tenants"][0]
_t1.update({"slug": "marcos", "activo": True})
db.get_tenant_by_id = lambda tid: dict(_t1) if tid == "t1" else None
db.get_tenant_by_slug = lambda slug: dict(_t1) if slug == "marcos" else None
panel_c.post("/agenda/sedes", data={"csrf_token": "tok", "sede1_nombre": "Palermo", "sede1_direccion": "Santa Fe 1234",
                                    "sede2_nombre": "Belgrano", "sede2_direccion": "Cabildo 2000"})
chequear("sedes: se guardan", [x["nombre"] for x in _t1["sedes"]] == ["Palermo", "Belgrano"])
_prev = {t["id"] for t in TABLAS["turnos"]}
panel_c.post("/agenda/horario", data={"csrf_token": "tok", "dia1_desde1": "09:00", "dia1_hasta1": "11:00", "dia1_sede1": "Palermo",
                                      "dia1_desde2": "15:00", "dia1_hasta2": "17:00", "dia1_sede2": "Belgrano", "horario_semanas": "2"})
_nv = [t for t in TABLAS["turnos"] if t["id"] not in _prev]
chequear("sedes: cada franja en su consultorio", _nv and all(
    (t["sede"] == "Palermo") == (datetime.fromisoformat(t["inicio"]).astimezone(app.ZoneInfo(TZ)).hour < 12) for t in _nv))
_tb = next(t for t in _nv if t["sede"] == "Belgrano")
chequear("sedes: el recordatorio dice dónde", "en Belgrano (Cabildo 2000)" in scheduler.cuando_de(_tb, dict(_t1), app.ZoneInfo(TZ)))
chequear("sedes: el formulario muestra la dirección", "Cabildo 2000" in app.app.test_client().get(f"/marcos/reservar/{_tb['id']}").data.decode())

# imprimir
reset()
_t1 = TABLAS["tenants"][0]
db.get_tenant_by_id = lambda tid: dict(_t1) if tid == "t1" else None
_lun = datetime.now(app.ZoneInfo(TZ)).date()
_lun -= timedelta(days=_lun.weekday())
_i10 = datetime.combine(_lun + timedelta(days=1), app.dtime(10, 0), tzinfo=app.ZoneInfo(TZ))
TABLAS["turnos"] = [turno("i1", "t1", _i10.astimezone(timezone.utc), estado="reservado", paciente_nombre="Lola Pérez",
                          paciente_telefono="5491166667777", tipo="Sesión", nota="Muy reservada")]
html = panel_c.get(f"/agenda/imprimir?semana={_lun.isoformat()}").data.decode()
chequear("imprimir: la semana con hora, paciente y tipo", "10:00" in html and "Lola Pérez" in html and "Sesión" in html)
chequear("imprimir: sin notas ni teléfonos", "Muy reservada" not in html and "5491166667777" not in html)

# --- Devoluciones, política, recibo, resumen web y aviso de pago por vencer ---
reset()
TABLAS["pagos"] = []
_t1 = TABLAS["tenants"][0]
_t1.update({"slug": "marcos", "activo": True, "cobro_web": "mercadopago", "precio_sesion": 20000,
            "aviso_telefono": "5491122223333", "devolucion_modo": "todo", "devolucion_horas": 24,
            "mp_token_cifrado": _pg.cifrar("TEST-tok"), "mp_vence": (datetime.now(timezone.utc) + timedelta(days=100)).isoformat()})
db.get_tenant_by_slug = lambda slug: dict(_t1) if slug == "marcos" else None
db.get_tenant_by_id = lambda tid: dict(_t1) if tid == "t1" else None
_avd = []
_va5 = scheduler.avisar_profesional
scheduler.avisar_profesional = lambda tenant, texto, **_kw: _avd.append(texto) or True
_pg.config.MP_CLIENT_ID, _pg.config.MP_CLIENT_SECRET = "app", "secreto"
_reint = []


def _mp_post2(url, json=None, headers=None, timeout=None):
    if url.endswith("/refunds"):
        _reint.append((url, json, headers))
        return _RMP(201, {"id": 1})
    return _RMP(201, {"id": "pref", "init_point": "https://mp.example/pagar"})


_pg.requests.post = _mp_post2
_d = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(days=3)
TABLAS["turnos"] = [turno("d1", "t1", _d), turno("d2", "t1", _d + timedelta(hours=1)),
                    turno("d3", "t1", datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=5))]
wd = app.app.test_client()


def _pagar(tid, nombre, mp_id):
    wd.post(f"/marcos/reservar/{tid}", data={"nombre": nombre, "telefono": "5491100003333", "confirmado": "1"})
    pg = TABLAS["pagos"][-1]
    _mp["pagos"][mp_id] = {"status": "approved", "external_reference": pg["token"], "transaction_amount": 20000}
    _pg.revisar_pago_mp(dict(_t1), mp_id)
    return pg


pd1 = _pagar("d1", "Ana Recibo", "201")
_td1 = TABLAS["turnos"][0]
chequear("pago_id: la reserva sabe qué pago la cubre", _td1["pago_id"] == pd1["id"] and pd1["paciente_nombre"] == "Ana Recibo"
         and pd1["sesiones"] == [_td1["inicio"]])
chequear("aviso: entra un pago por Mercado Pago y le llega al profesional",
         any("entró un pago de $ 20.000 por Mercado Pago" in x for x in _avd) and not any("Ana Recibo" in x for x in _avd), _avd)
_n_avd = len(_avd)
_pg._avisar_pago_mp({**_t1, "aviso_cada_pago": False}, pd1, [_td1])
chequear("aviso: si el profesional lo apagó, no le llega", len(_avd) == _n_avd)
chequear("recibo: número correlativo", isinstance(pd1.get("recibo_numero"), int)
         and f"N.º {pd1['recibo_numero']:04d}" in wd.get(f"/pago/{pd1['token']}/recibo").data.decode())
html = wd.get(f"/pago/{pd1['token']}/recibo").data.decode()
chequear("recibo: muestra profesional, paciente, monto y que no es factura",
         "Ana Recibo" in html and "$ 20.000" in html and "No es una factura" in html and "5491100003333" not in html)
_tr = next(t for t in TABLAS["tenants"] if t["id"] == pd1["tenant_id"])
chequear("recibo: sin foto no hay imagen", 'class="foto"' not in html)
_tr["foto"] = "data:image/png;base64,iVBORw0KGgo="
chequear("recibo: lleva la foto del profesional", 'src="data:image/png;base64,iVBORw0KGgo="' in wd.get(f"/pago/{pd1['token']}/recibo").data.decode())
_tr["foto"] = "data:text/html;base64,PHNjcmlwdD4="
chequear("recibo: una foto rara no se muestra", 'class="foto"' not in wd.get(f"/pago/{pd1['token']}/recibo").data.decode())
_tr["foto"] = None
chequear("recibo: sin pago aprobado no hay recibo", wd.get("/pago/inventado/recibo").status_code == 404)
_rp = wd.get(f"/pago/{pd1['token']}/recibo.pdf")
chequear("recibo: se baja en PDF", _rp.status_code == 200 and _rp.mimetype == "application/pdf" and _rp.data.startswith(b"%PDF")
         and "Descargar PDF" in html)
_tr.update({"recibo_cuit": "20-12345678-6", "recibo_domicilio": "Av. Ficticia 123"})
chequear("recibo: lleva CUIT y domicilio", "CUIT 20-12345678-6" in wd.get(f"/pago/{pd1['token']}/recibo").data.decode()
         and "Av. Ficticia 123" in wd.get(f"/pago/{pd1['token']}/recibo").data.decode())
import recibos as _rec  # noqa: E402
chequear("recibo: el PDF se arma con CUIT", (_rec.pdf(_tr, pd1) or b"").startswith(b"%PDF"))
_tr.update({"recibo_cuit": None, "recibo_domicilio": None})
chequear("recibo: sin pago aprobado no hay PDF", wd.get("/pago/inventado/recibo.pdf").status_code == 404)
_tr["foto"] = "data:image/png;base64,iVBORw0KGgo="
chequear("recibo: una foto rota no impide el PDF", wd.get(f"/pago/{pd1['token']}/recibo.pdf").data.startswith(b"%PDF"))
_tr["foto"] = None
chequear("recibo: la página de pago lo ofrece", "Ver tu recibo" in wd.get(f"/pago/{pd1['token']}").data.decode())
chequear("política: se le muestra al paciente", "con 24 horas o más de anticipación, te devolvemos todo"
         in wd.get(f"/pago/{pd1['token']}").data.decode())
html = wd.get(f"/agendar/{_td1['token_calendario']}/cancelar").data.decode()
chequear("cancelar: avisa cuánto se le devuelve", "$ 20.000" in html and "devolución" in html)
wd.post(f"/agendar/{_td1['token_calendario']}/cancelar")
chequear("cancelar con anticipación: devuelve sola por Mercado Pago", _reint and _reint[-1][0].endswith("/v1/payments/201/refunds")
         and _reint[-1][1] == {"amount": 20000.0} and pd1["estado"] == "devuelto" and pd1["devuelto"] == 20000
         and any("devolvieron" in x for x in _avd) and _reint[-1][2]["X-Idempotency-Key"])
chequear("recibo: dice lo devuelto", "Devuelto" in wd.get(f"/pago/{pd1['token']}/recibo").data.decode())
# resumen mensual de cobros al profesional
import mail as _mail_r  # noqa: E402
_rmd, _rme = _mail_r.disponible, _mail_r.enviar
_mail_r.disponible = lambda: True
_res_mails = []
_mail_r.enviar = lambda para, asunto, texto, adjuntos=None: _res_mails.append((para, asunto, texto, adjuntos)) or True
_zr = app.ZoneInfo("America/Argentina/Buenos_Aires")
_hoy_r = datetime.now(_zr)
_dia1 = (_hoy_r.replace(day=28) + timedelta(days=4)).replace(day=1, hour=10, minute=0, second=0, microsecond=0)
_tr.update({"email": "profe@ejemplo.com", "resumen_cobros_mes": None})
chequear("resumen: antes de las 9 no sale", _pg.mandar_resumen_mensual(_tr, _dia1.replace(hour=7)) is False and not _res_mails)
chequear("resumen: el día 1 sale con el PDF", _pg.mandar_resumen_mensual(_tr, _dia1) is True and len(_res_mails) == 1
         and _res_mails[0][0] == "profe@ejemplo.com" and "cobraste por tu página de reservas" in _res_mails[0][2]
         and _res_mails[0][3][0][1].startswith(b"%PDF") and _tr["resumen_cobros_mes"] == _hoy_r.strftime("%Y-%m"))
chequear("resumen: una sola vez por mes", _pg.mandar_resumen_mensual(_tr, _dia1 + timedelta(hours=3)) is False and len(_res_mails) == 1)
_dia_sig = (_dia1.replace(day=28) + timedelta(days=4)).replace(day=1, hour=10)
chequear("resumen: un mes sin cobros no escribe", _pg.mandar_resumen_mensual(_tr, _dia_sig) is False and len(_res_mails) == 1
         and _tr["resumen_cobros_mes"] == _dia1.strftime("%Y-%m"))
_tr.update({"resumen_cobros_mes": None, "resumen_cobros_mail": False})
chequear("resumen: si lo apagó no sale", _pg.mandar_resumen_mensual(_tr, _dia1) is False and len(_res_mails) == 1)
_tr["resumen_cobros_mail"] = True
_tr.update({"email": None, "resumen_cobros_mes": None})
chequear("resumen: sin mail no hace nada", _pg.mandar_resumen_mensual(_tr, _dia1) is False and len(_res_mails) == 1)
_mail_r.disponible, _mail_r.enviar = _rmd, _rme

# tarde para la política: no devuelve
_t1["cancelar_hasta_horas"] = 0
pd3 = _pagar("d3", "Beto Tarde", "203")
chequear("recibo: el siguiente lleva el número que sigue", pd3.get("recibo_numero") == pd1["recibo_numero"] + 1)
_pg.aprobar(dict(_t1), pd3)
chequear("recibo: aprobar de nuevo no cambia el número", pd3["recibo_numero"] == pd1["recibo_numero"] + 1)
_xl = panel_c.get("/agenda/pagos-web.xlsx")
from openpyxl import load_workbook as _lw  # noqa: E402
_hoja = _lw(io.BytesIO(_xl.data))["Pagos web"]
_filas = [[c.value for c in f] for f in _hoja.iter_rows()]
chequear("excel: baja los pagos web del mes sin nombres", _xl.status_code == 200 and _filas[0][0] == "Recibo"
         and "Paciente" not in _filas[0] and any(f[0] == f"{pd1['recibo_numero']:04d}" for f in _filas[1:])
         and "Ana Recibo" not in str(_filas), _filas[:3])
_filas_n = [[c.value for c in f] for f in _lw(io.BytesIO(panel_c.get("/agenda/pagos-web.xlsx?nombres=1").data))["Pagos web"].iter_rows()]
chequear("excel: con nombres si se piden", "Paciente" in _filas_n[0] and "Ana Recibo" in str(_filas_n))
chequear("excel: el panel ofrece bajarlo", "pagos-web.xlsx" in _panel(panel_c))
_n = len(_reint)
_tk = TABLAS["turnos"][2]["token_calendario"]
chequear("cancelar tarde: avisa que no se devuelve", "no se devuelve" in wd.get(f"/agendar/{_tk}/cancelar").data.decode())
wd.post(f"/agendar/{_tk}/cancelar")
chequear("cancelar tarde: no devuelve nada", len(_reint) == _n and pd3["estado"] == "aprobado" and not pd3.get("devuelto"))

# todo menos la seña, por transferencia: queda para devolver a mano
_t1.update({"devolucion_modo": "menos_sena", "sena_monto": 5000})
pd2 = _pagar("d2", "Ceci Trans", "202")
pd2["metodo"] = "transferencia"
_td2 = TABLAS["turnos"][1]
chequear("menos la seña: calcula la parte", _pg.cuanto_devolver(dict(_t1), dict(_td2))[1] == 15000)
wd.post(f"/agendar/{_td2['token_calendario']}/cancelar")
chequear("transferencia: queda anotado para devolver y avisa", pd2["a_devolver"] == 15000 and any("devolvéselos" in x for x in _avd))
html = _panel(panel_c)
chequear("panel: lo muestra en «Pagos para revisar»", "hay que devolverle" in html and "Ya lo devolví" in html)
panel_c.post(f"/agenda/pago-web/{pd2['id']}/devuelto", data={"csrf_token": "tok"})
chequear("panel: «Ya lo devolví» lo anota", pd2["a_devolver"] == 0 and pd2["devuelto"] == 15000 and pd2["estado"] == "aprobado")

# el profesional cancela una sesión pagada → para devolver desde el panel
TABLAS["turnos"][0].update(db._LIBERADO)
_t1.update({"devolucion_modo": "nada"})
pd4 = _pagar("d1", "Dani Prof", "204")
panel_c.post("/agenda/cancelar/d1", data={"csrf_token": "tok"})
chequear("profesional cancela: queda su parte entera para devolver", pd4["a_devolver"] == 20000)
_n = len(_reint)
panel_c.post(f"/agenda/pago-web/{pd4['id']}/devolver", data={"csrf_token": "tok"})
chequear("panel: «Devolver» hace el reintegro", len(_reint) == _n + 1 and pd4["estado"] == "devuelto" and pd4["a_devolver"] == 0)
panel_c.post(f"/agenda/pago-web/{pd4['id']}/devolver", data={"csrf_token": "tok"})
chequear("panel: no se devuelve dos veces", len(_reint) == _n + 1)
chequear("política nada: lo dice", _pg.politica(dict(_t1)) == "Si cancelás, lo pagado no se devuelve.")

# resumen web del mes
html = _panel(panel_c)
chequear("resumen web: separa Mercado Pago y transferencias", "Lo que entró por la web" in html and "Transferencias" in html
         and "Devuelto" in html and "Recibo" in html)
_cw = app._cobros_web(dict(_t1), datetime.now(app.ZoneInfo(TZ)).date().replace(day=1), app.ZoneInfo(TZ), [])
chequear("resumen web: cuentas", _cw["mp"] == 60000 and _cw["transferencia"] == 20000 and _cw["devuelto"] == 55000
         and _cw["neto"] == 25000)

# aviso de pago por vencer
_vt = _pg.config.PAGO_PENDIENTE_TEMPLATE
_pg.config.PAGO_PENDIENTE_TEMPLATE = "pago_pendiente"
_enviadas = []
_vep = _wa.enviar_plantilla
_wa.enviar_plantilla = lambda numero, tel, nombre, lang, params: _enviadas.append((tel, nombre, params)) or True
TABLAS["turnos"][0].update(db._LIBERADO)
wd.post("/marcos/reservar/d1", data={"nombre": "Eli", "telefono": "5491100008888", "confirmado": "1"})
pd5 = TABLAS["pagos"][-1]
_ahora = datetime.now(timezone.utc)
chequear("por vencer: al principio no avisa", _pg.avisar_por_vencer(dict(_t1), "https://x.ar", _ahora) == 0)
chequear("por vencer: en el último tercio avisa con el link", _pg.avisar_por_vencer(dict(_t1), "https://x.ar", _ahora + timedelta(minutes=22)) == 1
         and _enviadas[-1][0] == "5491100008888" and _enviadas[-1][2][2] == f"https://x.ar/pago/{pd5['token']}")
chequear("por vencer: una sola vez", _pg.avisar_por_vencer(dict(_t1), "https://x.ar", _ahora + timedelta(minutes=25)) == 0)
_pg.config.PAGO_PENDIENTE_BOTON = True
_wa.enviar_plantilla = lambda numero, tel, nombre, lang, params, boton_url=None: _enviadas.append((tel, nombre, params, boton_url)) or True
pd5["avisado_at"] = None
chequear("por vencer: con botón de link, el token va en el botón y el texto lleva profesional y cuándo",
         _pg.avisar_por_vencer(dict(_t1), "https://x.ar", _ahora + timedelta(minutes=22)) == 1
         and len(_enviadas[-1][2]) == 2 and _enviadas[-1][3] == pd5["token"], _enviadas[-1])
_pg.config.PAGO_PENDIENTE_BOTON = False
_pg.config.PAGO_PENDIENTE_TEMPLATE = ""
chequear("por vencer: sin plantilla no manda", _pg.avisar_por_vencer(dict(_t1), "https://x.ar", _ahora + timedelta(minutes=22)) == 0)
_pg.config.PAGO_PENDIENTE_TEMPLATE = _vt
_wa.enviar_plantilla = _vep
_pg.config.MP_CLIENT_ID, _pg.config.MP_CLIENT_SECRET = _vc
scheduler.avisar_profesional = _va5

# panel: horario con más franjas, consultorios y tipos sin tope de 3/4
reset()
_t1 = TABLAS["tenants"][0]
db.get_tenant_by_id = lambda tid: dict(_t1) if tid == "t1" else None
panel_c.post("/agenda/sedes", data={"csrf_token": "tok", **{f"sede{n}_nombre": f"Sede {n}" for n in range(1, 6)}})
chequear("sedes: se pueden cargar más de 3", len(_t1["sedes"]) == 5)
panel_c.post("/agenda/tipos", data={"csrf_token": "tok", **{f"tipo{n}_nombre": f"T{n}" for n in range(1, 7)},
                                    **{f"tipo{n}_minutos": "50" for n in range(1, 7)}})
chequear("tipos: se pueden cargar más de 4", len(_t1["tipos_turno"]) == 6)
panel_c.post("/agenda/horario", data={"csrf_token": "tok", "dia0_desde1": "08:00", "dia0_hasta1": "09:00",
                                      "dia0_desde2": "10:00", "dia0_hasta2": "11:00", "dia0_desde3": "15:00", "dia0_hasta3": "16:00",
                                      "horario_semanas": "1"})
chequear("horario: tres franjas en un día", len([r for r in _t1["horario_semanal"] if r["dia"] == 0]) == 3)
_antes = list(_t1["horario_semanal"])
panel_c.post("/agenda/horario", data={"csrf_token": "tok", "dia0_desde1": "08:00", "dia0_hasta1": "10:00",
                                      "dia0_desde2": "09:30", "dia0_hasta2": "11:00", "horario_semanas": "1"})
chequear("horario: franjas que se pisan no se guardan", _t1["horario_semanal"] == _antes)
html = panel_c.get("/agenda").data.decode()
_aj = panel_c.get("/ajustes").data.decode()
chequear("panel: secciones plegables agrupadas", html.count("data-plegable") >= 5 and "Tu agenda" in html and "Vacaciones y días bloqueados" in html
         and 'href="/ajustes"' in html and "+ otra franja" in _aj and "+ otro consultorio" in _aj and 'value="Sede 2"' in _aj)

# --- página de ajustes: pestañas, guardado parcial y volver a la pestaña ---
_aj = panel_c.get("/ajustes?p=cobros").data.decode()
chequear("ajustes: siete pestañas, abre la pedida", _aj.count('role="tab"') == 7 and 'id="tab-cobros" aria-controls="p-cobros" data-pestania="cobros"\n            aria-selected="true"' in _aj
         and 'data-panel="agenda" hidden' in _aj and 'data-panel="cobros">' in _aj)
chequear("ajustes: pestaña inexistente abre Agenda", 'data-panel="agenda">' in panel_c.get("/ajustes?p=nada").data.decode())
chequear("ajustes: un solo «Guardar cambios» y campos que dependen de otros",
         'id="guardar-todo"' in _aj and _aj.count("data-ajuste") >= 10 and 'data-si="cobro_web!=no;cobro_modo=sena"' in _aj
         and 'data-si="al_cancelar=preguntar"' in _aj and 'data-si="modalidades_publicas!=presencial"' in _aj and "data-sede" in _aj)
_t1.update({"al_cancelar": "preguntar", "en_directorio": True, "ofrecer_espera_auto": True, "cobro_web": "transferencia",
            "modalidades_publicas": "presencial", "devolucion_modo": "nada"})
r = panel_c.post("/agenda/perfil", data={"csrf_token": "tok", "modalidades_publicas": "ambas", "casillas": "en_directorio",
                                         "volver": "ajustes", "pestania": "pagina"})
chequear("ajustes: Mi página guarda sólo lo suyo", _t1["modalidades_publicas"] == "ambas" and _t1["en_directorio"] is False
         and _t1["al_cancelar"] == "preguntar" and _t1["ofrecer_espera_auto"] is True)
chequear("ajustes: vuelve a la pestaña de donde vino", r.status_code == 302 and r.headers["Location"].endswith("/ajustes?p=pagina"))
panel_c.post("/agenda/perfil", data={"csrf_token": "tok", "al_cancelar": "liberar", "cancelar_hasta_horas": "12", "casillas": "ofrecer_espera_auto"})
chequear("ajustes: Cancelaciones guarda sólo lo suyo", _t1["al_cancelar"] == "liberar" and _t1["cancelar_hasta_horas"] == 12
         and _t1["ofrecer_espera_auto"] is False and _t1["modalidades_publicas"] == "ambas" and _t1["en_directorio"] is False)
panel_c.post("/agenda/perfil", data={"csrf_token": "tok", "modalidades_publicas": "presencial"})
chequear("ajustes: un form viejo sin «casillas» sigue destildando las dos", _t1["en_directorio"] is False and _t1["ofrecer_espera_auto"] is False)
panel_c.post("/agenda/cobros", data={"csrf_token": "tok", "precio_sesion": "$ 31.000"})
chequear("ajustes: el precio se guarda sin tocar cómo se cobra", _t1["precio_sesion"] == 31000 and _t1["cobro_web"] == "transferencia"
         and _t1["devolucion_modo"] == "nada")
panel_c.post("/agenda/cobros", data={"csrf_token": "tok", "devolucion_modo": "todo", "devolucion_horas": "48"})
chequear("ajustes: la devolución se guarda sin tocar el precio", _t1["devolucion_modo"] == "todo" and _t1["devolucion_horas"] == 48
         and _t1["precio_sesion"] == 31000 and _t1["cobro_web"] == "transferencia")
panel_c.post("/agenda/avisos", data={"csrf_token": "tok", "matricula": "MN 999"})
chequear("ajustes: Tus datos no toca los avisos", _t1["matricula"] == "MN 999" and _t1.get("resumen_noche") is not False)
chequear("agenda: las anclas viejas llevan a ajustes", "'mi-pagina': 'pagina'" in panel_c.get("/agenda").data.decode())

# --- buscador (tipo Spotlight) y primeros pasos ---
import html as _html_mod  # noqa: E402
import re as _re  # noqa: E402
TABLAS["turnos"].append(turno("ajena-busq", "t2", manana + timedelta(hours=6), estado="reservado", paciente_telefono="5491166660001",
                              paciente_nombre="Zoe De Otra Cuenta"))


def _indice(html):
    m = _re.search(r'<script type="application/json" id="buscador-indice">(.*?)</script>', html, _re.S)
    return json.loads(m.group(1)) if m else None


_ag, _aj = panel_c.get("/agenda").data.decode(), panel_c.get("/ajustes").data.decode()
_ix = _indice(_ag)
chequear("buscador: está en la agenda y en ajustes, con botón y atajo", _ix is not None and _indice(_aj) is not None
         and "data-buscar-abrir" in _ag and "data-buscar-abrir" in _aj and "e.ctrlKey || e.metaKey" in _ag)
chequear("buscador: trae los pacientes de la cuenta con su ficha", len(_ix["fichas"]) > 0 and all(f["d"].startswith("ficha-") and f["c"] for f in _ix["fichas"]))
chequear("buscador: nunca trae pacientes de otra cuenta", "Zoe De Otra Cuenta" not in json.dumps(_ix) and "5491166660001" not in json.dumps(_ix))
chequear("buscador: cada ajuste lleva a su pestaña", any(x["t"] == "Precio de la sesión" and x["u"] == "/ajustes?p=agenda#tipos" for x in _ix["items"])
         and any(x["t"] == "Cambiar la clave" and x["u"] == "/ajustes?p=cuenta#seguridad" for x in _ix["items"])
         and any(x["t"] == "Bloquear días (vacaciones)" and x["u"] == "/agenda#bloqueos" for x in _ix["items"]))
_dest = {x["u"].split("#")[1] for x in _ix["items"] if "#" in x["u"]}
_todo = _ag + _aj + panel_c.get("/agenda?revision=1").data.decode()
chequear("buscador: todas las anclas del catálogo existen en el panel", all(f'id="{a}"' in _todo for a in _dest), [a for a in _dest if f'id="{a}"' not in _todo])
chequear("buscador: la agenda abre la ficha pedida por ?ficha=", "get('ficha')" in _ag)

_t1.pop("preferencias_vista", None)
_t1.update({"horario_semanal": [{"dia": 0, "desde": "09:00", "hasta": "12:00"}], "precio_sesion": None, "tipos_turno": None,
            "foto": None, "sobre_mi": None})
_ag = panel_c.get("/agenda").data.decode()
chequear("primeros pasos: aparece con lo que falta y lo hecho tachado", 'id="primeros-pasos"' in _ag and "<s>Cargá tu horario de atención</s>" in _ag
         and 'href="/ajustes?p=agenda#tipos"><strong>Poné el precio de la sesión' in _ag)
panel_c.post("/agenda/primeros-pasos/ocultar", data={"csrf_token": "tok"})
chequear("primeros pasos: se puede ocultar y queda en la cuenta", 'id="primeros-pasos"' not in panel_c.get("/agenda").data.decode()
         and _t1["preferencias_vista"].get("sin_primeros_pasos") is True)

# --- buscador: pedirle al asistente, recientes, barra del celular y paciente nuevo ---
TABLAS.setdefault("acciones_profesional", [])
_ix = _indice(panel_c.get("/agenda").data.decode())
chequear("recientes: cada ficha tiene una referencia opaca (sin teléfono ni nombre)",
         all(f["r"] and len(f["r"]) == 14 and f["r"] not in f["c"] and not any(ch in f["r"] for ch in "~ ") for f in _ix["fichas"])
         and _ix["cuenta"] and _ix["salir"] == "/logout")
_bjs = panel_c.get("/agenda").data.decode()
chequear("recientes: se guardan por cuenta y se borran al salir", "'fibot-recientes-' + idx.cuenta" in _bjs and "borrarRec" in _bjs
         and "x.r === pedida" in _bjs)
_r = panel_c.post("/agenda/asistente", data={"csrf_token": "tok", "texto": "quién me debe"}).get_json()
chequear("asistente en el buscador: contesta consultas sin vincular WhatsApp", _r["entendido"] and _r["texto"] and not _r.get("confirmar"), _r)
_r = panel_c.post("/agenda/asistente", data={"csrf_token": "tok", "texto": "precio"}).get_json()
chequear("asistente en el buscador: lo que no es un pedido no se inventa", _r == {"entendido": False}, _r)
chequear("asistente en el buscador: sin CSRF no", panel_c.post("/agenda/asistente", data={"texto": "hoy"}).status_code == 403)
_nb = len(TABLAS["bloqueos"])
_r = panel_c.post("/agenda/asistente", data={"csrf_token": "tok", "texto": "bloquear pasado mañana"}).get_json()
chequear("asistente en el buscador: una acción vuelve como pregunta, sin hacer nada", _r.get("confirmar") and len(TABLAS["bloqueos"]) == _nb, _r)
_n = _r["confirmar"]["n"]
_r2 = panel_c.post("/agenda/asistente/confirmar", data={"csrf_token": "tok", "n": _n, "modo": "no"}).get_json()
chequear("asistente en el buscador: «No» no hace nada", not _r2["hecho"] and len(TABLAS["bloqueos"]) == _nb, _r2)
_r = panel_c.post("/agenda/asistente", data={"csrf_token": "tok", "texto": "bloquear pasado mañana"}).get_json()
_r2 = panel_c.post("/agenda/asistente/confirmar", data={"csrf_token": "tok", "n": _r["confirmar"]["n"], "modo": "si"}).get_json()
chequear("asistente en el buscador: «Sí» lo hace", _r2["hecho"] and len(TABLAS["bloqueos"]) == _nb + 1, _r2)
_r3 = panel_c.post("/agenda/asistente/confirmar", data={"csrf_token": "tok", "n": _r["confirmar"]["n"], "modo": "si"}).get_json()
chequear("asistente en el buscador: confirmar dos veces no lo repite", not _r3["hecho"] and len(TABLAS["bloqueos"]) == _nb + 1, _r3)
chequear("asistente en el buscador: la cola del panel es de esta cuenta",
         all(f["tenant_id"] == "t1" for f in TABLAS["acciones_profesional"] if f["telefono"].startswith("panel:")))
TABLAS["bloqueos"] = TABLAS["bloqueos"][:_nb]

_ag, _aj = panel_c.get("/agenda").data.decode(), panel_c.get("/ajustes").data.decode()
chequear("barra del celular: en agenda y ajustes, con Hoy, Buscar y Ajustes", all('class="barra-movil"' in h and 'href="/agenda#hoy"' in h
         and 'href="/ajustes"' in h for h in (_ag, _aj)) and 'id="hoy"' in _ag)
_libre = next(t for t in TABLAS["turnos"] if t["tenant_id"] == "t1" and t["estado"] == "libre"
              and datetime.fromisoformat(t["inicio"]) > datetime.now(timezone.utc))
panel_c.post(f"/agenda/agendar/{_libre['id']}", data={"csrf_token": "tok", "nombre": "Paciente Recién Llegada"})
_ag = panel_c.get("/agenda").data.decode()
chequear("paciente nuevo: la guía sale con lo que le falta", 'id="paciente-nuevo"' in _ag and "Paciente Recién Llegada quedó agendado/a" in _ag
         and "Sin WhatsApp no le llegan" in _ag and "Sin valor no sabés" in _ag and "Quedó una sola sesión" in _ag)
chequear("paciente nuevo: sale una sola vez", 'id="paciente-nuevo"' not in panel_c.get("/agenda").data.decode())
_libre2 = next(t for t in TABLAS["turnos"] if t["tenant_id"] == "t1" and t["estado"] == "libre"
               and datetime.fromisoformat(t["inicio"]) > datetime.now(timezone.utc))
panel_c.post(f"/agenda/agendar/{_libre2['id']}", data={"csrf_token": "tok", "nombre": "Paciente Recién Llegada"})
chequear("paciente nuevo: a alguien que ya tenía sesiones no se le repite", 'id="paciente-nuevo"' not in panel_c.get("/agenda").data.decode())

# --- Hoy con acciones rápidas, repaso de la semana y nombres que abren la ficha ---
_tf = lambda i: next(t for t in TABLAS["turnos"] if t["id"] == i)
_z = app.ZoneInfo(TZ)
_ahl = datetime.now(_z)
_lun = (_ahl - timedelta(days=_ahl.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
_pas = max(_lun + timedelta(minutes=1), _ahl - timedelta(hours=3))
TABLAS["turnos"] += [
    turno("hoy-r1", "t1", _ahl.replace(hour=12, minute=0, second=0, microsecond=0).astimezone(timezone.utc), estado="reservado",
          paciente_telefono="5491100001111", paciente_nombre="Rita Rápida", valor=30000),
    turno("rev-falto", "t1", _pas.astimezone(timezone.utc), estado="reservado", paciente_telefono="5491100002222",
          paciente_nombre="Fausto Faltó", asistencia="no_vino", valor=25000),
    turno("rev-sinm", "t1", (_pas + timedelta(seconds=30)).astimezone(timezone.utc), estado="reservado",
          paciente_telefono="5491100003333", paciente_nombre="Sonia Sinmarcar", valor=20000),
    turno("rev-libre", "t1", (_lun + timedelta(days=8, hours=10)).astimezone(timezone.utc)),
]
_ag = panel_c.get("/agenda").data.decode()
_bloque_hoy = _ag[_ag.index('id="hoy"'):_ag.index('class="tablero"')]
chequear("hoy: cada sesión trae Vino / No vino, Pagó con el valor y nota", "/agenda/asistencia/hoy-r1" in _bloque_hoy
         and "/agenda/pago/hoy-r1" in _bloque_hoy and "Pagó $ 30.000" in _bloque_hoy and "/agenda/nota/hoy-r1" in _bloque_hoy
         and "data-sin-ficha" in _bloque_hoy)
chequear("hoy: tocar los botones no abre la ficha", "closest('[data-sin-ficha]')" in _ag)
_r = panel_c.post("/agenda/asistencia/hoy-r1", data={"csrf_token": "tok", "asistencia": "vino", "ancla": "hoy"})
chequear("hoy: marcar que vino vuelve a la tarjeta de Hoy", _r.status_code == 302 and _r.headers["Location"].endswith("/agenda#hoy")
         and _tf("hoy-r1")["asistencia"] == "vino")
panel_c.post("/agenda/pago/hoy-r1", data={"csrf_token": "tok", "pago": "pagado", "ancla": "hoy"})
panel_c.post("/agenda/nota/hoy-r1", data={"csrf_token": "tok", "nota": "Trajo el sueño de la escalera", "ancla": "hoy"})
_bloque_hoy = (_ag := panel_c.get("/agenda").data.decode())[_ag.index('id="hoy"'):_ag.index('class="tablero"')]
chequear("hoy: pago y nota quedan marcados", _tf("hoy-r1")["pago"] == "pagado" and _tf("hoy-r1")["nota"] == "Trajo el sueño de la escalera"
         and "Pagó ✓" in _bloque_hoy and "Nota ✓" in _bloque_hoy)
chequear("hoy: la ancla no sirve para redirigir afuera", panel_c.post("/agenda/asistencia/hoy-r1", data={
    "csrf_token": "tok", "asistencia": "vino", "ancla": "https://malo.com"}).headers["Location"] in ("/agenda", "http://localhost/agenda"))

_rv = panel_c.get("/agenda?revision=1").data.decode()
_bloque_rv = _rv[_rv.index('id="revision"'):_rv.index('id="hoy"')]
chequear("repaso: con ?revision=1 sale cualquier día y no se oculta sola", "Repaso de la semana" in _bloque_rv and " data-ocultable>" not in _bloque_rv)
chequear("repaso: quién faltó y qué quedó sin marcar", "Fausto Faltó" in _bloque_rv and "Faltaron" in _bloque_rv
         and "Sonia Sinmarcar" in _bloque_rv and "/agenda/asistencia/rev-sinm" in _bloque_rv)
chequear("repaso: quién debe y la semana que viene", "Deben" in _bloque_rv and "$ 25.000" in _bloque_rv
         and "La semana que viene" in _bloque_rv and "horario" in _bloque_rv.split("La semana que viene")[1])
chequear("repaso: nunca muestra pacientes de otra cuenta", "Zoe De Otra Cuenta" not in _bloque_rv)
_r = panel_c.post("/agenda/asistencia/rev-sinm", data={"csrf_token": "tok", "asistencia": "vino", "ancla": "revision"})
chequear("repaso: marcar desde el repaso vuelve al repaso", _r.headers["Location"].endswith("/agenda?revision=1#revision")
         and _tf("rev-sinm")["asistencia"] == "vino")
_dias = app.DIAS_REVISION
app.DIAS_REVISION = tuple(range(7))
chequear("repaso: el viernes sale sola y se puede dar por vista", 'id="revision"' in (_x := panel_c.get("/agenda").data.decode())
         and " data-ocultable>" in _x and "fibot-revision-vista-" in _x)
app.DIAS_REVISION = ()
chequear("repaso: el resto de la semana no aparece sola", 'id="revision"' not in panel_c.get("/agenda").data.decode())
app.DIAS_REVISION = _dias
chequear("repaso: no sale en ajustes", 'id="revision"' not in panel_c.get("/ajustes?revision=1").data.decode())
chequear("repaso: el buscador lo encuentra", any(x["t"] == "Repaso de la semana" and x["u"] == "/agenda?revision=1#revision"
                                                for x in _indice(_rv)["items"]))
chequear("asistente: los nombres de la respuesta abren la ficha", "data-ir-ficha" in _rv and "conFichas(esc(t))" in _rv
         and "porNombre.has(k) ? null" in _rv)
TABLAS["turnos"] = [t for t in TABLAS["turnos"] if t["id"] not in ("hoy-r1", "rev-falto", "rev-sinm", "rev-libre")]

# --- Listas de WhatsApp, reprogramar y «¿con quién es?» ---
import conversation as _conv  # noqa: E402
_wa_env, _wa_int = [], []
_sv = (app.whatsapp.enviar_texto, app.whatsapp._interactivo, app.config.PLATAFORMA_PHONE_NUMBER_ID, db.get_tenant_by_phone_number_id,
       scheduler.avisar_profesional)
app.whatsapp.enviar_texto = lambda n, to, texto: _wa_env.append((n, to, texto)) or True
app.whatsapp._interactivo = lambda n, to, inter: _wa_int.append((n, to, inter)) or True
app.config.PLATAFORMA_PHONE_NUMBER_ID = "PLAT"
db.get_tenant_by_phone_number_id = lambda pid: None
_avisos_mov = []
scheduler.avisar_profesional = lambda t, texto, **_kw: _avisos_mov.append(texto) or True
TABLAS["tenants"][1].update({"activo": True, "slug": "otra"})
_dl = datetime.now(timezone.utc).replace(hour=13, minute=0, second=0, microsecond=0) + timedelta(days=4)
_TEL = "5491177778888"
TABLAS["turnos"] = [turno("w-mio", "t1", _dl, estado="reservado", paciente_telefono=_TEL, paciente_nombre="Rita"),
                    turno("w-l1", "t1", _dl + timedelta(hours=2)), turno("w-l2", "t1", _dl + timedelta(days=1)),
                    turno("w-ajeno", "t1", _dl + timedelta(hours=4), estado="reservado", paciente_telefono="5491100000000",
                          paciente_nombre="Otro")]
TABLAS["elecciones_whatsapp"] = []
_fw = lambda i: next(t for t in TABLAS["turnos"] if t["id"] == i)


def _wh(mensaje):
    cuerpo = json.dumps({"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "PLAT"}, "messages": [{"from": _TEL, "id": "wamid.x", **mensaje}]}}]}]}).encode()
    firma = "sha256=" + _hm.new(app.config.META_APP_SECRET.encode(), cuerpo, _hl.sha256).hexdigest()
    return app.app.test_client().post("/webhook", data=cuerpo, content_type="application/json", headers={"X-Hub-Signature-256": firma})


_wh({"type": "text", "text": {"body": "turno"}})
_lista = _wa_int[-1][2] if _wa_int else {}
chequear("lista: los horarios libres salen para tocar", _lista.get("type") == "list"
         and [r["id"] for r in _lista["action"]["sections"][0]["rows"]][:2] == ["reservar:w-l1", "reservar:w-l2"], _lista)
_wh({"type": "text", "text": {"body": "cambiar el turno"}})
_filas = [r["id"] for r in _wa_int[-1][2]["action"]["sections"][0]["rows"]]
chequear("reprogramar: lista con el turno y el horario nuevo", "mover:w-mio:w-l1" in _filas, _filas)
_wh({"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": "mover:w-ajeno:w-l2", "title": "x"}}})
chequear("reprogramar: nunca el turno de otro teléfono", _fw("w-ajeno")["estado"] == "reservado"
         and _fw("w-l2")["estado"] == "libre" and "ya no está reservado a tu nombre" in _wa_env[-1][2], _wa_env[-1:])
_wh({"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": "mover:w-mio:w-l1", "title": "x"}}})
chequear("reprogramar: pasa el turno, confirma y avisa al profesional", _fw("w-l1")["estado"] == "reservado"
         and _fw("w-l1")["paciente_telefono"] == _TEL and _fw("w-mio")["estado"] == "libre"
         and "Listo: tu turno pasó al" in _wa_env[-1][2] and any("por WhatsApp" in a for a in _avisos_mov), (_wa_env[-1:], _avisos_mov))
_wh({"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": "reservar:w-l2", "title": "x"}}})
chequear("lista: tocar un horario lo reserva", _fw("w-l2")["estado"] == "reservado" and _fw("w-l2")["paciente_telefono"] == _TEL
         and "quedó reservado" in _wa_env[-1][2], _wa_env[-1:])
_wh({"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": "reservar:w-l2", "title": "x"}}})
chequear("lista: un horario que ya no está, se avisa", "Justo se acaba de reservar" in _wa_env[-1][2])

# dos profesionales en el número compartido
_gtid = db.get_tenant_by_id
db.get_tenant_by_id = lambda tid: next((dict(t) for t in TABLAS["tenants"] if t["id"] == tid), None)
TABLAS["turnos"].append(turno("w-otra", "t2", _dl + timedelta(days=2), estado="reservado", paciente_telefono=_TEL, paciente_nombre="Rita"))
_n_int = len(_wa_int)
_wh({"type": "text", "text": {"body": "mi turno"}})
_pregunta = _wa_int[-1][2] if len(_wa_int) > _n_int else {}
chequear("con quién: pregunta con un botón por profesional", _pregunta.get("type") == "button"
         and sorted(b["reply"]["id"] for b in _pregunta["action"]["buttons"]) == ["con:t1", "con:t2"], _pregunta)
_wh({"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "con:t2", "title": "Dra. Otra"}}})
chequear("con quién: al elegir, contesta lo que había preguntado", "Tu próximo turno con *Dra. Otra*" in _wa_env[-1][2], _wa_env[-1:])
_wh({"type": "text", "text": {"body": "mi turno"}})
chequear("con quién: recuerda la elección un rato", "Dra. Otra" in _wa_env[-1][2] and len(_wa_int) == _n_int + 1, _wa_env[-1:])
TABLAS["elecciones_whatsapp"] = []
_wh({"type": "text", "text": {"body": "mi turno"}})
_wh({"type": "text", "text": {"body": "con Marcos"}})
chequear("con quién: también por escrito («con Marcos»)", "Tu próximo turno con *Lic. Marcos*" in _wa_env[-1][2], _wa_env[-1:])
_wh({"type": "text", "text": {"body": "con Otra"}})
_wh({"type": "text", "text": {"body": "mi turno"}})
chequear("con quién: «con Otra» cambia aunque ya hubiera elegido", "Dra. Otra" in _wa_env[-1][2], _wa_env[-1:])
_wh({"type": "text", "text": {"body": "con Marcos"}})
_wh({"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "con:t-inventado", "title": "x"}}})
chequear("con quién: un profesional que no es suyo no se elige", TABLAS["elecciones_whatsapp"][0]["tenant_id"] == "t1")

(app.whatsapp.enviar_texto, app.whatsapp._interactivo, app.config.PLATAFORMA_PHONE_NUMBER_ID, db.get_tenant_by_phone_number_id,
 scheduler.avisar_profesional) = _sv
db.get_tenant_by_id = _gtid

# ---------- bitácora de la ficha ----------
print("Bitácora")
reset()
_gtid_b = db.get_tenant_by_id
db.get_tenant_by_id = lambda tid: next((dict(t) for t in TABLAS["tenants"] if t["id"] == tid), None)
cb = app.app.test_client()
with cb.session_transaction() as s:
    s["tenant_id"] = "t1"
    s["sesion_id"] = _sesion_de("t1")
    s["csrf_token"] = "tok"
TELB = "5491133334444"
_hoyb = datetime.now(app.ZoneInfo(TZ))
TABLAS["turnos"] += [turno("bt1", "t1", (_hoyb.replace(hour=23, minute=0) if _hoyb.hour < 22 else _hoyb - timedelta(hours=2)).astimezone(timezone.utc),
                           estado="reservado", paciente_nombre="Mía Sol", paciente_telefono=TELB),
                     turno("bt2", "t1", datetime.now(timezone.utc) + timedelta(days=9), estado="reservado", paciente_nombre="Teo Bravo",
                           paciente_telefono="5491155550000"),
                     turno("bt3", "t2", datetime.now(timezone.utc) + timedelta(days=2), estado="reservado", paciente_nombre="De Otra Cuenta",
                           paciente_telefono="5491166660000")]
r = cb.post("/agenda/bitacora", data={"csrf_token": "tok", "telefono": TELB, "fecha": "2026-11-03", "texto": "Trabajamos lectura de sílabas."})
r = cb.post("/agenda/bitacora", data={"csrf_token": "tok", "telefono": TELB, "fecha": "2026-11-10", "texto": "Juego de memoria.\nMuy concentrada."})
chequear("bitácora: guarda dos entradas con su fecha", len(TABLAS["paciente_bitacora"]) == 2
         and TABLAS["paciente_bitacora"][0]["fecha"] == "2026-11-03" and TABLAS["paciente_bitacora"][0]["ficha"] == "")
html = cb.post("/agenda/bitacora", data={"csrf_token": "tok", "telefono": TELB, "fecha": "2026-13-40", "texto": "x"}, follow_redirects=True).data.decode()
chequear("bitácora: fecha inválida no se guarda", "no es válida" in html and len(TABLAS["paciente_bitacora"]) == 2)
cb.post("/agenda/bitacora", data={"csrf_token": "tok", "telefono": TELB, "fecha": "2026-11-10", "texto": "   "})
chequear("bitácora: vacío no se guarda", len(TABLAS["paciente_bitacora"]) == 2)
html = _panel(cb)
_ficha_b = html[html.index("Mía Sol</h4>"):]
chequear("bitácora: en la ficha, la más nueva primero", "Juego de memoria" in _ficha_b
         and _ficha_b.index("Juego de memoria") < _ficha_b.index("Trabajamos lectura"))
chequear("bitácora: la fecha se ve con el día", "mar 10/11/2026" in _ficha_b and "mar 03/11/2026" in _ficha_b)
chequear("bitácora: los campos de antes quedan plegados", "objetivos y notas generales" in _ficha_b)
lista = cb.get("/agenda/fichas").data.decode()
chequear("rápida: lista los pacientes de la cuenta", "Mía Sol" in lista and "Teo Bravo" in lista and "De Otra Cuenta" not in lista)
chequear("rápida: los de hoy van arriba", lista.index("Mía Sol") < lista.index("Teo Bravo") and ">Hoy<" in lista)
chequear("rápida: el teléfono no va en la página", TELB not in lista)
import buscador as _bus  # noqa: E402
ref_b = _bus._ref("t1", TELB)
r = cb.get(f"/agenda/fichas/{ref_b}")
pag = r.data.decode()
chequear("rápida: abre la bitácora del paciente", r.status_code == 200 and "Mía Sol" in pag and "Juego de memoria" in pag
         and pag.index("Juego de memoria") < pag.index("Trabajamos lectura"))
chequear("rápida: sin caché y la fecha de hoy ya puesta", r.headers.get("Cache-Control") == "no-store"
         and f'value="{_hoyb.date().isoformat()}"' in pag)
r = cb.post("/agenda/bitacora", data={"csrf_token": "tok", "telefono": TELB, "rapida": ref_b, "fecha": "2026-11-09", "texto": "Desde el celu"})
chequear("rápida: al anotar vuelve a la misma página", r.status_code == 302 and r.headers["Location"].endswith(f"/agenda/fichas/{ref_b}"))
r = cb.get(f"/agenda/fichas/{_bus._ref('t2', '5491166660000')}")
chequear("rápida: una ficha de otra cuenta no se abre", r.status_code == 302 and r.headers["Location"].endswith("/agenda/fichas"))
TABLAS["paciente_bitacora"].append({"id": "ajena-b", "tenant_id": "t2", "paciente_telefono": TELB, "ficha": "", "fecha": "2026-11-01",
                                    "texto": "Secreto de otra cuenta", "created_at": "0"})
chequear("rápida: no muestra lo de otra cuenta", "Secreto de otra cuenta" not in cb.get(f"/agenda/fichas/{ref_b}").data.decode())
cb.post("/agenda/bitacora/ajena-b", data={"csrf_token": "tok", "fecha": "2026-11-01", "texto": "pisado"})
cb.post("/agenda/bitacora/ajena-b/borrar", data={"csrf_token": "tok"})
chequear("bitácora: no edita ni borra lo de otra cuenta", next(e for e in TABLAS["paciente_bitacora"] if e["id"] == "ajena-b")["texto"] == "Secreto de otra cuenta")
mia = next(e for e in TABLAS["paciente_bitacora"] if e["texto"] == "Desde el celu")
cb.post(f"/agenda/bitacora/{mia['id']}", data={"csrf_token": "tok", "rapida": ref_b, "fecha": "2026-11-08", "texto": "Corregido"})
chequear("bitácora: edita fecha y texto", mia["texto"] == "Corregido" and mia["fecha"] == "2026-11-08" and mia.get("updated_at"))
cb.post(f"/agenda/bitacora/{mia['id']}/borrar", data={"csrf_token": "tok", "rapida": ref_b})
chequear("bitácora: borra la propia", not any(e["id"] == mia["id"] for e in TABLAS["paciente_bitacora"]))
r = cb.post("/agenda/bitacora", data={"telefono": TELB, "fecha": "2026-11-10", "texto": "sin token"})
chequear("bitácora: pide CSRF", r.status_code in (400, 403) and not any(e["texto"] == "sin token" for e in TABLAS["paciente_bitacora"]))
fc = cb.post("/agenda/paciente/ficha-completa", data={"csrf_token": "tok", "telefono": TELB}).data.decode()
chequear("ficha completa: lleva la bitácora en orden", "Bitácora" in fc and fc.index("Trabajamos lectura") < fc.index("Juego de memoria"))
chequear("barra del celular: lleva a las fichas", 'href="/agenda/fichas"' in html)
# «Para la próxima»: sale como «Para hoy» desde la anotación anterior a hoy.
_ayer_b = (_hoyb.date() - timedelta(days=1)).isoformat()
cb.post("/agenda/bitacora", data={"csrf_token": "tok", "telefono": TELB, "fecha": _ayer_b, "texto": "Lámina 2",
                                  "para_proxima": "  Traer   el cuaderno  "})
_pp = next(e for e in TABLAS["paciente_bitacora"] if e["texto"] == "Lámina 2")
chequear("para la próxima: se guarda limpio", _pp["para_proxima"] == "Traer el cuaderno")
pag = cb.get(f"/agenda/fichas/{ref_b}").data.decode()
chequear("para la próxima: «Para hoy» en la página rápida", "Para hoy" in pag and "Traer el cuaderno" in pag)
html = _panel(cb)
chequear("para la próxima: «Para hoy» en la tarjeta de Hoy", "<strong>Para hoy</strong>" in html and "Traer el cuaderno" in html)
chequear("dictado: botón (oculto hasta saber si hay dictado local) y script", 'data-dictar="rapida-texto" hidden' in pag
         and "processLocally" in pag and "data-dictar=" in html)
cb.post("/agenda/bitacora", data={"csrf_token": "tok", "telefono": TELB, "fecha": _hoyb.date().isoformat(), "texto": "Hoy",
                                  "para_proxima": "Para el jueves"})
pag = cb.get(f"/agenda/fichas/{ref_b}").data.decode()
chequear("para la próxima: lo de hoy no es «Para hoy»", "Traer el cuaderno" in pag and "<strong>Para hoy</strong>" in pag
         and "Para el jueves" in pag and pag.index("Traer el cuaderno") < pag.index("Para el jueves"))
cb.post(f"/agenda/bitacora/{_pp['id']}", data={"csrf_token": "tok", "rapida": ref_b, "fecha": _ayer_b, "texto": "Lámina 2", "para_proxima": ""})
pag = cb.get(f"/agenda/fichas/{ref_b}").data.decode()
chequear("para la próxima: al vaciarlo deja de salir", _pp["para_proxima"] is None and "<strong>Para hoy</strong>" not in pag)
r = cb.post("/agenda/bitacora", data={"csrf_token": "tok", "telefono": TELB, "fecha": _ayer_b, "texto": "x", "para_proxima": "a" * 1001},
            follow_redirects=True)
chequear("para la próxima: muy largo no se guarda", "muy largo" in r.data.decode() and not any(e["texto"] == "x" for e in TABLAS["paciente_bitacora"]))
# Tilde del «para la próxima».
_pp2_id = db.agregar_bitacora("t1", TELB, _ayer_b, "Lámina 3", "Repasar la r")
_pp2 = next(e for e in TABLAS["paciente_bitacora"] if e["id"] == _pp2_id)
pag = cb.get(f"/agenda/fichas/{ref_b}").data.decode()
chequear("tilde: el «Para hoy» es un tilde táctil sin marcar", 'role="checkbox" aria-checked="false"' in pag and "Repasar la r" in pag)
r = cb.post(f"/agenda/bitacora/{_pp2_id}/hecho", data={"csrf_token": "tok", "hecho": "1", "rapida": ref_b}, headers={"X-Parcial": "1"})
chequear("tilde: con X-Parcial contesta 204 y queda hecho", r.status_code == 204 and _pp2.get("proxima_hecha_at"))
pag = cb.get(f"/agenda/fichas/{ref_b}").data.decode()
chequear("tilde: se ve tildado (y se puede destildar)", 'aria-checked="true"' in pag and 'name="hecho" value="0"' in pag)
html = _panel(cb)
chequear("tilde: también en la tarjeta de Hoy", "Repasar la r" in html and 'aria-checked="true"' in html)
r = cb.post(f"/agenda/bitacora/{_pp2_id}/hecho", data={"csrf_token": "tok", "hecho": "0", "rapida": ref_b})
chequear("tilde: sin JS vuelve a la página y destilda", r.status_code == 302 and _pp2.get("proxima_hecha_at") is None)
TABLAS["paciente_bitacora"].append({"id": "ajena-h", "tenant_id": "t2", "paciente_telefono": TELB, "ficha": "", "fecha": "2026-11-01",
                                    "texto": "otra", "para_proxima": "x", "created_at": "0"})
cb.post("/agenda/bitacora/ajena-h/hecho", data={"csrf_token": "tok", "hecho": "1"})
chequear("tilde: no toca lo de otra cuenta", not TABLAS["paciente_bitacora"][-1].get("proxima_hecha_at"))
r = cb.post(f"/agenda/bitacora/{_pp2_id}/hecho", data={"hecho": "1"}, headers={"X-Parcial": "1"})
chequear("tilde: pide CSRF", r.status_code in (400, 403) and _pp2.get("proxima_hecha_at") is None)

# Fotos de la anotación.
import io as _io_b  # noqa: E402
JPG = b"\xff\xd8\xff\xe0" + b"0" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100
r = cb.post("/agenda/bitacora", data={"csrf_token": "tok", "telefono": TELB, "rapida": ref_b, "fecha": _hoyb.date().isoformat(),
                                      "texto": "Con fotos", "fotos": [(_io_b.BytesIO(JPG), "a.jpg"), (_io_b.BytesIO(PNG), "b.png"),
                                                                     (_io_b.BytesIO(b"<svg onload=alert(1)>"), "c.svg")]},
            content_type="multipart/form-data", follow_redirects=True)
_cf = next(e for e in TABLAS["paciente_bitacora"] if e["texto"] == "Con fotos")
_fotos = [f for f in TABLAS["bitacora_fotos"] if f["entrada_id"] == _cf["id"]]
chequear("fotos: guarda JPG y PNG, rechaza lo demás", len(_fotos) == 2 and "no se pudo guardar" in r.data.decode()
         and _fotos[0]["imagen"].startswith("data:image/jpeg;base64,") and _fotos[0]["tenant_id"] == "t1")
pag = cb.get(f"/agenda/fichas/{ref_b}").data.decode()
chequear("fotos: miniaturas en la página rápida, sin la imagen adentro", f"/agenda/bitacora/foto/{_fotos[0]['id']}" in pag and "base64" not in pag)
r = cb.get(f"/agenda/bitacora/foto/{_fotos[0]['id']}")
chequear("fotos: se sirven con su tipo y sin caché", r.status_code == 200 and r.data == JPG and r.headers["Content-Type"] == "image/jpeg"
         and r.headers.get("Cache-Control") == "no-store")
TABLAS["bitacora_fotos"].append({"id": "foto-ajena", "tenant_id": "t2", "entrada_id": "ajena-h", "imagen": "data:image/png;base64,AAAA"})
chequear("fotos: la de otra cuenta no se ve", cb.get("/agenda/bitacora/foto/foto-ajena").status_code == 404)
cb.post("/agenda/bitacora/foto/foto-ajena/borrar", data={"csrf_token": "tok"})
chequear("fotos: la de otra cuenta no se borra", any(f["id"] == "foto-ajena" for f in TABLAS["bitacora_fotos"]))
cb.post("/agenda/bitacora/ajena-h/fotos", data={"csrf_token": "tok", "fotos": [(_io_b.BytesIO(JPG), "a.jpg")]}, content_type="multipart/form-data")
chequear("fotos: no se suman a una anotación de otra cuenta", not any(f["entrada_id"] == "ajena-h" and f["tenant_id"] == "t1" for f in TABLAS["bitacora_fotos"]))
cb.post(f"/agenda/bitacora/{_cf['id']}/fotos", data={"csrf_token": "tok", "rapida": ref_b,
        "fotos": [(_io_b.BytesIO(JPG), f"{i}.jpg") for i in range(6)]}, content_type="multipart/form-data")
chequear("fotos: hasta 6 por anotación", len([f for f in TABLAS["bitacora_fotos"] if f["entrada_id"] == _cf["id"]]) == app.FOTOS_POR_ENTRADA)
html = _panel(cb)
chequear("fotos: miniaturas en la ficha del panel, sin la imagen adentro", f"/agenda/bitacora/foto/{_fotos[0]['id']}" in html
         and "data:image/jpeg;base64,/9j" not in html)
cb.post(f"/agenda/bitacora/foto/{_fotos[0]['id']}/borrar", data={"csrf_token": "tok", "rapida": ref_b})
chequear("fotos: borra una", not any(f["id"] == _fotos[0]["id"] for f in TABLAS["bitacora_fotos"]))
cb.post(f"/agenda/bitacora/{_cf['id']}/borrar", data={"csrf_token": "tok", "rapida": ref_b})
chequear("fotos: al borrar la anotación se van sus fotos", not any(f["entrada_id"] == _cf["id"] for f in TABLAS["bitacora_fotos"]))
fc = cb.post("/agenda/paciente/ficha-completa", data={"csrf_token": "tok", "telefono": TELB}).data.decode()
chequear("fotos: no van en la ficha completa", "/agenda/bitacora/foto/" not in fc)

# La bitácora en el borrador de informe.
inf = cb.post("/agenda/paciente/informe", data={"csrf_token": "tok", "telefono": TELB}).data.decode()
chequear("informe: «Sumar la bitácora» con desde/hasta", "Sumar la bitácora" in inf and 'id="bit-desde" value="2026-11-03"' in inf
         and "Trabajamos lectura de s" in inf and "bitacora-textos" in inf)
chequear("informe: los textos van escapados", "</script><" not in inf.split('id="bitacora-textos">')[1].split("</script>")[0])
# Abrir la app: rápido, y durante una sesión directo a ese paciente.
MOVIL = {"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/131 Mobile Safari/537.36"}
_ahora_u = datetime.now(timezone.utc)
_bt1 = next(t for t in TABLAS["turnos"] if t["id"] == "bt1")
_guardo = (_bt1["inicio"], _bt1["fin"])
_bt1["inicio"], _bt1["fin"] = (_ahora_u - timedelta(minutes=10)).isoformat(), (_ahora_u + timedelta(minutes=35)).isoformat()
r = cb.get("/agenda/abrir", headers=MOVIL)
chequear("abrir app: durante la sesión va a la bitácora de ese paciente", r.status_code == 302 and r.headers["Location"].endswith(f"/agenda/fichas/{ref_b}")
         and r.headers.get("Cache-Control") == "no-store")
_bt1["inicio"], _bt1["fin"] = (_ahora_u - timedelta(minutes=80)).isoformat(), (_ahora_u - timedelta(minutes=35)).isoformat()
r = cb.get("/agenda/abrir", headers=MOVIL)
chequear("abrir app: sin sesión, en el celular va a la lista de pacientes", r.status_code == 302 and r.headers["Location"].endswith("/agenda/fichas"))
_bt1["inicio"], _bt1["fin"] = (_ahora_u + timedelta(minutes=10)).isoformat(), (_ahora_u + timedelta(minutes=55)).isoformat()
r = cb.get("/agenda/abrir", headers=MOVIL)
chequear("abrir app: 10 minutos antes ya abre ese paciente", r.headers["Location"].endswith(f"/agenda/fichas/{ref_b}"))
_bt1["inicio"], _bt1["fin"] = _guardo
r = cb.get("/agenda/abrir", headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/131"})
chequear("abrir app: en la compu va al panel", r.status_code == 302 and "/agenda?app=1" in r.headers["Location"])
TABLAS["turnos"].append(turno("bt-ajeno", "t2", _ahora_u - timedelta(minutes=5), estado="reservado", paciente_nombre="Ajeno",
                              paciente_telefono="5491166660000"))
r = cb.get("/agenda/abrir", headers=MOVIL)
chequear("abrir app: la sesión de otra cuenta no cuenta", r.headers["Location"].endswith("/agenda/fichas"))
TABLAS["turnos"].pop()
chequear("manifest: la app arranca en /agenda/abrir", cb.get("/manifest.webmanifest").get_json()["start_url"] == "/agenda/abrir")
html = _panel(cb)
chequear("celular: los pacientes van primero en la agenda", 'id="pacientes-movil"' in html
         and html.index('id="pacientes-movil"') < html.index('id="hoy"') and "Mía Sol</span><span class=\"chip\">hoy" in html)
# Precarga en paralelo: cada función toma SU resultado una vez y después consulta de nuevo.
with app.app.test_request_context("/agenda"):
    _antes = list(TABLAS["lista_espera"])
    db.precargar([("lista_de_espera", ("t1",))])
    TABLAS["lista_espera"].append({"id": "pre-1", "tenant_id": "t1", "nombre": "Nueva", "telefono": "5491100055555",
                                   "created_at": "2026-11-10T10:00:00+00:00"})
    _uno = db.lista_de_espera("t1")
    _dos = db.lista_de_espera("t1")
    chequear("precarga: la primera llamada usa lo precargado y la segunda consulta", not any(x["id"] == "pre-1" for x in _uno)
             and any(x["id"] == "pre-1" for x in _dos))
    db.precargar([("lista_de_espera", ("t2",))])
    chequear("precarga: otros argumentos no se mezclan", not any(x["tenant_id"] == "t2" for x in db.lista_de_espera("t1")))
    chequear("precarga: una lista como argumento no rompe", isinstance(db.fotos_bitacora("t1", ["x"]), list))
    TABLAS["lista_espera"] = _antes
chequear("precarga: fuera de un pedido consulta normal", isinstance(db.lista_de_espera("t1"), list))

# Sesión en curso y «Falta anotar».
_bt1 = next(t for t in TABLAS["turnos"] if t["id"] == "bt1")
_guardo = {k: _bt1.get(k) for k in ("inicio", "fin", "nota", "asistencia", "pago", "valor")}
_ahora_u = datetime.now(timezone.utc)
_bt1.update(inicio=(_ahora_u - timedelta(minutes=10)).isoformat(), fin=(_ahora_u + timedelta(minutes=35)).isoformat(),
            pago="pendiente", valor=30000, asistencia=None)
TABLAS["turnos"].append(turno("bt-pasado", "t1", _ahora_u - timedelta(days=7), estado="reservado", paciente_nombre="Mía Sol",
                              paciente_telefono=TELB, pago="pendiente", valor=25000, nota="Habló de la escuela nueva."))
pag = cb.get(f"/agenda/fichas/{ref_b}").data.decode()
chequear("sesión en curso: arriba, con su horario y Vino / Pagó", "En sesión ·" in pag and 'name="asistencia" value="vino"' in pag
         and "Pagó $ 30.000" in pag)
chequear("sesión en curso: deuda de antes (sin contar la de ahora) y última nota", "Debe $ 25.000 (1 sesión sin pagar)" in pag
         and "Habló de la escuela nueva." in pag)
r = cb.post("/agenda/asistencia/bt1", data={"csrf_token": "tok", "asistencia": "vino", "rapida": ref_b})
chequear("sesión en curso: Vino vuelve a la página rápida", r.status_code == 302 and r.headers["Location"].endswith(f"/agenda/fichas/{ref_b}")
         and _bt1["asistencia"] == "vino")
_bt1.update(asistencia=None)
pag = cb.get(f"/agenda/fichas/{ref_b}").data.decode()
chequear("terminar sesión: los dos botones cuando falta el pago", "Terminar · pagó $ 30.000" in pag and "Terminar · no pagó" in pag)
r = cb.post("/agenda/terminar/bt1", data={"csrf_token": "tok", "pago": "pagado", "rapida": ref_b})
chequear("terminar sesión: marca vino y pagó y vuelve a la bitácora", r.status_code == 302
         and r.headers["Location"].endswith(f"/agenda/fichas/{ref_b}#rapida-texto") and _bt1["asistencia"] == "vino" and _bt1["pago"] == "pagado")
pag = cb.get(f"/agenda/fichas/{ref_b}").data.decode()
chequear("terminar sesión: avisa y ya no ofrece terminar", "Sesión terminada" in pag and "Terminar" not in pag)
_bt1.update(asistencia=None, pago="pendiente")
cb.post("/agenda/terminar/bt1", data={"csrf_token": "tok", "pago": "", "rapida": ref_b})
chequear("terminar sesión: «no pagó» sólo marca que vino", _bt1["asistencia"] == "vino" and _bt1["pago"] == "pendiente")
chequear("terminar sesión: pago raro, 400", cb.post("/agenda/terminar/bt1", data={"csrf_token": "tok", "pago": "x"}).status_code == 400)
cb.post("/agenda/terminar/bt-ajeno", data={"csrf_token": "tok", "rapida": ref_b})
_bt1.update(asistencia=None)
# Una sesión de HOY que ya terminó (pasada la medianoche, la de hace 70 min sería de ayer).
_ini_hoy = max(_ahora_u - timedelta(minutes=70), datetime.combine(_hoyb.date(), datetime.min.time(), app.ZoneInfo(TZ)).astimezone(timezone.utc))
_bt1.update(inicio=_ini_hoy.isoformat(), fin=min(_ini_hoy + timedelta(minutes=45), _ahora_u - timedelta(minutes=1)).isoformat())
for e in [e for e in TABLAS["paciente_bitacora"] if e.get("fecha") == _hoyb.date().isoformat()]:
    TABLAS["paciente_bitacora"].remove(e)
lista = cb.get("/agenda/fichas").data.decode()
chequear("falta anotar: la sesión que terminó sin nada anotado", "Falta anotar" in lista and f"/agenda/fichas/{ref_b}" in lista.split("Falta anotar")[1][:600])
html = _panel(cb)
chequear("falta anotar: también arriba del panel", 'id="falta-anotar"' in html and html.index('id="falta-anotar"') < html.index('id="hoy"'))
# Aviso de las 21: sólo la cantidad, una vez por día.
_tb = {**db.get_tenant_by_id("t1"), "aviso_telefono": "5491100099999", "ultimo_aviso_sin_anotar": None}
_zb = app.ZoneInfo(_tb.get("timezone") or "America/Argentina/Buenos_Aires")
_hoy_l = datetime.now(_zb).replace(hour=0, minute=0, second=0, microsecond=0)
_g2 = {k: _bt1.get(k) for k in ("inicio", "fin")}
_bt1.update(inicio=(_hoy_l + timedelta(hours=19)).isoformat(), fin=(_hoy_l + timedelta(hours=19, minutes=45)).isoformat())
_avisos, _ap = [], scheduler.avisar_profesional
scheduler.avisar_profesional = lambda ten, texto, **kw: _avisos.append(texto) or True
scheduler._aviso_sin_anotar(_tb, _zb, _hoy_l + timedelta(hours=20, minutes=50))
chequear("aviso sin anotar: antes de las 21 no sale", _avisos == [])
scheduler._aviso_sin_anotar(_tb, _zb, _hoy_l + timedelta(hours=21, minutes=10))
chequear("aviso sin anotar: a las 21 sale con la cantidad y sin nombres", _avisos == ["hoy te quedó 1 sesión sin anotar"], _avisos)
_tb["ultimo_aviso_sin_anotar"] = _hoy_l.date().isoformat()
scheduler._aviso_sin_anotar(_tb, _zb, _hoy_l + timedelta(hours=21, minutes=20))
chequear("aviso sin anotar: una sola vez por día", len(_avisos) == 1)
_tb.update(ultimo_aviso_sin_anotar=None, aviso_sin_anotar=False)
scheduler._aviso_sin_anotar(_tb, _zb, _hoy_l + timedelta(hours=21, minutes=20))
chequear("aviso sin anotar: apagado desde ajustes, no sale", len(_avisos) == 1)
_tb.update(aviso_sin_anotar=None)
_bt1["nota"] = "anotada"
scheduler._aviso_sin_anotar(_tb, _zb, _hoy_l + timedelta(hours=21, minutes=20))
chequear("aviso sin anotar: todo anotado, no sale", len(_avisos) == 1)
_bt1.update(nota=None, **_g2)
scheduler.avisar_profesional = _ap
cb.post("/agenda/bitacora", data={"csrf_token": "tok", "telefono": TELB, "fecha": _hoyb.date().isoformat(), "texto": "Listo"})
chequear("falta anotar: con la bitácora de hoy ya no sale", "Falta anotar" not in cb.get("/agenda/fichas").data.decode()
         and 'id="falta-anotar"' not in _panel(cb))
TABLAS["paciente_bitacora"] = [e for e in TABLAS["paciente_bitacora"] if e["texto"] != "Listo"]
_bt1["nota"] = "algo"
chequear("falta anotar: con nota de sesión tampoco", "Falta anotar" not in cb.get("/agenda/fichas").data.decode())
_bt1.update(nota=None, asistencia="no_vino")
chequear("falta anotar: si no vino, no", "Falta anotar" not in cb.get("/agenda/fichas").data.decode())
_bt1.update(asistencia=None, inicio=(_ahora_u + timedelta(minutes=60)).isoformat(), fin=(_ahora_u + timedelta(minutes=105)).isoformat())
chequear("falta anotar: una sesión que no terminó, no", "Falta anotar" not in cb.get("/agenda/fichas").data.decode())
_bt1.update(_guardo)
TABLAS["turnos"] = [t for t in TABLAS["turnos"] if t["id"] != "bt-pasado"]
chequear("manifest: atajo a las fichas", any(x["url"] == "/agenda/fichas" for x in cb.get("/manifest.webmanifest").get_json()["shortcuts"]))
# «Para preparar»: el «para la próxima» en el día de la sesión que sigue.
_zp = app.ZoneInfo(TZ)
_hp = datetime.now(_zp).replace(hour=0, minute=0, second=0, microsecond=0)
TELP, TELS = "5491177770000", "5491177771111"
TABLAS["turnos"] += [turno("pp1", "t1", (_hp + timedelta(days=1, hours=10)).astimezone(timezone.utc), estado="reservado",
                           paciente_nombre="Luz Prepara", paciente_telefono=TELP),
                     turno("pp2", "t1", (_hp + timedelta(days=3, hours=10)).astimezone(timezone.utc), estado="reservado",
                           paciente_nombre="Luz Prepara", paciente_telefono=TELP),
                     turno("pp-ajeno", "t2", (_hp + timedelta(days=1, hours=11)).astimezone(timezone.utc), estado="reservado",
                           paciente_nombre="Ajena Prepara", paciente_telefono="5491177772222")]
_ayer = (_hp - timedelta(days=1)).date().isoformat()
TABLAS["paciente_bitacora"] += [
    {"id": "pp-e1", "tenant_id": "t1", "paciente_telefono": TELP, "ficha": "", "fecha": _ayer, "texto": "Sesión secreta",
     "para_proxima": "Traer la lámina 3", "created_at": _ayer + "T12:00:00+00:00"},
    {"id": "pp-e2", "tenant_id": "t1", "paciente_telefono": TELS, "ficha": "", "fecha": _ayer, "texto": "x",
     "para_proxima": "Llamar a la escuela", "created_at": _ayer + "T12:00:00+00:00"},
    {"id": "pp-e3", "tenant_id": "t2", "paciente_telefono": "5491177772222", "ficha": "", "fecha": _ayer, "texto": "x",
     "para_proxima": "Cosa de otra cuenta", "created_at": _ayer + "T12:00:00+00:00"}]
TABLAS["pacientes"].append({"tenant_id": "t1", "paciente_telefono": TELS, "ficha": "", "nombre": "Sol Sin Turno"})
def _cal(p):
    """El calendario, sin la lista «Todo junto» de arriba."""
    return p.split('class="preparar-grilla"', 1)[1]


r = cb.get("/agenda/preparar")
pag = r.data.decode()
_man = _cal(pag)[_cal(pag).index("Mañana"):]
chequear("preparar: lo de «para la próxima» sale en la sesión que sigue", r.status_code == 200 and "Traer la lámina 3" in _man
         and _man.index("Luz Prepara") < _man.index("Traer la lámina 3") and r.headers.get("Cache-Control") == "no-store")
chequear("preparar: sólo en la primera sesión después de anotarlo", _cal(pag).count("Traer la lámina 3") == 1
         and _cal(pag).count("Luz Prepara") == 2)
_junto = pag.split('id="todo-junto"', 1)[1].split("</ul>")[0]
chequear("todo junto: lo de la semana en una lista, con quién y qué día",
         "<strong>Todo junto</strong> · " in pag and "Traer la lámina 3" in _junto and "Luz Prepara, Mañana" in _junto
         and "Llamar a la escuela" not in _junto and "Cosa de otra cuenta" not in _junto)
chequear("preparar: nunca el texto de la sesión ni lo de otra cuenta", "Sesión secreta" not in pag and "Cosa de otra cuenta" not in pag
         and "Ajena Prepara" not in pag and TELP not in pag)
chequear("preparar: sin sesión agendada, aparte", "Sin próxima sesión agendada" in pag and "Llamar a la escuela" in pag.split("Sin próxima sesión agendada")[1])
r = cb.post("/agenda/bitacora/pp-e1/hecho", data={"csrf_token": "tok", "hecho": "1", "preparar": _hp.date().isoformat()})
chequear("preparar: el tilde vuelve al calendario", r.status_code == 302 and "/agenda/preparar?desde=" in r.headers["Location"]
         and next(e for e in TABLAS["paciente_bitacora"] if e["id"] == "pp-e1").get("proxima_hecha_at"))
pag = cb.get("/agenda/preparar").data.decode()
chequear("preparar: tildado se sigue viendo, marcado", 'aria-checked="true"' in pag and "Traer la lámina 3" in pag)
TABLAS["paciente_bitacora"].append({"id": "pp-e4", "tenant_id": "t1", "paciente_telefono": TELP, "ficha": "",
                                    "fecha": (_hp + timedelta(days=1)).date().isoformat(), "texto": "y", "para_proxima": "Juego de cartas",
                                    "created_at": "2099-01-01T00:00:00+00:00"})
pag = cb.get("/agenda/preparar").data.decode()
chequear("preparar: lo anotado en una sesión va a la siguiente", _cal(pag).count("Juego de cartas") == 1
         and _cal(pag).index("Traer la lámina 3") < _cal(pag).index("Juego de cartas"))
_junto = pag.split('id="todo-junto"', 1)[1].split("</ul>")[0]
chequear("todo junto: lo ya tildado no está", "Juego de cartas" in _junto and "Traer la lámina 3" not in _junto)
pag = cb.get(f"/agenda/preparar?desde={(_hp + timedelta(days=7)).date().isoformat()}").data.decode()
chequear("preparar: la semana siguiente", "Traer la lámina 3" not in pag and "Hoy</a>" in pag and 'id="todo-junto"' not in pag)
chequear("preparar: fecha rara, desde hoy", cb.get("/agenda/preparar?desde=xx").status_code == 200)
chequear("preparar: accesos desde fichas y panel", 'href="/agenda/preparar"' in cb.get("/agenda/fichas").data.decode()
         and 'href="/agenda/preparar"' in _panel(cb))
TABLAS["turnos"] = [t for t in TABLAS["turnos"] if not t["id"].startswith("pp")]
TABLAS["paciente_bitacora"] = [e for e in TABLAS["paciente_bitacora"] if not e["id"].startswith("pp-")]
TABLAS["pacientes"] = [x for x in TABLAS["pacientes"] if x.get("paciente_telefono") != TELS]
db.get_tenant_by_id = _gtid_b

# Sitio propio: link corto que elige el profesional (el viejo redirige), preguntas frecuentes y Google.
_gtid_s = db.get_tenant_by_id
db.get_tenant_by_id = lambda tid: next((dict(t) for t in TABLAS["tenants"] if t["id"] == tid), None)
_ts = next(t for t in TABLAS["tenants"] if t["id"] == "t1")
_ts.update(slug="marcos", activo=True, preguntas=None, en_directorio=None)
_gtbs = db.get_tenant_by_slug
db.get_tenant_by_slug = lambda slug: next((dict(t) for t in TABLAS["tenants"] if t.get("slug") == slug and t.get("activo")), None)
next(t for t in TABLAS["tenants"] if t["id"] == "t2").update(slug="otra", activo=True)
TABLAS["slugs_anteriores"] = [{"slug": "reservado-ajeno", "tenant_id": "t2"}]
_w = app.app.test_client()
r = cb.post("/agenda/link", data={"csrf_token": "tok", "slug": " Lic Marcos "}, follow_redirects=True)
chequear("link corto: se cambia (minúsculas, espacios a guiones) y avisa", _ts["slug"] == "lic-marcos"
         and "tu página ahora es" in r.data.decode() and "El link anterior sigue funcionando" in r.data.decode())
chequear("link corto: el anterior queda guardado", {"slug": "marcos", "tenant_id": "t1"}.items()
         <= next(x for x in TABLAS["slugs_anteriores"] if x["slug"] == "marcos").items())
r = _w.get("/marcos?semana=2026-11-16")
chequear("link corto: el viejo redirige al nuevo", r.status_code == 301 and r.headers["Location"].endswith("/lic-marcos?semana=2026-11-16"))
chequear("link corto: el nuevo anda", _w.get("/lic-marcos").status_code == 200)
for _malo, _por in (("otra", "de otra cuenta"), ("reservado-ajeno", "viejo de otra cuenta"), ("agenda", "ruta del sistema"),
                    ("ñu", "corto y con ñ"), ("con_guion_bajo", "guión bajo"), ("-arranca", "guión al principio")):
    cb.post("/agenda/link", data={"csrf_token": "tok", "slug": _malo})
    chequear(f"link corto: no deja usar uno {_por}", _ts["slug"] == "lic-marcos", _malo)
cb.post("/agenda/link", data={"csrf_token": "tok", "slug": "marcos"})
chequear("link corto: volver al de antes lo saca de los viejos", _ts["slug"] == "marcos"
         and not any(x["slug"] == "marcos" for x in TABLAS["slugs_anteriores"])
         and any(x["slug"] == "lic-marcos" and x["tenant_id"] == "t1" for x in TABLAS["slugs_anteriores"]))
chequear("link corto: pide CSRF", cb.post("/agenda/link", data={"slug": "otro-mas"}).status_code in (400, 403) and _ts["slug"] == "marcos")
cb.post("/agenda/presentacion", data={"csrf_token": "tok", "titulo": "Psicopedagoga", "ubicacion": "Caballito",
                                      "pregunta_0": " ¿Cuánto dura   la primera entrevista? ", "respuesta_0": "Una hora.",
                                      "pregunta_1": "Sin respuesta", "respuesta_1": " ",
                                      "pregunta_2": "¿Trabajás con la escuela?", "respuesta_2": "Sí, con el equipo de orientación.\nY con la familia."})
chequear("preguntas: se guardan limpias y sólo las completas", _ts["preguntas"] == [
    {"p": "¿Cuánto dura la primera entrevista?", "r": "Una hora."},
    {"p": "¿Trabajás con la escuela?", "r": "Sí, con el equipo de orientación.\nY con la familia."}], _ts["preguntas"])
pag = _w.get("/marcos").data.decode()
chequear("preguntas: salen en la página y como FAQPage para Google", "Preguntas frecuentes" in pag
         and "¿Trabajás con la escuela?" in pag and '"@type": "FAQPage"' in pag and '"acceptedAnswer"' in pag)
chequear("google: título con nombre, profesión y lugar; indexable", f"<title>{_ts['nombre']} — Psicopedagoga en Caballito · Turnos online</title>" in pag
         and 'name="robots" content="noindex"' not in pag)
_aj = cb.get("/ajustes?p=pagina").data.decode()
chequear("ajustes: el link y las preguntas se pueden editar", 'name="slug" value="marcos"' in _aj and "¿Cuánto dura la primera entrevista?" in _aj
         and 'name="pregunta_2"' in _aj and "Que Google pueda mostrar tu página" in _aj)
_ts["en_directorio"] = False
chequear("google: si la oculta, noindex y fuera del sitemap", 'name="robots" content="noindex"' in _w.get("/marcos").data.decode()
         and "/marcos<" not in _w.get("/sitemap.xml").data.decode())
_ts["en_directorio"] = None
chequear("google: si no, en el sitemap", "/marcos</loc>" in _w.get("/sitemap.xml").data.decode())
_ts.update(preguntas=None, titulo=None, ubicacion=None)

# --- De dónde llegan las visitas, «Tu página en Google» y tarjeta para Instagram ---
TABLAS["eventos_pagina"] = []
_w.get("/marcos", headers={"Referer": "https://www.google.com.ar/"})
_w.get("/marcos", headers={"Referer": "android-app://com.google.android.googlequicksearchbox/"})
_w.get("/marcos", headers={"User-Agent": "Mozilla/5.0 (iPhone) Instagram 300.0"})
_w.get("/marcos", headers={"Referer": "https://l.facebook.com/"})
_w.get("/marcos?desde=qr")
_w.get("/marcos", headers={"Referer": "https://localhost/marcos?semana=2026-11-16"})
_w.get("/marcos", headers={"Referer": "https://blog.example.com/nota"})
_w.get("/marcos")
_or = [e.get("origen") for e in TABLAS["eventos_pagina"] if e["tipo"] == "visita"]
chequear("visitas: se anota sólo el sitio de origen", _or == ["google", "google", "instagram", "facebook", "qr", None, "otro", None], _or)
chequear("visitas: no se guarda nada de la persona", all(set(e) <= {"id", "tenant_id", "tipo", "cuando", "origen", "created_at"} for e in TABLAS["eventos_pagina"]))
_aj = cb.get("/ajustes?p=pagina").data.decode()
chequear("visitas: «Este mes» dice de dónde llegaron", "2 desde Google" in _aj and "1 desde Instagram" in _aj and "1 desde el QR" in _aj
         and "2 con el link directo" in _aj, _aj[_aj.find("De dónde"):_aj.find("De dónde") + 300])
_ts.update(titulo="Psicopedagoga", ubicacion=None, sobre_mi="corto", enfoques=["Lectura"], foto=None, preguntas=None,
           preferencias_vista={})
_ag = _panel(cb)
chequear("google: el panel dice qué falta del perfil", 'id="aviso-perfil-google"' in _ag
         and "te falta dónde atendés, «Sobre mí», tu foto y preguntas frecuentes" in _ag)
_aj = cb.get("/ajustes?p=pagina").data.decode()
chequear("google: en ajustes, la lista con lo hecho tachado", 'id="perfil-google"' in _aj and "· 2 de 6" in _aj and "<s>Título</s>" in _aj)
cb.post("/agenda/perfil-google/ocultar", data={"csrf_token": "tok"})
chequear("google: «Ahora no» lo saca del panel pero no de ajustes", 'id="aviso-perfil-google"' not in _panel(cb)
         and 'id="perfil-google"' in cb.get("/ajustes?p=pagina").data.decode())
_ts.update(preferencias_vista={}, ubicacion="Caballito", sobre_mi="x" * 200, foto="data:image/png;base64,AAAA",
           preguntas=[{"p": "a", "r": "b"}, {"p": "c", "r": "d"}])
chequear("google: con todo completo no hay aviso", 'id="aviso-perfil-google"' not in _panel(cb)
         and "Tu perfil está completo" in cb.get("/ajustes?p=pagina").data.decode())
_ts["en_directorio"] = False
chequear("google: si la ocultó, no le pide completar (ya avisa el otro cartel)", 'id="aviso-perfil-google"' not in _panel(cb))
_ts["en_directorio"] = None
_tj = cb.get("/agenda/tarjeta")
_tjh = _tj.data.decode()
chequear("tarjeta: se arma en el navegador con el link corto y el QR", _tj.status_code == 200 and '"localhost/marcos"' in _tjh
         and 'qr: ["' in _tjh and "Psicopedagoga" in _tjh and "noindex" in _tjh)
chequear("tarjeta: pide sesión", _w.get("/agenda/tarjeta").status_code in (302, 401))
chequear("tarjeta: está en ajustes", 'href="/agenda/tarjeta"' in cb.get("/ajustes?p=pagina").data.decode())
_ts.update(sobre_mi=None, enfoques=None, foto=None, preguntas=None, titulo=None, ubicacion=None)

# --- Diseño propio de la página ---
import diseno as _dis  # noqa: E402
for _k in ("diseno", "diseno_borrador", "css_propio", "css_borrador"):
    _ts[_k] = None
_ts.update(sobre_mi="Acompaño procesos de aprendizaje.", enfoques=["Lectura"], preguntas=[{"p": "¿Cuánto dura?", "r": "Una hora."}],
           ubicacion="Caballito")
_src = open("templates/publico.html", encoding="utf8").read() + open("templates/_sitio_macros.html", encoding="utf8").read()
chequear("diseño: las partes que se le explican a la IA existen en la página",
         all(c[1:] in _src for sel, _ in _dis.PARTES for c in re.findall(r"\.[a-z][a-z0-9-]*", sel))
         and all(f'data-modulo="{m}"' in _src for m in _dis.MODULOS))
_pg = _w.get("/marcos")
chequear("diseño: sin diseño propio la página queda igual y sin CSP", 'id="diseno-estilo"' not in _pg.data.decode()
         and "Content-Security-Policy" not in _pg.headers)
_pd = cb.get("/agenda/diseno").data.decode()
chequear("diseño: la pantalla trae las instrucciones para la IA con el link", "localhost/marcos" in _pd and "```json" in _pd
         and "pf-caja" in _pd and "url()" in _pd)
_resp = 'Acá va:\n```json\n{"principal": "#7a4b2a", "calendario": "izquierda", "orden": ["preguntas", "sobre"], "letra_titulos": "lora"}\n```\n```css\n.pf-cabeza h1 { letter-spacing: 0 !important; }\n```'
cb.post("/agenda/diseno/pegar", data={"csrf_token": "tok", "respuesta": _resp})
chequear("diseño: lo de la IA queda como borrador, sin publicar", _ts["diseno_borrador"]["principal"] == "#7a4b2a"
         and _ts["diseno_borrador"]["letra_titulos"] == "Lora" and _ts["diseno_borrador"]["orden"][:2] == ["preguntas", "sobre"]
         and _ts["css_borrador"] == ".pf-cabeza h1 { letter-spacing: 0; }" and not _ts["diseno"], (_ts["diseno_borrador"], _ts["css_borrador"]))
chequear("diseño: los pacientes no ven el borrador", 'id="diseno-css"' not in _w.get("/marcos").data.decode()
         and 'id="diseno-css"' not in _w.get("/marcos?borrador=1").data.decode())
TABLAS["eventos_pagina"] = []
_pb = cb.get("/marcos?borrador=1")
_pbh = _pb.data.decode()
chequear("diseño: la profesional ve su borrador, sin contar visita ni indexar", 'id="diseno-css"' in _pbh and "letter-spacing: 0;" in _pbh
         and ".pf { flex-direction:row-reverse; }" in _pbh and "Borrador: así se vería" in _pbh
         and _pb.headers.get("X-Robots-Tag") == "noindex" and not TABLAS["eventos_pagina"]
         and "fonts.googleapis.com/css2?family=Lora" in _pbh)
chequear("diseño: con diseño, la página no puede pedir nada a otros sitios", "connect-src 'self'" in _pb.headers.get("Content-Security-Policy", "")
         and "img-src 'self' data: blob:" in _pb.headers["Content-Security-Policy"])
chequear("diseño: el pie de urgencias va después del CSS propio", _pbh.index('id="diseno-fijo"') > _pbh.index('id="diseno-css"'))
for _malo, _por in ((".a{background:url(https://malo.example/x?d=1)}", "url("), ('.a{background:image-set("https://malo.example/x" 1x)}', "image-set("),
                    ("@import 'https://malo.example/a.css';", "@import"), (".a{color:red}</style><script>alert(1)</script>", "HTML"),
                    (".a{background:u\\72l(x)}", "escapes"), (".a{color:red", "llaves sin cerrar"), ("@font-face{font-family:x}", "@font-face")):
    _antes = _ts["css_borrador"]
    _r = cb.post("/agenda/diseno/borrador", data={"csrf_token": "tok", "css": _malo})
    chequear(f"diseño: rechaza CSS con {_por} y no lo guarda", _ts["css_borrador"] == _antes and "No lo guardé" in _r.data.decode(), _por)
_r = cb.post("/agenda/diseno/pegar", data={"csrf_token": "tok", "respuesta": '```json\n{"texto": "#f4efe6"}\n```'})
chequear("diseño: avisa si el texto no se lee sobre el fondo", "casi no se lee" in _r.data.decode() and _ts["diseno_borrador"]["principal"] == "#7a4b2a")
cb.post("/agenda/diseno/publicar", data={"csrf_token": "tok"})
_pp = _w.get("/marcos")
chequear("diseño: publicado, lo ven los pacientes", _ts["diseno"]["principal"] == "#7a4b2a" and _ts["diseno_borrador"] is None
         and 'id="diseno-css"' in _pp.data.decode() and "Content-Security-Policy" in _pp.headers
         and "Borrador:" not in _pp.data.decode())
chequear("diseño: también en las pantallas de reservar", 'id="diseno-estilo"' in _w.get(f"/marcos/reservar/{next((t['id'] for t in TABLAS['turnos'] if t['tenant_id'] == 't1' and t['estado'] == 'libre'), 'x')}").data.decode()
         or not any(t["tenant_id"] == "t1" and t["estado"] == "libre" for t in TABLAS["turnos"]))
cb.post("/agenda/diseno/borrador", data={"csrf_token": "tok", **{k: v for k, v in _dis.ORIGINAL.items() if isinstance(v, str)},
                                         "principal": "#7a4b2a", "calendario": "derecha", "en_celular": "calendario_primero",
                                         "orden": "donde,sobre,temas", "ocultar": ["mas"], "css": ""})
chequear("diseño: a mano, guarda sólo lo que cambia (módulos incluidos)", _ts["diseno_borrador"] == {
    "principal": "#7a4b2a", "en_celular": "calendario_primero", "orden": ["donde", "sobre", "temas", "formacion", "mas", "preguntas"],
    "ocultar": ["mas"]} and _ts["css_borrador"] is None, _ts["diseno_borrador"])
_pbh = cb.get("/marcos?borrador=1").data.decode()
chequear("diseño: módulos en el orden elegido y el calendario primero en el celular", '[data-modulo="donde"] { order:0; }' in _pbh
         and '.pf-secciones [data-modulo="mas"] { display:none; }' in _pbh and ".pf-lado { order:-1; }" in _pbh)
cb.post("/agenda/diseno/original", data={"csrf_token": "tok"})
chequear("diseño: «Volver al diseño original» borra todo", not any(_ts.get(k) for k in ("diseno", "diseno_borrador", "css_propio", "css_borrador"))
         and 'id="diseno-estilo"' not in _w.get("/marcos").data.decode())
chequear("diseño: pide CSRF y sesión", cb.post("/agenda/diseno/pegar", data={"respuesta": _resp}).status_code in (400, 403)
         and _w.get("/agenda/diseno").status_code in (302, 401))
chequear("diseño: está en ajustes", 'href="/agenda/diseno"' in cb.get("/ajustes?p=pagina").data.decode())
_ts.update(sobre_mi=None, enfoques=None, preguntas=None, ubicacion=None)
db.get_tenant_by_id, db.get_tenant_by_slug = _gtid_s, _gtbs

if fallos:
    raise SystemExit(f"\nFALLARON {len(fallos)}: {fallos}")
print("\nTODOS LOS TESTS DE SEGUIMIENTO DE SESIONES PASARON")
