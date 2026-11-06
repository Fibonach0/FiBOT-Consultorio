"""Toda la configuración por variable de entorno. Sin defaults inseguros:
si falta algo obligatorio, el proceso no arranca (mejor eso que arrancar
a medias y fallar en el primer webhook)."""
import os
import sys

REQUIRED = [
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "META_ACCESS_TOKEN",
    "META_APP_SECRET",
    "META_VERIFY_TOKEN",
    "FLASK_SECRET_KEY",
]

_faltantes = [k for k in REQUIRED if not os.environ.get(k)]
if _faltantes:
    sys.exit(f"Faltan variables de entorno obligatorias: {', '.join(_faltantes)}")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

META_ACCESS_TOKEN = os.environ["META_ACCESS_TOKEN"]
META_APP_SECRET = os.environ["META_APP_SECRET"]
META_VERIFY_TOKEN = os.environ["META_VERIFY_TOKEN"]
META_GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v20.0")

FLASK_SECRET_KEY = os.environ["FLASK_SECRET_KEY"]

RECORDATORIO_INTERVALO_MIN = int(os.environ.get("RECORDATORIO_INTERVALO_MIN", "10"))

# Número de WhatsApp de la PLATAFORMA. Habilita el modo "sin número propio":
# un profesional puede empezar a usar el sistema hoy, sin dar de alta su número
# en el Business Portfolio (que es verificación de Meta y días de trámite).
# El recordatorio ya nombra al profesional en el cuerpo del mensaje, así que
# sale bien igual: "Te recuerdo tu turno con Lic. Ana Pérez…".
# Vacío = la función está apagada y cada tenant necesita su propio número.
PLATAFORMA_PHONE_NUMBER_ID = os.environ.get("PLATAFORMA_PHONE_NUMBER_ID", "")

# Cómo se presenta el bot en el número de la plataforma. Ahí lo comparten
# varios profesionales, así que no habla en nombre de uno solo: se presenta
# como la plataforma y aclara con quién son los turnos del paciente.
PLATAFORMA_NOMBRE = os.environ.get("PLATAFORMA_NOMBRE", "").strip() or "FiBOT Consultorio"

# Identificador de la cuenta de WhatsApp (WABA) de ese número. Opcional: sólo
# lo usa la página de WhatsApp de administración para revisar si la app está
# suscripta y en qué estado están las plantillas.
META_WABA_ID = os.environ.get("META_WABA_ID", "")

# El recordatorio sale el día ANTES del turno, a alguien que reservó por la
# página web y nunca le escribió al bot: siempre fuera de la ventana de 24 h
# de Meta, donde el texto libre está prohibido. Por eso hace falta una
# plantilla aprobada, con dos parámetros en este orden:
#   {{1}} nombre del profesional      {{2}} cuándo es el turno
# Vacía = se manda texto libre, que sólo funciona si el paciente escribió en
# las últimas 24 h. Sirve para probar, no para producción.
RECORDATORIO_TEMPLATE = os.environ.get("RECORDATORIO_TEMPLATE", "")
RECORDATORIO_TEMPLATE_LANG = os.environ.get("RECORDATORIO_TEMPLATE_LANG", "es_AR")

# Lo mismo para avisar que una sesión cambió de horario, con los mismos dos
# parámetros: {{1}} nombre del profesional   {{2}} cuándo es el nuevo horario.
# Vacía = texto libre, con la misma limitación de las 24 h.
REPROGRAMADO_TEMPLATE = os.environ.get("REPROGRAMADO_TEMPLATE", "")

# Resumen diario al profesional, con un parámetro: {{1}} el resumen en una línea.
# Vacía = texto libre, con la misma limitación de las 24 h.
AVISO_TEMPLATE = os.environ.get("AVISO_TEMPLATE", "")

# Recordatorio de pago, lo dispara el profesional a mano desde la ficha:
#   {{1}} nombre del profesional      {{2}} cuánto debe
# Vacía = texto libre, con la misma limitación de las 24 h.
RECORDATORIO_PAGO_TEMPLATE = os.environ.get("RECORDATORIO_PAGO_TEMPLATE", "")

