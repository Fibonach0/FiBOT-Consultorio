"""El buscador del panel (tipo Spotlight: Ctrl/⌘+K o «/»).

Busca en todo lo del profesional: sus pacientes (nombre, teléfono, obra
social, notas, motivo de consulta, notas de sesión…), sus próximas sesiones,
la lista de espera, los mensajes, cada ajuste y cada acción del panel. El
índice va dentro de la página (lo arma `indice()` con el mismo contexto de la
agenda) y se filtra en el navegador: responde mientras se escribe, sin ir al
servidor. Sólo trae lo de ESTE tenant: sale de `_comunes_panel`, que ya está
filtrado por tenant_id; nada de otro profesional entra en la página.
"""
import hashlib
import hmac

import config
import db

# (grupo, título, detalle, (endpoint, args, ancla), palabras para encontrarlo)
CATALOGO = [
    # --- Ajustes · Agenda
    ("Ajustes", "Horario de atención", "Días y horas en que atendés", ("ajustes", {"p": "agenda"}, "horario"),
     "horario dias horas franjas atiendo atencion semana lunes martes miercoles jueves viernes sabado domingo"),
    ("Ajustes", "Duración de cada turno y descanso", "Minutos de cada sesión y entre sesiones", ("ajustes", {"p": "agenda"}, "horario"),
     "duracion minutos sesion turno descanso entre turnos 45 50 60"),
    ("Ajustes", "Feriados y días puente", "Si atendés o se bloquean solos", ("ajustes", {"p": "agenda"}, "horario"),
     "feriados puente no laborables bloquear atender"),
    ("Ajustes", "Precio de la sesión", "Cuánto cobrás la sesión", ("ajustes", {"p": "agenda"}, "tipos"),
     "precio valor honorarios cuanto cobro tarifa arancel plata pesos"),
    ("Ajustes", "Tipos de turno", "Primera entrevista, sesión… con duración y precio", ("ajustes", {"p": "agenda"}, "tipos"),
     "tipos de turno primera entrevista admision duracion distinta"),
    ("Ajustes", "Consultorios", "Si atendés en más de un lugar", ("ajustes", {"p": "agenda"}, "sedes"),
     "consultorios sedes lugar direccion donde atiendo"),
    # --- Ajustes · Mi página
    ("Ajustes", "Tu página de turnos (link)", "Copiar, compartir por WhatsApp o ver tu página", ("ajustes", {"p": "pagina"}, "mi-pagina"),
     "link enlace pagina url compartir copiar instagram reservas web publica"),
    ("Ajustes", "Código QR de tu página", "Para imprimir o poner en el consultorio", ("ajustes", {"p": "pagina"}, "mi-pagina"),
     "qr codigo imprimir cartel"),
    ("Ajustes", "Cómo te presentás", "Título, sobre mí, temas, formación", ("ajustes", {"p": "pagina"}, "presentacion"),
     "presentacion sobre mi titulo descripcion enfoques temas formacion perfil bio"),
    ("Ajustes", "Foto de perfil y portada", "Las imágenes de tu página", ("ajustes", {"p": "pagina"}, "presentacion"),
     "foto imagen portada perfil avatar"),
    ("Ajustes", "Color de tu página", "Verde, azul, terracota…", ("ajustes", {"p": "pagina"}, "presentacion"), "color colores tema pagina"),
    ("Ajustes", "Obras sociales y prepagas", "Lo que aceptás, en tu página", ("ajustes", {"p": "pagina"}, "presentacion"),
     "obras sociales prepagas osde swiss reintegro factura"),
    ("Ajustes", "Presencial o videollamada", "Qué turnos ofrecés en tu página", ("ajustes", {"p": "pagina"}, "reservas"),
     "modalidad presencial virtual videollamada online"),
    ("Ajustes", "Link de videollamada", "Meet, Zoom… se lo mandamos con el recordatorio", ("ajustes", {"p": "pagina"}, "reservas"),
     "link videollamada meet zoom jitsi online virtual"),
    ("Ajustes", "Aparecer en la lista de profesionales", "El directorio de la plataforma", ("ajustes", {"p": "pagina"}, "reservas"),
     "directorio lista de profesionales aparecer buscar"),
    # --- Ajustes · Cobros
    ("Ajustes", "Cobrar las reservas web", "Mercado Pago o transferencia, total o seña", ("ajustes", {"p": "cobros"}, "cobros"),
     "cobrar cobros pagos reservas web mercado pago transferencia"),
    ("Ajustes", "Conectar Mercado Pago", "Para cobrar con tarjeta o dinero en cuenta", ("ajustes", {"p": "cobros"}, "cobros"),
     "mercado pago mp conectar tarjeta"),
    ("Ajustes", "Seña", "Cobrar una parte al reservar", ("ajustes", {"p": "cobros"}, "cobros"), "sena anticipo adelanto"),
    ("Ajustes", "Datos para transferir", "Alias, CBU y titular", ("ajustes", {"p": "cobros"}, "cobros"), "alias cbu cvu transferencia banco titular"),
    # --- Ajustes · Cancelaciones
    ("Ajustes", "Hasta cuándo pueden cancelar", "Cancelar o cambiar desde la web", ("ajustes", {"p": "cancelaciones"}, "cancelaciones"),
     "cancelar cancelacion plazo anticipacion horas antes cambiar turno"),
    ("Ajustes", "Qué pasa con el horario cancelado", "Liberarlo o preguntarte antes", ("ajustes", {"p": "cancelaciones"}, "cancelaciones"),
     "horario cancelado liberar retener preguntar hueco"),
    ("Ajustes", "Devolución si cancelan", "Qué se devuelve si ya había pagado", ("ajustes", {"p": "cancelaciones"}, "cancelaciones"),
     "devolucion devolver reintegro reembolso plata"),
    ("Ajustes", "Ofrecer el horario a la lista de espera", "Automático o te lo propone el asistente", ("ajustes", {"p": "cancelaciones"}, "cancelaciones"),
     "lista de espera ofrecer automatico"),
    # --- Ajustes · Avisos
    ("Ajustes", "Tu WhatsApp para avisos", "Donde te llegan reservas, cancelaciones y pagos", ("ajustes", {"p": "avisos"}, "avisos"),
     "whatsapp telefono celular avisos notificaciones numero"),
    ("Ajustes", "Vincular WhatsApp con el asistente", "El código VINCULAR", ("ajustes", {"p": "avisos"}, "avisos"),
     "vincular codigo asistente bot whatsapp"),
    ("Ajustes", "Resumen de la noche y avisos de pago", "Qué te mandamos y cuándo", ("ajustes", {"p": "avisos"}, "avisos"),
     "resumen noche avisos pagos mail mensual"),
    # --- Ajustes · Cuenta
    ("Ajustes", "Matrícula, CUIT y domicilio", "Salen en recibos e informes", ("ajustes", {"p": "cuenta"}, "ajustes"),
     "matricula cuit domicilio recibo factura datos"),
    ("Ajustes", "Tu mail", "Para recuperar la clave", ("ajustes", {"p": "cuenta"}, "ajustes"), "mail email correo"),
    ("Ajustes", "Firma y sello", "Para informes y constancias", ("ajustes", {"p": "cuenta"}, "firma"), "firma sello informes constancias"),
    ("Ajustes", "Cambiar la clave", "Tu contraseña del panel", ("ajustes", {"p": "cuenta"}, "seguridad"), "clave contrasena password cambiar"),
    ("Ajustes", "Doble factor", "Un código del celular al entrar", ("ajustes", {"p": "cuenta"}, "seguridad"), "doble factor 2fa codigo autenticador seguridad"),
    ("Ajustes", "Llaves de acceso", "Entrar con huella o cara", ("ajustes", {"p": "cuenta"}, "seguridad"), "llaves de acceso huella cara passkey"),
    ("Ajustes", "Sesiones abiertas", "Dónde tenés el panel abierto; cerrarlas", ("ajustes", {"p": "cuenta"}, "seguridad"),
     "sesiones abiertas dispositivos cerrar sesion"),
    ("Ajustes", "Instalar el panel como app", "Con su ícono en el celular o la compu", ("ajustes", {"p": "cuenta"}, "instalar"),
     "instalar app aplicacion icono celular"),
    # --- Ajustes · Avanzado
    ("Ajustes", "Plantillas de informe", "Tus modelos de informe", ("ajustes", {"p": "avanzado"}, "herramientas"), "plantillas informe modelos"),
    ("Ajustes", "Cuestionarios propios", "Crear, importar o compartir cuestionarios", ("ajustes", {"p": "avanzado"}, "herramientas"),
     "cuestionarios propios crear importar biblioteca escalas"),
    # --- Acciones en la agenda
    ("Acciones", "Repaso de la semana", "Quién faltó, quién debe, series por terminar y la semana que viene",
     ("agenda", {"revision": "1"}, "revision"), "repaso revision resumen semana semanal viernes faltaron deben balance cierre"),
    ("Acciones", "Bloquear días (vacaciones)", "Nadie puede reservar esos días", ("agenda", {}, "bloqueos"),
     "bloquear dias vacaciones congreso licencia feriado libre"),
    ("Acciones", "Pausar mi página", "Dejar de tomar turnos un tiempo", ("agenda", {}, "bloqueos"), "pausar pausa vacaciones dejar de tomar turnos"),
    ("Acciones", "Mensaje a la lista de espera al volver", "Lo que les llega 2 días antes de que termine tu pausa", ("agenda", {}, "bloqueos"), "mensaje vuelta volver pausa lista de espera aviso"),
    ("Acciones", "Lista de espera", "Anotar a alguien o ofrecerle un horario", ("agenda", {}, "espera"), "lista de espera anotar ofrecer"),
    ("Acciones", "Generar horarios a mano", "Fechas sueltas fuera de tu horario", ("agenda", {}, "generar"), "generar horarios mano fechas sueltas"),
    ("Acciones", "Aumento de honorarios", "Subir el valor de las próximas sesiones", ("agenda", {}, "aumento"), "aumento subir valor precio honorarios"),
    ("Acciones", "Planilla para obra social", "Imprimible por mes", ("agenda", {}, "obra-social"), "planilla obra social imprimir mes"),
    ("Acciones", "Cobros del mes", "Lo que entró por la web; Excel para el contador", ("agenda", {}, "cobros"), "cobros mes excel contador ingresos"),
    ("Acciones", "Para resolver", "Mensajes, pagos, reservas nuevas", ("agenda", {}, "para-resolver"), "pendientes resolver bandeja"),
    ("Acciones", "Mensajes de pacientes", "Lo que te escribieron por WhatsApp", ("agenda", {}, "mensajes"), "mensajes whatsapp bandeja responder"),
    ("Acciones", "Pagos para revisar", "Comprobantes y devoluciones", ("agenda", {}, "pagos-revisar"), "pagos comprobantes transferencias devoluciones revisar"),
    ("Acciones", "Agenda de hoy", "Tus sesiones de hoy", ("agenda", {}, ""), "hoy agenda sesiones dia"),
    ("Acciones", "Calendario del mes", "El mes completo", ("agenda", {}, ""), "calendario mes"),
    ("Ayuda", "Cómo se usa", "La guía, con videos de cada parte", ("ayuda", {}, ""), "ayuda guia como se usa tutorial videos"),
    ("Ayuda", "Salir", "Cerrar tu sesión", ("logout", {}, ""), "salir cerrar sesion logout"),
]
# Sólo para los rubros con tests.
CATALOGO_TESTS = [
    ("Ajustes", "Plazo para repetir cada test", "Cuándo se puede volver a tomar", ("ajustes", {"p": "avanzado"}, "herramientas"),
     "plazo repetir test meses"),
    ("Ajustes", "Textos para fluidez lectora", "Tus textos para tomar lectura", ("ajustes", {"p": "avanzado"}, "herramientas"),
     "textos fluidez lectora lectura"),
]


