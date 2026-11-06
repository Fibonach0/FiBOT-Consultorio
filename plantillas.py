"""Las plantillas de WhatsApp que usa la plataforma, como hay que crearlas en
WhatsApp Manager: texto sugerido, categoría, qué va en cada {{n}} y botones.

Es la fuente de la tabla de la guía y de las pruebas de los botones
(`test_conversation.py` manda cada texto de botón al bot y revisa que
entienda). Si cambia una frase que el bot reconoce, se rompe el botón: por
eso el texto de cada botón vive acá y no en la cabeza de quien crea la
plantilla.

Los botones de respuesta rápida son fijos (20 letras, hasta 3): cuando el
paciente toca uno, al webhook le llega el texto del botón y el bot lo trata
como si lo hubiera escrito (`app.webhook`, `type: button`). El botón de link
(«URL dinámica») lleva el final del link en el envío
(`whatsapp.enviar_plantilla(..., boton_url=)`), con `PAGO_PENDIENTE_BOTON` /
`PAGO_RECHAZADO_BOTON` en 1.
"""

# variable: la de Railway con el nombre aprobado. params: {{1}}, {{2}}…
# botones: respuesta rápida (texto exacto). url: botón de link, si lleva.
CATALOGO = [
    {"uso": "Recordatorio", "variable": "RECORDATORIO_TEMPLATE", "nombre": "recordatorio_turno",
     "params": ["profesional", "cuándo"],
     "texto": "Hola. Te recordamos tu turno con {{1}}: {{2}}. ¿Venís?",
     "ejemplo": ["Lic. Ana Gómez", "mañana martes 12/11 a las 10:00 (virtual)"],
     "botones": ["Sí, confirmo", "No puedo ir", "Cambiar el turno"]},
    {"uso": "Reprogramado", "variable": "REPROGRAMADO_TEMPLATE", "nombre": "turno_reprogramado",
     "params": ["profesional", "cuándo"],
     "texto": "Hola. {{1}} cambió el horario de tu turno: ahora es {{2}}.",
     "ejemplo": ["Lic. Ana Gómez", "jueves 14/11 a las 18:00"],
     "botones": ["Sí, confirmo", "Cambiar el turno"]},
    {"uso": "Confirmación de reserva web", "variable": "CONFIRMACION_TEMPLATE", "nombre": "confirmar_reserva",
     "params": ["profesional", "cuándo"],
     "texto": "Hola. Reservaste un turno con {{1}}: {{2}}. ¿Lo confirmás?",
     "ejemplo": ["Lic. Ana Gómez", "lunes 11/11 a las 9:15"],
     "botones": ["Sí, confirmo", "Cancelar"]},
    {"uso": "Recordatorio de pago", "variable": "RECORDATORIO_PAGO_TEMPLATE", "nombre": "recordatorio_pago",
     "params": ["profesional", "cuánto debe"],
     "texto": "Hola. Te recordamos que tenés {{2}} pendientes de sesiones con {{1}}.",
     "ejemplo": ["Lic. Ana Gómez", "$ 50.000"],
     "botones": ["¿Qué debo?", "Mi turno"]},
    {"uso": "Lista de espera", "variable": "LISTA_ESPERA_TEMPLATE", "nombre": "lista_espera_horario",
     "params": ["profesional", "cuándo", "link"],
     "texto": "Hola. Se liberó un horario con {{1}}: {{2}}. Si lo querés, reservalo acá: {{3}} (queda para quien lo reserve primero).",
     "ejemplo": ["Lic. Ana Gómez", "viernes 15/11 a las 11:00", "https://consultorio.fibot.ar/ana-gomez"],
     "botones": []},
    {"uso": "Pago por vencer", "variable": "PAGO_PENDIENTE_TEMPLATE", "nombre": "pago_por_vencer",
     "params": ["profesional", "cuándo"],
     "texto": "Hola. Tu reserva con {{1}} ({{2}}) todavía no está paga y se libera en unos minutos. Terminá el pago con el botón.",
     "ejemplo": ["Lic. Ana Gómez", "lunes 11/11 a las 9:15"],
     "botones": [], "url": {"texto": "Pagar", "base": "https://consultorio.fibot.ar/pago/{{1}}",
                             "ejemplo": "https://consultorio.fibot.ar/pago/abc123", "variable": "PAGO_PENDIENTE_BOTON"}},
    {"uso": "Comprobante rechazado", "variable": "PAGO_RECHAZADO_TEMPLATE", "nombre": "comprobante_rechazado",
     "params": ["profesional"],
     "texto": "Hola. {{1}} no pudo validar el comprobante de tu transferencia, así que el horario quedó libre. Si querés, podés volver a reservar.",
     "ejemplo": ["Lic. Ana Gómez"],
     "botones": [], "url": {"texto": "Reservar de nuevo", "base": "https://consultorio.fibot.ar/{{1}}",
                             "ejemplo": "https://consultorio.fibot.ar/ana-gomez", "variable": "PAGO_RECHAZADO_BOTON"}},
    {"uso": "Aviso al profesional", "variable": "AVISO_TEMPLATE", "nombre": "aviso_profesional",
     "params": ["el aviso en una línea"],
     "texto": "Aviso de FiBOT Consultorio: {{1}}. El detalle está en tu panel.",
     "ejemplo": ["hoy tenés 4 sesiones; la primera a las 9:15"],
     "botones": []},
    {"uso": "Mensaje del profesional", "variable": "MENSAJE_PROFESIONAL_TEMPLATE", "nombre": "mensaje_profesional",
     "params": ["profesional", "el mensaje"],
     "texto": "Hola. {{1}} te dejó este mensaje: {{2}}. Si querés contestarle, escribí por acá.",
     "ejemplo": ["Lic. Ana Gómez", "Llego 10 minutos tarde, perdón"],
     "botones": []},
    {"uso": "Resumen de la mañana", "variable": "RESUMEN_TEMPLATE", "nombre": "resumen_manana",
     "params": ["el resumen en una línea"], "para": "profesional",
     "texto": "Buen día. Tu consultorio hoy: {{1}}. Tocá un botón para ver el detalle.",
     "ejemplo": ["4 sesiones, la primera a las 9:15, y 1 comprobante para validar"],
     "botones": ["Ver hoy", "Pagos", "Mensajes"]},
]

# Qué tiene que contestar el bot a cada botón (lo prueba test_conversation).
QUE_HACE = {
    "Sí, confirmo": "confirma el próximo turno",
    "No puedo ir": "pregunta antes de cancelar (no cancela solo)",
    "Cambiar el turno": "manda la lista de horarios para pasarlo",
    "Cancelar": "cancela el turno",
    "¿Qué debo?": "lista las sesiones sin pagar con fecha y valor",
    "Mi turno": "dice cuándo es el próximo turno",
}


# Los botones de las plantillas que le llegan al PROFESIONAL los contesta su
# asistente (lo prueba test_asistente_profesional), no el bot de pacientes.
QUE_HACE_PROFESIONAL = {
    "Ver hoy": "le muestra las sesiones de hoy, con nombres",
    "Pagos": "le lista los comprobantes y devoluciones para revisar",
    "Mensajes": "le muestra los mensajes de pacientes sin leer",
}


def botones_de_todas(para: str = "paciente") -> list[str]:
    """Los botones de las plantillas de pacientes (o, con
    para="profesional", los de las que le llegan al profesional)."""
    vistos: list[str] = []
    for p in CATALOGO:
        if p.get("para", "paciente") != para:
            continue
        for b in p["botones"]:
            if b not in vistos:
                vistos.append(b)
    return vistos
