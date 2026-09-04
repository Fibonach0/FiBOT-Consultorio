"""Capa de datos. Todo lo que toca la tabla `turnos` o `tenants` pasa por acá,
y TODO recibe tenant_id explícito — no hay una sola función que pueda tocar
datos de un tenant sin que se lo pidan a propósito. Esa disciplina es la que
evita que un bug cruce datos de un profesional a otro."""
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
    """Próximos turnos libres de ESTE tenant, a partir de `desde`."""
    res = (
        _client.table("turnos")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("estado", "libre")
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


def reservar_turno(tenant_id: str, turno_id: str, telefono: str, nombre: str | None) -> bool:
    """Devuelve False si el turno ya no estaba libre (alguien se adelantó) —
    el llamador tiene que chequear esto y avisarle al paciente, nunca asumir
    éxito."""
    res = (
        _client.table("turnos")
        .update({"estado": "reservado", "paciente_telefono": telefono, "paciente_nombre": nombre})
        .eq("tenant_id", tenant_id)
        .eq("id", turno_id)
        .eq("estado", "libre")
        .execute()
    )
    return len(res.data) > 0


def turno_reservado_de(tenant_id: str, telefono: str) -> dict | None:
    """El próximo turno reservado de este paciente, si tiene uno."""
    ahora = datetime.now(timezone.utc)
    res = (
        _client.table("turnos")
        .select("*")
        .eq("tenant_id", tenant_id)
        .eq("estado", "reservado")
        .eq("paciente_telefono", telefono)
        .gte("inicio", ahora.isoformat())
        .order("inicio")
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def cancelar_turno(tenant_id: str, turno_id: str) -> None:
    """Libera el slot en vez de borrarlo — así vuelve a aparecer en
    turnos_libres() para que otro paciente lo pueda tomar."""
    _client.table("turnos").update(
        {"estado": "libre", "paciente_telefono": None, "paciente_nombre": None}
    ).eq("tenant_id", tenant_id).eq("id", turno_id).execute()


def crear_turnos_libres(tenant_id: str, inicios: list[datetime], duracion_minutos: int) -> int:
    """Alta en lote de horarios disponibles. `inicios` ya viene con timezone.
    Usa upsert por (tenant_id, inicio) para que correr esto dos veces con el
    mismo rango no duplique turnos."""
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
    res = _client.table("turnos").upsert(filas, on_conflict="tenant_id,inicio").execute()
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


def tenants_activos() -> list[dict]:
    res = _client.table("tenants").select("*").eq("activo", True).execute()
    return res.data
