# Poner Consultorio en producción

El código está listo y probado, pero **no hay ningún deploy corriendo**. Esta
es la lista de lo que falta, en orden — cada paso depende del anterior.

Todo lo que dice "cargar" acá lo hace Nacho a mano en el dashboard que
corresponda. **Ninguna credencial pasa por el chat.**

---

## 1. Base de datos (Supabase)

Puede ser un proyecto nuevo o el mismo de `hub.fibot.ar`. **Conviene uno
nuevo**: el de hub lo comparten `cantapp` y `fleet-bot-pastor`, y un cambio de
esquema ahí les pega a los dos.

1. Crear el proyecto.
2. Correr las migraciones **en orden numérico**, en el SQL Editor:
   - `migrations/001_init.sql`
   - `migrations/002_slug.sql`
   - `migrations/003_numero_compartido.sql`
3. Anotar de Settings → API: la **URL** y la **service role key**.

> La service role key saltea las políticas de RLS. Es correcto acá —
> el filtro por tenant lo hace `db.py` en cada query, no la base— pero por eso
> mismo esa key **nunca** puede terminar del lado del navegador.

Verificación: `select count(*) from tenants;` tiene que devolver `0`, no un
error de tabla inexistente.

---

## 2. Meta / WhatsApp

Un solo Business Portfolio y una sola App para toda la plataforma.

1. En Meta Business Suite: App de tipo Business con el producto **WhatsApp**.
2. Agregar el número de la plataforma (el que van a compartir los tenants sin
   número propio) y anotar su **phone number id**.
3. Generar un **System User token permanente** — el token de prueba de 24 h no
   sirve para producción.
4. Anotar el **App Secret** (Settings → Basic).
5. Inventar un **verify token** (cualquier string largo y random; lo elegís
   vos, Meta sólo lo compara).

El webhook se configura recién en el paso 4, cuando exista la URL.

### La plantilla del recordatorio

En WhatsApp Manager → Plantillas de mensaje, crear una de categoría
**UTILITY**, idioma **es_AR**, con **dos** parámetros de body:

```
Hola. Te recuerdo tu turno con {{1}}: {{2}}.
Si no podés venir, respondé CANCELAR y lo liberamos.
```

`{{1}}` = nombre del profesional, `{{2}}` = cuándo es el turno. Ese orden
importa: es el que manda `scheduler.py`.

La aprobación tarda de minutos a un par de días. **Hasta que esté aprobada,
dejar `RECORDATORIO_TEMPLATE` vacía** — con la plantilla a medio aprobar los
envíos fallan; con la variable vacía el bot cae a texto libre y al menos
funciona para los pacientes que escribieron hace poco.

---

## 3. Railway

Proyecto nuevo, servicio desde este repo, branch `main`.

Variables (nombres; los valores salen de los pasos 1 y 2):

```
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
META_ACCESS_TOKEN
META_APP_SECRET
META_VERIFY_TOKEN
FLASK_SECRET_KEY              # string largo y random, lo generás vos
PLATAFORMA_PHONE_NUMBER_ID    # el del paso 2.2
RECORDATORIO_TEMPLATE         # VACÍA hasta que Meta apruebe la plantilla
RECORDATORIO_TEMPLATE_LANG    # es_AR
RECORDATORIO_INTERVALO_MIN    # 10 (opcional, es el default)
```

**Acordarse del Apply/Deploy**: en Railway las variables quedan *staged* y el
servicio sigue corriendo con las de antes.

**No tocar el `--workers 1` del `Procfile`.** El scheduler de recordatorios es
un hilo dentro del proceso: con dos workers cada paciente recibe el
recordatorio dos veces.

Verificación: `GET /health` tiene que devolver `FiBOT Consultorio activo.`

---

## 4. Dominio y webhook

1. Railway → Settings → Domains → agregar `consultorios.fibot.ar`.
2. Cloudflare (zona `fibot.ar`): `CNAME consultorios → <lo que diga Railway>`,
   **sin proxy naranja** (con el proxy Railway no puede emitir el
   certificado — mismo problema que tuvo `hub.fibot.ar`).
3. Recién con el certificado emitido, en Meta → WhatsApp → Configuration:
   - Callback URL: `https://consultorios.fibot.ar/webhook`
   - Verify token: el del paso 2.5
   - Suscribirse al campo **`messages`**.

Meta pega un `GET /webhook` en ese momento; si el verify token no coincide
devuelve 403 y la configuración no se guarda.

Verificación: mandarle un mensaje al número de la plataforma y mirar los logs
de Railway. Sin tenants cargados todavía va a loguear
`Mensaje al número compartido de un teléfono sin turnos` — eso **es** el
resultado correcto: llegó, se verificó la firma, y no encontró a quién
rutearlo.

---

## 5. Primer tenant

```bash
python scripts/crear_tenant.py
```

Cargar nombre, slug, usuario y clave de panel. **Dejar el
`whatsapp_phone_number_id` vacío**: así usa el número de la plataforma y el
profesional no tiene que abrir ninguna cuenta de WhatsApp Business.

Después, entrando al panel en `consultorios.fibot.ar/login`, generar los
turnos libres de las próximas semanas.

Verificación de punta a punta:

1. Abrir `consultorios.fibot.ar/<slug>` — tienen que aparecer los horarios.
2. Reservar uno con tu propio teléfono.
3. Escribirle al número de la plataforma: ahora sí te reconoce y te contesta
   el menú **con el nombre del profesional**.
4. Para probar el recordatorio sin esperar: en Supabase, mover el `inicio` de
   ese turno a dentro de ~20 h y esperar el próximo tick (10 min).

---

## Lo que este checklist NO cubre

- **Cobrar**: no hay integración de pagos. El canon se cobra a mano, fuera del
  sistema. Es a propósito (ver `CLAUDE.md`).
- **Alta self-service**: los profesionales no se dan de alta solos. También a
  propósito, mientras sean un puñado.
- **Backups**: Supabase hace los suyos en el plan pago. En el plan free no hay
  backup automático — con dos o tres tenants es asumible, con diez no.
