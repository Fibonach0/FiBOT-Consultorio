"""Qué le contesta FiBOT Consultorio a un paciente. Deliberadamente SIN estado
de conversación en memoria: interpretar "3" como "el turno #3 de la lista de
libres AHORA MISMO" (no de una lista que le mostramos hace rato) hace que todo
sea stateless — no hay sesión que se pierda si el proceso reinicia, y no hay
lista vieja que quede desincronizada. El costo es chico: si alguien tarda en
contestar y la lista cambió, en el peor caso reserva un horario distinto al
que vio, nunca uno inexistente (reservar_turno() sigue siendo atómico igual)."""
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
import db

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

logger = logging.getLogger("conversation")


class Respuesta(str):
    """Texto de siempre, con una versión para tocar (lista o botones de
    WhatsApp) en `interactivo`. Si la lista no sale, se manda el texto: por
    eso el texto sigue diciendo cómo hacerlo escribiendo."""
    interactivo: dict | None = None

    @classmethod
    def con(cls, texto: str, interactivo: dict | None) -> "Respuesta":
        r = cls(texto)
        r.interactivo = interactivo
        return r


DIAS_CORTOS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def _corto(dt_iso: str, tz: ZoneInfo) -> str:
    dt = datetime.fromisoformat(dt_iso).astimezone(tz)
    return f"{DIAS_CORTOS[dt.weekday()]} {dt.day:02d}/{dt.month:02d} {dt.hour:02d}:{dt.minute:02d}"

_SELECCION = re.compile(r"^\s*(\d{1,2})\b(.*)$")
_CANCELAR = re.compile(r"^cancelar(?:\s+(?:el\s+)?turno)?(?:\s+(?:de|a|para))?(?:\s+(.*))?$", re.IGNORECASE)


def _fmt(dt_iso: str, tz: ZoneInfo) -> str:
    dt = datetime.fromisoformat(dt_iso).astimezone(tz)
    return f"{DIAS[dt.weekday()]} {dt.day} de {MESES[dt.month - 1]}, {dt.hour:02d}:{dt.minute:02d}"


def _tz(tenant: dict) -> ZoneInfo:
    return ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")


def _menu(tenant: dict) -> str:
    opciones = ("• *turno* — ver horarios disponibles\n"
                "• *mi turno* — cuándo es tu próximo turno\n"
                "• *cancelar* — cancelar tu próximo turno\n\n"
                "Si escribís otra cosa, se la paso a tu profesional.\n\n")
    if tenant.get("whatsapp_phone_number_id"):
        # Número propio del profesional: el bot habla en su nombre.
        return (f"Hola, soy el asistente de *{tenant['nombre']}*. Puedo ayudarte con:\n\n"
                + opciones + "Para lo demás, escribile directo.")
    # Número compartido de la plataforma: no es "de" un profesional. Se
    # presenta como la plataforma y dice con quién son los turnos, porque
    # *turno* y *cancelar* operan sobre ese profesional.
    return (f"Hola, soy el asistente de turnos de *{config.PLATAFORMA_NOMBRE}*.\n"
            f"Tus turnos son con *{tenant['nombre']}*. Puedo ayudarte con:\n\n"
            + opciones + "Para otras consultas, escribile a tu profesional.")


def _pausado(tenant: dict) -> str:
    vuelve = f" Vuelve el {str(tenant['pausado_hasta'])[8:10]}/{str(tenant['pausado_hasta'])[5:7]}." if tenant.get("pausado_hasta") else ""
    return (f"*{tenant['nombre']}* no está tomando turnos nuevos por ahora.{vuelve}"
            + (f" {tenant['mensaje_pausa']}" if tenant.get("mensaje_pausa") else "")
            + " Si ya tenés un turno, sigue en pie.")


