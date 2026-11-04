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
    if config.AVISO_TEMPLATE:
        ok = whatsapp.enviar_plantilla(numero, tenant["aviso_telefono"], config.AVISO_TEMPLATE,
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
    if config.AVISO_TEMPLATE:
        ok = whatsapp.enviar_plantilla(numero, tenant["aviso_telefono"], config.AVISO_TEMPLATE,
                                       config.RECORDATORIO_TEMPLATE_LANG, [resumen])
    else:
        ok = whatsapp.enviar_texto(numero, tenant["aviso_telefono"], f"Tu consultorio: {resumen}. Mirá el panel.")
    if not ok:
        logger.error("No se pudo avisar la reserva al tenant %s", tenant["id"])


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
            cancelaciones.vencer_huecos(tenant)
        except Exception:
            logger.exception("Fallo venciendo horarios retenidos de tenant %s", tenant["id"])
        try:
            import pagos  # acá: pagos importa db y config, y de acá sólo esto
            pagos.vencer(tenant)
            pagos.avisar_por_vencer(tenant, f"https://{config.DOMINIO_PRINCIPAL}" if config.DOMINIO_PRINCIPAL else None)
        except Exception:
            logger.exception("Fallo venciendo pagos de tenant %s", tenant["id"])
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


def avisar_profesional(tenant: dict, texto: str) -> bool:
    """WhatsApp al profesional (a `aviso_telefono`) en el momento, con la
    misma plantilla del resumen. Sin nombres ni datos de pacientes: el detalle
    está en el panel."""
    numero = whatsapp.numero_de(tenant)
    if not tenant.get("aviso_telefono") or not numero:
        return False
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
    rangos = rangos_horario(tenant)
    if not rangos:
        return 0
    from datetime import time as _t
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    ahora = ahora or datetime.now(timezone.utc)
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
    return sum(db.crear_turnos_libres(tenant["id"], inicios, duracion, modalidad, sede)
               for (modalidad, sede), inicios in por_grupo.items())


PREFIJO_FERIADO = "Feriado: "


def sincronizar_feriados(tenant: dict, hoy) -> int:
    """Con `feriados = 'bloquear'`, cada feriado de los próximos 12 meses es un
    bloqueo (y así lo respetan la página, el bot y la generación). Con
    'atender', se borran los bloqueos de feriado futuros. Idempotente.
    Devuelve cuántos agregó o quitó."""
    import feriados
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    existentes = {datetime.fromisoformat(b["desde"]).astimezone(tz).date(): b
                  for b in db.bloqueos_vigentes(tenant["id"], datetime.now(timezone.utc))
                  if (b.get("motivo") or "").startswith(PREFIJO_FERIADO)}
    cambios = 0
    if (tenant.get("feriados") or "bloquear") == "atender":
        for b in existentes.values():
            db.borrar_bloqueo(tenant["id"], b["id"])
            cambios += 1
        return cambios
    for dia, nombre in feriados.entre(hoy, hoy + timedelta(days=365)):
        if dia in existentes:
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
