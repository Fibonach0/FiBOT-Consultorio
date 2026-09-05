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

### El modo "sin número propio" (y la única excepción a esa disciplina)

Pedirle a un profesional que abra su propio número de WhatsApp Business, lo
verifique y lo sume al Business Portfolio **antes** de poder probar nada es el
paso donde se cae la mayoría de los pilotos. Por eso `whatsapp_phone_number_id`
es **opcional** (`migrations/003`): si el tenant no tiene número propio, usa el
de la plataforma (`PLATAFORMA_PHONE_NUMBER_ID`) y arranca el mismo día.

El costo de eso: cuando varios tenants comparten un número, el
`phone_number_id` del webhook **ya no identifica a nadie**. El tenant se
resuelve por el paciente — `db.tenant_por_paciente(telefono)` busca el turno
más reciente de ese teléfono y devuelve su tenant. Es **la única función de
`db.py` sin `tenant_id` explícito, y es deliberado**: es justamente la que lo
averigua. No copiarla como precedente.

Dos consecuencias que hay que tener presentes al vender esto:

- Un paciente que **nunca reservó** y le escribe al número compartido no se
  puede rutear a nadie: el webhook lo ignora y loguea. Por eso el camino de
  entrada de un tenant sin número propio es la **página pública `/<slug>`**,
  no el WhatsApp — primero reserva, después puede escribir.
- Como el número no es del profesional, todo lo que sale por ahí **lo nombra**:
  el recordatorio dice quién es, y el saludo también. `whatsapp.numero_de(tenant)`
  es el único lugar donde se decide desde qué número sale cada mensaje.

Cuando el profesional consigue su propio número, se le carga el
`whatsapp_phone_number_id` en su fila y no hay nada más que migrar: el webhook
lo resuelve por número otra vez y `numero_de()` empieza a devolver el suyo.

### El recordatorio necesita una plantilla aprobada por Meta

Meta sólo deja mandar texto libre dentro de las **24 h** desde el último
mensaje del paciente. El recordatorio de turno se manda la noche anterior —
o sea, casi siempre **fuera** de esa ventana. Sin plantilla aprobada, el
recordatorio no llega y el envío falla en silencio.

`RECORDATORIO_TEMPLATE` es el nombre de una plantilla aprobada con dos
parámetros de body: `{{1}}` = nombre del profesional, `{{2}}` = cuándo es el
turno. Si está vacía, `scheduler.py` avisa al arrancar y cae a texto libre —
que sirve para probar en local, no para producción.

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
app.py            webhook de Meta (uno solo, para todos los tenants) + panel web + página pública
conversation.py   qué le contesta el bot a un paciente — SIN estado en memoria
db.py             toda la capa de datos, siempre con tenant_id explícito
whatsapp.py       envío por Graph API + verificación de firma del webhook
scheduler.py      hilo de fondo: recordatorios automáticos antes de cada turno
config.py         variables de entorno de la plataforma (no aborta con defaults inseguros)
migrations/       esquema de Supabase, aplicar en orden numérico
scripts/          alta de tenants a mano (crear_tenant.py)
```

### Página pública de reserva (`/<slug>`)

Cada tenant puede tener un `slug` (columna en `tenants`, cargada por
`scripts/crear_tenant.py`) que habilita `consultorios.fibot.ar/<slug>`: una
página sin login donde cualquiera ve los horarios libres y reserva dejando
nombre y WhatsApp — sin escribirle nada al bot primero. Es el link que el
profesional comparte en su firma, Instagram, etc.

Reusa exactamente los mismos `db.turnos_libres` / `db.turno_por_id` /
`db.reservar_turno` que ya usaba la conversación de WhatsApp — la reserva
pública no es un camino paralelo, es la misma operación atómica (filtra por
`estado = 'libre'` en el UPDATE) con otra puerta de entrada. Un tenant sin
`slug` cargado simplemente no tiene página pública; sigue andando por
WhatsApp y panel como siempre.

El teléfono que se carga en el formulario se limpia a solo dígitos antes de
guardarlo — el recordatorio automático (y que el paciente después le escriba
al bot para cancelar) necesitan el número en el mismo formato internacional
sin símbolos que usa WhatsApp.

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

## Tests y CI

Los `test_*.py` están sueltos en la raíz y se corren **uno por proceso**
(`python test_x.py`), no con pytest en batch: todos monkeypatchean `db` sin
restaurarlo, así que juntos en el mismo proceso se pisan los parches entre sí.
Está escrito en el docstring de cada archivo y el CI lo respeta.

**Un test que importe `app` tiene que apagar el scheduler primero**:

```python
import scheduler
scheduler.iniciar = lambda: None
import app          # recién ahora
```

`app.py` arranca el hilo de recordatorios al importarse y su primer tick pega
contra Supabase de una. Con la URL falsa de los tests eso vuelca un traceback
de DNS de 40 líneas en cada corrida del CI — no rompe nada (el `try/except` de
`_loop` lo agarra) pero tapa lo que sí importa el día que falle algo de verdad.
Se apaga en el test, no con una variable de entorno: un interruptor para
apagar los recordatorios es justo lo que no querés que alguien active sin
querer en Railway. Los recordatorios se prueban llamando a `scheduler._tick()`
a mano, que es lo que interesa verificar.

`.github/workflows/ci.yml` corre en cada PR: `compileall` sobre todo el repo
(cubre lo que ningún test importa, como `scripts/crear_tenant.py`) y después
cada suite por separado, en un bucle que **no corta en el primer fallo** —
interesa ver todas las que rompieron, no la primera. Las descubre por glob:
un `test_nuevo.py` entra solo, sin tocar el workflow.

La versión de Python del CI (3.11) es la de `nixpacks.toml`. Si cambia en
Railway, cambiarla ahí también — un verde sobre otra versión no dice nada del
runtime real.

## Convenciones

- **Nunca commitear directo a `main`**: rama + PR.
- Credenciales sólo por variables de entorno / Railway. En Railway quedan
  *staged* tras editarlas: falta el Apply/Deploy.
- No reutilizar el patrón de `Fleet-Test-Template` (`client/` por cliente,
  un deploy por cliente) — ese modelo es para clientes de ticket alto
  (flotas). Acá el modelo es el opuesto: un deploy, muchos tenants chicos.