def _listar_libres(tenant: dict, dia: str | None = None) -> str:
    """Los próximos libres, numerados. Con `dia` («martes», «hoy», «mañana»)
    muestra sólo los de ese día pero con su número en la lista completa, así
    «2 Juan» reserva lo mismo que vio (la conversación no guarda estado)."""
    if tenant.get("pausado"):
        return _pausado(tenant)
    tz = _tz(tenant)
    ahora = datetime.now(timezone.utc)
    libres = db.turnos_libres(tenant["id"], ahora, limite=LIBRES_CON_DIA if dia else 8)
    if not libres:
        return f"Por ahora no hay horarios cargados con *{tenant['nombre']}*. Probá de nuevo más tarde."
    numerados = list(enumerate(libres, start=1))
    titulo = "Estos son los próximos horarios libres:"
    if dia:
        hoy = ahora.astimezone(tz).date()
        if dia in ("hoy", "manana"):
            fecha = hoy + timedelta(days=0 if dia == "hoy" else 1)
            del_dia = [(i, t) for i, t in numerados if datetime.fromisoformat(t["inicio"]).astimezone(tz).date() == fecha]
            nombre_dia = "hoy" if dia == "hoy" else "mañana"
        else:
            nro = _DIAS_NORMALES.index(dia)
            del_dia = [(i, t) for i, t in numerados if datetime.fromisoformat(t["inicio"]).astimezone(tz).weekday() == nro]
            nombre_dia = f"el {DIAS[nro]}"
        if del_dia:
            numerados, titulo = del_dia[:8], f"Horarios libres {nombre_dia}:"
        else:
            numerados, titulo = numerados[:8], f"No hay horarios libres {nombre_dia}. Estos son los próximos:"
    lineas = [f"{i}. {_fmt(t['inicio'], tz)}" for i, t in numerados]
    texto = (
        f"{titulo}\n\n"
        + "\n".join(lineas)
        + "\n\nRespondé con el número para reservar (podés sumar tu nombre, ej: *2 Juan Pérez*)."
    )
    lista = {"tipo": "lista", "boton": "Ver horarios", "titulo": "Horarios libres",
             "cuerpo": f"{titulo.rstrip(':')}. Tocá *Ver horarios* y elegí uno.",
             "filas": [{"id": f"reservar:{t['id']}", "title": _corto(t["inicio"], tz)} for _, t in numerados[:10]]}
    return Respuesta.con(texto, lista)


def _reservar(tenant: dict, telefono: str, indice: int, nombre: str | None) -> str:
    if tenant.get("pausado"):
        return _pausado(tenant)
    libres = db.turnos_libres(tenant["id"], datetime.now(timezone.utc), limite=max(indice, 8))
    if indice < 1 or indice > len(libres):
        return "Ese número no corresponde a ningún horario de la lista. Escribí *turno* para ver los disponibles."
    return _reservar_elegido(tenant, telefono, libres[indice - 1], indice, nombre)


def elegir_horario(tenant: dict, telefono: str, turno_id: str) -> str:
    """El paciente tocó un horario de la lista. Se busca en los libres de
    AHORA: si ya no está, se le dice (nunca se reserva algo que no vio)."""
    if tenant.get("pausado"):
        return _pausado(tenant)
    libres = db.turnos_libres(tenant["id"], datetime.now(timezone.utc), limite=LIBRES_CON_DIA)
    posicion = next((i for i, t in enumerate(libres, start=1) if t["id"] == turno_id), None)
    if not posicion:
        return "Justo se acaba de reservar ese horario. Escribí *turno* para ver los que quedan libres."
    return _reservar_elegido(tenant, telefono, libres[posicion - 1], posicion, None)


def _reservar_elegido(tenant: dict, telefono: str, elegido: dict, indice: int, nombre: str | None) -> str:
    tz = _tz(tenant)
    if not nombre and len(db.fichas_del_telefono(tenant["id"], telefono)) > 1:
        return f"Este WhatsApp es de más de un paciente. Escribí el número y el nombre, ej: *{indice} Lola*."
    if nombre and db.nombre_ambiguo(tenant["id"], telefono, nombre):
        return (f"Hay más de un paciente que se llama {nombre} con este WhatsApp. Escribí el nombre completo, "
                f"o pedile la reserva directamente a *{tenant['nombre']}*.")
    if db.reservar_turno(tenant["id"], elegido["id"], telefono, nombre):
        return (
            f"Listo, quedó reservado para el {_fmt(elegido['inicio'], tz)}. "
            "Te aviso antes por acá. Si necesitás cancelarlo, escribí *cancelar*."
        )
    return "Justo se acaba de reservar ese horario. Escribí *turno* para ver los que quedan libres."


