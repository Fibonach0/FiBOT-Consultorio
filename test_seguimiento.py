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
                and _norm(r["fin"]) > _norm(t["inicio"]) for r in reservados)]
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
                        "reprogramado_desde": None, "valor": None, "frecuencia": None, **nueva}
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
            "paciente_telefono": None, "paciente_nombre": None, "recordatorio_enviado": False,
            "asistencia": None, "pago": "pendiente", "reprogramado_desde": None,
            "valor": None, "frecuencia": None}
    return {**base, **extra}


def reset():
    TABLAS["tenants"] = [dict(TENANT), dict(OTRO)]
    TABLAS["pacientes"] = []
    TABLAS["paciente_personas"] = []
    TABLAS["paciente_fechas"] = []
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
chequear("el panel muestra los cobros", "Cobros de" in html and "Quién debe" in html)
html = client.post("/agenda/paciente/recordar-pago", data={"telefono": "1"}, follow_redirects=True).data.decode()
chequear("recuerda el pago con el monto de la base", len(enviados) == 1 and enviados[0][1] == "1" and "$ 2.000" in enviados[0][2], enviados)
html = client.post("/agenda/paciente/recordar-pago", data={"telefono": "1"}, follow_redirects=True).data.decode()
chequear("no lo manda dos veces el mismo día", len(enviados) == 1 and "ya se le recordó" in html)
html = client.post("/agenda/paciente/recordar-pago", data={"telefono": "ajeno"}, follow_redirects=True).data.decode()
chequear("sin deuda no manda nada", len(enviados) == 1 and "no tiene deuda" in html)

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

if fallos:
    raise SystemExit(f"\nFALLARON {len(fallos)}: {fallos}")
print("\nTODOS LOS TESTS DE SEGUIMIENTO DE SESIONES PASARON")
