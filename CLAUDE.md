# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Qué es

**FiBOT Consultorio** — versión de FiBOT para profesionales con agenda de turnos
(psicólogos, odontólogos, nutricionistas, kinesiólogos…). Nace de una idea de
negocio distinta a la de `Fleet-Test-Template`: en vez de un deploy nuevo por
cliente, acá **un solo deploy sirve a todos los profesionales a la vez**
(multi-tenant), porque el ticket por cliente es demasiado chico para bancar un
deploy propio por cada uno.

Pensado para arrancar con conocidos de la familia (canon mínimo, a cambio de
que ellos mismos consigan más clientes) — no hay alta self-service todavía,
todo se da de alta a mano con `scripts/crear_tenant.py`.

Referencia de mercado: [Calena](https://calena.la/) hace lo mismo para
psicólogos (agenda + pagos + recordatorios + WhatsApp) y ya cobra por eso —
esto no es una apuesta a ciegas, el modelo está validado por un tercero.

## Cómo es "multi-tenant" acá

Un solo Meta Business Portfolio, una sola `META_ACCESS_TOKEN` — cada
profesional agrega su propio número de WhatsApp Business a ESE Business, y lo
que los distingue es `whatsapp_phone_number_id`, guardado en la tabla
`tenants` (no en variables de entorno, a diferencia de `Fleet-Test-Template`).
Meta manda ese id en cada webhook entrante; `db.get_tenant_by_phone_number_id`
resuelve a qué profesional pertenece el mensaje.

**Disciplina que hay que sostener**: cualquier función nueva en `db.py` tiene
que recibir `tenant_id` explícito y filtrar por él en la query — nunca "traer
todo y filtrar en Python". Un bug ahí cruza turnos o pacientes de un
profesional a otro.

## Comandos

```bash
pip install -r requirements.txt
cp .env.example .env      # completar (ver más abajo qué es compartido y qué no)
python app.py             # local, :5000
```

Deploy: Railway (`nixpacks.toml` + `Procfile`, gunicorn). **Un solo worker**
(`--workers 1`) porque el recordatorio automático corre como hilo de fondo
dentro del proceso — con dos workers se mandaría duplicado.

## Variables de entorno — qué es de la plataforma y qué es de cada profesional

Las variables de `.env` (`META_ACCESS_TOKEN`, `META_APP_SECRET`,
`SUPABASE_*`, etc.) son **de la plataforma entera**, una sola vez. Lo que es
específico de cada profesional (número de WhatsApp, nombre, horarios, usuario
de panel) vive en la tabla `tenants` de Supabase y se carga con
`scripts/crear_tenant.py` — nunca en variables de entorno ni hardcodeado.

## Arquitectura

```
app.py            webhook de Meta (uno solo, para todos los tenants) + panel web
conversation.py   qué le contesta el bot a un paciente — SIN estado en memoria
db.py             toda la capa de datos, siempre con tenant_id explícito
whatsapp.py       envío por Graph API + verificación de firma del webhook
scheduler.py      hilo de fondo: recordatorios automáticos antes de cada turno
config.py         variables de entorno de la plataforma (no aborta con defaults inseguros)
migrations/       esquema de Supabase, aplicar en orden numérico
scripts/          alta de tenants a mano (crear_tenant.py)
```

### Por qué `conversation.py` no guarda estado de conversación

Interpretar "3" como *"el turno #3 de la lista de horarios libres AHORA
MISMO"* (no de una lista vieja guardada en memoria) hace que todo el flujo de
reserva sea sin sesión: no hay nada que se pierda si el proceso reinicia, ni
lista desincronizada. `db.reservar_turno()` sigue siendo atómico (filtra por
`estado = 'libre'` en el UPDATE), así que en el peor caso alguien reserva un
horario distinto al que vio en pantalla — nunca uno inexistente ni el de otro
paciente.

### Los turnos son "libre" → "reservado" → "libre" de nuevo, nunca se borran

Cancelar un turno lo vuelve a poner en `libre` en vez de borrar la fila. Así
reaparece solo en la próxima consulta de horarios disponibles — no hace falta
que el profesional regenere nada.

## Lo que NO tiene esta v1 (a propósito)

- **Pagos**: no hay integración con MercadoPago ni nadie. El canon a los
  profesionales se cobra a mano, fuera del sistema — "como todo se hace a
  mano, se cobra primero y se arma después".
- **Notas clínicas / historia clínica**: fuera de alcance, es una superficie
  de datos sensibles (secreto profesional) que no vale la pena tocar hasta
  no tener validado que el negocio de turnos + recordatorios solo ya sirve.
- **Alta self-service**: los profesionales no se dan de alta solos. Es
  deliberado para la etapa de pilotos con conocidos — automatizar el alta es
  trabajo para cuando haya más de un puñado de tenants.

## Convenciones

- **Nunca commitear directo a `main`**: rama + PR.
- Credenciales sólo por variables de entorno / Railway. En Railway quedan
  *staged* tras editarlas: falta el Apply/Deploy.
- No reutilizar el patrón de `Fleet-Test-Template` (`client/` por cliente,
  un deploy por cliente) — ese modelo es para clientes de ticket alto
  (flotas). Acá el modelo es el opuesto: un deploy, muchos tenants chicos.