def _cancelar(tenant: dict, telefono: str, nombre: str | None = None) -> str:
    """Nunca adivina de quién es: con varias personas en este teléfono, o con
    un nombre que no coincide, pregunta y no cancela nada."""
    tz = _tz(tenant)
    turnos = db.proximos_turnos_de(tenant["id"], telefono)
    if not turnos:
        return "No tenés ningún turno reservado a tu nombre."
    if nombre:
        elegidos = db.elegir_por_nombre(turnos, nombre)
        if not elegidos and len(turnos) == 1 and not turnos[0].get("paciente_nombre"):
            elegidos = turnos
    else:
        elegidos = turnos if len(turnos) == 1 else []
    if len(elegidos) != 1:
        detalle = "\n".join(f"• {t.get('paciente_nombre') or 'Sin nombre'}: {_fmt(t['inicio'], tz)}" for t in turnos)
        ejemplo = next((t["paciente_nombre"] for t in turnos if t.get("paciente_nombre")), None)
        if len(elegidos) > 1:
            inicio = f"Hay más de un turno que coincide con {nombre}."
        else:
            inicio = f"No encontré un turno de {nombre}." if nombre else "Este WhatsApp tiene turnos de más de un paciente."
        iguales = len({db.nombre_normal(t.get("paciente_nombre")) for t in turnos}) < len(turnos)
        if iguales:
            pedido = f" Para cancelar, escribile directamente a *{tenant['nombre']}*."
        else:
            pedido = f" Escribí *cancelar* y el nombre completo, por ejemplo *cancelar {ejemplo}*." if ejemplo else ""
        return f"{inicio}\n\n{detalle}\n\nTodavía no se canceló nada.{pedido}"
    turno = elegidos[0]
    import cancelaciones  # acá: cancelaciones importa scheduler, que no hace falta para el resto del bot
    if not cancelaciones.puede_modificar(turno, tenant):
        return (f"Tu turno del {_fmt(turno['inicio'], tz)} es en menos de {cancelaciones.horas_limite(tenant)} horas: "
                f"para cancelarlo, escribile directamente a *{tenant['nombre']}*. Todavía no se canceló nada.")
    db.cancelar_turno(tenant["id"], turno["id"], "paciente")
    devolucion = ""
    try:
        if turno.get("pago_web") == "aprobado":
            import pagos
            monto, hecho = pagos.al_cancelar(tenant, turno)
            texto = f"${monto:,}".replace(",", ".")
            devolucion = (f" Te devolvimos {texto} por Mercado Pago." if hecho else
                          f" Te corresponde la devolución de {texto}: te la hace *{tenant['nombre']}*." if monto else
                          " Según la política de cancelación, lo pagado no se devuelve.")
        cancelaciones.tras_cancelar(tenant, turno, "WhatsApp")
    except Exception:
        import logging
        logging.getLogger("conversation").exception("Falló el aviso de la cancelación del turno %s", turno["id"])
    de_quien = f" de {turno['paciente_nombre']}" if nombre and turno.get("paciente_nombre") else ""
    return (f"Cancelado el turno{de_quien} del {_fmt(turno['inicio'], tz)}.{devolucion} "
            "Escribí *turno* si querés reservar otro horario.")


# «Sí, confirmo» llega como botón de la plantilla de confirmación o tipeado.
_CONFIRMAR = {"si", "sí", "si confirmo", "sí confirmo", "si, confirmo", "sí, confirmo", "confirmo", "confirmar",
              "confirmado", "ok", "dale"}


