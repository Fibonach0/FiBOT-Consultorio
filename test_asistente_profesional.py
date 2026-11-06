"""El asistente del profesional (asistente_profesional.py) y su entrada por el
webhook. Correrlo suelto: `python test_asistente_profesional.py` (monkeypatchea
`db` sin restaurarlo, como los demás)."""
import hashlib
import hmac
import json
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.fake_signature_not_real")
os.environ.setdefault("META_ACCESS_TOKEN", "fake")
os.environ.setdefault("META_APP_SECRET", "supersecret")
os.environ.setdefault("META_VERIFY_TOKEN", "verify123")
os.environ.setdefault("FLASK_SECRET_KEY", "fake")

import scheduler  # noqa: E402

scheduler.iniciar = lambda: None
import app  # noqa: E402
import asistente_profesional as ap  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import plantillas  # noqa: E402
import whatsapp  # noqa: E402

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
config.DOMINIO_PRINCIPAL = "consultorio.fibot.ar"
ANA = {"id": "t1", "nombre": "Lic. Ana", "slug": "ana", "aviso_telefono": "5491155550000", "timezone": "America/Argentina/Buenos_Aires",
       "sedes": [{"nombre": "Palermo"}]}
BETO = {"id": "t2", "nombre": "Lic. Beto", "slug": "beto", "aviso_telefono": "5491155550000", "timezone": "America/Argentina/Buenos_Aires"}
hoy = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)


def turno(i, dia, hora, tenant="t1", **extra):
    return {"id": f"s{i}", "tenant_id": tenant, "estado": "reservado", "inicio": (hoy + timedelta(days=dia, hours=hora)).isoformat(),
            "paciente_nombre": f"Paciente {tenant} {i}", "paciente_telefono": f"54911000000{i}", "ficha": "", **extra}


TURNOS = {"t1": [turno(1, 0, 10, modalidad="virtual"), turno(2, 0, 11, sede="Palermo", pago="pagado", confirmado_at="x"),
                 turno(3, 1, 9, pago_web="esperando"),
                 {"id": "l1", "tenant_id": "t1", "estado": "libre", "inicio": (hoy + timedelta(days=1, hours=12)).isoformat()}],
          "t2": [turno(7, 0, 15, tenant="t2")]}
PASADAS = {"t1": [turno(4, -7, 10, pago="pendiente", valor=25000, paciente_nombre="Juana Pérez"),
                  turno(4, -14, 10, pago="pendiente", valor=25000, paciente_nombre="Juana Pérez"),
                  turno(5, -3, 10, pago="pendiente", valor=None)],
           "t2": [turno(8, -2, 10, tenant="t2", pago="pendiente", valor=9000)]}
db.agenda_semana = lambda tid, desde, hasta: [t for t in TURNOS.get(tid, []) if desde <= datetime.fromisoformat(t["inicio"]) < hasta]
db.turnos_libres = lambda tid, desde, limite=8: [t for t in TURNOS.get(tid, []) if t["estado"] == "libre"][:limite]
db.turno_por_id = lambda tid, x: next((t for t in TURNOS.get(tid, []) + PASADAS.get(tid, []) if t["id"] == x), None)
db.pagos_para_revisar = lambda tid: {"t1": [{"id": "p1", "estado": "comprobante", "monto": 30000, "turnos": ["s3"]},
                                            {"id": "p2", "estado": "aprobado", "a_devolver": 5000, "turnos": ["s1"]}]}.get(tid, [])
db.pagos_esperando = lambda tid: {"t1": [{"id": "p3", "turnos": ["s2"]}]}.get(tid, [])
db.pendientes_de_pago = lambda tid, hasta, limite=50: PASADAS.get(tid, [])
db.mensajes_pacientes = lambda tid, limite=30: {"t1": [
    {"id": "m1", "telefono": "549110000001", "texto": "llego 10 minutos tarde", "leido": False, "creado": hoy.isoformat()},
    {"id": "m2", "telefono": "549110000002", "texto": "viejo", "leido": True, "creado": hoy.isoformat()}]}.get(tid, [])
db.fichas_del_telefono = lambda tid, tel: [t for t in TURNOS.get(tid, []) if t.get("paciente_telefono") == tel]
db.turnos_de_paciente_por_nombre = lambda tid, nombre: [t for t in TURNOS.get(tid, []) + PASADAS.get(tid, []) if t.get("paciente_nombre")]
VINCULADOS = []
db.vincular_aviso = lambda tid, numero: VINCULADOS.append((tid, numero))


