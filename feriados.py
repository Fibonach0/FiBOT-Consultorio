"""Feriados nacionales de Argentina.

La lista embebida sale de https://api.argentinadatos.com/v1/feriados/<año>
(consultada el 03/11/2026). Una vez por día el scheduler pide los del año en
curso y el siguiente para tomar cambios (traslados, puentes nuevos); si la API
no responde, queda la lista embebida.
"""
import logging
from datetime import date

import requests

logger = logging.getLogger("feriados")

EMBEBIDOS = {
    "2026-01-01": "Año nuevo", "2026-02-16": "Carnaval", "2026-02-17": "Carnaval",
    "2026-03-23": "Puente turístico", "2026-03-24": "Día de la Memoria",
    "2026-04-02": "Día del Veterano y de los Caídos en Malvinas", "2026-04-03": "Viernes Santo",
    "2026-05-01": "Día del Trabajador", "2026-05-25": "Revolución de Mayo", "2026-06-15": "Paso a la Inmortalidad de Güemes",
    "2026-06-20": "Paso a la Inmortalidad de Belgrano", "2026-07-09": "Día de la Independencia",
    "2026-07-10": "Puente turístico", "2026-08-17": "Paso a la Inmortalidad de San Martín",
    "2026-10-12": "Día del Respeto a la Diversidad Cultural", "2026-11-09": "Visita del papa León XIV",
    "2026-11-23": "Día de la Soberanía Nacional", "2026-12-07": "Puente turístico",
    "2026-12-08": "Inmaculada Concepción de María", "2026-12-25": "Navidad",
    "2027-01-01": "Año nuevo", "2027-02-08": "Carnaval", "2027-02-09": "Carnaval", "2027-03-24": "Día de la Memoria",
    "2027-03-26": "Viernes Santo", "2027-04-02": "Día del Veterano y de los Caídos en Malvinas",
    "2027-05-01": "Día del Trabajador", "2027-05-25": "Revolución de Mayo", "2027-06-17": "Paso a la Inmortalidad de Güemes",
    "2027-06-20": "Paso a la Inmortalidad de Belgrano", "2027-07-09": "Día de la Independencia",
    "2027-08-17": "Paso a la Inmortalidad de San Martín", "2027-10-12": "Día del Respeto a la Diversidad Cultural",
    "2027-11-20": "Día de la Soberanía Nacional", "2027-12-08": "Inmaculada Concepción de María",
    "2027-12-25": "Navidad",
}

_actualizados: dict[str, str] = {}
_tipos: dict[str, str] = {}

# Los días puente son «no laborables»: el profesional puede atender aunque
# bloquee los feriados (tenants.puentes). La API los marca con tipo «puente»;
# en la lista embebida se reconocen por el nombre.
PUENTE = "puente"
_ultimo_intento: date | None = None


def actualizar(hoy: date) -> None:
    """Una vez por día: pide a la API los del año y el siguiente."""
    global _ultimo_intento
    if _ultimo_intento == hoy:
        return
    _ultimo_intento = hoy
    for anio in (hoy.year, hoy.year + 1):
        try:
            r = requests.get(f"https://api.argentinadatos.com/v1/feriados/{anio}", timeout=10)
            if r.status_code < 300:
                for f in r.json():
                    if f.get("fecha") and f.get("nombre"):
                        _actualizados[f["fecha"]] = f["nombre"]
                        if f.get("tipo"):
                            _tipos[f["fecha"]] = f["tipo"]
        except (requests.RequestException, ValueError):
            logger.info("No se pudieron actualizar los feriados de %s; quedan los embebidos", anio)


def todos() -> dict[str, str]:
    return {**EMBEBIDOS, **_actualizados}


def entre(desde: date, hasta: date) -> list[tuple[date, str]]:
    return sorted((date.fromisoformat(f), n) for f, n in todos().items() if desde <= date.fromisoformat(f) <= hasta)


def es_puente(dia: date) -> bool:
    clave = dia.isoformat()
    if clave in _tipos:
        return _tipos[clave] == PUENTE
    return "puente" in todos().get(clave, "").lower()