def _base_url() -> str:
    if config.DOMINIO_PRINCIPAL:
        return f"https://{config.DOMINIO_PRINCIPAL}"
    from flask import has_request_context, request
    return request.host_url.rstrip("/") if has_request_context() else ""


def _confirmar(tenant: dict, telefono: str) -> str | None:
    """Respuesta a la confirmación de una reserva hecha en la web, con el link
    para agendarla. None si no había nada esperando confirmación."""
    turno = db.confirmar_proximo(tenant["id"], telefono)
    if not turno:
        return None
    texto = f"¡Listo! Quedó confirmado tu turno con *{tenant['nombre']}* el {_fmt(turno['inicio'], _tz(tenant))}."
    if turno.get("modalidad") == "virtual" and tenant.get("link_videollamada"):
        texto += f"\n\nEs por videollamada: {tenant['link_videollamada']}"
    if turno.get("token_calendario"):
        texto += (f"\n\nAgendalo en tu calendario (y desde ahí podés cambiarlo o cancelarlo): "
                  f"{_base_url()}/agendar/{turno['token_calendario']}")
        return texto
    return texto + "\n\nSi no podés ir, escribí *cancelar*."


def responder(tenant: dict, telefono: str, texto: str) -> str:
    limpio = (texto or "").strip()
    baja = limpio.lower()

    if baja.rstrip(".!") in _CONFIRMAR:
        confirmado = _confirmar(tenant, telefono)
        if confirmado:
            return confirmado
        if "confirm" in baja:
            # El botón «Sí, confirmo» del recordatorio o del aviso de cambio.
            return _confirmar_proximo_turno(tenant, telefono)

    m = _SELECCION.match(limpio)
    if m and _parece_nombre(m.group(2)):
        indice = int(m.group(1))
        nombre = m.group(2).strip(" ,.-") or None
        return _reservar(tenant, telefono, indice, nombre)

    normal = _normal(limpio)
    if normal in ("turno", "turnos", "horario", "horarios", "reservar", "quiero un turno"):
        return _listar_libres(tenant)

    m = _CANCELAR.match(limpio)
    if m:
        return _cancelar(tenant, telefono, (m.group(1) or "").strip() or None)

    if normal in _SALUDOS:
        return _menu(tenant)
    if _DEUDA.search(normal):
        return _que_debo(tenant, telefono)
    if _MI_TURNO.search(normal):
        return _mi_turno(tenant, telefono)
    if _QUIERE_CANCELAR.search(normal):
        return _antes_de_cancelar(tenant, telefono)
    if _REPROGRAMAR.search(normal):
        return _como_reprogramar(tenant, telefono)
    if _QUIERE_TURNO.search(normal):
        return _listar_libres(tenant, _dia_de(normal))
    if not normal or normal in _CORTESIA:
        return CORTESIA
    if _a_la_bandeja(tenant, telefono, limpio):
        return (f"Le pasé tu mensaje a *{tenant['nombre']}*: te va a responder cuando pueda.\n\n"
                "Si querés, escribí *turno* para ver horarios, *mi turno* para saber cuándo es el tuyo o *cancelar*.")
    return _menu(tenant)


# ---------- lo que no es una palabra clave ----------
# Reglas locales, a propósito: el texto de los pacientes no sale a ningún
# servicio externo (ver CLAUDE.md). Se compara sin tildes ni signos.

LIBRES_CON_DIA = 40
_DIAS_NORMALES = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
_SALUDOS = {"hola", "holis", "buenas", "buen dia", "buenos dias", "buenas tardes", "buenas noches", "hola buenas",
            "hola buen dia", "hola buenas tardes", "ayuda", "menu", "opciones", "info", "informacion"}
_CORTESIA = {"gracias", "muchas gracias", "mil gracias", "gracias!", "ok", "oka", "okey", "dale", "perfecto", "genial",
             "listo", "joya", "buenisimo", "barbaro", "de nada", "chau", "saludos", "igualmente", "ok gracias",
             "dale gracias", "perfecto gracias", "genial gracias", "listo gracias", "buenisimo gracias"}
