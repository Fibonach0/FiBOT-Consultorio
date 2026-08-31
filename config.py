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
