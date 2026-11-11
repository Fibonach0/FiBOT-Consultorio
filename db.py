"""Capa de datos. Todo lo que toca la tabla `turnos` o `tenants` pasa por acá,
y TODO recibe tenant_id explícito — no hay una sola función que pueda tocar
datos de un tenant sin que se lo pidan a propósito. Esa disciplina es la que
evita que un bug cruce datos de un profesional a otro."""
import functools
import hashlib
import secrets
import sys
from concurrent.futures import ThreadPoolExecutor
import unicodedata
from datetime import datetime, timedelta, timezone

from supabase import create_client

import config

_client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_ROLE_KEY)


# --- Precarga en paralelo (el panel) -------------------------------------
# El panel hace ~35 consultas; en fila, a ~100 ms cada una contra Supabase,
# son 4-5 s (la pantalla negra al abrir la app). `precargar` lanza juntas las
# que sólo dependen de la cuenta y deja los resultados en `flask.g`; cada
# función marcada con @_precargable toma SU resultado UNA vez, con los mismos
# argumentos. Si una falló o no se precargó, consulta como siempre.
_NADA = object()
PRECARGA_HILOS = 12
_hilos = ThreadPoolExecutor(max_workers=PRECARGA_HILOS, thread_name_prefix="precarga")


def _precargable(fn):
    @functools.wraps(fn)
    def envuelta(*args, **kwargs):
        if kwargs:
            return fn(*args, **kwargs)
        guardado = _tomar_precarga(fn.__name__, args)
        return fn(*args) if guardado is _NADA else guardado
    envuelta.sin_precarga = fn
    return envuelta


def _tomar_precarga(nombre: str, args: tuple):
    from flask import g, has_request_context
    if not has_request_context() or "_precarga" not in g:
        return _NADA
    try:
        return g._precarga.pop((nombre, args), _NADA)
    except TypeError:  # un argumento que no sirve de clave (una lista): nunca se precargó
        return _NADA


def precargar(pedidos: list[tuple]) -> None:
    """pedidos = [(nombre de función de este módulo, (args...)), ...]."""
    from flask import g
    modulo = sys.modules[__name__]
    futuros = {}
    for nombre, args in pedidos:
        fn = getattr(modulo, nombre)
        futuros[(nombre, args)] = _hilos.submit(getattr(fn, "sin_precarga", fn), *args)
    listos = g.setdefault("_precarga", {})
    for clave, futuro in futuros.items():
        try:
            listos[clave] = futuro.result(timeout=20)
        except Exception:
            pass  # la función consulta sola cuando la llamen, y ahí se ve el error