def vinculada(t):
    return dict(t, aviso_verificado=t["aviso_telefono"])


# --- el candado: sin vincular no hay datos ---
r = ap.responder(ANA, "hoy")
assert r == ap.sin_vincular() and "Paciente" not in r and "Lic. Ana" not in r, r
assert ap.responder_a([ANA], "pagos") == ap.sin_vincular()
print("sin vincular: a un pedido sólo le dice cómo vincular, ni el nombre de la cuenta OK")
codigo = ap.codigo_vinculo(ANA)
assert codigo and len(codigo) == 6 and codigo.isdigit() and codigo != ap.codigo_vinculo(BETO), codigo
assert ap.codigo_vinculo(dict(ANA, aviso_telefono="5491166660000")) != codigo, "otro número, otro código"
malo = f"{(int(codigo) + 1) % 1_000_000:06d}"
assert "no corresponde" in ap.responder_a([ANA], f"VINCULAR {malo}", "5491155550000") and not VINCULADOS
assert "no corresponde" in ap.responder_a([ANA], f"vincular {codigo}", "5491199990000") and not VINCULADOS, \
    "el código sirve sólo desde el número cargado"
r = ap.responder_a([ANA, BETO], f"Vincular {codigo}", "5491155550000")
assert VINCULADOS == [("t1", "5491155550000")] and "Lic. Ana" in r and "Lic. Beto" not in r, (VINCULADOS, r)
print("vincular: código del panel, sólo desde ese número y sólo para esa cuenta OK")
assert ap.vinculado(vinculada(ANA)) and not ap.vinculado(dict(vinculada(ANA), aviso_telefono="5491166660000")), \
    "si cambia el número en el panel, queda desvinculado"
print("cambiar el número desvincula solo OK")

# --- vinculada: todos los datos ---
A = vinculada(ANA)
r = ap.responder(A, "Hoy")
assert r.startswith("Hoy: 2 sesiones.") and "10:00 Paciente t1 1 (virtual)" in r and "11:00 Paciente t1 2 (Palermo, pagada, confirmó)" in r, r
print("hoy: con nombre, modalidad, consultorio, pago y confirmación OK")
r = ap.responder(A, "mañana")
assert "Mañana: 1 sesión." in r and "Paciente t1 3 (pago web sin confirmar)" in r and "Horarios libres: 1 (12:00)" in r, r
print("mañana: con el pago web pendiente y los libres OK")
r = ap.responder(A, "pagos")
assert "Comprobante de Paciente t1 3" in r and "$ 30.000" in r and "Devolverle $ 5.000 a Paciente t1 1" in r \
    and "Paciente t1 2" in r and "todavía no pagó" in r, r
print("pagos: de quién es cada comprobante, devolución y reserva esperando OK")
r = ap.responder(A, "¿Quién me debe?")
assert "Te deben $ 50.000 de 3 sesiones pasadas" in r and "Juana Pérez: $ 50.000 (2 sesiones)" in r \
    and "Paciente t1 5: $ 0 (1 sesión, 1 sin valor)" in r and "+549110000004" in r, r
print("deudas: quién, cuánto y su teléfono OK")
r = ap.responder(A, "mensajes")
assert "Mensajes sin leer (1)" in r and "Paciente t1 1" in r and "llego 10 minutos tarde" in r and "viejo" not in r, r
print("mensajes: quién escribió y qué OK")
r = ap.responder(A, "paciente juana perez")
assert "*Juana Pérez*" in r and "+549110000004" in r and "Debe $ 50.000 de 2 sesiones" in r and "Próximas: ninguna" in r, r
assert "No encontré" in ap.responder(A, "buscar Zoe")
print("paciente: teléfono, próximas, última y deuda OK")
assert "https://consultorio.fibot.ar/ana" in ap.responder(A, "mi link")
assert "*hoy*" in ap.responder(A, "ayuda") and "*paciente Juan*" in ap.responder(A, "hola")
assert "libres" in ap.responder(A, "libres").lower()
print("link, ayuda y libres OK")
for paciente in ("turno", "2 Juan", "cancelar", "mi turno", "llego tarde", "quiero un turno"):
    assert ap.responder(A, paciente) is None and ap.responder_a([A], paciente) is None, paciente
