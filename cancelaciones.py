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
import hashlib
import hmac
import logging
import threading
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


MINUTOS_GUARDADO = 60
MAX_EN_AVISO = 8  # el aviso va en un parámetro de plantilla: que no se pase de largo
HORAS_ENTRE_OFERTAS = 24  # a quien se le guardó un horario no se le pasa otro hasta el día siguiente
MARGEN_PASAR = timedelta(minutes=30)  # un horario que empieza antes de esto ya no se pasa


def _inicio(turno: dict) -> datetime:
    return datetime.fromisoformat(str(turno["inicio"]).replace("Z", "+00:00"))


def token_guardado(turno_id: str, telefono: str) -> str:
    """Credencial del link «quiero otro»: sólo quien recibió el horario
    guardado la tiene. No se guarda: se recalcula."""
    clave = f"guardado:{turno_id}:{db._ultimos10(telefono)}".encode()
    return hmac.new(config.FLASK_SECRET_KEY.encode(), clave, hashlib.sha256).hexdigest()[:24]


def _programar(segundos: float, tenant_id: str) -> None:
    """Apenas vence un horario guardado, pasarlo al siguiente (sin esperar el
    tick de 10 min, que queda de respaldo si el proceso se reinicia)."""
    def correr():
        try:
            tenant = db.get_tenant_by_id(tenant_id)
            if tenant:
                pasar_vencidos(tenant)
        except Exception:
            logger.exception("Fallo pasando horarios guardados vencidos de %s", tenant_id)
    t = threading.Timer(max(segundos, 1) + 5, correr)
    t.daemon = True
    t.start()


def guardar_y_ofrecer(tenant: dict, persona: dict, turno: dict, ahora: datetime, mandar: bool = True) -> datetime | None:
    """Le guarda `turno` a `persona` `MINUTOS_GUARDADO` (o hasta que empiece)
    y se lo ofrece con el link directo. Devuelve hasta cuándo, o None si no se
    le pudo escribir."""
    tz = _tz(tenant)
    fin = min(ahora + timedelta(minutes=MINUTOS_GUARDADO), _inicio(turno))
    db.guardar_turno_para(tenant["id"], turno["id"], persona["telefono"], fin)
    db.marcar_oferta(tenant["id"], persona["id"], {
        "ofrecido_en": ahora.astimezone(tz).date().isoformat(), "ofrecido_turno": turno["id"],
        "ofrecido_hasta": fin.isoformat(), "ofrecido_estado": "esperando"})
    _programar((fin - ahora).total_seconds(), tenant["id"])
    if not mandar:
        return fin
    link = f"{base_url()}/{tenant['slug']}/reservar/{turno['id']}?g={token_guardado(turno['id'], persona['telefono'])}"
    texto = (f"{scheduler.cuando_de(turno, tenant, tz)} (te lo guardamos hasta las {fin.astimezone(tz):%H:%M}; "
             f"si no te sirve, desde el link pedís otro)")
    return fin if ofrecer_horario(tenant, persona["telefono"], texto, link) else None


def ofrecer_repartido(tenant: dict, hoy) -> tuple[list[tuple[str, str]], bool, int]:
    """Al terminar una pausa: a cada persona de la lista de espera (en orden de
    anotación) le ofrece un horario libre DISTINTO y se lo GUARDA
    `MINUTOS_GUARDADO` (nadie más lo ve ni lo reserva); el link lleva directo a
    ese horario. Si hay más personas que horarios, las que sobran quedan en
    cola: apenas vence uno sin reservar, `pasar_vencidos` se lo ofrece a la
    siguiente. Con varios tipos de turno no se guarda (el paciente elige el
    tipo en la página, donde un guardado no se ve): a cada uno un horario
    distinto, repitiendo desde el primero si faltan. Saltea a quien hoy ya
    recibió un ofrecimiento. Devuelve ([(nombre, cuándo)], si se guardaron,
    cuántos quedaron en cola), para el profesional."""
    if not tenant.get("slug"):
        return [], False, 0
    espera = [p for p in db.lista_de_espera(tenant["id"]) if p.get("ofrecido_en") != hoy.isoformat()]
    if not espera:
        return [], False, 0
    ahora = datetime.now(ZoneInfo("UTC"))
    libres = db.turnos_libres(tenant["id"], ahora, limite=len(espera))
    if not libres:
        return [], False, 0
    tz = _tz(tenant)
    ofrecidos = []
    if len(tenant.get("tipos_turno") or []) > 1:
        for i, p in enumerate(espera):
            turno = libres[i % len(libres)]
            db.marcar_ofrecido(tenant["id"], p["id"], hoy.isoformat())
            cuando = scheduler.cuando_de(turno, tenant, tz)
            if ofrecer_horario(tenant, p["telefono"], cuando, f"{base_url()}/{tenant['slug']}"):
                ofrecidos.append((p.get("nombre") or "alguien", cuando))
        return ofrecidos, False, 0
    for p, turno in zip(espera, libres):
        if guardar_y_ofrecer(tenant, p, turno, ahora):
            ofrecidos.append((p.get("nombre") or "alguien", scheduler.cuando_de(turno, tenant, tz)))
    return ofrecidos, True, max(len(espera) - len(libres), 0)