def get_tenant_by_phone_number_id(phone_number_id: str) -> dict | None:
    """Resuelve qué profesional es dueño de este número de WhatsApp. Esto es
    lo que permite que UN webhook sirva a TODOS los tenants: Meta manda el
    phone_number_id en cada mensaje, y acá lo mapeamos al tenant dueño."""
    res = (
        _client.table("tenants")
        .select("*")
        .eq("whatsapp_phone_number_id", phone_number_id)
        .eq("activo", True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def tenant_por_paciente(telefono: str) -> dict | None:
    """Resuelve el tenant a partir del teléfono del PACIENTE, no del número al
    que le escribió.

    Hace falta sólo en el modo "sin número propio": ahí varios profesionales
    comparten el número de la plataforma, así que el phone_number_id que manda
    Meta ya no alcanza para saber de quién es el mensaje. Se busca el turno más
    reciente de ese teléfono y se devuelve su dueño.

    Es la ÚNICA función de este archivo que no recibe tenant_id, y es a
    propósito: es justamente la que lo averigua. Todo lo que venga después sí
    lo recibe explícito. Si un paciente tiene turnos con dos profesionales
    distintos en el número compartido, gana el más reciente — y por eso el
    modo compartido es para pilotos, no para escalar.
    """
    res = (
        _client.table("turnos")
        .select("tenant_id")
        .eq("paciente_telefono", telefono)
        .order("inicio", desc=True)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    return get_tenant_by_id(res.data[0]["tenant_id"])


def get_tenant_by_panel_user(panel_user: str) -> dict | None:
    res = (
        _client.table("tenants")
        .select("*")
        .eq("panel_user", panel_user)
        .eq("activo", True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def get_tenant_by_id(tenant_id: str) -> dict | None:
    res = _client.table("tenants").select("*").eq("id", tenant_id).limit(1).execute()
    return res.data[0] if res.data else None


def get_tenant_by_slug(slug: str) -> dict | None:
    """Resuelve la página pública de reserva (consultorio.fibot.ar/<slug>)."""
    res = (
        _client.table("tenants")
        .select("*")
        .eq("slug", slug)
        .eq("activo", True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def turnos_libres(tenant_id: str, desde: datetime, limite: int = 8) -> list[dict]:
    """Próximos turnos libres de ESTE tenant, a partir de `desde`. Sale de la
    vista turnos_disponibles (migrations/005): no incluye los horarios que se
    pisan con una sesión reservada."""
    res = (
        _client.table("turnos_disponibles")
        .select("*")
        .eq("tenant_id", tenant_id)
        .gte("inicio", desde.isoformat())
        .order("inicio")
        .limit(limite)
        .execute()
    )
    return res.data


def _ultimos10(telefono) -> str:
    return "".join(ch for ch in str(telefono or "") if ch.isdigit())[-10:]


def guardado_para_otro(turno: dict, telefono: str, ahora: datetime | None = None) -> bool:
    """True si el horario está guardado (todavía vigente) para OTRO teléfono.
    Se comparan los últimos 10 dígitos: 549… y 54 9 11… son el mismo."""
    hasta = turno.get("guardado_hasta")
    if not hasta or not turno.get("guardado_tel"):
        return False
    if datetime.fromisoformat(str(hasta).replace("Z", "+00:00")) <= (ahora or datetime.now(timezone.utc)):
        return False
    return _ultimos10(turno["guardado_tel"]) != _ultimos10(telefono)


def guardar_turno_para(tenant_id: str, turno_id: str, telefono: str, hasta: datetime) -> None:
    _client.table("turnos").update({"guardado_tel": telefono, "guardado_hasta": hasta.isoformat()}).eq(
        "tenant_id", tenant_id).eq("id", turno_id).eq("estado", "libre").execute()


def soltar_guardado(tenant_id: str, turno_id: str) -> None:
    _client.table("turnos").update({"guardado_tel": None, "guardado_hasta": None}).eq(
        "tenant_id", tenant_id).eq("id", turno_id).execute()


def turno_por_id(tenant_id: str, turno_id: str) -> dict | None:
    res = (
        _client.table("turnos")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("id", turno_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def _se_pisa(tenant_id: str, inicio: str, fin: str, excluir_id: str) -> bool:
    """True si ya hay otra sesión reservada de ESTE tenant que se superpone con
    [inicio, fin). Una sesión que termina justo cuando empieza otra no se pisa."""
    res = (
        _client.table("turnos")
        .select("id")
        .eq("tenant_id", tenant_id)
        .eq("estado", "reservado")
        .neq("id", excluir_id)
        .lt("inicio", fin)
        .gt("fin", inicio)
        .limit(1)
        .execute()
    )
    return bool(res.data)


def _esta_bloqueado(tenant_id: str, inicio: str, fin: str) -> bool:
    return bloqueo_en(tenant_id, inicio, fin) is not None


def bloqueo_en(tenant_id: str, inicio: str, fin: str) -> dict | None:
    res = (
        _client.table("bloqueos")
        .select("*")
        .eq("tenant_id", tenant_id)
        .lt("desde", fin)
        .gt("hasta", inicio)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def clave_de(fila: dict) -> str:
    """Identidad de una ficha. El teléfono sólo no alcanza: dos hermanos pueden
    usar el WhatsApp del mismo adulto."""
    ficha = fila.get("ficha")
    return f"{fila['paciente_telefono']}~{ficha}" if ficha else fila["paciente_telefono"]


# Paciente agendado sin WhatsApp: el sistema identifica cada ficha por
# teléfono, así que se le inventa una clave que no es un número. Nunca son
# todos dígitos, y whatsapp.py no manda nada a algo que no lo sea.
SIN_WHATSAPP = "sin-"
TABLAS_DEL_PACIENTE = ("turnos", "pacientes", "paciente_personas", "paciente_fechas", "cancelaciones",
                       "paciente_tests", "lecturas", "cuestionarios", "informes", "entregas",
                       "paciente_bitacora")


def tiene_whatsapp(telefono: str | None) -> bool:
    return bool(telefono) and telefono.isdigit()


def clave_sin_whatsapp(tenant_id: str, nombre: str, otra_persona: bool = False) -> str:
    """La clave de un paciente sin WhatsApp. Si ya hay uno sin WhatsApp con el
    mismo nombre completo, es ese (el profesional le está dando otro turno).
    Sólo nombre idéntico: sin número no hay nada más que confirme que es la
    misma persona, y «Juan» no tiene por qué ser «Juan Pérez»."""
    if not otra_persona:
        sesiones = (_client.table("turnos").select("paciente_telefono,paciente_nombre,inicio")
                    .eq("tenant_id", tenant_id).eq("estado", "reservado")
                    .ilike("paciente_telefono", SIN_WHATSAPP + "%").order("inicio", desc=True).execute().data)
        guardadas = (_client.table("pacientes").select("paciente_telefono,nombre")
                     .eq("tenant_id", tenant_id).ilike("paciente_telefono", SIN_WHATSAPP + "%").execute().data)
        for clave, otro in [*((t["paciente_telefono"], t.get("paciente_nombre")) for t in sesiones),
                            *((p["paciente_telefono"], p.get("nombre")) for p in guardadas)]:
            if _palabras(otro) and _palabras(otro) == _palabras(nombre):
                return clave
    return SIN_WHATSAPP + secrets.token_hex(5)


def cargar_whatsapp(tenant_id: str, clave: str, telefono: str, nombre: str | None) -> str | None:
    """Le pone número a un paciente que se agendó sin WhatsApp: todo lo suyo
    (sesiones, ficha, notas, tests…) pasa al número nuevo. Si ese número ya lo
    usa otro paciente, queda en una ficha aparte, como cuando dos personas
    comparten WhatsApp. Devuelve la clave nueva, o None si no se pudo."""
    viejo, _ = partir_clave(clave)
    if not viejo.startswith(SIN_WHATSAPP) or not tiene_whatsapp(telefono):
        return None
    filas = fichas_del_telefono(tenant_id, telefono)
    ficha = _ficha_libre(filas, nombre) if filas else ""
    for tabla in TABLAS_DEL_PACIENTE:
        (_client.table(tabla).update({"paciente_telefono": telefono, "ficha": ficha})
         .eq("tenant_id", tenant_id).eq("paciente_telefono", viejo).execute())
    return f"{telefono}~{ficha}" if ficha else telefono


def partir_clave(clave: str) -> tuple[str, str]:
    telefono, _, ficha = clave.partition("~")
    return telefono, ficha


def _palabras(nombre: str | None) -> list[str]:
    sin_acentos = unicodedata.normalize("NFD", nombre or "")
    limpio = "".join(c for c in sin_acentos if not unicodedata.combining(c)).lower()
    return [p for p in ("".join(c for c in crudo if c.isalnum()) for crudo in limpio.split()) if p]


def nombre_normal(nombre: str | None) -> str:
    return " ".join(_palabras(nombre))


def mismo_nombre(uno: str | None, otro: str | None) -> bool:
    """«Nina» y «Nina Acosta» pueden ser la misma persona; «Juan Pablo» y «Juan
    Ignacio», no: el nombre más corto tiene que estar entero en el más largo."""
    a, b = _palabras(uno), _palabras(otro)
    if not a or not b or a[0] != b[0]:
        return False
    corto, largo = sorted((a, b), key=len)
    return set(corto) <= set(largo)


def elegir_por_nombre(filas: list[dict], nombre: str | None) -> list[dict]:
    """Las filas que pueden ser esa persona. Si el nombre completo coincide con
    alguna, esas ganan. Más de una significa que el nombre no alcanza."""
    posibles = [f for f in filas if mismo_nombre(f.get("paciente_nombre"), nombre)]
    exactas = [f for f in posibles if _palabras(f.get("paciente_nombre")) == _palabras(nombre)]
    return exactas or posibles


def fichas_del_telefono(tenant_id: str, telefono: str) -> list[dict]:
    """Una fila por persona que usa este teléfono: primero las que tienen
    sesiones, la más reciente antes. Después, las que se quedaron sin ninguna:
    siguen siendo de alguien, aunque ya no aparezcan en la agenda."""
    res = (
        _client.table("turnos")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("estado", "reservado")
        .eq("paciente_telefono", telefono)
        .order("inicio", desc=True)
        .execute()
    )
    vistas: dict[str, dict] = {}
    for t in res.data:
        vistas.setdefault(t.get("ficha") or "", {**t, "con_sesiones": True})
    guardadas = (
        _client.table("pacientes")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("paciente_telefono", telefono)
        .execute()
    )
    for p in guardadas.data:
        vistas.setdefault(p.get("ficha") or "", {"ficha": p.get("ficha") or "", "paciente_nombre": p.get("nombre"),
                                                 "con_sesiones": False})
    return list(vistas.values())


def ficha_para(tenant_id: str, telefono: str, nombre: str | None) -> tuple[str, bool]:
    """A qué ficha va una reserva nueva, y si es una ficha nueva junto a otra.
    Mismo nombre, misma ficha (ver `mismo_nombre`). Si el nombre es otro, ficha aparte, aunque la otra persona ya no
    tenga sesiones: nadie hereda los datos de otro. Un error de tipeo se corrige
    moviendo la sesión desde la ficha."""
    filas = fichas_del_telefono(tenant_id, telefono)
    if not filas:
        return "", False
    if not nombre:
        return filas[0].get("ficha") or "", False
    elegidas = elegir_por_nombre(filas, nombre)
    if elegidas:
        return elegidas[0].get("ficha") or "", False
    sin_nombre = [f for f in filas if f["con_sesiones"] and not f.get("paciente_nombre")]
    if sin_nombre:
        return sin_nombre[0].get("ficha") or "", False
    return _ficha_libre(filas, nombre), True


def _ficha_libre(filas: list[dict], nombre: str | None) -> str:
    usadas = {f.get("ficha") or "" for f in filas}
    palabras = _palabras(nombre)
    base = palabras[0] if palabras else "otra"
    for candidata in (base, "".join(palabras) or base):
        if candidata not in usadas:
            return candidata
    numero = 2
    while f"{base}{numero}" in usadas:
        numero += 1
    return f"{base}{numero}"


def ficha_aparte(tenant_id: str, telefono: str, nombre: str | None) -> str:
    """Para cuando el profesional aclara que es otra persona, aunque el nombre
    sea idéntico: eso no se puede deducir."""
    filas = fichas_del_telefono(tenant_id, telefono)
    return _ficha_libre(filas, nombre) if filas else ""


def nombre_ambiguo(tenant_id: str, telefono: str, nombre: str | None) -> bool:
    return len(elegir_por_nombre(fichas_del_telefono(tenant_id, telefono), nombre)) > 1


def mover_sesion(tenant_id: str, turno_id: str, clave: str, nombre: str) -> bool:
    """Pasa una sesión a otra ficha del mismo teléfono."""
    telefono, ficha = partir_clave(clave)
    res = (
        _client.table("turnos")
        .update({"ficha": ficha, "paciente_nombre": nombre, "revisar_ficha": False})
        .eq("tenant_id", tenant_id)
        .eq("id", turno_id)
        .eq("estado", "reservado")
        .eq("paciente_telefono", telefono)
        .execute()
    )
    return len(res.data) > 0


def reservas_sin_avisar(tenant_id: str) -> list[dict]:
    res = (
        _client.table("turnos")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("estado", "reservado")
        .eq("reserva_nueva", True)
        .eq("reserva_avisada", False)
        .order("inicio")
        .execute()
    )
    return res.data


def marcar_reserva_avisada(tenant_id: str, turno_id: str) -> None:
    _client.table("turnos").update({"reserva_avisada": True}).eq("tenant_id", tenant_id).eq("id", turno_id).execute()


def marcar_reserva_vista(tenant_id: str, turno_id: str) -> None:
    _client.table("turnos").update({"reserva_nueva": False}).eq("tenant_id", tenant_id).eq("id", turno_id).execute()


def confirmar_ficha(tenant_id: str, turno_id: str) -> None:
    _client.table("turnos").update({"revisar_ficha": False}).eq("tenant_id", tenant_id).eq("id", turno_id).execute()


def reservar_turno(tenant_id: str, turno_id: str, telefono: str, nombre: str | None,
                   valor: int | None = None, frecuencia: str | None = None,
                   modalidad: str | None = None, ficha: str | None = None,
                   ignorar_bloqueo: bool = False) -> bool:
    """Devuelve False si el turno ya no estaba libre (alguien se adelantó) —
    el llamador tiene que chequear esto y avisarle al paciente, nunca asumir
    éxito. También si se pisa con otra sesión: alguien pudo tener abierto ese
    horario desde antes de que la pasaran a esa hora."""
    turno = turno_por_id(tenant_id, turno_id)
    if not turno or _se_pisa(tenant_id, turno["inicio"], turno["fin"], turno_id):
        return False
    # Un bloqueo (feriado, vacaciones) cierra el horario a los pacientes. El
    # profesional, desde el panel, puede agendar igual: es su decisión.
    if not ignorar_bloqueo and not turno.get("abierto") and _esta_bloqueado(tenant_id, turno["inicio"], turno["fin"]):
        return False
    # Guardado para alguien de la lista de espera (fin de una pausa): sólo ese
    # teléfono lo puede reservar hasta que vence. El panel, igual que con un
    # bloqueo, puede agendar a otro.
    if not ignorar_bloqueo and guardado_para_otro(turno, telefono):
        return False
    # Sin ficha explícita nadie pudo aclarar de quién es (página pública): se
    # reserva igual y lo revisa el profesional. Preguntarle a quien reserva
    # mostraría nombres de pacientes a cualquiera que escriba ese teléfono.
    revisar = False
    llego_sola = ficha is None
    if ficha is None:
        ficha = ficha_para(tenant_id, telefono, nombre)[0]
        revisar = bool(nombre) and nombre_ambiguo(tenant_id, telefono, nombre)
    res = (
        _client.table("turnos")
        .update({"estado": "reservado", "hueco": None, "guardado_tel": None, "guardado_hasta": None, "paciente_telefono": telefono, "paciente_nombre": nombre, "ficha": ficha,
                 "revisar_ficha": revisar, "reserva_nueva": llego_sola, "reserva_avisada": False,
                 "valor": valor, "frecuencia": frecuencia, "modalidad": modalidad})
        .eq("tenant_id", tenant_id)
        .eq("id", turno_id)
        .eq("estado", "libre")
        .execute()
    )
    if not res.data:
        return False
    if nombre:
        guardar_paciente(tenant_id, f"{telefono}~{ficha}" if ficha else telefono, {"nombre": nombre})
    return True


def proximos_turnos_de(tenant_id: str, telefono: str) -> list[dict]:
    """El próximo turno de cada persona que usa este teléfono, el más cercano
    primero."""
    ahora = datetime.now(timezone.utc)
    res = (
        _client.table("turnos")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("estado", "reservado")
        .eq("paciente_telefono", telefono)
        .gte("inicio", ahora.isoformat())
        .order("inicio")
        .execute()
    )
    vistos: dict[str, dict] = {}
    for t in res.data:
        vistos.setdefault(t.get("ficha") or "", t)
    return list(vistos.values())


# Todo lo que describe a la RESERVA (no al horario) se limpia cuando el turno
# vuelve a 'libre'. Si quedara algo, el próximo paciente que reserve ese
# horario heredaría el "pagó" o el "ya le mandamos el recordatorio" del
# anterior.
_LIBERADO = {
    "estado": "libre",
    "paciente_telefono": None,
    "paciente_nombre": None,
    "ficha": "",
    "revisar_ficha": False,
    "reserva_nueva": False,
    "reserva_avisada": False,
    "recordatorio_enviado": False,
    "asistencia": None,
    "pago": "pendiente",
    "reprogramado_desde": None,
    "valor": None,
    "frecuencia": None,
    "modalidad": None,
    "nota": None,
    "confirmacion_enviada_at": None,
    "confirmado_at": None,
    "token_calendario": None,
    "confirmacion_recordada_at": None,
    "hueco": None,
    "hueco_desde": None,
    "tipo": None,
    "pago_web": None,
    "pago_vence": None,
    "pago_id": None,
    "guardado_tel": None,
    "guardado_hasta": None,
}


def cancelar_turno(tenant_id: str, turno_id: str, origen: str | None = None) -> None:
    """Libera el slot en vez de borrarlo — así vuelve a aparecer en
    turnos_libres() para que otro paciente lo pueda tomar. Con `origen` queda
    registrada como cancelación; reprogramar o cerrar un tratamiento no lo pasan."""
    if origen:
        turno = turno_por_id(tenant_id, turno_id)
        if turno and turno["estado"] == "reservado":
            _client.table("cancelaciones").insert(
                {"tenant_id": tenant_id, "paciente_telefono": turno["paciente_telefono"],
                 "ficha": turno.get("ficha") or "", "inicio": turno["inicio"], "origen": origen,
                 "created_at": datetime.now(timezone.utc).isoformat()}
            ).execute()
    _client.table("turnos").update(_LIBERADO).eq("tenant_id", tenant_id).eq("id", turno_id).execute()


ASISTENCIAS = (None, "vino", "no_vino")
PAGOS = ("pendiente", "pagado", "no_cobra")


def marcar_asistencia(tenant_id: str, turno_id: str, asistencia: str | None) -> None:
    """Sólo sobre turnos reservados: un horario libre no tiene a nadie que
    pueda haber venido o no."""
    if asistencia not in ASISTENCIAS:
        raise ValueError(f"asistencia inválida: {asistencia!r}")
    _client.table("turnos").update({"asistencia": asistencia}).eq("tenant_id", tenant_id).eq(
        "id", turno_id
    ).eq("estado", "reservado").execute()


def marcar_pago(tenant_id: str, turno_id: str, pago: str) -> None:
    if pago not in PAGOS:
        raise ValueError(f"pago inválido: {pago!r}")
    _client.table("turnos").update({"pago": pago}).eq("tenant_id", tenant_id).eq(
        "id", turno_id
    ).eq("estado", "reservado").execute()


def marcar_valor(tenant_id: str, turno_id: str, valor: int | None) -> None:
    _client.table("turnos").update({"valor": valor}).eq("tenant_id", tenant_id).eq(
        "id", turno_id
    ).eq("estado", "reservado").execute()


def marcar_modalidad(tenant_id: str, turno_id: str, modalidad: str) -> None:
    _client.table("turnos").update({"modalidad": modalidad}).eq("tenant_id", tenant_id).eq(
        "id", turno_id
    ).eq("estado", "reservado").execute()


def marcar_nota(tenant_id: str, turno_id: str, nota: str | None) -> None:
    _client.table("turnos").update({"nota": nota}).eq("tenant_id", tenant_id).eq(
        "id", turno_id
    ).eq("estado", "reservado").execute()


def reservar_en_horario(tenant_id: str, inicio: datetime, duracion_minutos: int, telefono: str,
                        nombre: str | None, valor: int | None, frecuencia: str | None,
                        modalidad: str | None = None, ficha: str | None = None,
                        ignorar_bloqueo: bool = False) -> bool:
    """Agenda una sesión a una hora dada, creando el horario si no estaba
    cargado. Es lo que usan los pacientes fijos: la sesión de dentro de dos
    meses seguramente todavía no tiene horario generado. False si está ocupado."""
    crear_turnos_libres(tenant_id, [inicio], duracion_minutos)
    res = (
        _client.table("turnos")
        .select("id")
        .eq("tenant_id", tenant_id)
        .eq("inicio", inicio.isoformat())
        .limit(1)
        .execute()
    )
    if not res.data:
        return False
    return reservar_turno(tenant_id, res.data[0]["id"], telefono, nombre, valor, frecuencia, modalidad, ficha,
                          ignorar_bloqueo=ignorar_bloqueo)


def horario_ocupado(tenant_id: str, inicio: str, fin: str) -> bool:
    """Si [inicio, fin) se pisa con alguna sesión reservada de este tenant."""
    return _se_pisa(tenant_id, inicio, fin, "00000000-0000-0000-0000-000000000000")


def reprogramar_turno(tenant_id: str, turno_id: str, nuevo_inicio: datetime, duracion_minutos: int,
                      ignorar_bloqueo: bool = False) -> str | None:
    """Pasa la sesión a otro horario. Devuelve None si salió bien, o el motivo
    por el que no se pudo (para mostrárselo al profesional).

    El orden importa: PRIMERO se toma el horario nuevo (con el mismo UPDATE
    atómico de reservar_turno, que filtra por estado = 'libre') y RECIÉN
    DESPUÉS se libera el viejo. Si el nuevo ya estaba ocupado, no se tocó
    nada y el paciente sigue teniendo su sesión original.

    Si en ese horario no había un turno generado, se crea — el profesional
    puede pasar una sesión a cualquier hora, no sólo a las que cargó como
    disponibles. El pago viaja con la sesión (si ya la había pagado, sigue
    pagada); la asistencia no, porque la sesión nueva todavía no pasó."""
    original = turno_por_id(tenant_id, turno_id)
    if not original or original["estado"] != "reservado":
        return "Esa sesión ya no está reservada."
    if datetime.fromisoformat(original["inicio"]) == nuevo_inicio:
        return "Es el mismo horario que ya tenía."

    # Como se puede elegir cualquier hora, el nuevo horario puede pisarse con
    # otra sesión sin coincidir en `inicio` (15:30 contra una de 15:00 a
    # 15:50). La sesión que se está moviendo no cuenta: puede correrse 20 min.
    nuevo_fin = nuevo_inicio + timedelta(minutes=duracion_minutos)
    if _se_pisa(tenant_id, nuevo_inicio.isoformat(), nuevo_fin.isoformat(), turno_id):
        return "Ese horario ya está ocupado por otra sesión."
    if not ignorar_bloqueo and _esta_bloqueado(tenant_id, nuevo_inicio.isoformat(), nuevo_fin.isoformat()):
        return "Ese día está bloqueado."

    crear_turnos_libres(tenant_id, [nuevo_inicio], duracion_minutos)
    res = (
        _client.table("turnos")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("inicio", nuevo_inicio.isoformat())
        .limit(1)
        .execute()
    )
    if not res.data:
        return "No se pudo crear el horario nuevo."
    destino = res.data[0]

    tomado = (
        _client.table("turnos")
        .update(
            {
                "estado": "reservado",
                "paciente_telefono": original["paciente_telefono"],
                "paciente_nombre": original["paciente_nombre"],
                "ficha": original.get("ficha") or "",
                "revisar_ficha": bool(original.get("revisar_ficha")),
                "pago": original.get("pago") or "pendiente",
                "valor": original.get("valor"),
                "frecuencia": original.get("frecuencia"),
                "modalidad": original.get("modalidad"),
                "nota": original.get("nota"),
                "tipo": original.get("tipo"),
                # Una sesión ya pagada por la web sigue cubierta por ese pago
                # (para devolverla si después se cancela).
                "pago_web": "aprobado" if original.get("pago_web") == "aprobado" else None,
                "pago_id": original.get("pago_id") if original.get("pago_web") == "aprobado" else None,
                "reprogramado_desde": original["inicio"],
            }
        )
        .eq("tenant_id", tenant_id)
        .eq("id", destino["id"])
        .eq("estado", "libre")
        .execute()
    )
    if not tomado.data:
        return "Ese horario ya está ocupado por otra sesión."
    cancelar_turno(tenant_id, turno_id)
    return None


@_precargable
def pendientes_de_pago(tenant_id: str, hasta: datetime, limite: int = 50) -> list[dict]:
    """Sesiones reservadas que ya pasaron y siguen sin cobrar, de la más vieja
    a la más nueva. Incluye a los que no vinieron: si a ese no se le cobra,
    el profesional lo marca 'no_cobra' y sale de la lista."""
    res = (
        _client.table("turnos")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("estado", "reservado")
        .eq("pago", "pendiente")
        .lt("inicio", hasta.isoformat())
        .order("inicio")
        .limit(limite)
        .execute()
    )
    return res.data


def pendientes_de_pago_de(tenant_id: str, telefono: str, hasta: datetime) -> list[dict]:
    """Lo mismo, de un teléfono: lo que ve el paciente al preguntar «¿qué debo?»."""
    res = (_client.table("turnos").select("*").eq("tenant_id", tenant_id).eq("paciente_telefono", telefono)
           .eq("estado", "reservado").eq("pago", "pendiente").lt("inicio", hasta.isoformat())
           .order("inicio").limit(50).execute())
    return res.data


def crear_turnos_libres(tenant_id: str, inicios: list[datetime], duracion_minutos: int,
                        solo_modalidad: str | None = None, sede: str | None = None,
                        a_mano: bool = False) -> int:
    """Alta en lote de horarios disponibles. `inicios` ya viene con timezone.
    Usa upsert por (tenant_id, inicio) para que correr esto dos veces con el
    mismo rango no duplique turnos.

    `ignore_duplicates` es lo que hace que sea ON CONFLICT DO NOTHING y no
    DO UPDATE: sin eso, regenerar una semana que ya tenía sesiones
    reservadas las pisaba con estado = 'libre'. Devuelve sólo los creados."""
    filas = [
        {
            "tenant_id": tenant_id,
            "inicio": ini.isoformat(),
            "fin": (ini + timedelta(minutes=duracion_minutos)).isoformat(),
            "estado": "libre",
            **({"solo_modalidad": solo_modalidad} if solo_modalidad else {}),
            **({"sede": sede} if sede else {}),
            **({"a_mano": True} if a_mano else {}),
        }
        for ini in inicios
    ]
    if not filas:
        return 0
    res = (
        _client.table("turnos")
        .upsert(filas, on_conflict="tenant_id,inicio", ignore_duplicates=True)
        .execute()
    )
    return len(res.data)


def sacar_libre(tenant_id: str, turno_id: str) -> dict | None:
    """Borra un horario libre y lo anota como sacado, para que el scheduler no
    lo vuelva a crear desde el horario de atención. Uno reservado no se toca."""
    res = (_client.table("turnos").delete().eq("tenant_id", tenant_id).eq("id", turno_id)
           .eq("estado", "libre").execute())
    if not res.data:
        return None
    turno = res.data[0]
    _client.table("horarios_sacados").upsert({"tenant_id": tenant_id, "inicio": turno["inicio"]},
                                             on_conflict="tenant_id,inicio", ignore_duplicates=True).execute()
    return turno


def horarios_sacados(tenant_id: str, desde: datetime) -> set[datetime]:
    res = (_client.table("horarios_sacados").select("inicio").eq("tenant_id", tenant_id)
           .gte("inicio", desde.isoformat()).execute())
    return {datetime.fromisoformat(f["inicio"]) for f in res.data}


def volver_a_ofrecer(tenant_id: str, inicios: list[datetime]) -> None:
    """Generarlo a mano es pedirlo de nuevo: deja de estar sacado."""
    if inicios:
        (_client.table("horarios_sacados").delete().eq("tenant_id", tenant_id)
         .in_("inicio", [i.isoformat() for i in inicios]).execute())


def libres_desde_horario(tenant_id: str, desde: datetime) -> list[dict]:
    """Horarios libres futuros que salieron del horario de atención: sin
    paciente, sin decisión pendiente por una cancelación y no generados a mano."""
    res = (_client.table("turnos").select("id,inicio,hueco,a_mano").eq("tenant_id", tenant_id)
           .eq("estado", "libre").gt("inicio", desde.isoformat()).execute())
    return [t for t in res.data if not t.get("hueco") and not t.get("a_mano")]


def borrar_libres(tenant_id: str, ids: list[str]) -> int:
    if not ids:
        return 0
    res = (_client.table("turnos").delete().eq("tenant_id", tenant_id).eq("estado", "libre")
           .in_("id", ids).execute())
    return len(res.data)


def abrir_libres(tenant_id: str, ids: list[str], abierto: bool) -> int:
    """Ofrece (o deja de ofrecer) a los pacientes horarios libres de un día
    bloqueado. El bloqueo no se toca: es la excepción de cada horario."""
    if not ids:
        return 0
    res = (_client.table("turnos").update({"abierto": abierto}).eq("tenant_id", tenant_id)
           .eq("estado", "libre").in_("id", ids).execute())
    return len(res.data)


def agenda_semana(tenant_id: str, desde: datetime, hasta: datetime) -> list[dict]:
    res = (
        _client.table("turnos")
        .select("*")
        .eq("tenant_id", tenant_id)
        .gte("inicio", desde.isoformat())
        .lt("inicio", hasta.isoformat())
        .order("inicio")
        .execute()
    )
    return res.data


@_precargable
def sesiones_reservadas(tenant_id: str) -> list[dict]:
    """Todas las sesiones reservadas de ESTE tenant, de la más vieja a la más
    nueva. Va de a páginas porque PostgREST corta en 1000 filas: sin esto, las
    fichas de un consultorio con años de historia darían mal las cuentas."""
    filas: list[dict] = []
    while True:
        res = (
            _client.table("turnos")
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("estado", "reservado")
            .order("inicio")
            .range(len(filas), len(filas) + 999)
            .execute()
        )
        filas += res.data
        if len(res.data) < 1000:
            return filas


@_precargable
def datos_de_pacientes(tenant_id: str) -> dict[str, dict]:
    res = _client.table("pacientes").select("*").eq("tenant_id", tenant_id).execute()
    return {clave_de(p): p for p in res.data}


def guardar_paciente(tenant_id: str, clave: str, campos: dict) -> None:
    """Sólo pisa las columnas que vienen en `campos`."""
    telefono, ficha = partir_clave(clave)
    _client.table("pacientes").upsert(
        {"tenant_id": tenant_id, "paciente_telefono": telefono, "ficha": ficha, **campos},
        on_conflict="tenant_id,paciente_telefono,ficha",
    ).execute()


def cancelar_sesiones_futuras(tenant_id: str, clave: str, desde: datetime) -> int:
    telefono, ficha = partir_clave(clave)
    res = (
        _client.table("turnos")
        .select("id")
        .eq("tenant_id", tenant_id)
        .eq("estado", "reservado")
        .eq("paciente_telefono", telefono)
        .eq("ficha", ficha)
        .gt("inicio", desde.isoformat())
        .execute()
    )
    for t in res.data:
        cancelar_turno(tenant_id, t["id"])
    return len(res.data)


def sesiones_sin_pagar_desde(tenant_id: str, desde: datetime) -> list[dict]:
    res = (
        _client.table("turnos")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("estado", "reservado")
        .eq("pago", "pendiente")
        .gte("inicio", desde.isoformat())
        .execute()
    )
    return res.data


@_precargable
def bloqueos_vigentes(tenant_id: str, ahora: datetime) -> list[dict]:
    res = (
        _client.table("bloqueos")
        .select("*")
        .eq("tenant_id", tenant_id)
        .gt("hasta", ahora.isoformat())
        .order("desde")
        .execute()
    )
    return res.data


def agregar_bloqueo(tenant_id: str, desde: datetime, hasta: datetime, motivo: str | None) -> None:
    _client.table("bloqueos").insert(
        {"tenant_id": tenant_id, "desde": desde.isoformat(), "hasta": hasta.isoformat(), "motivo": motivo}
    ).execute()


def borrar_bloqueo(tenant_id: str, bloqueo_id: str) -> None:
    _client.table("bloqueos").delete().eq("tenant_id", tenant_id).eq("id", bloqueo_id).execute()


def cancelaciones_desde(tenant_id: str, desde: datetime) -> list[dict]:
    res = (
        _client.table("cancelaciones")
        .select("*")
        .eq("tenant_id", tenant_id)
        .gte("created_at", desde.isoformat())
        .execute()
    )
    return res.data


@_precargable
def personas_de_pacientes(tenant_id: str) -> list[dict]:
    res = _client.table("paciente_personas").select("*").eq("tenant_id", tenant_id).order("nombre").execute()
    return res.data


def agregar_persona(tenant_id: str, clave: str, nombre: str, vinculo: str | None,
                    telefono_persona: str | None = None, emergencia: bool = False) -> None:
    telefono, ficha = partir_clave(clave)
    _client.table("paciente_personas").insert(
        {"tenant_id": tenant_id, "paciente_telefono": telefono, "ficha": ficha, "nombre": nombre, "vinculo": vinculo,
         "telefono": telefono_persona, "emergencia": emergencia}
    ).execute()


def marcar_emergencia(tenant_id: str, persona_id: str, emergencia: bool) -> None:
    _client.table("paciente_personas").update({"emergencia": emergencia}).eq("tenant_id", tenant_id).eq(
        "id", persona_id
    ).execute()


def borrar_persona(tenant_id: str, persona_id: str) -> None:
    _client.table("paciente_personas").delete().eq("tenant_id", tenant_id).eq("id", persona_id).execute()


@_precargable
def fechas_de_pacientes(tenant_id: str) -> list[dict]:
    res = _client.table("paciente_fechas").select("*").eq("tenant_id", tenant_id).execute()
    return res.data


def agregar_fecha(tenant_id: str, clave: str, titulo: str, dia: int, mes: int,
                  anio: int | None, animo: str | None) -> None:
    telefono, ficha = partir_clave(clave)
    _client.table("paciente_fechas").insert(
        {"tenant_id": tenant_id, "paciente_telefono": telefono, "ficha": ficha, "titulo": titulo,
         "dia": dia, "mes": mes, "anio": anio, "animo": animo}
    ).execute()


def guardar_plazos_tests(tenant_id: str, plazos: dict[str, int]) -> None:
    _client.table("tenants").update({"plazos_tests": plazos}).eq("id", tenant_id).execute()


@_precargable
def tests_tomados(tenant_id: str) -> list[dict]:
    res = _client.table("paciente_tests").select("*").eq("tenant_id", tenant_id).order("fecha", desc=True).execute()
    return res.data


@_precargable
def bitacora(tenant_id: str, clave: str | None = None) -> list[dict]:
    """Entradas de la bitácora, de la más nueva a la más vieja. Con `clave`,
    sólo las de esa ficha (la página rápida del celular no trae las demás)."""
    q = _client.table("paciente_bitacora").select("*").eq("tenant_id", tenant_id)
    if clave is not None:
        telefono, ficha = partir_clave(clave)
        q = q.eq("paciente_telefono", telefono).eq("ficha", ficha)
    res = q.order("fecha", desc=True).execute()
    return sorted(res.data, key=lambda e: (str(e["fecha"]), str(e.get("created_at") or "")), reverse=True)


def agregar_bitacora(tenant_id: str, clave: str, fecha: str, texto: str, para_proxima: str | None = None) -> str | None:
    telefono, ficha = partir_clave(clave)
    res = _client.table("paciente_bitacora").insert(
        {"tenant_id": tenant_id, "paciente_telefono": telefono, "ficha": ficha, "fecha": fecha, "texto": texto,
         "para_proxima": para_proxima}
    ).execute()
    return res.data[0]["id"] if res.data else None


def editar_bitacora(tenant_id: str, entrada_id: str, fecha: str, texto: str, para_proxima: str | None = None) -> None:
    (_client.table("paciente_bitacora")
     .update({"fecha": fecha, "texto": texto, "para_proxima": para_proxima,
              "updated_at": datetime.now(timezone.utc).isoformat()})
     .eq("tenant_id", tenant_id).eq("id", entrada_id).execute())


def borrar_bitacora(tenant_id: str, entrada_id: str) -> None:
    # La base ya borra las fotos en cascada; se borran igual por si acaso.
    _client.table("bitacora_fotos").delete().eq("tenant_id", tenant_id).eq("entrada_id", entrada_id).execute()
    _client.table("paciente_bitacora").delete().eq("tenant_id", tenant_id).eq("id", entrada_id).execute()


def marcar_proxima_hecha(tenant_id: str, entrada_id: str, hecha: bool) -> None:
    (_client.table("paciente_bitacora")
     .update({"proxima_hecha_at": datetime.now(timezone.utc).isoformat() if hecha else None})
     .eq("tenant_id", tenant_id).eq("id", entrada_id).execute())


def entrada_bitacora(tenant_id: str, entrada_id: str) -> dict | None:
    res = _client.table("paciente_bitacora").select("*").eq("tenant_id", tenant_id).eq("id", entrada_id).execute()
    return res.data[0] if res.data else None


@_precargable
def fotos_bitacora(tenant_id: str, entrada_ids: list[str] | None = None) -> list[dict]:
    """Sólo id y anotación (sin la imagen, que es pesada): para listar."""
    q = _client.table("bitacora_fotos").select("id,entrada_id,created_at").eq("tenant_id", tenant_id)
    if entrada_ids is not None:
        if not entrada_ids:
            return []
        q = q.in_("entrada_id", entrada_ids)
    return sorted(q.execute().data, key=lambda f: str(f.get("created_at") or ""))


def foto_bitacora(tenant_id: str, foto_id: str) -> dict | None:
    res = _client.table("bitacora_fotos").select("*").eq("tenant_id", tenant_id).eq("id", foto_id).execute()
    return res.data[0] if res.data else None


def agregar_foto_bitacora(tenant_id: str, entrada_id: str, imagen: str) -> None:
    _client.table("bitacora_fotos").insert({"tenant_id": tenant_id, "entrada_id": entrada_id, "imagen": imagen}).execute()


def borrar_foto_bitacora(tenant_id: str, foto_id: str) -> None:
    _client.table("bitacora_fotos").delete().eq("tenant_id", tenant_id).eq("id", foto_id).execute()


def fichas_livianas(tenant_id: str) -> tuple[list[dict], list[dict]]:
    """Lo mínimo para listar pacientes rápido en el celular: de las sesiones
    sólo teléfono, ficha, nombre e inicio; de las fichas, nombre y estado."""
    filas: list[dict] = []
    while True:
        res = (
            _client.table("turnos")
            .select("paciente_telefono,ficha,paciente_nombre,inicio")
            .eq("tenant_id", tenant_id)
            .eq("estado", "reservado")
            .order("inicio")
            .range(len(filas), len(filas) + 999)
            .execute()
        )
        filas += res.data
        if len(res.data) < 1000:
            break
    datos = _client.table("pacientes").select("paciente_telefono,ficha,nombre,estado").eq("tenant_id", tenant_id).execute()
    return filas, datos.data


def resumen_de_ficha(tenant_id: str, clave: str) -> dict:
    """Lo de UNA ficha para «Sesión en curso» en el celular: sus sesiones
    reservadas, sus fechas y sus datos. Tres consultas chicas, juntas."""
    telefono, ficha = partir_clave(clave)

    def sesiones():
        return (_client.table("turnos").select("id,inicio,fin,pago,valor,nota,asistencia")
                .eq("tenant_id", tenant_id).eq("paciente_telefono", telefono).eq("ficha", ficha)
                .eq("estado", "reservado").order("inicio").limit(500).execute().data)

    def fechas():
        return (_client.table("paciente_fechas").select("*").eq("tenant_id", tenant_id)
                .eq("paciente_telefono", telefono).eq("ficha", ficha).execute().data)

    def datos():
        res = (_client.table("pacientes").select("*").eq("tenant_id", tenant_id)
               .eq("paciente_telefono", telefono).eq("ficha", ficha).execute())
        return res.data[0] if res.data else {}

    f_s, f_f, f_d = _hilos.submit(sesiones), _hilos.submit(fechas), _hilos.submit(datos)
    return {"sesiones": f_s.result(timeout=20), "fechas": f_f.result(timeout=20), "datos": f_d.result(timeout=20)}


def bitacora_del_dia(tenant_id: str, fecha: str) -> list[dict]:
    """Qué fichas tienen algo anotado ese día (para «Falta anotar»)."""
    return (_client.table("paciente_bitacora").select("paciente_telefono,ficha")
            .eq("tenant_id", tenant_id).eq("fecha", fecha).execute().data)


def agregar_test(tenant_id: str, clave: str, test: str, fecha: str) -> None:
    telefono, ficha = partir_clave(clave)
    _client.table("paciente_tests").insert(
        {"tenant_id": tenant_id, "paciente_telefono": telefono, "ficha": ficha, "test": test, "fecha": fecha}
    ).execute()


def guardar_resultados_test(tenant_id: str, test_id: str, resultados: list[dict]) -> None:
    _client.table("paciente_tests").update({"resultados": resultados}).eq("tenant_id", tenant_id).eq("id", test_id).execute()


@_precargable
def textos_lectura(tenant_id: str) -> list[dict]:
    res = _client.table("textos_lectura").select("*").eq("tenant_id", tenant_id).order("titulo").execute()
    return res.data


def agregar_texto_lectura(tenant_id: str, titulo: str, texto: str) -> None:
    _client.table("textos_lectura").insert({"tenant_id": tenant_id, "titulo": titulo, "texto": texto}).execute()


def borrar_texto_lectura(tenant_id: str, texto_id: str) -> None:
    _client.table("textos_lectura").delete().eq("tenant_id", tenant_id).eq("id", texto_id).execute()


@_precargable
def lecturas(tenant_id: str) -> list[dict]:
    # La fecha es siempre la del día en que se guarda: alcanza con created_at.
    res = _client.table("lecturas").select("*").eq("tenant_id", tenant_id).order("created_at", desc=True).execute()
    return res.data


def agregar_lectura(tenant_id: str, clave: str, fila: dict) -> None:
    telefono, ficha = partir_clave(clave)
    _client.table("lecturas").insert({"tenant_id": tenant_id, "paciente_telefono": telefono, "ficha": ficha, **fila}).execute()


def borrar_lectura(tenant_id: str, lectura_id: str) -> None:
    _client.table("lecturas").delete().eq("tenant_id", tenant_id).eq("id", lectura_id).execute()


def crear_cuestionario(tenant_id: str, clave: str, instrumento: str, token: str, vence: str,
                       para: str | None = None, plantilla: dict | None = None) -> None:
    """plantilla: copia del cuestionario propio al momento de mandarlo, así
    editarlo después no cambia lo que ya se respondió."""
    telefono, ficha = partir_clave(clave)
    _client.table("cuestionarios").insert(
        {"tenant_id": tenant_id, "paciente_telefono": telefono, "ficha": ficha,
         "instrumento": instrumento, "token": token, "vence": vence, "para": para, "plantilla": plantilla}
    ).execute()


@_precargable
def cuestionarios(tenant_id: str) -> list[dict]:
    res = _client.table("cuestionarios").select("*").eq("tenant_id", tenant_id).order("created_at", desc=True).execute()
    return res.data


def cuestionario_por_token(token: str) -> dict | None:
    """Sin tenant: el token ES la credencial del link público."""
    res = _client.table("cuestionarios").select("*").eq("token", token).limit(1).execute()
    return res.data[0] if res.data else None


def responder_cuestionario(cuestionario_id: str, datos: dict) -> bool:
    """Sólo si todavía no se respondió: un link sirve una vez."""
    res = (_client.table("cuestionarios").update(datos).eq("id", cuestionario_id)
           .is_("respondido_at", "null").execute())
    return bool(res.data)


def marcar_cuestionario_visto(tenant_id: str, cuestionario_id: str) -> None:
    _client.table("cuestionarios").update({"visto": True}).eq("tenant_id", tenant_id).eq("id", cuestionario_id).execute()


def borrar_cuestionario(tenant_id: str, cuestionario_id: str) -> None:
    _client.table("cuestionarios").delete().eq("tenant_id", tenant_id).eq("id", cuestionario_id).execute()


@_precargable
def informes(tenant_id: str) -> list[dict]:
    res = _client.table("informes").select("*").eq("tenant_id", tenant_id).order("created_at", desc=True).execute()
    return res.data


def agregar_informe(tenant_id: str, clave: str, contenido: dict) -> None:
    telefono, ficha = partir_clave(clave)
    _client.table("informes").insert(
        {"tenant_id": tenant_id, "paciente_telefono": telefono, "ficha": ficha, "contenido": contenido}).execute()


def borrar_informe(tenant_id: str, informe_id: str) -> None:
    _client.table("informes").delete().eq("tenant_id", tenant_id).eq("id", informe_id).execute()


@_precargable
def plantillas_cuestionario(tenant_id: str) -> list[dict]:
    res = _client.table("plantillas_cuestionario").select("*").eq("tenant_id", tenant_id).order("created_at").execute()
    return res.data


def agregar_plantilla(tenant_id: str, contenido: dict) -> None:
    _client.table("plantillas_cuestionario").insert({"tenant_id": tenant_id, "contenido": contenido}).execute()


def guardar_plantilla(tenant_id: str, plantilla_id: str, contenido: dict) -> None:
    _client.table("plantillas_cuestionario").update({"contenido": contenido}).eq("tenant_id", tenant_id).eq("id", plantilla_id).execute()


def borrar_plantilla(tenant_id: str, plantilla_id: str) -> None:
    _client.table("plantillas_cuestionario").delete().eq("tenant_id", tenant_id).eq("id", plantilla_id).execute()


@_precargable
def plantillas_informe(tenant_id: str) -> list[dict]:
    res = _client.table("plantillas_informe").select("*").eq("tenant_id", tenant_id).order("created_at").execute()
    return res.data


def agregar_plantilla_informe(tenant_id: str, contenido: dict) -> None:
    _client.table("plantillas_informe").insert({"tenant_id": tenant_id, "contenido": contenido}).execute()


def borrar_plantilla_informe(tenant_id: str, plantilla_id: str) -> None:
    _client.table("plantillas_informe").delete().eq("tenant_id", tenant_id).eq("id", plantilla_id).execute()


@_precargable
def biblioteca() -> list[dict]:
    """Sin filtro de tenant a propósito: es lo que cada profesional eligió compartir."""
    res = _client.table("biblioteca_cuestionarios").select("*").order("created_at", desc=True).execute()
    return res.data


def compartir_en_biblioteca(tenant_id: str, autor: str, contenido: dict, con_preguntas: bool) -> None:
    _client.table("biblioteca_cuestionarios").insert(
        {"autor_tenant_id": tenant_id, "autor": autor, "contenido": contenido, "con_preguntas": con_preguntas}).execute()


def moderar_biblioteca(entrada_id: str, oculto: bool, por: str | None) -> None:
    """Sin tenant a propósito: sólo la llama un moderador (lo chequea app.py)."""
    _client.table("biblioteca_cuestionarios").update({"oculto": oculto, "oculto_por": por}).eq("id", entrada_id).execute()


def retirar_de_biblioteca(tenant_id: str, entrada_id: str) -> None:
    _client.table("biblioteca_cuestionarios").delete().eq("autor_tenant_id", tenant_id).eq("id", entrada_id).execute()


@_precargable
def entregas(tenant_id: str) -> list[dict]:
    """Sin el HTML: la lista de la ficha sólo necesita el estado."""
    res = (_client.table("entregas").select("id,tenant_id,paciente_telefono,ficha,token,vence,abierta_at,created_at,dni_hash,intentos")
           .eq("tenant_id", tenant_id).order("created_at", desc=True).execute())
    return res.data


def crear_entrega(tenant_id: str, clave: str, token: str, vence: str, html: str, dni_hash: str | None = None) -> None:
    telefono, ficha = partir_clave(clave)
    _client.table("entregas").insert({"tenant_id": tenant_id, "paciente_telefono": telefono, "ficha": ficha,
                                      "token": token, "vence": vence, "html": html, "dni_hash": dni_hash}).execute()


def entrega_por_token(token: str) -> dict | None:
    """Sin tenant: el token ES la credencial del link."""
    res = _client.table("entregas").select("*").eq("token", token).limit(1).execute()
    return res.data[0] if res.data else None


def abrir_entrega(entrega_id: str, cuando: str) -> bool:
    """Sólo la primera vez: dos aperturas simultáneas no pueden ganar las dos."""
    res = _client.table("entregas").update({"abierta_at": cuando}).eq("id", entrega_id).is_("abierta_at", "null").execute()
    return bool(res.data)


def borrar_entrega(tenant_id: str, entrega_id: str) -> None:
    _client.table("entregas").delete().eq("tenant_id", tenant_id).eq("id", entrega_id).execute()


# --- Administración de la plataforma ---

def admin_por_usuario(usuario: str) -> dict | None:
    res = _client.table("admins").select("*").eq("usuario", usuario).limit(1).execute()
    return res.data[0] if res.data else None


def admin_por_id(admin_id: str) -> dict | None:
    res = _client.table("admins").select("id,usuario").eq("id", admin_id).limit(1).execute()
    return res.data[0] if res.data else None


def marcar_ingreso(tabla: str, fila_id: str, cuando: str) -> None:
    _client.table(tabla).update({"ultimo_ingreso": cuando}).eq("id", fila_id).execute()


def intentos_login(claves: list[str]) -> list[dict]:
    res = _client.table("login_intentos").select("*").in_("clave", claves).execute()
    return res.data


def guardar_intento_login(clave: str, datos: dict) -> None:
    _client.table("login_intentos").upsert({"clave": clave, **datos}, on_conflict="clave").execute()


def borrar_intentos_login(clave: str) -> None:
    _client.table("login_intentos").delete().eq("clave", clave).execute()


def admin_completo(admin_id: str) -> dict | None:
    res = _client.table("admins").select("*").eq("id", admin_id).limit(1).execute()
    return res.data[0] if res.data else None


def guardar_totp(admin_id: str, campos: dict) -> None:
    _client.table("admins").update(campos).eq("id", admin_id).execute()


def todos_los_tenants() -> list[dict]:
    res = (_client.table("tenants").select("id,nombre,rubro,slug,panel_user,activo,moderador,ultimo_ingreso,created_at,cobro_web,mp_user_id,mp_caido_at")
           .order("nombre").execute())
    return res.data


def uso_de_tenants(desde: str, hasta: str | None = None) -> dict[str, dict]:
    """Sólo cuentas, nunca datos de pacientes: sesiones reservadas desde
    `desde`, pacientes distintos en ellas, cuestionarios y entregas."""
    uso: dict[str, dict] = {}
    vacio = lambda: {"sesiones": 0, "pacientes": set(), "cuestionarios": 0, "entregas": 0}
    q = _client.table("turnos").select("tenant_id,paciente_telefono,ficha").eq("estado", "reservado").gte("inicio", desde)
    turnos = (q.lt("inicio", hasta) if hasta else q).execute().data
    for t in turnos:
        u = uso.setdefault(t["tenant_id"], vacio())
        u["sesiones"] += 1
        u["pacientes"].add((t["paciente_telefono"], t.get("ficha") or ""))
    for tabla, campo in (("cuestionarios", "cuestionarios"), ("entregas", "entregas")):
        q = _client.table(tabla).select("tenant_id").gte("created_at", desde)
        for x in (q.lt("created_at", hasta) if hasta else q).execute().data:
            uso.setdefault(x["tenant_id"], vacio())[campo] += 1
    return {k: {**v, "pacientes": len(v["pacientes"])} for k, v in uso.items()}


def crear_tenant(datos: dict) -> None:
    _client.table("tenants").insert(datos).execute()


def tenant_por_slug_o_usuario(slug: str | None, panel_user: str) -> dict | None:
    if slug:
        res = _client.table("tenants").select("id").eq("slug", slug).limit(1).execute()
        if res.data:
            return res.data[0]
    res = _client.table("tenants").select("id").eq("panel_user", panel_user).limit(1).execute()
    return res.data[0] if res.data else None


def cambiar_clave(tabla: str, fila_id: str, campo_hash: str, nuevo_hash: str, version: int) -> None:
    """Clave nueva y versión de sesión nueva: las demás sesiones se cierran."""
    _client.table(tabla).update({campo_hash: nuevo_hash, "sesion_version": version}).eq("id", fila_id).execute()


def subir_version_sesion(tabla: str, fila_id: str, version: int) -> None:
    _client.table(tabla).update({"sesion_version": version}).eq("id", fila_id).execute()


def marcar_bloqueo(tenant_id: str, cuando: str) -> None:
    _client.table("tenants").update({"ultimo_bloqueo": cuando}).eq("id", tenant_id).execute()


def activar_tenant(tenant_id: str, activo: bool) -> None:
    _client.table("tenants").update({"activo": activo}).eq("id", tenant_id).execute()


def registrar_intento_entrega(entrega_id: str, intentos: int) -> None:
    _client.table("entregas").update({"intentos": intentos}).eq("id", entrega_id).execute()


# --- Sesiones, actividad, links de clave y QR ---

def crear_sesion(sesion_id: str, tenant_id: str, dispositivo: str, origen: str) -> None:
    from datetime import datetime, timezone
    ahora = datetime.now(timezone.utc).isoformat()
    _client.table("sesiones").insert({"id": sesion_id, "tenant_id": tenant_id, "dispositivo": dispositivo,
                                      "origen": origen, "creada": ahora, "ultima": ahora}).execute()


def sesion(sesion_id: str) -> dict | None:
    res = _client.table("sesiones").select("*").eq("id", sesion_id).limit(1).execute()
    return res.data[0] if res.data else None


def tocar_sesion(sesion_id: str, cuando: str) -> None:
    _client.table("sesiones").update({"ultima": cuando}).eq("id", sesion_id).execute()


@_precargable
def sesiones_de(tenant_id: str) -> list[dict]:
    res = _client.table("sesiones").select("*").eq("tenant_id", tenant_id).order("ultima", desc=True).execute()
    return res.data


def cerrar_sesion(tenant_id: str, sesion_id: str) -> None:
    _client.table("sesiones").delete().eq("tenant_id", tenant_id).eq("id", sesion_id).execute()


def cerrar_sesiones(tenant_id: str, excepto: str | None = None) -> None:
    q = _client.table("sesiones").delete().eq("tenant_id", tenant_id)
    (q.neq("id", excepto) if excepto else q).execute()


def registrar_actividad(actor: str, accion: str, tenant_id: str | None = None) -> None:
    from datetime import datetime, timezone
    _client.table("actividad").insert({"actor": actor, "accion": accion, "tenant_id": tenant_id,
                                       "cuando": datetime.now(timezone.utc).isoformat()}).execute()


def actividad_reciente(limite: int = 200, tenant_id: str | None = None) -> list[dict]:
    q = _client.table("actividad").select("*")
    if tenant_id:
        q = q.eq("tenant_id", tenant_id)
    return q.order("cuando", desc=True).limit(limite).execute().data


def crear_enlace_clave(token_hash: str, tenant_id: str, vence: str) -> None:
    _client.table("enlaces_clave").insert({"token_hash": token_hash, "tenant_id": tenant_id, "vence": vence}).execute()


def enlace_clave(token_hash: str) -> dict | None:
    res = _client.table("enlaces_clave").select("*").eq("token_hash", token_hash).limit(1).execute()
    return res.data[0] if res.data else None


def usar_enlace_clave(token_hash: str, cuando: str) -> bool:
    res = (_client.table("enlaces_clave").update({"usado_at": cuando}).eq("token_hash", token_hash)
           .is_("usado_at", "null").execute())
    return bool(res.data)


def crear_qr_login(token_hash: str, nonce_hash: str, dispositivo: str, vence: str, tipo: str = "panel") -> None:
    _client.table("qr_logins").insert({"token_hash": token_hash, "nonce_hash": nonce_hash,
                                       "dispositivo": dispositivo, "vence": vence, "tipo": tipo}).execute()


def qr_login(token_hash: str) -> dict | None:
    res = _client.table("qr_logins").select("*").eq("token_hash", token_hash).limit(1).execute()
    return res.data[0] if res.data else None


def aprobar_qr_login(token_hash: str, tenant_id: str, cuando: str) -> bool:
    res = (_client.table("qr_logins").update({"tenant_id": tenant_id, "aprobado_at": cuando})
           .eq("token_hash", token_hash).is_("aprobado_at", "null").execute())
    return bool(res.data)


def aprobar_qr_admin(token_hash: str, admin_id: str, cuando: str) -> bool:
    res = (_client.table("qr_logins").update({"admin_id": admin_id, "aprobado_at": cuando})
           .eq("token_hash", token_hash).eq("tipo", "admin").is_("aprobado_at", "null").execute())
    return bool(res.data)


def usar_qr_login(token_hash: str, cuando: str) -> bool:
    res = (_client.table("qr_logins").update({"usado_at": cuando}).eq("token_hash", token_hash)
           .is_("usado_at", "null").execute())
    return bool(res.data)


def tenant_por_email(email: str) -> dict | None:
    res = _client.table("tenants").select("*").ilike("email", email).eq("activo", True).limit(1).execute()
    return res.data[0] if res.data else None


@_precargable
def llaves_de(tenant_id: str) -> list[dict]:
    res = _client.table("llaves_acceso").select("*").eq("tenant_id", tenant_id).order("creada").execute()
    return res.data


def llave(llave_id: str) -> dict | None:
    res = _client.table("llaves_acceso").select("*").eq("id", llave_id).limit(1).execute()
    return res.data[0] if res.data else None


def agregar_llave(datos: dict) -> None:
    from datetime import datetime, timezone
    _client.table("llaves_acceso").insert({"creada": datetime.now(timezone.utc).isoformat(), **datos}).execute()


def usar_llave(llave_id: str, contador: int, cuando: str) -> None:
    _client.table("llaves_acceso").update({"contador": contador, "ultima": cuando}).eq("id", llave_id).execute()


def borrar_llave(tenant_id: str, llave_id: str) -> None:
    _client.table("llaves_acceso").delete().eq("tenant_id", tenant_id).eq("id", llave_id).execute()


def estado_seguridad_tenants() -> dict:
    """Sólo cuentas: cuántas profesionales activas tienen mail, doble factor y llave."""
    tenants = _client.table("tenants").select("id,email,totp_secreto,activo").execute().data
    activos = [t for t in tenants if t.get("activo") is not False]
    con_llave = {x["tenant_id"] for x in _client.table("llaves_acceso").select("tenant_id").execute().data}
    return {"sin_mail": sum(not t.get("email") for t in activos),
            "con_doble_factor": sum(bool(t.get("totp_secreto")) for t in activos),
            "con_llave": sum(t["id"] in con_llave for t in activos)}


def admin_llaves_de(admin_id: str) -> list[dict]:
    res = _client.table("admin_llaves").select("*").eq("admin_id", admin_id).order("creada").execute()
    return res.data


def admin_llave(llave_id: str) -> dict | None:
    res = _client.table("admin_llaves").select("*").eq("id", llave_id).limit(1).execute()
    return res.data[0] if res.data else None


def agregar_admin_llave(datos: dict) -> None:
    from datetime import datetime, timezone
    _client.table("admin_llaves").insert({"creada": datetime.now(timezone.utc).isoformat(), **datos}).execute()


def usar_admin_llave(llave_id: str, contador: int, cuando: str) -> None:
    _client.table("admin_llaves").update({"contador": contador, "ultima": cuando}).eq("id", llave_id).execute()


def borrar_admin_llave(admin_id: str, llave_id: str) -> None:
    _client.table("admin_llaves").delete().eq("admin_id", admin_id).eq("id", llave_id).execute()


def limpiar_vencidos(ahora: str, hace_un_dia: str, hace_una_semana: str, hace_un_mes: str, hace_tres_meses: str,
                     hace_un_anio: str) -> None:
    """Limpieza diaria: lo que ya no sirve para nada. Nunca toca datos de pacientes."""
    _client.table("qr_logins").delete().lt("vence", hace_un_dia).execute()
    _client.table("enlaces_clave").delete().lt("vence", hace_una_semana).execute()
    _client.table("sesiones").delete().lt("ultima", hace_un_mes).execute()
    _client.table("login_intentos").delete().lt("ultimo", hace_un_dia).execute()
    # Entregas viejas: queda la fila (para la ficha) pero se borra la copia de la ficha.
    _client.table("entregas").update({"html": ""}).lt("vence", hace_tres_meses).neq("html", "").execute()
    _client.table("cuestionarios").delete().is_("respondido_at", "null").lt("vence", hace_tres_meses).execute()
    _client.table("actividad").delete().lt("cuando", hace_un_anio).execute()
    _client.table("mensajes_sin_destino").delete().lt("cuando", hace_un_mes).execute()


def guardar_totp_tenant(tenant_id: str, campos: dict) -> None:
    _client.table("tenants").update(campos).eq("id", tenant_id).execute()


def borrar_test(tenant_id: str, test_id: str) -> None:
    _client.table("paciente_tests").delete().eq("tenant_id", tenant_id).eq("id", test_id).execute()


def borrar_fecha(tenant_id: str, fecha_id: str) -> None:
    _client.table("paciente_fechas").delete().eq("tenant_id", tenant_id).eq("id", fecha_id).execute()


def turnos_para_recordar(tenant_id: str, horas_antes: int, ahora: datetime | None = None) -> list[dict]:
    """Turnos reservados cuyo recordatorio todavía no salió y ya están
    dentro de la ventana (inicio - horas_antes <= ahora < inicio). `ahora`
    distinto del real sólo lo usa la vista previa de administración."""
    ahora = ahora or datetime.now(timezone.utc)
    limite = ahora + timedelta(hours=horas_antes)
    res = (
        _client.table("turnos")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("estado", "reservado")
        .eq("recordatorio_enviado", False)
        .gte("inicio", ahora.isoformat())
        .lte("inicio", limite.isoformat())
        .execute()
    )
    return res.data


def marcar_recordatorio_enviado(turno_id: str) -> None:
    _client.table("turnos").update({"recordatorio_enviado": True}).eq("id", turno_id).execute()


@_precargable
def lista_de_espera(tenant_id: str) -> list[dict]:
    res = _client.table("lista_espera").select("*").eq("tenant_id", tenant_id).order("created_at").execute()
    return res.data


def agregar_a_espera(tenant_id: str, nombre: str, telefono: str, preferencia: str | None,
                     origen: str = "panel") -> bool:
    """False si ese teléfono ya estaba en la lista de este profesional (no se
    duplica: desde la web alguien puede anotarse dos veces)."""
    ya = (_client.table("lista_espera").select("id").eq("tenant_id", tenant_id).eq("telefono", telefono)
          .limit(1).execute().data)
    if ya:
        return False
    _client.table("lista_espera").insert(
        {"tenant_id": tenant_id, "nombre": nombre, "telefono": telefono, "preferencia": preferencia, "origen": origen,
         "created_at": datetime.now(timezone.utc).isoformat()}
    ).execute()
    return True


def quitar_de_espera(tenant_id: str, espera_id: str) -> None:
    _client.table("lista_espera").delete().eq("tenant_id", tenant_id).eq("id", espera_id).execute()


def marcar_oferta(tenant_id: str, espera_id: str, campos: dict) -> None:
    """Qué horario se le guardó a alguien de la lista y cómo terminó
    (`ofrecido_estado`: esperando / reservo / vencio / otro)."""
    _client.table("lista_espera").update(campos).eq("tenant_id", tenant_id).eq("id", espera_id).execute()


def marcar_ofrecido(tenant_id: str, espera_id: str, dia: str) -> None:
    _client.table("lista_espera").update({"ofrecido_en": dia}).eq("tenant_id", tenant_id).eq("id", espera_id).execute()


def portada(tenant_id: str) -> dict | None:
    res = _client.table("portadas").select("tipo,datos").eq("tenant_id", tenant_id).limit(1).execute()
    return res.data[0] if res.data else None


def guardar_portada(tenant_id: str, tipo: str, datos_b64: str, version: int) -> None:
    from datetime import datetime, timezone
    _client.table("portadas").upsert({"tenant_id": tenant_id, "tipo": tipo, "datos": datos_b64,
                                      "actualizada": datetime.now(timezone.utc).isoformat()},
                                     on_conflict="tenant_id").execute()
    _client.table("tenants").update({"portada_version": version}).eq("id", tenant_id).execute()


def borrar_portada(tenant_id: str) -> None:
    _client.table("portadas").delete().eq("tenant_id", tenant_id).execute()
    _client.table("tenants").update({"portada_version": 0}).eq("id", tenant_id).execute()


def guardar_ajustes(tenant_id: str, campos: dict) -> None:
    _client.table("tenants").update(campos).eq("id", tenant_id).execute()


def marcar_aviso_enviado(tenant_id: str, dia: str) -> None:
    _client.table("tenants").update({"ultimo_aviso": dia}).eq("id", tenant_id).execute()


def tenants_activos() -> list[dict]:
    res = _client.table("tenants").select("*").eq("activo", True).execute()
    return res.data


def marcar_evento(clave: str, detalle: str | None = None) -> None:
    """Última vez que pasó algo de la plataforma (ej. 'webhook', 'mensaje')."""
    from datetime import datetime, timezone
    _client.table("eventos_plataforma").upsert(
        {"clave": clave, "cuando": datetime.now(timezone.utc).isoformat(), "detalle": detalle},
        on_conflict="clave").execute()


def eventos_plataforma() -> dict[str, dict]:
    return {f["clave"]: f for f in _client.table("eventos_plataforma").select("*").execute().data}


def ajustes_plataforma() -> dict[str, str]:
    return {f["clave"]: f["valor"] for f in _client.table("ajustes_plataforma").select("*").execute().data}


def guardar_ajustes_plataforma(valores: dict[str, str]) -> None:
    from datetime import datetime, timezone
    ahora = datetime.now(timezone.utc).isoformat()
    _client.table("ajustes_plataforma").upsert(
        [{"clave": k, "valor": v, "cuando": ahora} for k, v in valores.items()], on_conflict="clave").execute()


def guardar_aviso_plantilla(aviso: dict) -> None:
    from datetime import datetime, timezone
    _client.table("plantillas_meta").upsert(
        {**aviso, "cuando": datetime.now(timezone.utc).isoformat()}, on_conflict="nombre,idioma").execute()


def avisos_plantillas() -> list[dict]:
    return _client.table("plantillas_meta").select("*").order("cuando", desc=True).limit(50).execute().data


def registrar_sin_destino(final: str, telefono_hash: str, respondido: bool) -> None:
    from datetime import datetime, timezone
    _client.table("mensajes_sin_destino").insert({"final": final, "telefono_hash": telefono_hash, "respondido": respondido,
                                                  "cuando": datetime.now(timezone.utc).isoformat()}).execute()


def respondido_sin_destino_desde(telefono_hash: str, desde: str) -> bool:
    return bool(_client.table("mensajes_sin_destino").select("id").eq("telefono_hash", telefono_hash)
                .eq("respondido", True).gte("cuando", desde).limit(1).execute().data)


def mensajes_sin_destino(limite: int = 50) -> list[dict]:
    return _client.table("mensajes_sin_destino").select("final,respondido,cuando").order(
        "cuando", desc=True).limit(limite).execute().data


def actualizar_reserva(tenant_id: str, turno_id: str, campos: dict) -> None:
    """Datos de una reserva ya tomada (modalidad elegida en la web, envío de
    la confirmación). Sólo si sigue reservada."""
    _client.table("turnos").update(campos).eq("tenant_id", tenant_id).eq("id", turno_id).eq("estado", "reservado").execute()


def turno_por_token_calendario(token: str) -> dict | None:
    """Para /agendar/<token>: el token aleatorio ES la credencial (como en los
    cuestionarios). Sólo turnos reservados."""
    if not token:
        return None
    res = (_client.table("turnos").select("*").eq("token_calendario", token).eq("estado", "reservado")
           .limit(1).execute())
    return res.data[0] if res.data else None


def confirmar_proximo(tenant_id: str, telefono: str) -> dict | None:
    """El paciente respondió «sí» por WhatsApp: confirma su próxima reserva
    que esté esperando confirmación. Devuelve el turno confirmado."""
    ahora = datetime.now(timezone.utc)
    res = (_client.table("turnos").select("*").eq("tenant_id", tenant_id).eq("paciente_telefono", telefono)
           .eq("estado", "reservado").gte("inicio", ahora.isoformat()).is_("confirmado_at", "null")
           .order("inicio").execute())
    pendientes = [t for t in res.data if t.get("confirmacion_enviada_at")]
    if not pendientes:
        return None
    turno = pendientes[0]
    _client.table("turnos").update({"confirmado_at": ahora.isoformat()}).eq("tenant_id", tenant_id).eq(
        "id", turno["id"]).execute()
    return {**turno, "confirmado_at": ahora.isoformat()}


def marcar_confirmado(tenant_id: str, turno_id: str) -> None:
    """El paciente tocó «Sí, confirmo» en el recordatorio (no era una reserva
    web esperando confirmación): queda «✓ confirmó» en el panel."""
    _client.table("turnos").update({"confirmado_at": datetime.now(timezone.utc).isoformat()}).eq(
        "tenant_id", tenant_id).eq("id", turno_id).eq("estado", "reservado").execute()


def sin_confirmar(tenant_id: str) -> list[dict]:
    """Reservas web futuras a las que se les mandó la confirmación y todavía no
    confirmaron."""
    ahora = datetime.now(timezone.utc)
    res = (_client.table("turnos").select("*").eq("tenant_id", tenant_id).eq("estado", "reservado")
           .gte("inicio", ahora.isoformat()).is_("confirmado_at", "null").order("inicio").execute())
    return [t for t in res.data if t.get("confirmacion_enviada_at")]


def marcar_hueco(tenant_id: str, turno_id: str, valor: str | None) -> None:
    """None = se ofrece normal; 'pendiente' = retenido esperando que el
    profesional decida; 'propio' = se lo tomó. Sólo sobre horarios libres."""
    desde = datetime.now(timezone.utc).isoformat() if valor == "pendiente" else None
    _client.table("turnos").update({"hueco": valor, "hueco_desde": desde}).eq("tenant_id", tenant_id).eq(
        "id", turno_id).eq("estado", "libre").execute()


@_precargable
def huecos(tenant_id: str) -> list[dict]:
    """Horarios futuros retenidos o tomados por el profesional."""
    ahora = datetime.now(timezone.utc)
    res = (_client.table("turnos").select("*").eq("tenant_id", tenant_id).eq("estado", "libre")
           .gte("inicio", ahora.isoformat()).order("inicio").execute())
    return [t for t in res.data if t.get("hueco")]


def huecos_pendientes(tenant_id: str) -> list[dict]:
    return [t for t in huecos(tenant_id) if t["hueco"] == "pendiente"]


def tenant_por_aviso(telefono: str) -> dict | None:
    """El profesional activo cuyo WhatsApp de avisos es este (últimos 10
    dígitos: el 9 y el 15 argentinos varían)."""
    todos = tenants_por_aviso(telefono)
    return todos[0] if todos else None


def tenants_por_aviso(telefono: str) -> list[dict]:
    """Todas las cuentas activas con este WhatsApp de avisos (una persona
    puede tener más de una, o probar con cuentas de demo). Como
    `tenant_por_paciente`, no recibe tenant_id: es la que lo averigua."""
    final = "".join(ch for ch in telefono or "" if ch.isdigit())[-10:]
    if len(final) < 10:
        return []
    return [t for t in tenants_activos()
            if "".join(ch for ch in t.get("aviso_telefono") or "" if ch.isdigit())[-10:] == final]


def vincular_aviso(tenant_id: str, numero: str) -> None:
    """El profesional mandó desde su WhatsApp de avisos el código del panel
    (asistente_profesional). Vale mientras no cambie `aviso_telefono`."""
    _client.table("tenants").update({"aviso_verificado": numero}).eq("id", tenant_id).execute()


def turnos_de_paciente_por_nombre(tenant_id: str, nombre: str, limite: int = 200) -> list[dict]:
    """Sesiones reservadas de ESTE tenant cuyo paciente se llama parecido
    (para «paciente Juan» del asistente del profesional). Las vocales van
    como comodín (`_`): «perez» encuentra «Pérez»; quien llama filtra fino."""
    limpio = "".join(ch for ch in nombre if ch.isalpha() or ch == " ").strip()
    if len(limpio) < 2:
        return []
    patron = "%" + "%".join("".join("_" if ch in "aeiouáéíóú" else ch for ch in palabra) for palabra in limpio.split()) + "%"
    res = (_client.table("turnos").select("*").eq("tenant_id", tenant_id).eq("estado", "reservado")
           .ilike("paciente_nombre", patron).order("inicio", desc=True).limit(limite).execute())
    return res.data


# ---------- acciones del asistente del profesional (migrations/072) ----------
# Por WhatsApp de avisos (no reciben tenant_id para buscar: el tenant va
# guardado y asistente_profesional revisa que siga vinculado a ese número).

def guardar_accion_profesional(telefono: str, tenant_id: str, accion: dict) -> None:
    _client.table("acciones_profesional").upsert(
        {"telefono": telefono, "tenant_id": tenant_id, "accion": accion,
         "creado": datetime.now(timezone.utc).isoformat()}, on_conflict="telefono").execute()


def accion_profesional(telefono: str) -> dict | None:
    res = _client.table("acciones_profesional").select("*").eq("telefono", telefono).limit(1).execute()
    return res.data[0] if res.data else None


def borrar_accion_profesional(telefono: str) -> None:
    _client.table("acciones_profesional").delete().eq("telefono", telefono).execute()


def marcar_resumen_noche(tenant_id: str, dia: str) -> None:
    _client.table("tenants").update({"ultimo_resumen_noche": dia}).eq("id", tenant_id).execute()


def marcar_aviso_sin_anotar(tenant_id: str, dia: str) -> None:
    _client.table("tenants").update({"ultimo_aviso_sin_anotar": dia}).eq("id", tenant_id).execute()


def registrar_evento_pagina(tenant_id: str, tipo: str) -> None:
    _client.table("eventos_pagina").insert({"tenant_id": tenant_id, "tipo": tipo,
                                            "cuando": datetime.now(timezone.utc).isoformat()}).execute()


def eventos_pagina_desde(tenant_id: str, desde: datetime) -> dict[str, int]:
    res = (_client.table("eventos_pagina").select("tipo").eq("tenant_id", tenant_id)
           .gte("cuando", desde.isoformat()).limit(20000).execute())
    cuenta: dict[str, int] = {}
    for f in res.data:
        cuenta[f["tipo"]] = cuenta.get(f["tipo"], 0) + 1
    return cuenta


def crear_pago(fila: dict) -> dict:
    return _client.table("pagos").insert({**fila, "creado": datetime.now(timezone.utc).isoformat()}).execute().data[0]


def actualizar_pago(pago_id: str, campos: dict) -> None:
    _client.table("pagos").update(campos).eq("id", pago_id).execute()


def asignar_numero_recibo(tenant_id: str, pago_id: str) -> int | None:
    """Número correlativo del recibo dentro del profesional (1, 2, 3…). Se
    asigna una vez, al aprobarse; el índice único evita repetir si dos pagos
    se aprueban a la vez (se reintenta con el siguiente)."""
    actual = _client.table("pagos").select("recibo_numero").eq("id", pago_id).limit(1).execute().data
    if actual and actual[0].get("recibo_numero"):
        return actual[0]["recibo_numero"]
    for _ in range(3):
        usados = [p["recibo_numero"] for p in _client.table("pagos").select("recibo_numero").eq("tenant_id", tenant_id)
                  .execute().data if p.get("recibo_numero")]
        numero = max(usados, default=0) + 1
        try:
            _client.table("pagos").update({"recibo_numero": numero}).eq("id", pago_id).execute()
            return numero
        except Exception:
            continue
    return None


def pago_por_token(token: str) -> dict | None:
    """El token es la credencial de la página del paciente y la referencia en
    Mercado Pago."""
    if not token:
        return None
    res = _client.table("pagos").select("*").eq("token", token).limit(1).execute()
    return res.data[0] if res.data else None


def pago_de(tenant_id: str, pago_id: str) -> dict | None:
    res = _client.table("pagos").select("*").eq("tenant_id", tenant_id).eq("id", pago_id).limit(1).execute()
    return res.data[0] if res.data else None


def pagos_esperando(tenant_id: str) -> list[dict]:
    return _client.table("pagos").select("*").eq("tenant_id", tenant_id).eq("estado", "esperando").execute().data


@_precargable
def pagos_para_revisar(tenant_id: str) -> list[dict]:
    """Comprobantes por validar, pagos que llegaron tarde y devoluciones que
    el profesional tiene que hacer a mano."""
    res = _client.table("pagos").select("*").eq("tenant_id", tenant_id).in_("estado", ["comprobante", "aprobado_tarde"]) \
        .order("creado").execute()
    a_devolver = _client.table("pagos").select("*").eq("tenant_id", tenant_id).gt("a_devolver", 0).order("creado").execute()
    vistos = {p["id"] for p in res.data}
    return res.data + [p for p in a_devolver.data if p["id"] not in vistos]


@_precargable
def version_panel(tenant_id: str) -> str:
    """Una firma corta de lo que puede cambiar solo (pagos que llegan,
    devoluciones, reservas nuevas), para que el panel abierto se entere sin
    recargar. No lleva datos de pacientes."""
    pagos = (_client.table("pagos").select("id,estado,devuelto,resuelto").eq("tenant_id", tenant_id)
             .order("creado", desc=True).limit(30).execute().data)
    nuevas = (_client.table("turnos").select("id").eq("tenant_id", tenant_id).eq("reserva_nueva", True)
              .eq("estado", "reservado").execute().data)
    base = "|".join(f"{p['id']}:{p['estado']}:{p.get('devuelto') or 0}:{p.get('resuelto') or ''}" for p in pagos)
    base += "#" + ",".join(sorted(t["id"] for t in nuevas))
    return hashlib.sha256(base.encode()).hexdigest()[:16]


def pagos_resueltos_entre(tenant_id: str, desde: datetime, hasta: datetime) -> list[dict]:
    """Pagos web cobrados (o devueltos) en el período, para el resumen."""
    res = (_client.table("pagos").select("id,token,monto,metodo,estado,devuelto,a_devolver,mp_pago_id,resuelto,"
                                         "paciente_nombre,sesiones")
           .eq("tenant_id", tenant_id).in_("estado", ["aprobado", "aprobado_tarde", "devuelto"])
           .gte("resuelto", desde.isoformat()).lt("resuelto", hasta.isoformat()).order("resuelto", desc=True).execute())
    return res.data


def cobros_por_tenant(desde: datetime, hasta: datetime) -> dict[str, dict]:
    """Para /admin: cuánto cobró cada cuenta por la web en el período, por
    medio y neto de devoluciones. Sólo montos: nada de pacientes."""
    res = (_client.table("pagos").select("tenant_id,monto,metodo,devuelto")
           .in_("estado", ["aprobado", "aprobado_tarde", "devuelto"])
           .gte("resuelto", desde.isoformat()).lt("resuelto", hasta.isoformat()).execute())
    salida: dict[str, dict] = {}
    for p in res.data or []:
        c = salida.setdefault(p["tenant_id"], {"cantidad": 0, "total": 0, "mercadopago": 0, "transferencia": 0, "devuelto": 0})
        neto = int(p.get("monto") or 0) - int(p.get("devuelto") or 0)
        c["cantidad"] += 1
        c["total"] += neto
        c["devuelto"] += int(p.get("devuelto") or 0)
        c["mercadopago" if p.get("metodo") == "mercadopago" else "transferencia"] += neto
    return salida


def comprobantes_demorados(antes: str) -> dict[str, int]:
    """Para /admin: cuántos comprobantes de transferencia lleva cada cuenta
    sin revisar desde antes de `antes`. Sólo la cuenta, nada del pago."""
    res = (_client.table("pagos").select("tenant_id").eq("estado", "comprobante")
           .lt("comprobante_at", antes).execute())
    salida: dict[str, int] = {}
    for p in res.data or []:
        salida[p["tenant_id"]] = salida.get(p["tenant_id"], 0) + 1
    return salida


# ---------- interesados (/sumate) ----------
# De la plataforma, no de un tenant (como `admins`): sólo los lee administración.

def crear_interesado(datos: dict) -> None:
    _client.table("interesados").insert(datos).execute()


def interesados(limite: int = 50) -> list[dict]:
    res = _client.table("interesados").select("*").order("creado_at", desc=True).limit(limite).execute()
    return res.data


def marcar_interesado_visto(interesado_id: str, visto: bool = True) -> None:
    _client.table("interesados").update({"visto": visto}).eq("id", interesado_id).execute()


# ---------- bandeja de mensajes de pacientes (migrations/069) ----------

def guardar_mensaje_paciente(tenant_id: str, telefono: str, texto: str | None, tipo: str = "texto") -> dict:
    res = _client.table("mensajes_pacientes").insert({"tenant_id": tenant_id, "telefono": telefono,
                                                      "texto": (texto or "")[:2000] or None, "tipo": tipo}).execute()
    return res.data[0] if res.data else {}


def mensajes_sin_leer(tenant_id: str) -> int:
    res = (_client.table("mensajes_pacientes").select("id").eq("tenant_id", tenant_id)
           .eq("leido", False).execute())
    return len(res.data or [])


@_precargable
def mensajes_pacientes(tenant_id: str, limite: int = 30) -> list[dict]:
    """Los de ESTE tenant: primero los sin leer, después los más nuevos."""
    res = (_client.table("mensajes_pacientes").select("*").eq("tenant_id", tenant_id)
           .order("creado", desc=True).limit(limite).execute())
    return sorted(res.data or [], key=lambda m: m.get("leido") is True)


def mensaje_paciente(tenant_id: str, mensaje_id: str) -> dict | None:
    res = (_client.table("mensajes_pacientes").select("*").eq("tenant_id", tenant_id)
           .eq("id", mensaje_id).limit(1).execute())
    return res.data[0] if res.data else None


def actualizar_mensaje_paciente(tenant_id: str, mensaje_id: str, campos: dict) -> None:
    _client.table("mensajes_pacientes").update(campos).eq("tenant_id", tenant_id).eq("id", mensaje_id).execute()


def pago_esperando_de(tenant_id: str, telefono: str) -> dict | None:
    """El pago web de este paciente que espera transferencia, si hay uno."""
    res = (_client.table("turnos").select("pago_id").eq("tenant_id", tenant_id).eq("estado", "reservado")
           .eq("paciente_telefono", telefono).eq("pago_web", "esperando").limit(1).execute())
    if not res.data or not res.data[0].get("pago_id"):
        return None
    pago = pago_de(tenant_id, res.data[0]["pago_id"])
    return pago if pago and pago.get("estado") == "esperando" else None


# ---------- número compartido: «¿con quién es?» (migrations/070) ----------
# Igual que tenant_por_paciente, estas dos NO reciben tenant_id a propósito:
# son las que averiguan de qué profesional es el mensaje.

def tenants_de_paciente(telefono: str, dias: int = 120) -> list[dict]:
    """Los profesionales activos con turnos de este teléfono (futuros o de
    los últimos `dias`), el del turno más reciente primero."""
    desde = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
    res = (_client.table("turnos").select("tenant_id,inicio").eq("paciente_telefono", telefono)
           .eq("estado", "reservado").gte("inicio", desde).order("inicio", desc=True).execute())
    vistos, salida = set(), []
    for fila in res.data or []:
        if fila["tenant_id"] in vistos:
            continue
        vistos.add(fila["tenant_id"])
        t = get_tenant_by_id(fila["tenant_id"])
        if t and t.get("activo", True):
            salida.append(t)
    return salida


def eleccion_whatsapp(telefono: str) -> dict | None:
    res = _client.table("elecciones_whatsapp").select("*").eq("telefono", telefono).limit(1).execute()
    fila = res.data[0] if res.data else None
    if fila and datetime.fromisoformat(str(fila["vence"])) > datetime.now(timezone.utc):
        return fila
    return None


def guardar_eleccion_whatsapp(telefono: str, tenant_id: str | None, pendiente: str | None, vence: datetime) -> None:
    _client.table("elecciones_whatsapp").upsert({"telefono": telefono, "tenant_id": tenant_id, "pendiente": pendiente,
                                                 "vence": vence.isoformat()}, on_conflict="telefono").execute()
