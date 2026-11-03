"""Qué pasa después de que un PACIENTE cancela (por la web o por WhatsApp).

Un solo lugar para las dos puertas de entrada, así se comportan igual:

- Con `tenants.al_cancelar = 'liberar'` (lo de siempre) el horario vuelve a
  estar disponible enseguida, se le avisa al profesional y, si tiene activado
  `ofrecer_espera_auto`, se le ofrece al primero de la lista de espera.
- Con `'preguntar'` el horario queda retenido (`turnos.hueco = 'pendiente'`):
  libre pero fuera de `turnos_disponibles`, nadie lo ve. El profesional decide
  desde el panel o contestando por WhatsApp «liberar» / «me lo tomo».
  «Me lo tomo» lo deja en `'propio'` (sigue sin ofrecerse).

Los avisos al profesional nunca llevan nombres de pacientes.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import db
import scheduler
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


def tras_cancelar(tenant: dict, turno: dict, por: str) -> None:
    """Después de que el paciente canceló `turno` (ya liberado). `por` es
    «la web» o «WhatsApp», para el aviso."""
    cuando = scheduler.cuando_de(turno, tenant, _tz(tenant))
    if tenant.get("al_cancelar") == "preguntar":
        db.marcar_hueco(tenant["id"], turno["id"], "pendiente")
        scheduler.avisar_profesional(
            tenant, f"un paciente canceló por {por} el turno del {cuando}. Lo retuve: respondé LIBERAR para que "
                    f"otro lo pueda tomar o ME LO TOMO para quedártelo (también desde el panel)")
        return
    esperando = len(db.lista_de_espera(tenant["id"]))
    ofrecido = ofrecer_a_espera(tenant, turno)
    texto = f"un paciente canceló por {por} el turno del {cuando}"
    if ofrecido:
        texto += "; se lo ofrecimos al primero de tu lista de espera"
    elif esperando:
        texto += f"; tenés {esperando} en lista de espera"
    scheduler.avisar_profesional(tenant, texto)


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
    entonces el mensaje sigue su camino normal."""
    palabra = " ".join((texto or "").lower().replace(",", " ").replace(".", " ").split())
    if palabra not in LIBERAR and palabra not in TOMAR:
        return None
    tenant = db.tenant_por_aviso(telefono)
    if not tenant:
        return None
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
        tomar(tenant, turno)
        texto = f"Listo, el {cuando} es tuyo: no se lo ofrecemos a nadie. ¡Que descanses!"
    if len(pendientes) > 1:
        texto += f"\n\nTenés {len(pendientes) - 1} horario{'s' if len(pendientes) > 2 else ''} retenido{'s' if len(pendientes) > 2 else ''} más: está{'n' if len(pendientes) > 2 else ''} en el panel."
    return texto

