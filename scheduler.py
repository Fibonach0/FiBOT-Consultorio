"""Recordatorios automáticos. Corre como un hilo de fondo dentro del mismo
proceso — por eso Procfile/nixpacks.toml fuerzan --workers 1: con dos workers
este hilo correría dos veces y mandaría el recordatorio duplicado."""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import db
import rubros
import tests_sugeridos
from fechas import proxima_vez, sesiones_que_quedan
import whatsapp

logger = logging.getLogger("scheduler")

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
DIAS_AVISO = 7
HORA_AVISO = 8
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _fmt(dt_iso: str, tz: ZoneInfo) -> str:
    dt = datetime.fromisoformat(dt_iso).astimezone(tz)
    return f"{DIAS[dt.weekday()]} {dt.day} de {MESES[dt.month - 1]}, {dt.hour:02d}:{dt.minute:02d}"


def cuando_de(turno: dict, tenant: dict, tz: ZoneInfo) -> str:
    """Va dentro del parámetro {{2}} de las plantillas ya aprobadas: así las
    virtuales llevan el link sin tener que aprobar plantillas nuevas."""
    cuando = _fmt(turno["inicio"], tz)
    if turno.get("tipo"):
        # «Primera entrevista de 100 min»: para que el paciente reserve el tiempo.
        minutos = int((datetime.fromisoformat(turno["fin"]) - datetime.fromisoformat(turno["inicio"])).total_seconds() // 60) \
            if turno.get("fin") else None
        cuando += f" ({turno['tipo']}" + (f" de {minutos} min" if minutos else "") + ")"
    if turno.get("sede") and turno.get("modalidad") != "virtual":
        direccion = next((x.get("direccion") for x in (tenant.get("sedes") or []) if x.get("nombre") == turno["sede"]), None)
        cuando += f", en {turno['sede']}" + (f" ({direccion})" if direccion else "")
    if turno.get("modalidad") == "virtual" and tenant.get("link_videollamada"):
        return f"{cuando}, por videollamada: {tenant['link_videollamada']}"
    return cuando


def _partes_del_resumen(tenant: dict, hoy) -> list[str]:
    """Sólo cuentas: el detalle queda en el panel. Por WhatsApp no sale ningún
    nombre ni dato de un paciente."""
    datos = db.datos_de_pacientes(tenant["id"])
    cerrado = lambda clave: datos.get(clave, {}).get("estado", "activo") != "activo"
    fechas = sum(
        (proxima_vez(f["mes"], f["dia"], hoy) - hoy).days <= DIAS_AVISO
        for f in db.fechas_de_pacientes(tenant["id"]) if not cerrado(db.clave_de(f))
    )
    partes = [f"{fechas} fecha{'s' if fechas > 1 else ''} importante{'s' if fechas > 1 else ''} en la semana"] if fechas else []
    por_paciente: dict[str, list[dict]] = {}
    sesiones = db.sesiones_reservadas(tenant["id"])
    revisar = sum(bool(t.get("revisar_ficha")) for t in sesiones)
    if revisar:
        partes.append(f"{revisar} sesi{'ones' if revisar > 1 else 'ón'} para revisar de quién {'son' if revisar > 1 else 'es'}")
    for t in sesiones:
        por_paciente.setdefault(db.clave_de(t), []).append(t)
    ahora = datetime.now(timezone.utc)
    renovar = sum(sesiones_que_quedan(s, ahora) is not None for clave, s in por_paciente.items() if not cerrado(clave))
    if renovar:
        partes.append(f"{renovar} paciente{'s' if renovar > 1 else ''} por renovar")
    partes += _bloqueos_con_sesiones(tenant, hoy, sesiones)
    if tenant.get("ultimo_bloqueo") and \
            datetime.fromisoformat(tenant["ultimo_bloqueo"]).timestamp() > datetime.now(timezone.utc).timestamp() - 24 * 3600:
        partes.append("tu cuenta se bloqueó por claves equivocadas en las últimas 24 horas: si no fuiste vos, cambiá la clave")
    # Entregas abiertas desde el resumen anterior (sale una vez por día).
    hace_un_dia = datetime.now(timezone.utc).timestamp() - 24 * 3600
    abiertas = sum(1 for e in db.entregas(tenant["id"])
                   if e.get("abierta_at") and datetime.fromisoformat(e["abierta_at"]).timestamp() > hace_un_dia)
    if abiertas:
        partes.append(f"{abiertas} ficha{'s' if abiertas > 1 else ''} entregada{'s' if abiertas > 1 else ''} "
                      f"se abri{'eron' if abiertas > 1 else 'ó'} en las últimas 24 horas")
    # Lista de espera sin un horario guardado ahora: si son varios, quizás
    # conviene abrir más horarios. Sólo el número.
    en_cola = sum(1 for p in db.lista_de_espera(tenant["id"]) if p.get("ofrecido_estado") not in ("esperando", "reservo"))
    if en_cola:
        partes.append(f"{en_cola} persona{'s' if en_cola > 1 else ''} en lista de espera esperando horario")
    sin_confirmar = len(db.sin_confirmar(tenant["id"]))
    if sin_confirmar:
        partes.append(f"{sin_confirmar} reserva{'s' if sin_confirmar > 1 else ''} web sin confirmar por WhatsApp")
    if rubros.de(tenant)["tests"]:
        por_ficha: dict[str, list[dict]] = {}
        for x in db.tests_tomados(tenant["id"]):
            por_ficha.setdefault(db.clave_de(x), []).append(x)
        hoy_vencen = sum(a["vence"] == hoy for clave, xs in por_ficha.items() if not cerrado(clave)
                         for a in tests_sugeridos.para_repetir(xs, hoy, tenant.get("plazos_tests")))
        if hoy_vencen:
            partes.append(f"{hoy_vencen} test{'s' if hoy_vencen > 1 else ''} que hoy se {'pueden' if hoy_vencen > 1 else 'puede'} repetir")
    return partes


AVISO_BLOQUEO_DIAS = (3, 1)


def _bloqueos_con_sesiones(tenant: dict, hoy, sesiones: list[dict]) -> list[str]:
    """Un feriado o un bloqueo que cae sobre sesiones agendadas: se avisa 3
    días antes y el día anterior, para decidir si se atiende o se pasan. Sin
    nombres (sale por WhatsApp)."""
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    partes = []
    for b in db.bloqueos_vigentes(tenant["id"], datetime.now(timezone.utc)):
        desde, hasta = datetime.fromisoformat(b["desde"]), datetime.fromisoformat(b["hasta"])
        dia = desde.astimezone(tz).date()
        if (dia - hoy).days not in AVISO_BLOQUEO_DIAS:
            continue
        cuantas = sum(datetime.fromisoformat(t["inicio"]) < hasta and datetime.fromisoformat(t["fin"]) > desde
                      for t in sesiones)
        if not cuantas:
            continue
        motivo = b.get("motivo") or ""
        que = "es feriado" if motivo.startswith(PREFIJO_FERIADO) else "lo tenés bloqueado"
        partes.append(f"el {DIAS[dia.weekday()]} {dia.day}/{dia.month:02d} {que} y tenés {cuantas} "
                      f"sesi{'ones' if cuantas > 1 else 'ón'}: decidí si atendés o las pasás "
                      f"(en el panel, «Pasarlas a la semana siguiente» las mueve todas)")
    return partes


def _resumen_diario(tenant: dict, tz: ZoneInfo) -> None:
    """Se marca ANTES de mandar: si el envío falla se pierde el de hoy, pero
    nunca le llegan dos."""
    ahora = datetime.now(tz)
    hoy = ahora.date()
    if not tenant.get("aviso_telefono") or ahora.hour < HORA_AVISO or tenant.get("ultimo_aviso") == hoy.isoformat():
        return
    db.marcar_aviso_enviado(tenant["id"], hoy.isoformat())
    partes = _partes_del_resumen(tenant, hoy)
    numero = whatsapp.numero_de(tenant)
    if not partes or not numero:
        return
    resumen = ", ".join(partes)
    # Con botones («Ver hoy», «Pagos», «Mensajes») si está la plantilla: el
    # toque llega como texto y lo contesta asistente_profesional.
    plantilla = config.RESUMEN_TEMPLATE or config.AVISO_TEMPLATE
    if plantilla:
        ok = whatsapp.enviar_plantilla(numero, tenant["aviso_telefono"], plantilla,
                                       config.RECORDATORIO_TEMPLATE_LANG, [resumen])
    else:
        ok = whatsapp.enviar_texto(numero, tenant["aviso_telefono"], f"Tu consultorio hoy: {resumen}. Mirá el panel.")
    if not ok:
        logger.error("No se pudo mandar el resumen del tenant %s", tenant["id"])


def _avisar_reservas(tenant: dict, tz: ZoneInfo) -> None:
    """Reservas que llegaron por la página o el bot, sin nombres. Se marcan
    ANTES de mandar, igual que el resumen: nunca le llegan dos."""
    turnos = db.reservas_sin_avisar(tenant["id"])
    if not turnos:
        return
    for t in turnos:
        db.marcar_reserva_avisada(tenant["id"], t["id"])
    numero = whatsapp.numero_de(tenant)
    if not tenant.get("aviso_telefono") or not numero:
        return
    cuando = _fmt(turnos[0]["inicio"], tz)
    resumen = f"1 reserva nueva para el {cuando}" if len(turnos) == 1 else f"{len(turnos)} reservas nuevas, la próxima el {cuando}"
    if _vinculado(tenant):  # con el WhatsApp vinculado, quién y cuándo
        resumen = "; ".join(f"{t.get('paciente_nombre') or 'Sin nombre'} reservó para el {_fmt(t['inicio'], tz)}"
                            for t in sorted(turnos, key=lambda t: t["inicio"])[:8])
        if len(turnos) > 8:
            resumen += f"; y {len(turnos) - 8} reservas más"
    if config.AVISO_TEMPLATE:
        ok = whatsapp.enviar_plantilla(numero, tenant["aviso_telefono"], config.AVISO_TEMPLATE,
                                       config.RECORDATORIO_TEMPLATE_LANG, [resumen])
    else:
        ok = whatsapp.enviar_texto(numero, tenant["aviso_telefono"], f"Tu consultorio: {resumen}. Mirá el panel.")
    if not ok:
        logger.error("No se pudo avisar la reserva al tenant %s", tenant["id"])


HORA_NOCHE = 20


def _resumen_noche(tenant: dict, tz: ZoneInfo) -> None:
    """A las 20, las sesiones de mañana con nombres, para llegar preparado.
    Sólo con el WhatsApp vinculado y si no lo apagó. Se marca antes de
    mandar (nunca dos) y no sale si mañana no tiene sesiones."""
    ahora = datetime.now(tz)
    hoy = ahora.date().isoformat()
    if (ahora.hour < HORA_NOCHE or tenant.get("resumen_noche") is False or tenant.get("ultimo_resumen_noche") == hoy
            or not _vinculado(tenant)):
        return
    db.marcar_resumen_noche(tenant["id"], hoy)
    manana = ahora.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    turnos = sorted((t for t in db.agenda_semana(tenant["id"], manana, manana + timedelta(days=1)) if t.get("estado") == "reservado"),
                    key=lambda t: t["inicio"])
    if not turnos:
        return
    partes = []
    for t in turnos[:10]:
        extra = "virtual" if t.get("modalidad") == "virtual" else t.get("sede")
        partes.append(f"{datetime.fromisoformat(t['inicio']).astimezone(tz):%H:%M} {t.get('paciente_nombre') or 'Sin nombre'}"
                      + (f" ({extra})" if extra else ""))
    texto = (f"mañana {DIAS[manana.weekday()]} {manana.day}/{manana.month:02d} tenés {len(turnos)} "
             f"sesi{'ón' if len(turnos) == 1 else 'ones'}: " + ", ".join(partes)
             + (f" y {len(turnos) - 10} más" if len(turnos) > 10 else ""))
    if not avisar_profesional(tenant, texto):
        logger.error("No se pudo mandar el resumen de la noche del tenant %s", tenant["id"])


MINUTOS_PARA_ANOTAR = 0   # «Falta anotar» apenas termina la sesión
HORA_SIN_ANOTAR = 21


def sin_anotar(sesiones_hoy: list[dict], anotadas: set[str], ahora: datetime) -> list[dict]:
    """Sesiones de hoy que ya terminaron, a las que vino (o sin marcar), sin
    nota de sesión ni anotación de bitácora de hoy; una por paciente."""
    faltan, vistos = [], set()
    for t in sorted(sesiones_hoy, key=lambda t: t["inicio"]):
        clave = db.clave_de(t)
        if (t.get("estado") != "reservado" or t.get("asistencia") == "no_vino" or (t.get("nota") or "").strip()
                or clave in anotadas or clave in vistos
                or datetime.fromisoformat(t["fin"]) + timedelta(minutes=MINUTOS_PARA_ANOTAR) > ahora):
            continue
        vistos.add(clave)
        faltan.append(t)
    return faltan


def _aviso_sin_anotar(tenant: dict, tz: ZoneInfo, ahora: datetime | None = None) -> None:
    """A las 21, si quedó alguna sesión de hoy sin anotar, sólo la cantidad
    (nunca nombres ni lo anotado). Se marca antes de mandar: nunca dos."""
    ahora = ahora or datetime.now(tz)
    hoy = ahora.date().isoformat()
    if (ahora.hour < HORA_SIN_ANOTAR or tenant.get("aviso_sin_anotar") is False
            or tenant.get("ultimo_aviso_sin_anotar") == hoy or not tenant.get("aviso_telefono")):
        return
    db.marcar_aviso_sin_anotar(tenant["id"], hoy)
    inicio = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    utc = ZoneInfo("UTC")
    sesiones = db.agenda_semana(tenant["id"], inicio.astimezone(utc), (inicio + timedelta(days=1)).astimezone(utc))
    anotadas = {db.clave_de(e) for e in db.bitacora_del_dia(tenant["id"], hoy)}
    n = len(sin_anotar(sesiones, anotadas, ahora))
    if not n:
        return
    texto = "hoy te quedó 1 sesión sin anotar" if n == 1 else f"hoy te quedaron {n} sesiones sin anotar"
    if not avisar_profesional(tenant, texto):
        logger.error("No se pudo mandar el aviso de sin anotar del tenant %s", tenant["id"])


HORA_SERIES = 10
_series_revisadas: dict[str, str] = {}  # tenant -> día: una vuelta por día (el aviso en sí se marca por paciente)


def _series_por_terminar(tenant: dict, tz: ZoneInfo) -> None:
    ahora = datetime.now(tz)
    hoy = ahora.date().isoformat()
    if ahora.hour < HORA_SERIES or _series_revisadas.get(tenant["id"]) == hoy or not _vinculado(tenant):
        return
    _series_revisadas[tenant["id"]] = hoy
    import asistente_profesional
    asistente_profesional.series_por_terminar(tenant)


_ultima_limpieza = None


def _limpiar() -> None:
    """Una vez por día (el scheduler es un solo hilo en un solo worker)."""
    global _ultima_limpieza
    hoy = datetime.now(timezone.utc).date()
    if _ultima_limpieza == hoy:
        return
    _ultima_limpieza = hoy
    from datetime import timedelta
    ahora = datetime.now(timezone.utc)
    hace = lambda dias: (ahora - timedelta(days=dias)).isoformat()
    db.limpiar_vencidos(ahora.isoformat(), hace(1), hace(7), hace(30), hace(90), hace(365))


_ultimo_tick = None


def _tick() -> None:
    global _ultimo_tick
    _ultimo_tick = datetime.now(timezone.utc)
    try:
        _limpiar()
    except Exception:
        logger.exception("Fallo la limpieza diaria")
    for tenant in db.tenants_activos():
        horas_antes = tenant.get("recordatorio_horas_antes") or 24
        tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
        try:
            _resumen_diario(tenant, tz)
        except Exception:
            logger.exception("Fallo armando el resumen de tenant %s", tenant["id"])
        try:
            _resumen_noche(tenant, tz)
        except Exception:
            logger.exception("Fallo el resumen de la noche de tenant %s", tenant["id"])
        try:
            _aviso_sin_anotar(tenant, tz)
        except Exception:
            logger.exception("Fallo el aviso de sin anotar de tenant %s", tenant["id"])
        try:
            _series_por_terminar(tenant, tz)
        except Exception:
            logger.exception("Fallo avisando series por terminar de tenant %s", tenant["id"])
        try:
            _avisar_reservas(tenant, tz)
        except Exception:
            logger.exception("Fallo avisando reservas de tenant %s", tenant["id"])
        try:
            _recordar_confirmaciones(tenant, tz)
        except Exception:
            logger.exception("Fallo recordando confirmaciones de tenant %s", tenant["id"])
        try:
            _generar_una_vez_por_dia(tenant, tz)
        except Exception:
            logger.exception("Fallo generando horarios de tenant %s", tenant["id"])
        try:
            import cancelaciones  # acá: cancelaciones importa este módulo
            if cancelaciones.reactivar_si_toca(tenant, datetime.now(tz).date()):
                tenant = {**tenant, "pausado": False}
        except Exception:
            logger.exception("Fallo reactivando la pausa de tenant %s", tenant["id"])
        try:
            import cancelaciones  # acá: cancelaciones importa este módulo
            cancelaciones.avisar_vuelta_si_toca(tenant, datetime.now(tz))
        except Exception:
            logger.exception("Fallo avisando la vuelta de la pausa de tenant %s", tenant["id"])
        try:
            import cancelaciones  # acá: cancelaciones importa este módulo
            cancelaciones.vencer_huecos(tenant)
        except Exception:
            logger.exception("Fallo venciendo horarios retenidos de tenant %s", tenant["id"])
        try:
            import cancelaciones  # acá: cancelaciones importa este módulo
            cancelaciones.pasar_vencidos(tenant)
        except Exception:
            logger.exception("Fallo pasando horarios guardados vencidos de tenant %s", tenant["id"])
        try:
            import pagos  # acá: pagos importa db y config, y de acá sólo esto
            pagos.vencer(tenant)
            pagos.verificar_conexion(tenant, datetime.now(tz).date())
            pagos.avisar_por_vencer(tenant, f"https://{config.DOMINIO_PRINCIPAL}" if config.DOMINIO_PRINCIPAL else None)
        except Exception:
            logger.exception("Fallo venciendo pagos de tenant %s", tenant["id"])
        try:
            import pagos
            pagos.mandar_resumen_mensual(tenant, datetime.now(tz))
        except Exception:
            logger.exception("Fallo el resumen mensual de cobros de tenant %s", tenant["id"])
        try:
            # Una reserva web sin pagar todavía no está aceptada: no se recuerda.
            pendientes = [t for t in db.turnos_para_recordar(tenant["id"], horas_antes)
                          if t.get("pago_web") not in ("esperando", "comprobante")]
        except Exception:
            logger.exception("Fallo consultando recordatorios de tenant %s", tenant["id"])
            continue
        numero = whatsapp.numero_de(tenant)
        if not numero:
            logger.error(
                "Tenant %s no tiene número propio y PLATAFORMA_PHONE_NUMBER_ID está vacía: "
                "no hay desde dónde mandar el recordatorio", tenant["id"])
            continue
        for turno in pendientes:
            cuando = cuando_de(turno, tenant, tz)
            # El recordatorio siempre cae fuera de la ventana de 24 h de Meta
            # (el paciente reservó por la web y nunca escribió), así que sin
            # plantilla aprobada esto NO llega. Ver config.RECORDATORIO_TEMPLATE.
            if config.RECORDATORIO_TEMPLATE:
                ok = whatsapp.enviar_plantilla(
                    numero, turno["paciente_telefono"],
                    config.RECORDATORIO_TEMPLATE, config.RECORDATORIO_TEMPLATE_LANG,
                    [tenant["nombre"], cuando],
                )
            else:
                texto = (
                    f"Te recuerdo tu turno con *{tenant['nombre']}* "
                    f"el {cuando}. Si no podés ir, respondé *cancelar*."
                )
                ok = whatsapp.enviar_texto(numero, turno["paciente_telefono"], texto)
            if ok:
                db.marcar_recordatorio_enviado(turno["id"])
            else:
                logger.error("No se pudo mandar el recordatorio del turno %s", turno["id"])


def vista_previa(tenant: dict, momento: datetime) -> dict:
    """Lo que el scheduler mandaría en `momento` para este tenant, sin mandar
    ni marcar nada. Para la vista previa de administración: sin nombres de
    pacientes, sólo los últimos 4 dígitos del teléfono."""
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    local = momento.astimezone(tz)
    hoy = local.date()
    partes = _partes_del_resumen(tenant, hoy)
    if not tenant.get("aviso_telefono"):
        resumen = {"sale": False, "texto": None, "por_que": "La cuenta no tiene cargado un teléfono para avisos."}
    elif local.hour < HORA_AVISO:
        resumen = {"sale": False, "texto": ", ".join(partes) or None,
                   "por_que": f"Todavía no son las {HORA_AVISO}: el resumen sale desde esa hora."}
    elif not partes:
        resumen = {"sale": False, "texto": None, "por_que": "No hay nada que contar ese día: el resumen no sale."}
    else:
        resumen = {"sale": True, "texto": ", ".join(partes), "por_que": "Sale una vez ese día."}
    horas_antes = tenant.get("recordatorio_horas_antes") or 24
    recordatorios = [
        {"final": "…" + "".join(ch for ch in t.get("paciente_telefono") or "" if ch.isdigit())[-4:],
         "cuando": cuando_de(t, tenant, tz)}
        for t in sorted(db.turnos_para_recordar(tenant["id"], horas_antes, ahora=momento.astimezone(timezone.utc)),
                        key=lambda t: t["inicio"])
    ]
    return {"resumen": resumen, "recordatorios": recordatorios, "horas_antes": horas_antes,
            "plantilla": config.RECORDATORIO_TEMPLATE or None}


# Segundo aviso de confirmación: a las cuántas horas del primero, y en qué
# horario (no se le escribe a nadie de noche).
HORAS_PARA_RECORDAR = 6
HORARIO_AVISOS = range(9, 21)


def mandar_confirmacion(tenant: dict, turno: dict, tz: ZoneInfo) -> bool:
    """WhatsApp al paciente para que confirme una reserva web (y así saber que
    el número está bien). Plantilla con botones; sin plantilla no se manda."""
    if not config.CONFIRMACION_TEMPLATE:
        return False
    numero = whatsapp.numero_de(tenant)
    if not numero:
        return False
    try:
        return whatsapp.enviar_plantilla(numero, turno["paciente_telefono"], config.CONFIRMACION_TEMPLATE,
                                         config.RECORDATORIO_TEMPLATE_LANG, [tenant["nombre"], cuando_de(turno, tenant, tz)])
    except Exception:
        logger.warning("No se pudo mandar la confirmación del turno %s", turno.get("id"), exc_info=True)
        return False


def _recordar_confirmaciones(tenant: dict, tz: ZoneInfo, ahora: datetime | None = None) -> None:
    """Una sola vez: si a las HORAS_PARA_RECORDAR del primer aviso no
    confirmó, se le vuelve a mandar. Se marca antes de mandar (como el
    resumen): si falla, no se reintenta cada 10 minutos."""
    ahora = ahora or datetime.now(timezone.utc)
    if ahora.astimezone(tz).hour not in HORARIO_AVISOS:
        return
    for t in db.sin_confirmar(tenant["id"]):
        if t.get("confirmacion_recordada_at"):
            continue
        enviada = datetime.fromisoformat(t["confirmacion_enviada_at"])
        if ahora - enviada < timedelta(hours=HORAS_PARA_RECORDAR):
            continue
        if datetime.fromisoformat(t["inicio"]) - ahora < timedelta(hours=1):
            continue
        db.actualizar_reserva(tenant["id"], t["id"], {"confirmacion_recordada_at": ahora.isoformat()})
        mandar_confirmacion(tenant, t, tz)


def _vinculado(tenant: dict) -> bool:
    import asistente_profesional
    return asistente_profesional.vinculado(tenant)


def _una_linea(texto: str) -> str:
    """Los parámetros de una plantilla no aceptan saltos de línea."""
    return " ".join((texto or "").split())


def avisar_profesional(tenant: dict, texto: str, paciente: str | None = None, con_nombre: str | None = None) -> bool:
    """WhatsApp al profesional (a `aviso_telefono`) en el momento, con la
    misma plantilla del resumen. Con el WhatsApp VINCULADO
    (asistente_profesional) va con nombre: `con_nombre` reemplaza el texto, o
    `paciente` reemplaza «un paciente». Sin vincular, el texto sin nombres."""
    numero = whatsapp.numero_de(tenant)
    if not tenant.get("aviso_telefono") or not numero:
        return False
    if _vinculado(tenant):
        if con_nombre:
            texto = con_nombre
        elif paciente:
            texto = texto.replace("un paciente", paciente, 1) if "un paciente" in texto else f"{texto} ({paciente})"
    texto = _una_linea(texto)
    try:
        if config.AVISO_TEMPLATE:
            return whatsapp.enviar_plantilla(numero, tenant["aviso_telefono"], config.AVISO_TEMPLATE,
                                             config.RECORDATORIO_TEMPLATE_LANG, [texto])
        return whatsapp.enviar_texto(numero, tenant["aviso_telefono"], f"Tu consultorio: {texto}. Mirá el panel.")
    except Exception:
        logger.warning("No se pudo avisar al tenant %s", tenant.get("id"), exc_info=True)
        return False


def rangos_horario(tenant: dict) -> list[tuple]:
    """(día 0-6, desde, hasta, modalidad o None, sede o None) válidos del
    horario semanal. La sede tiene que existir en `tenants.sedes`."""
    sedes = {str(x.get("nombre")) for x in (tenant.get("sedes") or []) if x.get("nombre")}
    salida = []
    for r in tenant.get("horario_semanal") or []:
        try:
            dia, desde, hasta = int(r["dia"]), str(r["desde"]), str(r["hasta"])
            modalidad = r.get("modalidad") if r.get("modalidad") in ("presencial", "virtual") else None
            sede = r.get("sede") if r.get("sede") in sedes else None
            from datetime import time as _t
            if 0 <= dia <= 6 and _t.fromisoformat(desde) < _t.fromisoformat(hasta):
                salida.append((dia, desde, hasta, modalidad, sede))
        except (KeyError, TypeError, ValueError):
            continue
    return salida


def generar_desde_horario(tenant: dict, ahora: datetime | None = None) -> int:
    """Crea los horarios libres de las próximas `horario_semanas` según el
    horario de atención. Es idempotente (crear_turnos_libres no pisa nada que
    ya exista): lo que el profesional reservó, se tomó o bloqueó sigue igual."""
    ahora = ahora or datetime.now(timezone.utc)
    sacados = db.horarios_sacados(tenant["id"], ahora)
    duracion = tenant.get("duracion_turno_minutos") or 50
    return sum(db.crear_turnos_libres(tenant["id"], [i for i in inicios if i not in sacados], duracion, modalidad, sede)
               for (modalidad, sede), inicios in inicios_del_horario(tenant, ahora).items())


def inicios_del_horario(tenant: dict, ahora: datetime) -> dict:
    """Los horarios que tocan según el horario de atención, de ahora a
    `horario_semanas`, agrupados por (modalidad, sede)."""
    rangos = rangos_horario(tenant)
    if not rangos:
        return {}
    from datetime import time as _t
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    duracion = tenant.get("duracion_turno_minutos") or 50
    paso_min = duracion + (tenant.get("descanso_minutos") or 0)
    hoy = ahora.astimezone(tz).date()
    por_grupo: dict = {}
    for i in range(7 * (tenant.get("horario_semanas") or 4)):
        dia = hoy + timedelta(days=i)
        for d, desde, hasta, modalidad, sede in rangos:
            if dia.weekday() != d:
                continue
            paso = datetime.combine(dia, _t.fromisoformat(desde), tzinfo=tz)
            fin = datetime.combine(dia, _t.fromisoformat(hasta), tzinfo=tz)
            while paso + timedelta(minutes=duracion) <= fin:
                if paso > ahora:
                    por_grupo.setdefault((modalidad, sede), []).append(paso.astimezone(timezone.utc))
                paso += timedelta(minutes=paso_min)
    return por_grupo


def limpiar_fuera_de_horario(tenant: dict, ahora: datetime | None = None) -> int:
    """Al cambiar el horario de atención, los libres que ya no entran se borran
    (si no, quedan los del horario viejo mezclados con los nuevos). Nunca toca
    una sesión, un horario retenido por una cancelación ni uno hecho a mano."""
    if not rangos_horario(tenant):
        return 0
    ahora = ahora or datetime.now(timezone.utc)
    validos = {i for inicios in inicios_del_horario(tenant, ahora).values() for i in inicios}
    sobran = [t["id"] for t in db.libres_desde_horario(tenant["id"], ahora)
              if datetime.fromisoformat(t["inicio"]) not in validos]
    return db.borrar_libres(tenant["id"], sobran)


PREFIJO_FERIADO = "Feriado: "


def sincronizar_feriados(tenant: dict, hoy) -> int:
    """Con `feriados = 'bloquear'`, cada feriado de los próximos 12 meses es un
    bloqueo (y así lo respetan la página, el bot y la generación). Con
    'atender', se borran los bloqueos de feriado futuros. Los días puente (no
    laborables) siguen `puentes` aparte: se pueden atender aunque los feriados
    se bloqueen. Idempotente.
    Devuelve cuántos agregó o quitó."""
    import feriados
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    existentes = {datetime.fromisoformat(b["desde"]).astimezone(tz).date(): b
                  for b in db.bloqueos_vigentes(tenant["id"], datetime.combine(hoy, datetime.min.time(), tzinfo=tz))
                  if (b.get("motivo") or "").startswith(PREFIJO_FERIADO)}
    cambios = 0
    atiende_feriados = (tenant.get("feriados") or "bloquear") == "atender"
    atiende_puentes = atiende_feriados or tenant.get("puentes") == "atender"

    def atiende(dia) -> bool:
        return atiende_puentes if feriados.es_puente(dia) else atiende_feriados

    for dia, b in existentes.items():
        if atiende(dia):
            db.borrar_bloqueo(tenant["id"], b["id"])
            cambios += 1
    for dia, nombre in feriados.entre(hoy, hoy + timedelta(days=365)):
        if dia in existentes or atiende(dia):
            continue
        desde = datetime.combine(dia, datetime.min.time(), tzinfo=tz)
        db.agregar_bloqueo(tenant["id"], desde.astimezone(timezone.utc), (desde + timedelta(days=1)).astimezone(timezone.utc),
                           PREFIJO_FERIADO + nombre)
        cambios += 1
    return cambios


_generado: dict[str, object] = {}


def _generar_una_vez_por_dia(tenant: dict, tz: ZoneInfo) -> None:
    hoy = datetime.now(tz).date()
    if _generado.get(tenant["id"]) == hoy:
        return
    _generado[tenant["id"]] = hoy
    try:
        import feriados
        feriados.actualizar(hoy)
        sincronizar_feriados(tenant, hoy)
    except Exception:
        logger.exception("Fallo sincronizando feriados de tenant %s", tenant["id"])
    creados = generar_desde_horario(tenant)
    if creados:
        logger.info("Tenant %s: %s horarios nuevos desde su horario de atención", tenant["id"], creados)


def _loop() -> None:
    intervalo = config.RECORDATORIO_INTERVALO_MIN * 60
    while True:
        try:
            _tick()
        except Exception:
            logger.exception("Fallo en el ciclo de recordatorios")
        time.sleep(intervalo)


def iniciar() -> None:
    # Los ajustes de WhatsApp guardados en administración, antes del primer
    # envío. Va acá y no al importar app: los tests apagan iniciar().
    whatsapp.cargar_ajustes()
    hilo = threading.Thread(target=_loop, daemon=True, name="recordatorios")
    hilo.start()
    logger.info("Scheduler de recordatorios arrancado (cada %s min)", config.RECORDATORIO_INTERVALO_MIN)
    if not config.RECORDATORIO_TEMPLATE:
        logger.warning(
            "RECORDATORIO_TEMPLATE vacía: los recordatorios salen como texto libre y "
            "Meta los va a rechazar salvo que el paciente haya escrito en las últimas 24 h.")