# Ofrecer un horario a alguien de la lista de espera:
#   {{1}} nombre del profesional      {{2}} cuándo es      {{3}} link para reservar
# Vacía = texto libre, con la misma limitación de las 24 h.
LISTA_ESPERA_TEMPLATE = os.environ.get("LISTA_ESPERA_TEMPLATE", "")

# Freno de envíos: lista de teléfonos separados por coma. Si está cargada, el
# sistema SOLO le manda WhatsApp a esos números y al resto lo saltea (lo deja
# en el log). Es para probar con un número real sin que los pacientes
# inventados de la demo —que pueden ser teléfonos de gente real— reciban
# nada. Vacía = sin freno: le manda a todos (producción).
WHATSAPP_SOLO_A = [n.strip() for n in os.environ.get("WHATSAPP_SOLO_A", "").split(",") if n.strip()]

# Confirmación de una reserva hecha en la web (el paciente nunca escribió:
# fuera de las 24 h, hace falta plantilla). Botones de respuesta rápida
# «Sí, confirmo» y «Cancelar»:
#   {{1}} nombre del profesional      {{2}} cuándo es
# Vacía = no se manda confirmación.
CONFIRMACION_TEMPLATE = os.environ.get("CONFIRMACION_TEMPLATE", "")

# Aviso al paciente de que se vence el plazo para pagar su reserva web:
#   {{1}} nombre del profesional      {{2}} cuándo es      {{3}} link para pagar
# Vacía = no se avisa (el horario igual se libera al vencer).
PAGO_PENDIENTE_TEMPLATE = os.environ.get("PAGO_PENDIENTE_TEMPLATE", "")

# Aviso al paciente de que el profesional rechazó su comprobante de
# transferencia y el horario se liberó:
#   {{1}} nombre del profesional      {{2}} link para volver a reservar
# Vacía = sólo se avisa por mail (si dejó email).
PAGO_RECHAZADO_TEMPLATE = os.environ.get("PAGO_RECHAZADO_TEMPLATE", "")

# Botón de link en vez del link en el texto (plantillas.py tiene el texto de
# cada una). Con «1», la plantilla tiene un botón «URL dinámica» y el link va
# como sufijo del botón, no como {{n}} del texto:
#   Pago por vencer:        {{1}} profesional, {{2}} cuándo; botón https://<dominio>/pago/{{1}}
#   Comprobante rechazado:  {{1}} profesional;              botón https://<dominio>/{{1}}
# Vacía = el link va en el texto, como antes.
PAGO_PENDIENTE_BOTON = os.environ.get("PAGO_PENDIENTE_BOTON", "") in ("1", "true", "si")
PAGO_RECHAZADO_BOTON = os.environ.get("PAGO_RECHAZADO_BOTON", "") in ("1", "true", "si")

# Mercado Pago (cobro de las reservas web). Son de la APLICACIÓN de la
# plataforma en Mercado Pago Developers; cada profesional conecta su propia
# cuenta con «Conectar Mercado Pago» (OAuth) y cobra en ella. MP_PRUEBA=1 usa
# las credenciales y el checkout de prueba.
MP_CLIENT_ID = os.environ.get("MP_CLIENT_ID", "")
MP_CLIENT_SECRET = os.environ.get("MP_CLIENT_SECRET", "")
MP_PRUEBA = os.environ.get("MP_PRUEBA", "") in ("1", "true", "si")

# Mails (recuperar la clave). Proveedor: Resend (API HTTP). Sin las dos
# variables, el sistema no manda mails y la recuperación sigue siendo por
# el link que genera administración.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
MAIL_REMITENTE = os.environ.get("MAIL_REMITENTE", "")  # ej. "FiBOT Consultorio <avisos@tudominio.com>"

# Dominio principal (ej. consultorio.fibot.ar). Si está cargado, cualquier otro
# host redirige ahí, salvo el webhook de Meta y /health. Vacío = sin redirección.
DOMINIO_PRINCIPAL = os.environ.get("DOMINIO_PRINCIPAL", "").strip().lower()
