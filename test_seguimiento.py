"""Tests del seguimiento de sesiones: asistencia, pago y reprogramar.

Correrlo SUELTO (`python test_seguimiento.py`), igual que los otros test_*.py
de este repo: reemplaza `db._client` sin restaurarlo.

A diferencia de los otros tests, acá NO se pisan las funciones de `db`: se
pisa el cliente de Supabase por uno en memoria, para que corra la lógica real
de db.py — que es justo donde están las cosas que importan (el orden de
reprogramar, que liberar un turno limpie el pago, que nada cruce de tenant).
"""
import itertools
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

fallos = []


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
            filas = [t for t in filas if t["estado"] == "libre" and not any(
                r["tenant_id"] == t["tenant_id"] and _norm(r["inicio"]) < _norm(t["fin"])
                and _norm(r["fin"]) > _norm(t["inicio"]) for r in reservados)
                and not any(b["tenant_id"] == t["tenant_id"] and _norm(b["desde"]) < _norm(t["fin"])
                            and _norm(b["hasta"]) > _norm(t["inicio"]) for b in TABLAS["bloqueos"])]
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


def reset():
    TABLAS["tenants"] = [dict(TENANT), dict(OTRO)]
    TABLAS["pacientes"] = []
    TABLAS["paciente_personas"] = []
    TABLAS["paciente_fechas"] = []
    TABLAS["lista_espera"] = []
    TABLAS["cancelaciones"] = []
    TABLAS["bloqueos"] = []
    TABLAS["paciente_tests"] = []
    TABLAS["textos_lectura"] = []
    TABLAS["lecturas"] = []
    TABLAS["cuestionarios"] = []
    TABLAS["informes"] = []
    TABLAS["plantillas_cuestionario"] = []
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
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
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
chequear("el panel lo muestra", "Para mirar" in client.get("/agenda").data.decode())

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
chequear("no muestra la de otro tenant", "De la otra" not in client.get("/agenda").data.decode())
client.post(f"/agenda/espera/{espera['id']}/quitar")
chequear("se puede quitar", not any(p["id"] == espera["id"] for p in TABLAS["lista_espera"]))

print("Sesiones virtuales")
reset()
enviados.clear()
client.post("/agenda/avisos", data={"link_videollamada": "http://inseguro"})
chequear("rechaza un link sin https", TABLAS["tenants"][0].get("link_videollamada") is None)
client.post("/agenda/avisos", data={"link_videollamada": "https://meet.example/sala"})
chequear("guarda el link", TABLAS["tenants"][0]["link_videollamada"] == "https://meet.example/sala")
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
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
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
chequear("el panel lo muestra", "Últimos 90 días" in client.get("/agenda").data.decode())

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
html = client.get("/agenda").data.decode()
chequear("el panel muestra el bloqueo", "Días bloqueados" in html and "Congreso" in html)
afectados = app._bloqueos(TENANT, app._fichas(TENANT, zona, ahora.astimezone(zona)), zona, ahora.astimezone(zona))
chequear("dice a quién avisarle", [a["ficha"]["nombre"] for a in afectados[0]["_afectados"]] == (["Ana"] if _norm(fila("futuro")["inicio"]).astimezone(zona).date() == dia_libre else []), afectados[0]["_afectados"])
chequear("el mes lo marca", 'bloqueado"' in html or " bloqueado" in html)
html = client.get(f"/agenda?semana={dia_libre.isoformat()}").data.decode()
chequear("la semana no deja agendar ese día", "bloqueado: Congreso" in html)
client.post(f"/agenda/bloqueo/{b['id']}/borrar")
chequear("se puede quitar", TABLAS["bloqueos"] == [] and "libre-manana" in [t["id"] for t in db.turnos_libres("t1", ahora)])
html = client.post("/agenda/bloqueo", data={"desde": "2030-02-10", "hasta": "2030-02-01"}, follow_redirects=True).data.decode()
chequear("hasta antes que desde se rechaza", "no puede ser anterior" in html and TABLAS["bloqueos"] == [])
TABLAS["bloqueos"].append({"id": "aj", "tenant_id": "t2", "desde": ahora.isoformat(), "hasta": (ahora + timedelta(days=3)).isoformat(), "motivo": "Ajeno"})
client.post("/agenda/bloqueo/aj/borrar")
chequear("no borra el de otro tenant", len(TABLAS["bloqueos"]) == 1 and "Ajeno" not in client.get("/agenda").data.decode())

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
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
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
    s["csrf_token"] = "tok"
