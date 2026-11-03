#!/usr/bin/env python3
"""Alta de una cuenta de administración (/admin). La clave se escribe acá,
no pasa por ningún chat ni archivo.

Uso (con las variables de Railway, desde la carpeta del proyecto):
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # una sola vez
    railway run .venv/bin/python scripts/crear_admin.py
"""
import getpass
import os
import sys

import bcrypt
from supabase import create_client

url, clave_servicio = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
if not url or not clave_servicio:
    sys.exit("Faltan SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY en el entorno.")

usuario = input("Usuario de administración: ").strip()
clave = getpass.getpass("Clave (no se muestra; mínimo 12 caracteres): ")
if not usuario or len(clave) < 12:
    sys.exit("Hacen falta usuario y una clave de al menos 12 caracteres.")
if clave != getpass.getpass("Repetí la clave: "):
    sys.exit("Las claves no coinciden.")

create_client(url, clave_servicio).table("admins").insert(
    {"usuario": usuario, "pass_hash": bcrypt.hashpw(clave.encode(), bcrypt.gensalt()).decode()}
).execute()
print(f"OK — admin «{usuario}» creado. Entrá por /admin/login.")
