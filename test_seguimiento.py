"""Tests del seguimiento de sesiones: asistencia, pago y reprogramar.

Correrlo SUELTO (`python test_seguimiento.py`), igual que los otros test_*.py
de este repo: reemplaza `db._client` sin restaurarlo.

A diferencia de los otros tests, acá NO se pisan las funciones de `db`: se
pisa el cliente de Supabase por uno en memoria, para que corra la lógica real
de db.py — que es justo donde están las cosas que importan (el orden de
reprogramar, que liberar un turno limpie el pago, que nada cruce de tenant).
"""
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
            filas.append({"id": f"{self.tabla}-{len(filas) + 1}", **self.payload})
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
            "paciente_telefono": None, "paciente_nombre": None, "ficha": "", "revisar_ficha": False, "recordatorio_enviado": False,
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

if fallos:
    raise SystemExit(f"\nFALLARON {len(fallos)}: {fallos}")
print("\nTODOS LOS TESTS DE SEGUIMIENTO DE SESIONES PASARON")