reset()
TABLAS["turnos"].append(turno("esc", "t1", manana, estado="reservado", paciente_nombre="Nina", paciente_telefono="549111"))
html = client.get("/agenda").data.decode()
chequear("psicología no ve la pestaña Escuela", ">Crisis</button>" in html and 'name="escuela"' not in html)
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
html = client.get("/agenda").data.decode()
chequear("psicopedagogía ve Escuela y Familia", ">Escuela</button>" in html and ">Familia</button>" in html and ">Crisis</button>" not in html and 'name="equipo_orientador"' in html)
client.post("/agenda/paciente", data={"telefono": "549111", "escuela": " Escuela 12 ", "grado": "3°"})
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
chequear("la ficha vieja se ofrece como destino", f'<option value="{TEL4}">Mariana</option>' in html)
client.post("/agenda/sesion/m1/ficha", data={"destino": TEL4})
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
chequear("el panel pide revisar", "Revisar de quién es: 1 sesión" in html and "Es de esta ficha" in html)
client.post("/agenda/sesion/p2/ficha/confirmar", data={})
chequear("confirmar saca la marca y no mueve", not fila_p("p2")["revisar_ficha"] and fila_p("p2")["estado"] == "reservado")
html = client.get("/agenda").data.decode()
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
respuesta = client.post("/marcos/reservar/p9", data={"nombre": "Juan Pérez", "telefono": TEL6}).data.decode()
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
client.post("/marcos/reservar/w0", data={"nombre": "Mora Díaz", "telefono": "54 9 11 2222 3333"})
chequear("la reserva pública entra en la agenda", fila_w("w0")["estado"] == "reservado" and fila_w("w0")["paciente_telefono"] == "5491122223333")
html = client.get("/agenda").data.decode()
chequear("aparece en Reservas nuevas", "Reservas nuevas" in html and "Mora Díaz" in html[html.index("Reservas nuevas"):html.index("<h3>Hoy")])
semana_w = (manana + timedelta(days=1)).astimezone(zona).date().isoformat()
chequear("aparece en el calendario", "Mora Díaz" in client.get(f"/agenda?semana={semana_w}").data.decode())
tenant_w = db.get_tenant_by_id("t1")
scheduler._avisar_reservas(tenant_w, zona)
chequear("avisa al profesional sin nombres", len(envios) == 1 and envios[0][0] == "5491100009999" and "1 reserva nueva" in envios[0][1] and "Mora" not in envios[0][1], envios)
scheduler._avisar_reservas(tenant_w, zona)
chequear("no avisa dos veces", len(envios) == 1)
client.post("/agenda/sesion/w0/vista", data={})
chequear("Visto la saca de la tarjeta", "Reservas nuevas" not in client.get("/agenda").data.decode() and fila_w("w0")["estado"] == "reservado")
client.post("/agenda/agendar/w1", data={"nombre": "Del panel", "telefono": "5491133334444"})
chequear("lo agendado desde el panel no avisa", not fila_w("w1")["reserva_nueva"] and scheduler.db.reservas_sin_avisar("t1") == [])
client.post("/marcos/reservar/w2", data={"nombre": "Ida", "telefono": "5491144440000"})
db.cancelar_turno("t1", "w2")
scheduler._avisar_reservas(tenant_w, zona)
chequear("cancelada antes del aviso no se avisa", len(envios) == 1 and not fila_w("w2")["reserva_nueva"])
client.post("/marcos/reservar/w2", data={"nombre": "Ida", "telefono": "5491144440000"})
client.post("/marcos/reservar/w3", data={"nombre": "Eva", "telefono": "5491155550000"})
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
chequear("psicología no ve tests", "Tests para considerar" not in client.get("/agenda").data.decode())
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
html = client.get("/agenda").data.decode()
chequear("psicopedagogía los ve, también por la nota de sesión", "Tests para considerar" in html and "PROLEC-R" in html and "CARAS-R" in html)
chequear("quedan en la pestaña Trabajo", html.index('data-panel="terapia"') < html.index("Tests para considerar") < html.index('data-panel="sesiones"'))

# --- Cómo se usa ---
reset()
html = client.get("/agenda").data.decode()
chequear("el panel enlaza la guía", 'href="/agenda/ayuda"' in html)
html = client.get("/agenda/ayuda").data.decode()
chequear("psicología ve sus pestañas", "Terapia:" in html and "Crisis:" in html and "Tests para considerar" not in html)
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
TABLAS["tenants"][0]["slug"] = "marcos"
html = client.get("/agenda/ayuda").data.decode()
chequear("psicopedagogía ve las suyas", "Trabajo:" in html and "Escuela:" in html and "Tests para considerar" in html and "/marcos" in html)
for texto in ("Generar horarios", "Reservas nuevas", "Es de esta ficha"):
    chequear(f"la guía nombra lo que existe: {texto}", texto in client.get("/agenda").data.decode() + open("templates/agenda.html", encoding="utf8").read())
