"""Programar un diseño guardado por fechas (migrations/090).

`tenants.diseno_programado` = {id, nombre, desde, hasta, estado, volver_id}:
el día `desde` (en la zona del profesional) el scheduler publica el diseño
guardado `id`; el de antes queda en el historial (`volver_id`, que no se borra
mientras dure) y el día `hasta` vuelve a ese. Sin `hasta`, sólo lo publica.
Si en el medio el profesional publicó otro, al llegar `hasta` no se toca nada.
El borrador nunca se toca.
"""
import logging
from datetime import date

import db
import diseno

logger = logging.getLogger("disenos_programados")

HISTORIAL = 3      # = app.HISTORIAL_DISENOS
DIAS_MAX = 366     # hasta cuándo se puede programar


def leer(tenant: dict) -> dict | None:
    p = tenant.get("diseno_programado")
    if not isinstance(p, dict) or not p.get("id") or p.get("estado") not in ("esperando", "puesto"):
        return None
    try:
        desde = date.fromisoformat(str(p.get("desde"))[:10])
        hasta = date.fromisoformat(str(p["hasta"])[:10]) if p.get("hasta") else None
    except ValueError:
        return None
    return {**p, "desde": desde, "hasta": hasta}


def conservar(tenant: dict) -> tuple:
    """Ids del historial que no se pueden borrar: el diseño al que se vuelve."""
    p = leer(tenant)
    return tuple(x for x in ((p or {}).get("volver_id"), (p or {}).get("id")) if x)


def validar(desde: date | None, hasta: date | None, hoy: date) -> str | None:
    if not desde:
        return "Elegí desde qué día."
    if desde < hoy:
        return "La fecha de inicio ya pasó."
    if (desde - hoy).days > DIAS_MAX:
        return "Se puede programar hasta un año adelante."
    if hasta and hasta <= desde:
        return "El día de volver tiene que ser después del de inicio."
    return None


def _publicado(tenant: dict) -> tuple:
    return (tenant.get("diseno") or None, tenant.get("css_propio") or None)


def _limpio(fila: dict) -> tuple | None:
    """Estilo y CSS de una fila del historial con las reglas de hoy, o None."""
    css, errores, _ = diseno.limpiar_css(fila.get("css") or "")
    estilo, err = diseno.leer(fila.get("diseno") or {})
    return None if errores or err else (estilo or None, css or None)


def _avisar(tenant: dict, texto: str) -> None:
    try:
        db.registrar_actividad(tenant.get("nombre") or "profesional", texto, tenant["id"])
    except Exception:
        logger.warning("No se pudo registrar la actividad del diseño programado", exc_info=True)
    try:
        import scheduler  # acá: scheduler importa este módulo
        scheduler.avisar_profesional(tenant, texto)
    except Exception:
        logger.warning("No se pudo avisar del diseño programado", exc_info=True)


def aplicar(tenant: dict, hoy: date) -> str | None:
    """Lo que toca hoy. Devuelve 'puesto', 'vuelto' o 'terminado' si cambió algo."""
    p = leer(tenant)
    if not p:
        return None
    nombre = p.get("nombre") or "el diseño programado"
    if p["estado"] == "esperando" and hoy >= p["desde"]:
        fila = db.diseno_del_historial(tenant["id"], p["id"])
        limpio = _limpio(fila) if fila else None
        if not limpio:
            db.guardar_ajustes(tenant["id"], {"diseno_programado": None})
            _avisar(tenant, f"No pude poner «{nombre}» en tu página: ya no está guardado o no pasa la revisión")
            return "terminado"
        volver_id = None
        if any(_publicado(tenant)):
            volver_id = db.guardar_en_historial(tenant["id"], *_publicado(tenant), HISTORIAL, conservar=(p["id"],))
        sigue = {**tenant["diseno_programado"], "estado": "puesto", "volver_id": volver_id} if p["hasta"] else None
        db.guardar_ajustes(tenant["id"], {"diseno": limpio[0], "css_propio": limpio[1], "diseno_programado": sigue})
        cuando = f": vuelve al de antes el {p['hasta'].day}/{p['hasta'].month}" if p["hasta"] else ""
        _avisar(tenant, f"Tu página ya tiene el diseño «{nombre}»{cuando}")
        return "puesto"
    if p["estado"] == "puesto" and p["hasta"] and hoy >= p["hasta"]:
        fila = db.diseno_del_historial(tenant["id"], p["id"])
        puesto = _limpio(fila) if fila else None
        if puesto is None or puesto != _publicado(tenant):
            # Publicó otro en el medio: se respeta lo que eligió.
            db.guardar_ajustes(tenant["id"], {"diseno_programado": None})
            _avisar(tenant, f"Terminó «{nombre}», pero ya habías cambiado el diseño: tu página sigue como la dejaste")
            return "terminado"
        if p.get("volver_id"):
            antes = db.diseno_del_historial(tenant["id"], p["volver_id"])
            limpio = _limpio(antes) if antes else None
            if not limpio:
                db.guardar_ajustes(tenant["id"], {"diseno_programado": None})
                _avisar(tenant, f"Terminó «{nombre}», pero el diseño de antes ya no está guardado: tu página sigue igual")
                return "terminado"
        else:
            limpio = (None, None)  # antes tenía el original
        db.guardar_ajustes(tenant["id"], {"diseno": limpio[0], "css_propio": limpio[1], "diseno_programado": None})
        _avisar(tenant, f"Terminó «{nombre}»: tu página volvió al diseño de antes")
        return "vuelto"
    return None
