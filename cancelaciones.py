"""Qué pasa después de que un PACIENTE cancela (por la web o por WhatsApp).

Un solo lugar para las dos puertas de entrada, así se comportan igual:

- Con `tenants.al_cancelar = 'liberar'` (lo de siempre) el horario vuelve a
  estar disponible enseguida, se le avisa al profesional y, si tiene activado
  `ofrecer_espera_auto`, se le ofrece al primero de la lista de espera.
- Con `'preguntar'` el horario queda retenido (`turnos.hueco = 'pendiente'`):
  libre pero fuera de `turnos_disponibles`, nadie lo ve. El profesional decide
  desde el panel o contestando por WhatsApp «liberar» / «me lo tomo».
  «Me lo tomo» lo deja en `'propio'` (sigue sin ofrecerse).

Los avisos al profesional llevan el nombre del paciente sólo si su WhatsApp
de avisos está vinculado (asistente_profesional). Si hay lista de espera, el
aviso deja preparada la oferta al primero: el profesional responde «sí».
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import db
import scheduler

logger = logging.getLogger(__name__)
import whatsapp

LIBERAR = {"liberar", "liberalo", "liberarlo", "libre", "dejalo libre", "ofrecer", "ofrecelo"}
TOMAR = {"me lo tomo", "melotomo", "lo tomo", "descanso", "descansar", "me lo quedo", "no lo liberes"}


def _tz(tenant: dict) -> ZoneInfo:
    return ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")


def horas_limite(tenant: dict) -> int:
    horas = tenant.get("cancelar_hasta_horas")
    return 24 if horas is None else horas


def puede_modificar(turno: dict, tenant: dict) -> bool:
    """Política del profesional: hasta cuántas horas antes el paciente puede
    cancelar o cambiar por su cuenta (web o WhatsApp). 0 = hasta que empieza."""
    from zoneinfo import ZoneInfo as _Z
    return datetime.fromisoformat(turno["inicio"]) - datetime.now(_Z("UTC")) > timedelta(hours=horas_limite(tenant))


def base_url() -> str:
    if config.DOMINIO_PRINCIPAL:
        return f"https://{config.DOMINIO_PRINCIPAL}"
    from flask import has_request_context, request
    return request.host_url.rstrip("/") if has_request_context() else ""


def ofrecer_horario(tenant: dict, telefono: str, cuando: str, link: str) -> bool:
    numero = whatsapp.numero_de(tenant)
    if not numero:
        return False
    if config.LISTA_ESPERA_TEMPLATE:
        return whatsapp.enviar_plantilla(numero, telefono, config.LISTA_ESPERA_TEMPLATE,
                                         config.RECORDATORIO_TEMPLATE_LANG, [tenant["nombre"], cuando, link])
    return whatsapp.enviar_texto(numero, telefono, f"Hola. Se liberó un horario con *{tenant['nombre']}*: {cuando}. "
                                                   f"Si lo querés, reservalo acá: {link}")


def ofrecer_a_espera(tenant: dict, turno: dict) -> str | None:
    """Si el profesional lo activó, le ofrece el horario recién liberado al
    primero de la lista que hoy no recibió otro ofrecimiento. Devuelve su
    nombre (para el panel) o None."""
    if not tenant.get("ofrecer_espera_auto") or not tenant.get("slug"):
        return None
    tz = _tz(tenant)
    hoy = datetime.now(tz).date().isoformat()
    persona = next((p for p in db.lista_de_espera(tenant["id"]) if p.get("ofrecido_en") != hoy), None)
    if not persona:
        return None
    db.marcar_ofrecido(tenant["id"], persona["id"], hoy)
    cuando = scheduler.cuando_de(turno, tenant, tz)
    if ofrecer_horario(tenant, persona["telefono"], cuando, f"{base_url()}/{tenant['slug']}"):
        return persona["nombre"]
    return None


def ofrecer_a_toda_la_espera(tenant: dict, turno: dict, hoy) -> int:
    """Le ofrece `turno` a cada persona de la lista de espera que hoy no
    recibió otro ofrecimiento (al reactivar una pausa o abrir un feriado).
    Devuelve a cuántos se les pudo escribir."""
    if not tenant.get("slug"):
        return 0
    cuando = scheduler.cuando_de(turno, tenant, _tz(tenant))
    avisados = 0
    for p in db.lista_de_espera(tenant["id"]):
        if p.get("ofrecido_en") == hoy.isoformat():
            continue
        db.marcar_ofrecido(tenant["id"], p["id"], hoy.isoformat())
        if ofrecer_horario(tenant, p["telefono"], cuando, f"{base_url()}/{tenant['slug']}"):
            avisados += 1
    return avisados


def tras_cancelar(tenant: dict, turno: dict, por: str) -> None:
    """Después de que el paciente canceló `turno` (ya liberado). `por` es
    «la web» o «WhatsApp», para el aviso."""
    cuando = scheduler.cuando_de(turno, tenant, _tz(tenant))
    if tenant.get("al_cancelar") == "preguntar":
        db.marcar_hueco(tenant["id"], turno["id"], "pendiente")
        scheduler.avisar_profesional(
            tenant, f"un paciente canceló por {por} el turno del {cuando}. Lo retuve: respondé LIBERAR para que "
                    f"otro lo pueda tomar o ME LO TOMO para quedártelo (también desde el panel)",
            paciente=turno.get("paciente_nombre"))
        return
    esperando = len(db.lista_de_espera(tenant["id"]))
    ofrecido = ofrecer_a_espera(tenant, turno)
    texto = f"un paciente canceló por {por} el turno del {cuando}"
    if ofrecido:
        texto += "; se lo ofrecimos al primero de tu lista de espera"
    elif esperando:
        texto += f"; tenés {esperando} en lista de espera"
        propuesta = _proponer_a_espera(tenant, turno)
        if propuesta:
            texto += f": respondé {propuesta['si']} y se lo ofrezco a {propuesta['nombre']}"
    scheduler.avisar_profesional(tenant, texto, paciente=turno.get("paciente_nombre"))


def _proponer_a_espera(tenant: dict, turno: dict) -> dict | None:
    """Deja preparada la oferta del horario al primero de la lista de espera
    (el profesional responde «sí»). Devuelve {"nombre", "si"} o None."""
    try:
        import asistente_profesional
        return asistente_profesional.propuesta_tras_cancelar(tenant, {**turno, "estado": "libre"})
    except Exception:
        logger.exception("No se pudo preparar la oferta a la lista de espera (%s)", tenant.get("id"))
        return None


def liberar(tenant: dict, turno: dict) -> str | None:
    """El profesional decidió que el horario retenido vuelva a ofrecerse."""
    db.marcar_hueco(tenant["id"], turno["id"], None)
    return ofrecer_a_espera(tenant, turno)


def tomar(tenant: dict, turno: dict) -> None:
    db.marcar_hueco(tenant["id"], turno["id"], "propio")


def respuesta_del_profesional(telefono: str, texto: str) -> str | None:
    """«liberar» / «me lo tomo» desde el WhatsApp de avisos de un profesional.
    Decide sobre el horario retenido más próximo. None si no corresponde (no
    es un profesional, no es una de esas palabras o no hay nada retenido):
    entonces el mensaje sigue su camino normal. Desde un número de avisos sin
    vincular, sólo le dice cómo vincularlo."""
    palabra = " ".join((texto or "").lower().replace(",", " ").replace(".", " ").split())
    if palabra not in LIBERAR and palabra not in TOMAR:
        return None
    cuentas = db.tenants_por_aviso(telefono)
    if not cuentas:
        return None
    # Mismo candado que el asistente: sólo desde un WhatsApp vinculado.
    import asistente_profesional
    vinculadas = [t for t in cuentas if asistente_profesional.vinculado(t)]
    if not vinculadas:
        return asistente_profesional.sin_vincular()
    tenant = vinculadas[0]
    pendientes = db.huecos_pendientes(tenant["id"])
    if not pendientes:
        return "No tenés ningún horario retenido."
    turno = pendientes[0]
    cuando = scheduler.cuando_de(turno, tenant, _tz(tenant))
    if palabra in LIBERAR:
        ofrecido = liberar(tenant, turno)
        texto = f"Listo, el {cuando} quedó libre para tus pacientes."
        if ofrecido:
            texto += " Se lo ofrecimos al primero de tu lista de espera."
        else:
            primero = _proponer_a_espera(tenant, turno)
            if primero:
                texto += (f" Tenés {len(db.lista_de_espera(tenant['id']))} en lista de espera: "
                          f"respondé *{primero['si'].lower()}* y se lo ofrezco a {primero['nombre']}.")
    else:
        tomar(tenant, turno)
        texto = f"Listo, el {cuando} es tuyo: no se lo ofrecemos a nadie. ¡Que descanses!"
    if len(pendientes) > 1:
        texto += f"\n\nTenés {len(pendientes) - 1} horario{'s' if len(pendientes) > 2 else ''} retenido{'s' if len(pendientes) > 2 else ''} más: está{'n' if len(pendientes) > 2 else ''} en el panel."
    return texto


def vencer_huecos(tenant: dict, ahora: datetime | None = None) -> int:
    """Lo llama el scheduler: si el profesional configuró un plazo y no decidió
    a tiempo, se aplica lo que eligió por defecto y se le avisa. Devuelve
    cuántos resolvió."""
    horas = tenant.get("hueco_vence_horas") or 0
    if not horas:
        return 0
    from datetime import timezone as _tzu
    ahora = ahora or datetime.now(_tzu.utc)
    resueltos = 0
    for turno in db.huecos_pendientes(tenant["id"]):
        desde = datetime.fromisoformat(turno["hueco_desde"]) if turno.get("hueco_desde") else ahora
        if ahora - desde < timedelta(hours=horas):
            continue
        cuando = scheduler.cuando_de(turno, tenant, _tz(tenant))
        if tenant.get("hueco_por_defecto") == "tomar":
            tomar(tenant, turno)
            texto = f"no decidiste sobre el turno cancelado del {cuando}: te lo dejamos a vos, no se le ofrece a nadie"
        else:
            ofrecido = liberar(tenant, turno)
            texto = f"no decidiste sobre el turno cancelado del {cuando}: lo liberamos para tus pacientes" + (
                "; se lo ofrecimos al primero de tu lista de espera" if ofrecido else "")
            primero = None if ofrecido else _proponer_a_espera(tenant, turno)
            if primero:
                texto += f"; respondé {primero['si']} y se lo ofrezco a {primero['nombre']}"
        scheduler.avisar_profesional(tenant, texto)
        resueltos += 1
    return resueltos


DIAS_ANTES_VUELTA = 2
_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def avisar_vuelta_si_toca(tenant: dict, ahora: datetime) -> int:
    """Pausa con fecha de vuelta: DIAS_ANTES_VUELTA días antes, desde las 10,
    a la lista de espera le avisamos que vuelve (una vez por fecha de vuelta,
    `tenants.vuelta_avisada`; se marca antes de mandar). Devuelve a cuántos
    se les pudo escribir."""
    from datetime import date
    hasta_txt = str(tenant.get("pausado_hasta") or "")[:10]
    if not tenant.get("pausado") or not hasta_txt or not tenant.get("slug") or ahora.hour < 10:
        return 0
    try:
        hasta = date.fromisoformat(hasta_txt)
    except ValueError:
        return 0
    faltan = (hasta - ahora.date()).days
    if not 0 < faltan <= DIAS_ANTES_VUELTA or tenant.get("vuelta_avisada") == hasta_txt:
        return 0
    espera = db.lista_de_espera(tenant["id"])
    db.guardar_ajustes(tenant["id"], {"vuelta_avisada": hasta_txt})
    if not espera:
        return 0
    numero = whatsapp.numero_de(tenant)
    if not numero:
        return 0
    cuando = "mañana" if faltan == 1 else f"el {_DIAS[hasta.weekday()]} {hasta.day}/{hasta.month}"
    link = f"{base_url()}/{tenant['slug']}"
    mensaje = (f"vuelvo a tomar turnos {cuando}. Como estás en mi lista de espera, ese día te escribimos "
               f"con el primer horario libre. Mi página: {link}")
    avisados = 0
    for p in espera:
        if config.MENSAJE_PROFESIONAL_TEMPLATE:
            ok = whatsapp.enviar_plantilla(numero, p["telefono"], config.MENSAJE_PROFESIONAL_TEMPLATE,
                                           config.RECORDATORIO_TEMPLATE_LANG, [tenant["nombre"], mensaje])
        else:
            ok = whatsapp.enviar_texto(numero, p["telefono"], f"Hola. *{tenant['nombre']}*: {mensaje}")
        avisados += 1 if ok else 0
    if avisados:
        scheduler.avisar_profesional(tenant, f"faltan {faltan} día{'s' if faltan != 1 else ''} para que termine tu "
                                             f"pausa: le avisamos a {avisados} de tu lista de espera que volvés {cuando}")
    return avisados


def reactivar_si_toca(tenant: dict, hoy) -> bool:
    """Pausa con fecha de vuelta: el día que llega, la página vuelve a tomar
    turnos, se le avisa al profesional y a la lista de espera se le ofrece el
    próximo horario libre (una vez por persona y por día). Lo llama el
    scheduler; devuelve True si reactivó."""
    hasta = tenant.get("pausado_hasta")
    if not tenant.get("pausado") or not hasta or hoy.isoformat() < str(hasta)[:10]:
        return False
    db.guardar_ajustes(tenant["id"], {"pausado": False, "pausado_hasta": None, "mensaje_pausa": None})
    tenant = {**tenant, "pausado": False}
    avisados = 0
    proximo = db.turnos_libres(tenant["id"], datetime.now(ZoneInfo("UTC")), limite=1)
    if proximo:
        avisados = ofrecer_a_toda_la_espera(tenant, proximo[0], hoy)
    scheduler.avisar_profesional(tenant, "terminó tu pausa: tu página vuelve a tomar turnos"
                                 + (f"; le avisamos a {avisados} de tu lista de espera" if avisados else ""))
    return True