print("lo que no es un pedido del profesional sigue como paciente (puede probar su propio bot) OK")

r = ap.responder_a([A], "hoy")
assert r.interactivo and r.interactivo["tipo"] == "botones" and [b for _, b in r.interactivo["botones"]] == ["Hoy", "Mañana", "Pagos"], r
for ident, _ in ap.BOTONES:
    assert ap.responder_a([A], ident[len("prof:"):]) not in (None, ap.sin_vincular()), ident
TURNOS["t1"] += [turno(20 + i, 0, 8, paciente_nombre=f"Paciente con un nombre bastante largo {i}") for i in range(30)]
largo = ap.responder_a([A], "hoy")
assert len(largo) > 1024 and not getattr(largo, "interactivo", None), "más de 1024 letras sale como texto, sin botones"
del TURNOS["t1"][-30:]
print("botones Hoy / Mañana / Pagos debajo de cada respuesta (y texto solo si es largo) OK")
for boton, que in plantillas.QUE_HACE_PROFESIONAL.items():
    assert ap.responder_a([A], boton) not in (None, ap.sin_vincular()), boton
assert set(plantillas.botones_de_todas("profesional")) == set(plantillas.QUE_HACE_PROFESIONAL)
assert not set(plantillas.botones_de_todas()) & set(plantillas.QUE_HACE_PROFESIONAL), "los botones del profesional no van al bot de pacientes"
assert "Ver hoy" in ap.responder_a([A], "Ver hoy") or ap.responder_a([A], "Ver hoy").startswith("Hoy:")
print("botones del resumen de la mañana (Ver hoy, Pagos, Mensajes) los contesta el asistente OK")

# --- dos cuentas con el mismo número: nunca se mezclan ---
dos = ap.responder_a([A, BETO], "hoy")
assert "Paciente t1" in dos and "Paciente t2" not in dos and "*Lic. Beto*" not in dos, "Beto sin vincular no contesta"
dos = ap.responder_a([A, vinculada(BETO)], "hoy")
bloque_ana, bloque_beto = dos.split("———")
assert "*Lic. Ana*" in bloque_ana and "Paciente t1" in bloque_ana and "Paciente t2" not in bloque_ana, dos
assert "*Lic. Beto*" in bloque_beto and "Paciente t2 7" in bloque_beto and "Paciente t1" not in bloque_beto, dos
dos = ap.responder_a([A, vinculada(BETO)], "deudas")
assert dos.split("———")[1].count("Paciente t1") == 0 and "$ 9.000" in dos.split("———")[1], dos
print("mismo WhatsApp en dos cuentas: cada una con sus datos, sólo las vinculadas OK")

# --- por el webhook ---
db.get_tenant_by_phone_number_id = lambda pid: None
db.marcar_evento = lambda clave, detalle=None: None
CUENTAS = [ANA, {"id": "t9", "nombre": "Otro", "aviso_telefono": "5491177770000"}]
db.tenants_activos = lambda: CUENTAS
db.huecos_pendientes = lambda tid: []
ENVIADOS, AL_PACIENTE = [], []
whatsapp.marcar_leido = lambda pid, mid: True
whatsapp.enviar_texto = lambda pid, to, texto: ENVIADOS.append((pid, to, texto)) or True
whatsapp.enviar_respuesta = lambda pid, to, r: ENVIADOS.append((pid, to, r)) or True
app._contestar = lambda tenant, tel, texto: AL_PACIENTE.append((tel, texto))
app._tenant_del_mensaje = lambda tdn, tel, pid, texto=None: (ANA, False)
app.conversation.responder = lambda tenant, tel, texto: AL_PACIENTE.append((tel, texto)) or "ok"
client = app.app.test_client()