with client.session_transaction() as s:
    s.pop("tenant_id")
chequear("la guía pide login", client.get("/agenda/ayuda").status_code == 302)
with client.session_transaction() as s:
    s["tenant_id"] = "t1"

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
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
chequear("Ajustes muestra los plazos", "Plazo para repetir cada test" in html and 'name="meses-WISC-V"' in html and 'placeholder="24"' in html)
client.post("/agenda/plazos-tests", data={"meses-WISC-V": "12", "meses-PROLEC-R": "6", "meses-D2": ""})
chequear("guarda sólo lo que cambió", TABLAS["tenants"][0]["plazos_tests"] == {"WISC-V": 12})
html = client.post("/agenda/plazos-tests", data={"meses-D2": "99"}, follow_redirects=True).data.decode()
chequear("rechaza un plazo absurdo", "entre 0 y 60" in html and TABLAS["tenants"][0]["plazos_tests"] == {"WISC-V": 12})
html = client.post("/agenda/plazos-tests", data={"meses-D2": "doce"}, follow_redirects=True).data.decode()
chequear("rechaza texto", "número de meses" in html and TABLAS["tenants"][0]["plazos_tests"] == {"WISC-V": 12})
html = client.get("/agenda").data.decode()
chequear("muestra el propio y el de fábrica", 'value="12"' in html and "(de fábrica: 24)" in html)
chequear("otro profesional no se entera", not TABLAS["tenants"][1].get("plazos_tests"))
TABLAS["tenants"][0]["rubro"] = "psicología"
chequear("psicología no ve plazos", "Plazo para repetir cada test" not in client.get("/agenda").data.decode())

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
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
chequear("sin textos pide cargarlos", "cargá un texto en «Ajustes»" in html and "Textos para fluidez lectora (0)" in html)
client.post("/agenda/textos-lectura", data={"titulo": "La plaza", "texto": "Hoy fuimos a la plaza con mi abuela y vimos patos."})
client.post("/agenda/textos-lectura", data={"titulo": "", "texto": "algo"})
chequear("agrega textos con título", [t["titulo"] for t in TABLAS["textos_lectura"]] == ["La plaza"])
TABLAS["textos_lectura"].append({"id": "ajeno", "tenant_id": "t2", "titulo": "Ajeno", "texto": "uno dos"})
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
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
chequear("psicología no ve fluidez", "Fluidez lectora" not in client.get("/agenda").data.decode())

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
html = client.get("/agenda").data.decode()
chequear("psicología ofrece cuestionarios", "Generar link" in html and "PHQ-9 (depresión)" in html)
client.post("/agenda/cuestionario", data={"telefono": TELC, "instrumento": "phq9"})
client.post("/agenda/cuestionario", data={"telefono": TELC, "instrumento": "snap"})
chequear("crea el link", len(TABLAS["cuestionarios"]) == 1 and TABLAS["cuestionarios"][0]["instrumento"] == "phq9")
token = TABLAS["cuestionarios"][0]["token"]
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
chequear("aviso en el panel y en la ficha", "Cuestionarios respondidos" in html and "marcó ideas de hacerse daño" in html and "pregunta 9: varios días" in html)
TABLAS["cuestionarios"].append({"id": "viejo", "tenant_id": "t1", "paciente_telefono": TELC, "ficha": "", "instrumento": "phq9", "token": "tv",
                                "vence": "2026-01-01T00:00:00+00:00", "respondido_at": "2026-01-02T12:00:00+00:00", "puntaje": 22,
                                "severidad": "severa", "alerta": False, "respuestas": [2] * 9, "visto": True, "created_at": "00000000"})
html = client.get("/agenda").data.decode()
chequear("evolución contra la anterior", "↓ 5" in html and "grafico-fluidez" in html)
client.post(f"/agenda/cuestionario/{guardado['id']}/visto", data={})
html = client.get("/agenda").data.decode()
chequear("visto saca el aviso", "Cuestionarios respondidos" not in html and "sin ver marcó ideas" not in html)
TABLAS["cuestionarios"].append({"id": "venc", "tenant_id": "t1", "paciente_telefono": TELC, "ficha": "", "instrumento": "gad7", "token": "tvenc",
                                "vence": "2026-01-01T00:00:00+00:00", "created_at": "00000001"})
