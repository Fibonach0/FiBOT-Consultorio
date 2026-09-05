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

# El recordatorio sale el día ANTES del turno, a alguien que reservó por la
# página web y nunca le escribió al bot: siempre fuera de la ventana de 24 h
# de Meta, donde el texto libre está prohibido. Por eso hace falta una
# plantilla aprobada, con dos parámetros en este orden:
#   {{1}} nombre del profesional      {{2}} cuándo es el turno
# Vacía = se manda texto libre, que sólo funciona si el paciente escribió en
# las últimas 24 h. Sirve para probar, no para producción.
RECORDATORIO_TEMPLATE = os.environ.get("RECORDATORIO_TEMPLATE", "")
RECORDATORIO_TEMPLATE_LANG = os.environ.get("RECORDATORIO_TEMPLATE_LANG", "es_AR")
