"""App local con la base en memoria de test_seguimiento.py y datos INVENTADOS,
para grabar los GIF de «Cómo se usa» (escenas.py). Sólo en 127.0.0.1; usuario
`guia` con una clave de prueba local. Nada sale a Supabase, Meta ni Mercado Pago.

    python scripts/guia/servidor.py &          # deja la app en :8770
    python scripts/guia/escenas.py static/ayuda   # graba todas (o: ... static/ayuda ficha cobros)

Hace falta Chrome (google-chrome) y `pip install websocket-client pillow`
(no van en requirements.txt: son sólo para esto).
"""
import os, sys, random
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.getcwd())
src = open("test_seguimiento.py", encoding="utf8").read()
exec(compile(src[:src.index('print("Panel")')], "base", "exec"))
import app as A, bcrypt, scheduler as S
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
reset()
random.seed(7)
for t in ("sesiones", "llaves_acceso", "admin_llaves", "actividad", "login_intentos", "qr_logins", "enlaces_clave", "acciones_profesional"):
    TABLAS[t] = []
tz = ZoneInfo("America/Argentina/Buenos_Aires")
T = TABLAS["tenants"][0]
T.update({"nombre": "Lic. Laura Demo", "slug": "laura-demo", "panel_user": "guia", "activo": True, "rubro": "psicología",
          "panel_pass_hash": bcrypt.hashpw(b"clave-local-guia", bcrypt.gensalt(4)).decode(),
          "titulo": "Psicóloga clínica · MN 00000", "ubicacion": "Palermo y online",
          "sobre_mi": "Acompaño a adultos y adolescentes. Trabajo con ansiedad, duelos y vínculos.",
          "enfoques": ["Ansiedad", "Duelos", "Vínculos"], "descripcion": "Psicóloga clínica",
          "modalidades_publicas": "ambas", "aviso_telefono": "5491100009999",
          "sedes": [{"nombre": "Palermo", "direccion": "Av. Inventada 1234"}],
          "tipos_turno": [{"nombre": "Sesión", "minutos": 45, "precio": 30000}, {"nombre": "Primera entrevista", "minutos": 60, "precio": 35000}],
          "duracion_turno_minutos": 45, "descanso_minutos": 15, "precio_sesion": 30000,
          "horario_semanal": [{"dia": d, "desde": "09:00", "hasta": "13:00", "sede": "Palermo"} for d in (0, 2, 4)]
                             + [{"dia": d, "desde": "15:00", "hasta": "19:00", "modalidad": "virtual"} for d in (1, 3)],
          "horario_semanas": 4, "cobro_web": "transferencia", "transferencia_datos": "Alias: laura.demo.psico",
          "link_videollamada": "https://meet.example.com/laura-demo", "cancelar_hasta_horas": 24, "matricula": "MN 00000"})
TABLAS["tenants"] = [T]
TABLAS["turnos"] = [x for x in TABLAS["turnos"] if x["tenant_id"] == "t1" and False]
A.db.get_tenant_by_id = lambda tid: next((dict(t) for t in TABLAS["tenants"] if t["id"] == tid), None)
A.db.get_tenant_by_panel_user = lambda u: next((dict(t) for t in TABLAS["tenants"] if t.get("panel_user") == u), None)
# libres de las próximas semanas según el horario, y unas semanas para atrás (para el historial)
ahora = datetime.now(tz)
lunes = (ahora - timedelta(days=ahora.weekday() + 21)).replace(hour=0, minute=0, second=0, microsecond=0)
inicios_m, inicios_v = [], []
for d in range(0, 49):
    dia = lunes + timedelta(days=d)
    if dia.weekday() in (0, 2, 4):
        inicios_m += [dia.replace(hour=h) for h in (9, 10, 11, 12)]
    if dia.weekday() in (1, 3):
        inicios_v += [dia.replace(hour=h) for h in (15, 16, 17, 18)]
A.db.crear_turnos_libres("t1", inicios_m, 45, sede="Palermo")
A.db.crear_turnos_libres("t1", inicios_v, 45, solo_modalidad="virtual")
NOMBRES = ["Martina Ruiz", "Tomás Herrera", "Sofía Benítez", "Lucas Ferreyra", "Valentina Sosa", "Joaquín Paz",
           "Camila Ortega", "Mateo Vidal", "Julieta Ríos", "Bruno Acosta", "Lola Medina", "Facundo Ibarra"]
