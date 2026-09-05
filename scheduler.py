"""Recordatorios automáticos. Corre como un hilo de fondo dentro del mismo
proceso — por eso Procfile/nixpacks.toml fuerzan --workers 1: con dos workers
este hilo correría dos veces y mandaría el recordatorio duplicado."""
import logging
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
import db
import whatsapp

logger = logging.getLogger("scheduler")

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def _fmt(dt_iso: str, tz: ZoneInfo) -> str:
    dt = datetime.fromisoformat(dt_iso).astimezone(tz)
    return f"{DIAS[dt.weekday()]} {dt.day} de {MESES[dt.month - 1]}, {dt.hour:02d}:{dt.minute:02d}"


def _tick() -> None:
    for tenant in db.tenants_activos():
        horas_antes = tenant.get("recordatorio_horas_antes") or 24
        tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
        try:
            pendientes = db.turnos_para_recordar(tenant["id"], horas_antes)
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
            cuando = _fmt(turno["inicio"], tz)
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


def _loop() -> None:
    intervalo = config.RECORDATORIO_INTERVALO_MIN * 60
    while True:
        try:
            _tick()
        except Exception:
            logger.exception("Fallo en el ciclo de recordatorios")
        time.sleep(intervalo)


def iniciar() -> None:
    hilo = threading.Thread(target=_loop, daemon=True, name="recordatorios")
    hilo.start()
    logger.info("Scheduler de recordatorios arrancado (cada %s min)", config.RECORDATORIO_INTERVALO_MIN)
    if not config.RECORDATORIO_TEMPLATE:
        logger.warning(
            "RECORDATORIO_TEMPLATE vacía: los recordatorios salen como texto libre y "
            "Meta los va a rechazar salvo que el paciente haya escrito en las últimas 24 h.")
