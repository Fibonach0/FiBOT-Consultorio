#!/usr/bin/env python3
"""Alta de un profesional nuevo (tenant). No hay alta self-service en esta
versión — "todo se hace a mano": este script lo corre Nacho, con las
variables de entorno de Supabase ya cargadas (las mismas que usa la app).

Uso:
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... python scripts/crear_tenant.py
"""
import getpass
import os
import re
import sys

import bcrypt
from supabase import create_client

# No pueden ser slug: chocan con rutas fijas de la plataforma (aunque Flask
# les da prioridad a esas rutas fijas igual, un tenant con ese slug nunca
# tendría página pública alcanzable, así que mejor cortarlo acá).
SLUGS_RESERVADOS = {"login", "logout", "agenda", "health", "webhook"}

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    sys.exit("Faltan SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY en el entorno.")

client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

nombre = input("Nombre para mostrar (ej. 'Lic. Ana Pérez'): ").strip()
rubro = input("Rubro (ej. 'psicología') [opcional]: ").strip() or None
slug = input("Slug para la página pública de reserva (ej. 'anaperez', vacío = sin página pública): ").strip() or None
phone_number_id = input("whatsapp_phone_number_id (de la app de Meta): ").strip()
display_phone = input("Número que ve el paciente (opcional, solo informativo): ").strip() or None
timezone = input("Timezone [America/Argentina/Buenos_Aires]: ").strip() or "America/Argentina/Buenos_Aires"
duracion = input("Duración de turno en minutos [50]: ").strip() or "50"
recordatorio_horas = input("Recordatorio cuántas horas antes [24]: ").strip() or "24"
panel_user = input("Usuario de panel (para el login web): ").strip()
clave = getpass.getpass("Clave de panel (no se muestra en pantalla): ")

if not (nombre and phone_number_id and panel_user and clave):
    sys.exit("Nombre, phone_number_id, usuario y clave son obligatorios.")

if slug:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        sys.exit("Slug inválido: solo minúsculas, números y guiones (sin espacios ni acentos).")
    if slug in SLUGS_RESERVADOS:
        sys.exit(f"'{slug}' es una ruta reservada de la plataforma, elegí otro slug.")

pass_hash = bcrypt.hashpw(clave.encode(), bcrypt.gensalt()).decode()

res = (
    client.table("tenants")
    .insert(
        {
            "nombre": nombre,
            "rubro": rubro,
            "slug": slug,
            "whatsapp_phone_number_id": phone_number_id,
            "whatsapp_display_phone": display_phone,
            "timezone": timezone,
            "duracion_turno_minutos": int(duracion),
            "recordatorio_horas_antes": int(recordatorio_horas),
            "panel_user": panel_user,
            "panel_pass_hash": pass_hash,
        }
    )
    .execute()
)

print(f"OK — tenant creado: {res.data[0]['id']}")
if slug:
    print(f"Página pública de reserva: https://consultorios.fibot.ar/{slug}")
