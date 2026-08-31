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
        for turno in pendientes:
            texto = (
                f"Te recuerdo tu turno con *{tenant['nombre']}* "
                f"el {_fmt(turno['inicio'], tz)}. Si no podés ir, respondé *cancelar*."
            )
            if whatsapp.enviar_texto(tenant["whatsapp_phone_number_id"], turno["paciente_telefono"], texto):
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