chequear("link vencido avisa", "venció" in client.get("/cuestionario/tvenc").data.decode())
chequear("vencido no se muestra como pendiente", "/cuestionario/tvenc" not in client.get("/agenda").data.decode())
TABLAS["cuestionarios"].append({"id": "ajeno", "tenant_id": "t2", "paciente_telefono": TELC, "ficha": "", "instrumento": "gad7", "token": "taj",
                                "vence": "2099-01-01T00:00:00+00:00", "created_at": "00000002"})
client.post("/agenda/cuestionario/ajeno/borrar", data={})
client.post("/agenda/cuestionario/ajeno/visto", data={})
chequear("no toca cuestionarios de otro", any(c["id"] == "ajeno" and not c.get("visto") for c in TABLAS["cuestionarios"]))
chequear("ni lo muestra", "/cuestionario/taj" not in client.get("/agenda").data.decode())
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
chequear("se lee en la ficha con su pregunta", "Lee silabeando." in html and "¿Cómo es su lectura en voz alta" in html and "Cuestionario para el docente" in html)
chequear("y aparece como respondido", "Cuestionario para el docente respondido" in html)
chequear("no repite preguntas sin respuesta", "¿Cómo le va en matemática" not in html.split("Lee silabeando.")[1].split("Borrar respuestas")[0])
TABLAS["tenants"][0]["rubro"] = "psicología"
html = client.get("/agenda").data.decode()
chequear("psicología ofrece entrevista de adultos", "Antes de la primera sesión (entrevista inicial)" in html)

# --- Hoja de registro ---
reset()
TABLAS["tenants"][0]["rubro"] = "psicopedagogía"
TELR = "5491117171717"
TABLAS["turnos"].append(turno("h1", "t1", manana, estado="reservado", paciente_nombre="Sol Ríos", paciente_telefono=TELR))
TABLAS["paciente_tests"].append({"id": "hr", "tenant_id": "t1", "paciente_telefono": TELR, "ficha": "", "test": "WISC-V", "fecha": "2026-10-20",
                                 "resultados": [{"escala": "CI Total", "tipo": "compuesto", "puntaje": 95, "nivel": ""}]})
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
en_ventana = 0 <= (hoy_local - vence).days < 30
chequear("tarjeta de tests para repetir", ("Tests que ya se pueden repetir" in html) == en_ventana, (fecha_vieja, vence))
TABLAS["paciente_tests"][0]["fecha"] = _date(hoy_local.year - 1, hoy_local.month, min(hoy_local.day, 28)).isoformat() if hoy_local.day <= 28 else TABLAS["paciente_tests"][0]["fecha"]
TABLAS["paciente_tests"][0]["test"] = "D2"  # 12 meses: vence hoy si el día existe
html = client.get("/agenda").data.decode()
chequear("aparece el día que vence", hoy_local.day > 28 or ("Tests que ya se pueden repetir" in html and "Teresa Gil" in html and "D2" in html))
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
html = client.get("/agenda").data.decode()
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
html = client.post("/agenda/plantillas", data={"modelo": "scared_chico"}, follow_redirects=True).data.decode()
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
html = client.get("/agenda").data.decode()
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
html = client.get("/agenda").data.decode()
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
chequear("psicología también tiene propios", "Cuestionarios propios" in client.get("/agenda").data.decode())

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
html = client.get("/agenda").data.decode()
chequear("compara las versiones lado a lado", "SCARED: versiones lado a lado" in html and "Chico/a" in html and "Padres" in html)
tabla = html.split("SCARED: versiones lado a lado")[1].split("</table>")[0]
fila_social = tabla.split("Ansiedad social")[1].split("</tr>")[0]
fila_gen = tabla.split("Ansiedad generalizada")[1].split("</tr>")[0]
chequear("marca dónde no coinciden", "no coinciden" in fila_social and "<strong>14</strong>" in fila_social and "<strong>7</strong>" in fila_social)
chequear("generalizada: padres 9 sobre el corte, chico 0", "no coinciden" in fila_gen)
fila_esc = tabla.split("Evitación escolar")[1].split("</tr>")[0]
chequear("los dos sobre el corte no se marcan", "no coinciden" not in fila_esc and fila_esc.count("sobre el corte") == 2)
chequear("total con las dos", "<strong>41</strong>" in tabla.split(">Total")[1] and "<strong>22</strong>" in tabla.split(">Total")[1])

if fallos:
    raise SystemExit(f"\nFALLARON {len(fallos)}: {fallos}")
print("\nTODOS LOS TESTS DE SEGUIMIENTO DE SESIONES PASARON")