CORTESIA = "¡De nada! Si necesitás algo, escribí *turno*, *mi turno* o *cancelar*."
_MI_TURNO = re.compile(r"\b(mis? (proximos? )?turnos?|cuando (es|tengo|era) (mi|el) (proximo )?turno|"
                       r"a que hora (es|tengo|era)|tengo turno|que dia (es|tengo|era))\b")
_DEUDA = re.compile(r"\b((que|cuanto) (le |te )?(debo|adeudo)|mi deuda|cuanto (es|era) la deuda|debo algo)\b")
_QUIERE_CANCELAR = re.compile(r"\b(no (voy a )?(poder|puedo|podre|podria) (ir|asistir|venir|ir mas)|"
                              r"no voy a (ir|asistir|venir)|cancelo|quiero cancelar|quisiera cancelar|"
                              r"necesito cancelar|tengo que cancelar|suspend\w*|anul\w*|dar de baja)\b")
_REPROGRAMAR = re.compile(r"\b(reprogram\w*|cambiar (el |mi )?(turno|horario|dia)|mover (el |mi )?turno|"
                          r"pasar (el |mi )?turno|otro (dia|horario))\b")
_QUIERE_TURNO = re.compile(r"\b(quiero|quisiera|necesito|puedo|podria|me das|pedir|sacar|reservar|agendar)\b.*\bturno|"
                           r"\b(hay|tenes|tienen|tiene|queda|quedan) (algun )?(lugar|turnos?|horarios?|disponibilidad)|"
                           r"\bdisponibilidad\b|\bhorarios? (libres|disponibles)\b|"
                           r"\bturnos? (para|el|la|esta|este|hoy|manana|lunes|martes|miercoles|jueves|viernes|sabado|domingo)\b")


def _normal(texto: str) -> str:
    sin_tildes = "".join(c for c in unicodedata.normalize("NFD", (texto or "").lower()) if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^a-z0-9ñ ]", " ", sin_tildes).split())


def _parece_nombre(resto: str) -> bool:
    """«2 Juan Pérez» es reservar; «10 minutos tarde» no."""
    resto = (resto or "").strip(" ,.-")
    return not re.search(r"\d", resto) and len(resto.split()) <= 4 and _normal(resto).split()[:1] not in (
        ["minutos"], ["min"], ["hs"], ["horas"], ["personas"], ["dias"])


def _dia_de(normal: str) -> str | None:
    palabras = normal.split()
    for d in _DIAS_NORMALES:
        if d in palabras:
            return d
    if "hoy" in palabras:
        return "hoy"
    if "manana" in palabras and "la manana" not in normal:
        return "manana"
    return None


def _mi_turno(tenant: dict, telefono: str) -> str:
    tz = _tz(tenant)
    turnos = db.proximos_turnos_de(tenant["id"], telefono)
    if not turnos:
        return f"No tenés turnos reservados con *{tenant['nombre']}*. Escribí *turno* para ver horarios libres."
    if len(turnos) == 1:
        t = turnos[0]
        texto = f"Tu próximo turno con *{tenant['nombre']}* es el {_fmt(t['inicio'], tz)}."
        if t.get("modalidad") == "virtual" and tenant.get("link_videollamada"):
            texto += f" Es por videollamada: {tenant['link_videollamada']}"
    else:
        texto = (f"Los próximos turnos con *{tenant['nombre']}* de este WhatsApp:\n\n"
                 + "\n".join(f"• {t.get('paciente_nombre') or 'Sin nombre'}: {_fmt(t['inicio'], tz)}" for t in turnos))
    return texto + "\n\nSi no podés ir, escribí *cancelar*."


def _pesos(v) -> str:
    return f"$ {int(v or 0):,}".replace(",", ".")


