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

# Mails (recuperar la clave). Proveedor: Resend (API HTTP). Sin las dos
# variables, el sistema no manda mails y la recuperación sigue siendo por
# el link que genera administración.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
MAIL_REMITENTE = os.environ.get("MAIL_REMITENTE", "")  # ej. "FiBOT Consultorio <avisos@tudominio.com>"

# Dominio principal (ej. consultorio.fibot.ar). Si está cargado, cualquier otro
# host redirige ahí, salvo el webhook de Meta y /health. Vacío = sin redirección.
DOMINIO_PRINCIPAL = os.environ.get("DOMINIO_PRINCIPAL", "").strip().lower()
