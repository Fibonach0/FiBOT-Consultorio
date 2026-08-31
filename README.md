# FiBOT Consultorio

FiBOT para profesionales con turnos (psicólogos, odontólogos, nutricionistas,
kinesiólogos…): agenda, recordatorios automáticos y reserva de horarios, todo
por WhatsApp. Un solo deploy sirve a todos los profesionales — no hay que
levantar un servicio nuevo por cada uno.

Ver [`CLAUDE.md`](./CLAUDE.md) para la arquitectura completa.

## Arrancar

```bash
pip install -r requirements.txt
cp .env.example .env      # completar
python app.py             # local, :5000
```

Dar de alta un profesional (tenant):

```bash
python scripts/crear_tenant.py
```

Aplicar el esquema en Supabase: `migrations/001_init.sql`.

## Deploy

Railway, `nixpacks.toml` + `Procfile`. Un solo worker (el recordatorio
automático corre como hilo de fondo en el proceso).