pacientes = [(n, f"54911000{10 + i:02d}{i:03d}") for i, n in enumerate(NOMBRES)]
libres = sorted([x for x in TABLAS["turnos"] if x["estado"] == "libre"], key=lambda x: x["inicio"])
for k, x in enumerate(libres):
    if random.random() < 0.55:
        n, tel = pacientes[k % len(pacientes)]
        A.db.reservar_turno("t1", x["id"], tel, n)
        inicio = datetime.fromisoformat(x["inicio"])
        x["valor"] = 30000
        x["modalidad"] = "virtual" if x.get("solo_modalidad") == "virtual" else "presencial"
        if inicio < ahora:
            x["asistencia"] = random.choice(["vino", "vino", "vino", "no_vino"])
            x["pago"] = random.choice(["pagado", "pagado", "pendiente"])
            if x["asistencia"] == "vino" and random.random() < 0.5:
                x["nota"] = "Trabajamos la semana en el trabajo. Retomar el tema de la mudanza."
        x["reserva_nueva"] = inicio > ahora and random.random() < 0.08
# fichas
for n, tel in pacientes[:6]:
    A.db.guardar_paciente("t1", tel, {"motivo": "Ansiedad en el trabajo", "objetivos": "Herramientas para el día a día",
                                       "como_viene": "Mejor que al principio", "notas": "Le sirve escribir antes de la sesión.",
                                       "obra_social": "Particular"})
for dias, texto in ((14, "Primera entrevista con la mamá. Viene por dificultades en la lectura."),
                    (7, "Lectura de sílabas con tarjetas. Se cansa a los 20 minutos."),
                    (0, "Juego de memoria y lectura en voz alta.\nMucho mejor: leyó el cuento entero.")):
    A.db.agregar_bitacora("t1", pacientes[0][1], (ahora - timedelta(days=dias)).date().isoformat(), texto,
                          "Traer el cuaderno de lectura" if dias == 7 else None)
A.db.agregar_fecha("t1", pacientes[0][1], "Cumpleaños", min(28, ahora.day + 5), ahora.month, None, "Lo vive con alegría")
# mensajes y pagos para revisar
TABLAS["mensajes_pacientes"] += [
    {"id": "m1", "tenant_id": "t1", "telefono": pacientes[2][1], "tipo": "texto", "texto": "Hola, ¿puedo llevar a mi hermana a la próxima?",
     "creado": (ahora - timedelta(hours=2)).isoformat(), "leido": False},
    {"id": "m2", "tenant_id": "t1", "telefono": pacientes[4][1], "tipo": "audio", "texto": None,
     "creado": (ahora - timedelta(minutes=30)).isoformat(), "leido": False}]
prox = next(x for x in sorted(TABLAS["turnos"], key=lambda x: x["inicio"]) if x["estado"] == "libre" and datetime.fromisoformat(x["inicio"]) > ahora + timedelta(days=2))
A.db.reservar_turno("t1", prox["id"], "5491100077777", "Renata Gil")
import base64
png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
TABLAS["pagos"].append({"id": "pg1", "tenant_id": "t1", "token": "tok-guia", "turnos": [prox["id"]], "monto": 30000,
                        "estado": "comprobante", "metodo": "transferencia", "paciente_nombre": "Renata Gil",
                        "sesiones": [prox["inicio"]], "creado": (ahora - timedelta(hours=1)).isoformat(),
                        "comprobante_at": (ahora - timedelta(hours=1)).isoformat(),
                        "vence": (ahora + timedelta(hours=1)).isoformat(),
                        "comprobante": "data:image/png;base64," + base64.b64encode(png).decode()})
prox.update(pago_web="comprobante", pago_id="pg1", valor=30000)
TABLAS["lista_espera"].append({"id": "e1", "tenant_id": "t1", "nombre": "Pilar Luna", "telefono": "5491100066666",
                               "created_at": (ahora - timedelta(days=3)).isoformat(), "origen": "web", "notas": "Prefiere mañanas"})
print("datos listos:", sum(1 for x in TABLAS["turnos"] if x["estado"] == "reservado"), "sesiones", flush=True)
A.app.run(host="127.0.0.1", port=8770)