def postear(tel, texto=None, boton=None):
    msg = ({"id": "w1", "type": "text", "from": tel, "text": {"body": texto}} if boton is None else
           {"id": "w1", "type": "interactive", "from": tel, "interactive": {"type": "button_reply", "button_reply": boton}})
    cuerpo = json.dumps({"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "PLAT"}, "messages": [msg]}}]}]}).encode()
    f = hmac.new(b"supersecret", cuerpo, hashlib.sha256).hexdigest()
    return client.post("/webhook", data=cuerpo, content_type="application/json", headers={"X-Hub-Signature-256": f"sha256={f}"})


app.config.PLATAFORMA_PHONE_NUMBER_ID = "PLAT"
assert postear("5491155550000", "hoy").status_code == 200
assert ENVIADOS[-1][2] == ap.sin_vincular() and not AL_PACIENTE, (ENVIADOS, AL_PACIENTE)
print("webhook: el profesional sin vincular pide «hoy» y sólo le dice cómo vincular OK")
VINCULADOS.clear()
postear("5491155550000", f"VINCULAR {codigo}")
assert VINCULADOS == [("t1", "5491155550000")] and "quedó vinculado" in ENVIADOS[-1][2], ENVIADOS[-1]
CUENTAS[0] = A
print("webhook: VINCULAR con el código del panel OK")
postear("5491155550000", "hoy")
assert ENVIADOS[-1][1] == "5491155550000" and "Paciente t1 1" in ENVIADOS[-1][2] and ENVIADOS[-1][2].interactivo, ENVIADOS[-1]
postear("5491155550000", boton={"id": "prof:pagos", "title": "Pagos"})
assert ENVIADOS[-1][2].startswith("Pagos:") and "Comprobante de Paciente t1 3" in ENVIADOS[-1][2], ENVIADOS[-1]
print("webhook: vinculado, «hoy» con nombres y el botón «Pagos» OK")
postear("5491155550000", "turno")
assert AL_PACIENTE == [("5491155550000", "turno")], AL_PACIENTE
print("webhook: si escribe algo de paciente, sigue como paciente OK")
ENVIADOS.clear()
postear("5491122220000", "hoy")
assert not any("Paciente t1" in str(e[2]) for e in ENVIADOS) and AL_PACIENTE[-1] == ("5491122220000", "hoy")
postear("5491122220000", f"VINCULAR {codigo}")
assert len(VINCULADOS) == 1 and AL_PACIENTE[-1][0] == "5491122220000", "un paciente no puede vincularse"
postear("5491122220000", boton={"id": "prof:hoy", "title": "Hoy"})
assert not any("Paciente t1" in str(e[2]) for e in ENVIADOS) and AL_PACIENTE[-1] == ("5491122220000", "Hoy"), (ENVIADOS, AL_PACIENTE)
print("webhook: un paciente que escribe «hoy», manda un código o toca «prof:hoy» no ve ninguna agenda OK")

# --- 1. avisos con nombre sólo con el número vinculado ---
import scheduler as _sch  # noqa: E402
_AV = []
_sch.whatsapp.numero_de = lambda t: "PLAT"
_sch.config.AVISO_TEMPLATE = "aviso_profesional"
_sch.whatsapp.enviar_plantilla = lambda n, to, tpl, lang, params, **k: _AV.append((to, tpl, params)) or True
_sch.avisar_profesional(ANA, "un paciente canceló por WhatsApp el turno del jue 10:00", paciente="Juana Pérez")
_sch.avisar_profesional(A, "un paciente canceló por WhatsApp el turno del jue 10:00", paciente="Juana Pérez")
assert "un paciente canceló" in _AV[0][2][0] and "Juana" not in _AV[0][2][0], _AV
assert _AV[1][2][0].startswith("Juana Pérez canceló"), _AV
_sch.avisar_profesional(A, "un paciente te escribió", con_nombre="Juana te escribió:\n«llego\ntarde»")
assert "\n" not in _AV[-1][2][0] and "«llego tarde»" in _AV[-1][2][0], "en una línea (las plantillas no aceptan saltos)"
print("avisos: con nombre sólo si está vinculado, y en una línea OK")
_RES = [turno(30, 2, 10, paciente_nombre="Juana Pérez"), turno(31, 3, 11, paciente_nombre="Tomás Ruiz")]
_sch.db.reservas_sin_avisar = lambda tid: list(_RES)
_sch.db.marcar_reserva_avisada = lambda tid, x: None
_AV.clear()
_sch._avisar_reservas(ANA, TZ)
_sch._avisar_reservas(A, TZ)
assert "2 reservas nuevas" in _AV[0][2][0] and "Juana" not in _AV[0][2][0], _AV
assert "Juana Pérez reservó para el" in _AV[1][2][0] and "Tomás Ruiz reservó" in _AV[1][2][0], _AV
print("reservas nuevas: quién reservó y cuándo, con el número vinculado OK")

# --- 4. resumen de la noche ---
class _Noche(datetime):
    hora = 21

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz).replace(hour=cls.hora, minute=0)


_sch.datetime = _Noche
_NOCHE = []
_sch.db.marcar_resumen_noche = lambda tid, dia: _NOCHE.append(dia)
_AV.clear()
_sch._resumen_noche(ANA, TZ)
assert not _AV and not _NOCHE, "sin vincular no sale"
_sch._resumen_noche(dict(A, resumen_noche=False), TZ)
assert not _AV, "si lo apagó no sale"
_Noche.hora = 15
_sch._resumen_noche(A, TZ)
assert not _AV, "antes de las 20 no sale"
_Noche.hora = 21
_sch._resumen_noche(A, TZ)
assert len(_AV) == 1 and "mañana" in _AV[0][2][0] and "1 sesión" in _AV[0][2][0] and "09:00 Paciente t1 3" in _AV[0][2][0], _AV
_sch._resumen_noche(dict(A, ultimo_resumen_noche=_NOCHE[-1]), TZ)
assert len(_AV) == 1, "una vez por noche"
_sch.datetime = datetime
print("resumen de la noche: a las 20, con nombres, una vez, sólo vinculado y apagable OK")

# --- 2 y 3. acciones con confirmación ---
PEND = {}
db.guardar_accion_profesional = lambda tel, tid, accion: PEND.__setitem__(tel, {"telefono": tel, "tenant_id": tid, "accion": accion,
                                                                             "creado": datetime.now(timezone.utc).isoformat()})
db.accion_profesional = lambda tel: PEND.get(tel)
db.borrar_accion_profesional = lambda tel: PEND.pop(tel, None)
db.registrar_actividad = lambda *a, **k: None
HECHO = []
import pagos as _pagos  # noqa: E402
_pagos.aprobar = lambda t, p: HECHO.append(("aprobar", t["id"], p["id"])) or "aprobado"
_pagos.rechazar = lambda t, p, motivo=None: HECHO.append(("rechazar", t["id"], p["id"]))
db.pago_de = lambda tid, pid: next((p for p in db.pagos_para_revisar(tid) if p["id"] == pid), None)
db.agregar_bloqueo = lambda tid, desde, hasta, motivo: HECHO.append(("bloqueo", tid, desde, hasta))
db.guardar_ajustes = lambda tid, campos: HECHO.append(("ajustes", tid, campos))
TEL = "5491155550000"

r = ap.responder_a([A], "aceptar comprobante de paciente", TEL)
assert "¿Acepto el comprobante de Paciente t1 3" in r and "$ 30.000" in r and r.interactivo["botones"] == ap.BOTONES_CONFIRMAR, r
assert not HECHO, "no hace nada sin el «sí»"
r = ap.responder_a([A], "sí", TEL)
assert HECHO == [("aprobar", "t1", "p1")] and "aceptado" in r and not PEND, (HECHO, r)
print("aceptar comprobante: pregunta, y con «sí» lo acepta OK")
HECHO.clear()
ap.responder_a([A], "rechazar comprobante", TEL)
assert "Listo, no hice nada" in ap.responder_a([A], "no", TEL) and not HECHO and not PEND
print("«no» cancela sin hacer nada OK")
ap.responder_a([A], "rechazar el comprobante", TEL)
PEND[ap._diez(TEL)]["creado"] = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
assert "más de 10 minutos" in ap.responder_a([A], "si", TEL) and not HECHO
print("una acción vieja (más de 10 minutos) no se hace OK")
assert ap.responder_a([A], "si", TEL) is None, "«sí» sin nada pendiente sigue como paciente"
assert "No tenés comprobantes para validar de «zoe»" in ap.responder_a([A], "aceptar comprobante de zoe", TEL)

r = ap.responder_a([A], "bloquear mañana", TEL)
manana = (datetime.now(TZ) + timedelta(days=1)).date()
assert f"¿Bloqueo {ap._dia_corto(manana)}?" in r and "tenés 1 sesión" in r and "Paciente t1 3" in r, r
ap.responder_a([A], "dale", TEL)
assert HECHO[-1][0] == "bloqueo" and HECHO[-1][2].astimezone(TZ).date() == manana \
    and HECHO[-1][3].astimezone(TZ).date() == manana + timedelta(days=1), HECHO
r = ap.responder_a([A], "bloquear del 20/12 al 5/1", TEL)
assert "¿Bloqueo del " in r and "20/12" in r and "5/01" in r, r
ap.responder_a([A], "no", TEL)
assert "No entendí qué días" in ap.responder_a([A], "bloquear cuando pueda", TEL)
print("bloquear: mañana, el viernes o un rango; avisa las sesiones que hay esos días OK")
HECHO.clear()
r = ap.responder_a([A], "pausar hasta el lunes", TEL)
assert "¿Pauso tu página de turnos hasta el lunes" in r, r
ap.responder_a([A], "si", TEL)
assert HECHO[-1][0] == "ajustes" and HECHO[-1][2]["pausado"] is True and date.fromisoformat(HECHO[-1][2]["pausado_hasta"]).weekday() == 0, HECHO
assert "ya está tomando turnos" in ap.responder_a([A], "reactivar", TEL)
ap.responder_a([dict(A, pausado=True)], "reactivar", TEL)
ap.responder_a([dict(A, pausado=True)], "si", TEL)
assert HECHO[-1][2] == {"pausado": False, "pausado_hasta": None}, HECHO
print("pausar hasta el lunes y reactivar, con confirmación OK")

AL_TEL = []
whatsapp.enviar_texto = lambda pid, to, texto: AL_TEL.append((to, texto)) or True
whatsapp.enviar_plantilla = lambda pid, to, tpl, lang, params, **k: AL_TEL.append((to, tpl, params)) or True
whatsapp.numero_de = lambda t: "PLAT"
r = ap.responder_a([A], "Avisale a Juana que llego 10 minutos tarde, perdón", TEL)
assert "¿Le mando a Juana Pérez (+549110000004)" in r and "«Lic. Ana: Llego 10 minutos tarde, perdón»" in r and not AL_TEL, r
r = ap.responder_a([A], "si", TEL)
assert AL_TEL == [("549110000004", "*Lic. Ana*: Llego 10 minutos tarde, perdón")] and "24 h" in r, (AL_TEL, r)
config.MENSAJE_PROFESIONAL_TEMPLATE = "mensaje_profesional"
ap.responder_a([A], "decile a juana perez: hoy atiendo virtual", TEL)
ap.responder_a([A], "ok", TEL)
assert AL_TEL[-1] == ("549110000004", "mensaje_profesional", ["Lic. Ana", "Hoy atiendo virtual"]), AL_TEL
config.MENSAJE_PROFESIONAL_TEMPLATE = ""
assert "más de un paciente" in ap.responder_a([A], "avisale a paciente que llego tarde", TEL)
assert "No encontré" in ap.responder_a([A], "avisale a Zoe que llego tarde", TEL)
print("avisale a Juana que…: confirma el destinatario y el texto; con plantilla si está OK")

assert "varias cuentas" in ap.responder_a([A, vinculada(BETO)], "bloquear mañana", TEL) and not PEND
assert ap.responder_a([ANA], "bloquear mañana", TEL) == ap.sin_vincular() and not PEND
print("acciones: con el número en varias cuentas o sin vincular, no se hacen OK")
ap.responder_a([A], "pausar", TEL)
assert "ya no está vinculado" in ap.responder_a([dict(A, aviso_telefono="5491166660000", aviso_verificado="5491166660000")], "si", TEL)
assert ("ajustes", "t1", {"pausado": True, "pausado_hasta": None}) not in HECHO
print("acciones: si el número dejó de estar vinculado a esa cuenta antes del «sí», no se hace OK")

# por el webhook: el botón «Sí, hacelo» y un paciente que dice «sí»
whatsapp.enviar_respuesta = lambda pid, to, r: ENVIADOS.append((pid, to, r)) or True
HECHO.clear()
postear(TEL, "aceptar comprobante")
assert ENVIADOS[-1][2].interactivo["botones"] == ap.BOTONES_CONFIRMAR
postear(TEL, boton={"id": "prof:si", "title": "Sí, hacelo"})
assert HECHO == [("aprobar", "t1", "p1")], HECHO
AL_PACIENTE.clear()
postear("5491122220000", boton={"id": "prof:si", "title": "Sí, hacelo"})
postear("5491122220000", "aceptar comprobante")
assert len(HECHO) == 1 and not PEND and len(AL_PACIENTE) == 2, (HECHO, PEND, AL_PACIENTE)
print("webhook: el botón «Sí, hacelo» del profesional ejecuta; un paciente no puede pedir ni confirmar acciones OK")
print("\nTODOS LOS TESTS DEL ASISTENTE DEL PROFESIONAL PASARON")