def texto_reparto(ofrecidos: list[tuple[str, str]], guardados: bool, en_cola: int = 0) -> str:
    """Para el profesional (con el número vinculado): a quién se le ofreció qué."""
    detalle = "; ".join(f"{nombre}: {cuando}" for nombre, cuando in ofrecidos[:MAX_EN_AVISO])
    if len(ofrecidos) > MAX_EN_AVISO:
        detalle += f" y {len(ofrecidos) - MAX_EN_AVISO} más (en el panel)"
    texto = f"terminó tu pausa: tu página vuelve a tomar turnos. A tu lista de espera le ofrecimos — {detalle}"
    if guardados:
        texto += f". Cada horario queda guardado {MINUTOS_GUARDADO} min para esa persona"
    if en_cola:
        texto += (f". {en_cola} más quedan en cola: apenas venza un horario sin reservar, "
                  f"se lo ofrecemos al siguiente")
    return texto


def _ofrecido_hace_poco(persona: dict, ahora: datetime) -> bool:
    """Se le guardó un horario en las últimas `HORAS_ENTRE_OFERTAS`. Sin esto,
    un guardado de las 23:30 que vence a las 00:30 volvía a la misma persona
    (su `ofrecido_en` ya era «ayer»)."""
    hasta = persona.get("ofrecido_hasta")
    if not hasta:
        return False
    fin = datetime.fromisoformat(str(hasta).replace("Z", "+00:00"))
    return fin - timedelta(minutes=MINUTOS_GUARDADO) > ahora - timedelta(hours=HORAS_ENTRE_OFERTAS)


def _siguiente(tenant: dict, ahora: datetime) -> dict | None:
    """El primero de la lista (por anotación) sin un horario guardado ahora,
    que hoy no recibió ofrecimiento ni tuvo uno guardado en las últimas 24 h."""
    hoy = ahora.astimezone(_tz(tenant)).date().isoformat()
    return next((p for p in db.lista_de_espera(tenant["id"])
                 if p.get("ofrecido_estado") != "esperando" and p.get("ofrecido_en") != hoy
                 and not _ofrecido_hace_poco(p, ahora)), None)


def pasar_al_siguiente(tenant: dict, turno: dict | None, ahora: datetime, excluir: str | None = None) -> str | None:
    """Un horario guardado que quedó libre (venció o lo rechazaron) va al
    siguiente de la lista. Si ese horario ya no sirve (lo tomaron o empieza ya),
    el próximo libre. Devuelve el nombre de a quién se le ofreció."""
    persona = _siguiente(tenant, ahora)
    if not persona or not tenant.get("slug") or tenant.get("pausado"):
        return None
    if not (turno and turno.get("estado") == "libre" and not turno.get("hueco") and turno["id"] != excluir
            and _inicio(turno) > ahora + MARGEN_PASAR and not db.guardado_para_otro(turno, "", ahora)):
        libres = [t for t in db.turnos_libres(tenant["id"], ahora + MARGEN_PASAR, limite=2) if t["id"] != excluir]
        if not libres:
            return None
        turno = libres[0]
    if not guardar_y_ofrecer(tenant, persona, turno, ahora):
        return None
    return persona.get("nombre") or "alguien"


def pasar_vencidos(tenant: dict, ahora: datetime | None = None) -> int:
    """Lo llaman el temporizador del guardado y el tick: cada ofrecimiento
    guardado que venció queda «reservó» o «venció»; los vencidos pasan al
    siguiente de la lista. Devuelve cuántos pasó."""
    ahora = ahora or datetime.now(ZoneInfo("UTC"))
    pasados = 0
    for p in db.lista_de_espera(tenant["id"]):
        if p.get("ofrecido_estado") != "esperando" or not p.get("ofrecido_hasta"):
            continue
        turno = db.turno_por_id(tenant["id"], p["ofrecido_turno"]) if p.get("ofrecido_turno") else None
        if reservo(turno, p):
            db.marcar_oferta(tenant["id"], p["id"], {"ofrecido_estado": "reservo"})
            continue
        if datetime.fromisoformat(str(p["ofrecido_hasta"]).replace("Z", "+00:00")) > ahora:
            continue
        db.marcar_oferta(tenant["id"], p["id"], {"ofrecido_estado": "vencio"})
        a_quien = pasar_al_siguiente(tenant, turno, ahora)
        if a_quien:
            pasados += 1
            scheduler.avisar_profesional(
                tenant, "venció un horario guardado de tu lista de espera sin reservar; se lo ofrecimos al siguiente",
                con_nombre=f"a {p.get('nombre') or 'alguien'} se le venció el horario guardado sin reservar; "
                           f"se lo ofrecimos a {a_quien}")
    return pasados


def reservo(turno: dict | None, persona: dict) -> bool:
    return bool(turno and turno.get("estado") == "reservado"
                and db._ultimos10(turno.get("paciente_telefono")) == db._ultimos10(persona.get("telefono")))


