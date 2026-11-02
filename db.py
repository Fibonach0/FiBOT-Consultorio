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


def _primer_nombre(nombre: str | None) -> str:
    sin_acentos = unicodedata.normalize("NFD", nombre or "")
    limpio = "".join(c for c in sin_acentos if not unicodedata.combining(c)).lower().split()
    return "".join(c for c in limpio[0] if c.isalnum()) if limpio else ""


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
    Mismo primer nombre, misma ficha: «Nina» y «Nina Acosta» son la misma
    persona. Si el nombre es otro, ficha aparte, aunque la otra persona ya no
    tenga sesiones: nadie hereda los datos de otro. Un error de tipeo se corrige
    moviendo la sesión desde la ficha."""
    filas = fichas_del_telefono(tenant_id, telefono)
    if not filas:
        return "", False
    if not nombre:
        return filas[0].get("ficha") or "", False
    buscado = _primer_nombre(nombre)
    for f in filas:
        sin_nombre = f["con_sesiones"] and not f.get("paciente_nombre")
        if sin_nombre or _primer_nombre(f.get("paciente_nombre")) == buscado:
            return f.get("ficha") or "", False
    return buscado or "otra", True


def mover_sesion(tenant_id: str, turno_id: str, clave: str, nombre: str) -> bool:
    """Pasa una sesión a otra ficha del mismo teléfono."""
    telefono, ficha = partir_clave(clave)
    res = (
        _client.table("turnos")
        .update({"ficha": ficha, "paciente_nombre": nombre})
        .eq("tenant_id", tenant_id)
        .eq("id", turno_id)
        .eq("estado", "reservado")
        .eq("paciente_telefono", telefono)
        .execute()
    )
    return len(res.data) > 0


def reservar_turno(tenant_id: str, turno_id: str, telefono: str, nombre: str | None,
                   valor: int | None = None, frecuencia: str | None = None,
                   modalidad: str | None = None) -> bool:
    """Devuelve False si el turno ya no estaba libre (alguien se adelantó) —
    el llamador tiene que chequear esto y avisarle al paciente, nunca asumir
    éxito. También si se pisa con otra sesión: alguien pudo tener abierto ese
    horario desde antes de que la pasaran a esa hora."""
    turno = turno_por_id(tenant_id, turno_id)
    if not turno or _se_pisa(tenant_id, turno["inicio"], turno["fin"], turno_id):
        return False
    if _esta_bloqueado(tenant_id, turno["inicio"], turno["fin"]):
        return False
    ficha, _ = ficha_para(tenant_id, telefono, nombre)
    res = (
        _client.table("turnos")
        .update({"estado": "reservado", "paciente_telefono": telefono, "paciente_nombre": nombre, "ficha": ficha,
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


def mismo_nombre(uno: str | None, otro: str | None) -> bool:
    return bool(uno and otro) and _primer_nombre(uno) == _primer_nombre(otro)


# Todo lo que describe a la RESERVA (no al horario) se limpia cuando el turno
# vuelve a 'libre'. Si quedara algo, el próximo paciente que reserve ese
# horario heredaría el "pagó" o el "ya le mandamos el recordatorio" del
# anterior.
_LIBERADO = {
    "estado": "libre",
    "paciente_telefono": None,
    "paciente_nombre": None,
    "ficha": "",
    "recordatorio_enviado": False,
    "asistencia": None,
    "pago": "pendiente",
    "reprogramado_desde": None,
    "valor": None,
    "frecuencia": None,
    "modalidad": None,
    "nota": None,
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
                        modalidad: str | None = None) -> bool:
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
    return reservar_turno(tenant_id, res.data[0]["id"], telefono, nombre, valor, frecuencia, modalidad)


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


def borrar_fecha(tenant_id: str, fecha_id: str) -> None:
    _client.table("paciente_fechas").delete().eq("tenant_id", tenant_id).eq("id", fecha_id).execute()


def turnos_para_recordar(tenant_id: str, horas_antes: int) -> list[dict]:
    """Turnos reservados cuyo recordatorio todavía no salió y ya están
    dentro de la ventana (inicio - horas_antes <= ahora < inicio)."""
    ahora = datetime.now(timezone.utc)
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