def _que_debo(tenant: dict, telefono: str) -> str:
    """El botón «¿Qué debo?» del recordatorio de pago: las sesiones sin pagar
    de este teléfono, con fecha y valor. Sólo lo de quien escribe."""
    tz = _tz(tenant)
    sin_pagar = [t for t in db.pendientes_de_pago_de(tenant["id"], telefono, datetime.now(timezone.utc)) if t.get("valor")]
    if not sin_pagar:
        return f"No tenés sesiones pendientes de pago con *{tenant['nombre']}*. Si te parece que hay un error, escribile directamente."
    varios = len({t.get("paciente_nombre") for t in sin_pagar}) > 1
    lineas = [f"• {_fmt(t['inicio'], tz)}" + (f" ({t.get('paciente_nombre') or 'Sin nombre'})" if varios else "")
              + f": {_pesos(t['valor'])}" for t in sin_pagar]
    return (f"Sesiones pendientes de pago con *{tenant['nombre']}*:\n\n" + "\n".join(lineas)
            + f"\n\nTotal: *{_pesos(sum(t['valor'] for t in sin_pagar))}*. Para pagar o si algo no coincide, escribile directamente.")


def _confirmar_proximo_turno(tenant: dict, telefono: str) -> str:
    """«Sí, confirmo» sin reserva web esperando: confirma el próximo turno de
    este teléfono. Con turnos de varias personas, el más cercano (son todos
    de quien escribe). Sin turno, lo dice."""
    turnos = db.proximos_turnos_de(tenant["id"], telefono)
    if not turnos:
        return f"No tenés turnos reservados con *{tenant['nombre']}*. Escribí *turno* para ver horarios libres."
    turno = min(turnos, key=lambda t: t["inicio"])
    try:
        db.marcar_confirmado(tenant["id"], turno["id"])
    except Exception:
        logger.warning("No se pudo marcar la confirmación", exc_info=True)
    texto = f"¡Gracias! Quedó confirmado tu turno con *{tenant['nombre']}* el {_fmt(turno['inicio'], _tz(tenant))}."
    if turno.get("modalidad") == "virtual" and tenant.get("link_videollamada"):
        texto += f"\n\nEs por videollamada: {tenant['link_videollamada']}"
    return texto + "\n\nSi al final no podés ir, escribí *cancelar*."


def _antes_de_cancelar(tenant: dict, telefono: str) -> str:
    """Frases como «no voy a poder ir»: no se cancela nada sin la palabra
    *cancelar*, pero se le dice cuál turno y cómo."""
    tz = _tz(tenant)
    turnos = db.proximos_turnos_de(tenant["id"], telefono)
    if not turnos:
        return f"No tenés turnos reservados con *{tenant['nombre']}*."
    if len(turnos) == 1:
        return (f"¿Querés cancelar tu turno del {_fmt(turnos[0]['inicio'], tz)}? "
                "Respondé *cancelar* y lo cancelo. Todavía no se canceló nada.")
    return (f"Tenés estos turnos con *{tenant['nombre']}*:\n\n"
            + "\n".join(f"• {t.get('paciente_nombre') or 'Sin nombre'}: {_fmt(t['inicio'], tz)}" for t in turnos)
            + "\n\nPara cancelar uno, escribí *cancelar* y el nombre. Todavía no se canceló nada.")