def _texto(*partes) -> str:
    return " · ".join(str(p).strip() for p in partes if p and str(p).strip())


def _ref(*partes) -> str:
    """Un identificador opaco y estable (para «Recientes», que se guarda en el
    navegador): ni el nombre ni el teléfono del paciente salen de la página."""
    return hmac.new(config.FLASK_SECRET_KEY.encode(), "|".join(map(str, partes)).encode(), hashlib.sha256).hexdigest()[:14]


def indice(tenant: dict, comunes: dict, rubro: dict, url_for) -> dict:
    """Todo lo que se puede buscar, listo para JSON. `comunes` es el
    contexto del panel de ESTE tenant (_comunes_panel)."""
    def url(endpoint, args, ancla):
        return url_for(endpoint, **args) + (f"#{ancla}" if ancla else "")

    # Secciones que el panel muestra sólo cuando hay algo: si no están, no se ofrecen.
    fichas_t = comunes.get("fichas") or []
    hay = {"mensajes": bool(comunes.get("mensajes")), "pagos-revisar": bool(comunes.get("pagos_revisar")),
           "para-resolver": any(comunes.get(k) for k in ("mensajes", "pagos_revisar", "reservas_nuevas", "huecos",
                                                          "cuestionarios_nuevos", "tests_para_repetir")),
           "obra-social": any(f.get("obra_social") for f in fichas_t), "mi-pagina": bool(tenant.get("slug")),
           "aumento": any(f.get("estado") == "activo" and not f.get("sin_sesiones") for f in fichas_t)}
    items = [{"g": g, "t": t, "d": d, "u": url(*destino), "k": k}
             for g, t, d, destino, k in CATALOGO + (CATALOGO_TESTS if rubro.get("tests") else [])
             if hay.get(destino[2], True)]
    fichas, sesiones = [], []
    for f in comunes.get("fichas") or []:
        telefono = f.get("telefono") if db.tiene_whatsapp(f.get("telefono")) else ""
        notas_sesion = [t.get("nota") for t in (f.get("sesiones") or []) if t.get("nota")]
        fichas.append({
            "n": f.get("nombre") or "Sin nombre", "c": f.get("clave"), "r": _ref(tenant["id"], f.get("clave")), "d": f.get("dom"), "tel": telefono,
            "p": f.get("proxima"), "a": f.get("adeudado") or 0, "e": f.get("estado"), "os": f.get("obra_social") or "",
            # Lo que se busca «adentro» de la ficha; se muestra un pedacito alrededor de lo encontrado.
            "x": _texto(f.get("motivo_consulta"), f.get("objetivos"), f.get("notas"), f.get("como_viene"), f.get("psiquiatra"),
                        f.get("medicacion"), f.get("plan_crisis"), f.get("escuela"), f.get("docente"), f.get("numero_afiliado"),
                        *[p.get("nombre") for p in f.get("personas") or []], *[e.get("texto") for e in (f.get("bitacora") or [])[:30]],
                        *notas_sesion[-30:])[:6000],
        })
        for t in (f.get("proximas") or [])[:6]:
            sesiones.append({"d": f.get("dom"), "n": f.get("nombre") or "Sin nombre", "w": f"{t.get('_dia', '')} {t.get('_hora', '')}".strip(),
                             "i": t.get("inicio")})
    espera = [{"n": p.get("nombre"), "tel": p.get("telefono"), "pr": p.get("preferencia") or ""} for p in comunes.get("espera") or []]
    mensajes = [{"n": m.get("_quien"), "x": (m.get("_texto") or "")[:300], "w": m.get("_cuando")} for m in comunes.get("mensajes") or []]
    return {"items": items, "fichas": fichas, "sesiones": sesiones, "espera": espera, "mensajes": mensajes,
            "agenda": url_for("agenda"), "ayuda": url_for("ayuda"), "cuenta": _ref("cuenta", tenant["id"])[:10],
            "asistente": url_for("agenda_asistente"), "confirmar": url_for("agenda_asistente_confirmar"),
            "salir": url_for("logout")}
