"""Capa de datos. Todo lo que toca la tabla `turnos` o `tenants` pasa por acá,
y TODO recibe tenant_id explícito — no hay una sola función que pueda tocar
datos de un tenant sin que se lo pidan a propósito. Esa disciplina es la que
evita que un bug cruce datos de un profesional a otro."""
import unicodedata
from datetime import datetime, timedelta, timezone

from supabase import create_client

import config

_client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_ROLE_KEY)


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
    """Resuelve la página pública de reserva (consultorios.fibot.ar/<slug>)."""
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
    res = (
        _client.table("bloqueos")
        .select("id")
        .eq("tenant_id", tenant_id)
        .lt("desde", fin)
        .gt("hasta", inicio)
        .limit(1)
        .execute()
    )
    return bool(res.data)


def clave_de(fila: dict) -> str:
    """Identidad de una ficha. El teléfono sólo no alcanza: dos hermanos pueden
    usar el WhatsApp del mismo adulto."""
    ficha = fila.get("ficha")
    return f"{fila['paciente_telefono']}~{ficha}" if ficha else fila["paciente_telefono"]


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
                   modalidad: str | None = None, ficha: str | None = None) -> bool:
    """Devuelve False si el turno ya no estaba libre (alguien se adelantó) —
    el llamador tiene que chequear esto y avisarle al paciente, nunca asumir
    éxito. También si se pisa con otra sesión: alguien pudo tener abierto ese
    horario desde antes de que la pasaran a esa hora."""
    turno = turno_por_id(tenant_id, turno_id)
    if not turno or _se_pisa(tenant_id, turno["inicio"], turno["fin"], turno_id):
        return False
    if _esta_bloqueado(tenant_id, turno["inicio"], turno["fin"]):
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
        .update({"estado": "reservado", "paciente_telefono": telefono, "paciente_nombre": nombre, "ficha": ficha,
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
                        modalidad: str | None = None, ficha: str | None = None) -> bool:
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
    return reservar_turno(tenant_id, res.data[0]["id"], telefono, nombre, valor, frecuencia, modalidad, ficha)


def reprogramar_turno(tenant_id: str, turno_id: str, nuevo_inicio: datetime, duracion_minutos: int) -> str | None:
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
    if _esta_bloqueado(tenant_id, nuevo_inicio.isoformat(), nuevo_fin.isoformat()):
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


def crear_turnos_libres(tenant_id: str, inicios: list[datetime], duracion_minutos: int) -> int:
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


def tests_tomados(tenant_id: str) -> list[dict]:
    res = _client.table("paciente_tests").select("*").eq("tenant_id", tenant_id).order("fecha", desc=True).execute()
    return res.data


def agregar_test(tenant_id: str, clave: str, test: str, fecha: str) -> None:
    telefono, ficha = partir_clave(clave)
    _client.table("paciente_tests").insert(
        {"tenant_id": tenant_id, "paciente_telefono": telefono, "ficha": ficha, "test": test, "fecha": fecha}
    ).execute()


def guardar_resultados_test(tenant_id: str, test_id: str, resultados: list[dict]) -> None:
    _client.table("paciente_tests").update({"resultados": resultados}).eq("tenant_id", tenant_id).eq("id", test_id).execute()


def textos_lectura(tenant_id: str) -> list[dict]:
    res = _client.table("textos_lectura").select("*").eq("tenant_id", tenant_id).order("titulo").execute()
    return res.data


def agregar_texto_lectura(tenant_id: str, titulo: str, texto: str) -> None:
    _client.table("textos_lectura").insert({"tenant_id": tenant_id, "titulo": titulo, "texto": texto}).execute()


def borrar_texto_lectura(tenant_id: str, texto_id: str) -> None:
    _client.table("textos_lectura").delete().eq("tenant_id", tenant_id).eq("id", texto_id).execute()


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


def informes(tenant_id: str) -> list[dict]:
    res = _client.table("informes").select("*").eq("tenant_id", tenant_id).order("created_at", desc=True).execute()
    return res.data


def agregar_informe(tenant_id: str, clave: str, contenido: dict) -> None:
    telefono, ficha = partir_clave(clave)
    _client.table("informes").insert(
        {"tenant_id": tenant_id, "paciente_telefono": telefono, "ficha": ficha, "contenido": contenido}).execute()


def borrar_informe(tenant_id: str, informe_id: str) -> None:
    _client.table("informes").delete().eq("tenant_id", tenant_id).eq("id", informe_id).execute()


def plantillas_cuestionario(tenant_id: str) -> list[dict]:
    res = _client.table("plantillas_cuestionario").select("*").eq("tenant_id", tenant_id).order("created_at").execute()
    return res.data


def agregar_plantilla(tenant_id: str, contenido: dict) -> None:
    _client.table("plantillas_cuestionario").insert({"tenant_id": tenant_id, "contenido": contenido}).execute()


def guardar_plantilla(tenant_id: str, plantilla_id: str, contenido: dict) -> None:
    _client.table("plantillas_cuestionario").update({"contenido": contenido}).eq("tenant_id", tenant_id).eq("id", plantilla_id).execute()


def borrar_plantilla(tenant_id: str, plantilla_id: str) -> None:
    _client.table("plantillas_cuestionario").delete().eq("tenant_id", tenant_id).eq("id", plantilla_id).execute()


def plantillas_informe(tenant_id: str) -> list[dict]:
    res = _client.table("plantillas_informe").select("*").eq("tenant_id", tenant_id).order("created_at").execute()
    return res.data


def agregar_plantilla_informe(tenant_id: str, contenido: dict) -> None:
    _client.table("plantillas_informe").insert({"tenant_id": tenant_id, "contenido": contenido}).execute()


def borrar_plantilla_informe(tenant_id: str, plantilla_id: str) -> None:
    _client.table("plantillas_informe").delete().eq("tenant_id", tenant_id).eq("id", plantilla_id).execute()


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
    res = (_client.table("tenants").select("id,nombre,rubro,slug,panel_user,activo,moderador,ultimo_ingreso,created_at")
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


def lista_de_espera(tenant_id: str) -> list[dict]:
    res = _client.table("lista_espera").select("*").eq("tenant_id", tenant_id).order("created_at").execute()
    return res.data


def agregar_a_espera(tenant_id: str, nombre: str, telefono: str, preferencia: str | None) -> None:
    _client.table("lista_espera").insert(
        {"tenant_id": tenant_id, "nombre": nombre, "telefono": telefono, "preferencia": preferencia,
         "created_at": datetime.now(timezone.utc).isoformat()}
    ).execute()


def quitar_de_espera(tenant_id: str, espera_id: str) -> None:
    _client.table("lista_espera").delete().eq("tenant_id", tenant_id).eq("id", espera_id).execute()


def marcar_ofrecido(tenant_id: str, espera_id: str, dia: str) -> None:
    _client.table("lista_espera").update({"ofrecido_en": dia}).eq("tenant_id", tenant_id).eq("id", espera_id).execute()


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


def sin_confirmar(tenant_id: str) -> list[dict]:
    """Reservas web futuras a las que se les mandó la confirmación y todavía no
    confirmaron."""
    ahora = datetime.now(timezone.utc)
    res = (_client.table("turnos").select("*").eq("tenant_id", tenant_id).eq("estado", "reservado")
           .gte("inicio", ahora.isoformat()).is_("confirmado_at", "null").order("inicio").execute())
    return [t for t in res.data if t.get("confirmacion_enviada_at")]