def _como_reprogramar(tenant: dict, telefono: str) -> str:
    """Con un solo turno: la lista de horarios para pasarlo (cada fila lleva
    el turno y el horario nuevo; lo resuelve app._mover_desde_whatsapp). Por
    escrito, el link de la web si lo tiene, o cancelar y pedir otro."""
    tz = _tz(tenant)
    turnos = db.proximos_turnos_de(tenant["id"], telefono)
    if not turnos:
        return f"No tenés turnos reservados con *{tenant['nombre']}*. Escribí *turno* para ver horarios libres."
    a_mano = ("Para cambiar el horario: escribí *cancelar* para liberar el que tenés y después *turno* para elegir otro.")
    if len(turnos) > 1:
        return a_mano + f" Si tu turno es muy pronto, escribile directamente a *{tenant['nombre']}*."
    turno = turnos[0]
    import cancelaciones
    if not cancelaciones.puede_modificar(turno, tenant):
        return (f"Tu turno del {_fmt(turno['inicio'], tz)} es en menos de {cancelaciones.horas_limite(tenant)} horas: "
                f"para cambiarlo, escribile directamente a *{tenant['nombre']}*.")
    if tenant.get("pausado"):
        return _pausado(tenant)
    libres = [t for t in db.turnos_libres(tenant["id"], datetime.now(timezone.utc), limite=10) if t["id"] != turno["id"]]
    if turno.get("token_calendario"):
        a_mano = f"Para cambiar tu turno del {_fmt(turno['inicio'], tz)}, elegí otro horario acá: {_base_url()}/agendar/{turno['token_calendario']}/cambiar"
    if not libres:
        return f"Por ahora no hay otros horarios libres con *{tenant['nombre']}*. Tu turno del {_fmt(turno['inicio'], tz)} sigue en pie."
    return Respuesta.con(a_mano, {
        "tipo": "lista", "boton": "Elegir horario", "titulo": "Horarios libres",
        "cuerpo": f"Tu turno es el {_fmt(turno['inicio'], tz)}. Tocá *Elegir horario* para pasarlo a otro.",
        "filas": [{"id": f"mover:{turno['id']}:{t['id']}", "title": _corto(t["inicio"], tz)} for t in libres[:10]]})


def _a_la_bandeja(tenant: dict, telefono: str, texto: str | None, tipo: str = "texto") -> bool:
    """Lo guarda para el panel del profesional. Le avisa por WhatsApp sólo si
    no tenía otros sin leer (sin nombre ni texto: está en el panel)."""
    try:
        previos = db.mensajes_sin_leer(tenant["id"])
        db.guardar_mensaje_paciente(tenant["id"], telefono, texto, tipo)
    except Exception:
        logger.exception("No se pudo guardar el mensaje del paciente (…%s)", (telefono or "")[-4:])
        return False
    if not previos:
        try:
            import scheduler
            scheduler.avisar_profesional(tenant, "un paciente te escribió por WhatsApp: el mensaje está en el panel, en «Mensajes»")
        except Exception:
            logger.warning("No se pudo avisar el mensaje nuevo", exc_info=True)
    return True


def responder_no_texto(tenant: dict, telefono: str, tipo: str, media_id: str | None = None) -> str | None:
    """Audios, fotos, PDF, stickers… Una foto o PDF de alguien con un pago por
    transferencia pendiente es el comprobante (lo mismo que subirlo en la web).
    None = no se contesta (una reacción con emoji)."""
    if tipo == "reaction":
        return None
    if tipo == "sticker":
        return CORTESIA
    if tipo in ("image", "document"):
        import pagos
        pago = None
        try:
            if "transferencia" in pagos.metodos(tenant):
                pago = db.pago_esperando_de(tenant["id"], telefono)
        except Exception:
            logger.warning("No se pudo buscar el pago pendiente", exc_info=True)
        if pago and media_id:
            import whatsapp
            link = f"{_base_url()}/pago/{pago['token']}"
            crudo = whatsapp.bajar_media(media_id, pagos.COMPROBANTE_MAX)
            if crudo is None:
                return f"No pude abrir el archivo (hasta 2 MB). Probá de nuevo o subilo acá: {link}"
            error = pagos.subir_comprobante(tenant, pago, crudo)
            if error:
                return f"{error} Probá de nuevo o subilo acá: {link}"
            return f"Recibí tu comprobante. Cuando *{tenant['nombre']}* lo valide, te confirmo el turno."
        _a_la_bandeja(tenant, telefono, None, "imagen" if tipo == "image" else "documento")
        return f"Recibí el archivo y le avisé a *{tenant['nombre']}*. Si querés agregar algo, escribilo acá."
    if tipo in ("audio", "video"):
        _a_la_bandeja(tenant, telefono, None, tipo)
        return f"Por ahora leo sólo texto: ¿me lo escribís? Así se lo paso a *{tenant['nombre']}*."
    return "Por ahora leo sólo texto: ¿me lo escribís?"