def quiero_otro(tenant: dict, turno: dict, ahora: datetime | None = None) -> tuple[dict | None, datetime | None]:
    """«No me sirve, quiero otro» desde el link del horario guardado (el token
    ya se verificó): suelta ese horario, que pasa al siguiente de la lista, y
    le guarda otro a la misma persona (el siguiente en el tiempo, o si no hay,
    cualquier otro). Devuelve (el nuevo, hasta cuándo) o (None, None)."""
    ahora = ahora or datetime.now(ZoneInfo("UTC"))
    persona = next((p for p in db.lista_de_espera(tenant["id"])
                    if db._ultimos10(p["telefono"]) == db._ultimos10(turno.get("guardado_tel"))), None)
    db.soltar_guardado(tenant["id"], turno["id"])
    libres = [t for t in db.turnos_libres(tenant["id"], ahora + MARGEN_PASAR, limite=50) if t["id"] != turno["id"]]
    nuevo = next((t for t in libres if _inicio(t) > _inicio(turno)), libres[0] if libres else None)
    fin = None
    if persona:
        if nuevo:
            fin = guardar_y_ofrecer(tenant, persona, nuevo, ahora, mandar=False)
        else:
            db.marcar_oferta(tenant["id"], persona["id"], {"ofrecido_estado": "otro"})
    pasar_al_siguiente(tenant, {**turno, "guardado_tel": None, "guardado_hasta": None}, ahora, excluir=nuevo and nuevo["id"])
    return (nuevo, fin) if fin else (None, None)


def sacar_de_espera_al_reservar(tenant: dict, turno: dict | None, telefono: str, nombre: str | None = None,
                                avisar: bool = True) -> list[str]:
    """Quien reserva sale solo de la lista de espera (mismo teléfono, últimos
    10 dígitos; si los dos tienen nombre, tiene que ser la misma persona: un
    WhatsApp puede ser de una familia). Si tenía guardado OTRO horario, ese
    pasa al siguiente de la cola. Una reserva web esperando pago todavía no
    cuenta: se saca al aprobarse (`pagos.aprobar`). Con `avisar`, le cuenta al
    profesional. Devuelve los nombres que sacó."""
    if turno and turno.get("pago_web") in ("esperando", "comprobante"):
        return []
    sacados = []
    ahora = datetime.now(ZoneInfo("UTC"))
    for p in db.lista_de_espera(tenant["id"]):
        if db._ultimos10(p.get("telefono")) != db._ultimos10(telefono) or not db._ultimos10(telefono):
            continue
        if nombre and p.get("nombre") and not db.mismo_nombre(nombre, p["nombre"]):
            continue
        db.quitar_de_espera(tenant["id"], p["id"])
        sacados.append(p.get("nombre") or nombre or "alguien")
        otro = p.get("ofrecido_turno")
        if p.get("ofrecido_estado") == "esperando" and otro and (not turno or otro != turno.get("id")):
            guardado = db.turno_por_id(tenant["id"], otro)
            if guardado and guardado.get("estado") == "libre":
                db.soltar_guardado(tenant["id"], otro)
                pasar_al_siguiente(tenant, {**guardado, "guardado_tel": None, "guardado_hasta": None}, ahora)
    if sacados and avisar:
        cuando = f" el {scheduler.cuando_de(turno, tenant, _tz(tenant))}" if turno else ""
        scheduler.avisar_profesional(
            tenant, f"alguien de tu lista de espera reservó{cuando}: lo sacamos de la lista",
            con_nombre=f"{', '.join(sacados)} reservó{cuando}: lo sacamos de tu lista de espera")
    return sacados


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
    propio = " ".join((tenant.get("mensaje_vuelta") or "").split())
    if propio:
        mensaje = f"{propio.rstrip('.')}. Mi página: {link}"
    else:
        mensaje = (f"vuelvo a tomar turnos {cuando}. Como estás en mi lista de espera, ese día te escribimos "
                   f"con un horario libre. Mi página: {link}")
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
    turnos, se le avisa al profesional y a cada persona de la lista de espera
    se le ofrece un horario libre distinto (`ofrecer_repartido`). Lo llama el
    scheduler; devuelve True si reactivó."""
    hasta = tenant.get("pausado_hasta")
    if not tenant.get("pausado") or not hasta or hoy.isoformat() < str(hasta)[:10]:
        return False
    db.guardar_ajustes(tenant["id"], {"pausado": False, "pausado_hasta": None, "mensaje_pausa": None})
    tenant = {**tenant, "pausado": False}
    ofrecidos, guardados, en_cola = ofrecer_repartido(tenant, hoy)
    scheduler.avisar_profesional(tenant, "terminó tu pausa: tu página vuelve a tomar turnos"
                                 + (f"; le avisamos a {len(ofrecidos)} de tu lista de espera" if ofrecidos else ""),
                                 con_nombre=texto_reparto(ofrecidos, guardados, en_cola) if ofrecidos else None)
    return True
