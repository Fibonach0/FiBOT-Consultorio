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

**Freno de envíos (`WHATSAPP_SOLO_A`)**: lista de teléfonos separados por coma.
Si está cargada, `whatsapp.enviar_texto`/`enviar_plantilla` sólo mandan a esos
números (se comparan los últimos 10 dígitos) y al resto lo saltean devolviendo
`True`, para que el scheduler no reintente. Se carga mientras se prueba con un
número real y datos de demo: los teléfonos inventados pueden ser de gente
real. Vacía = producción, le manda a todos.

**WhatsApp en administración (`/admin/whatsapp`, `migrations/044`)**: el webhook
marca en `eventos_plataforma` la hora del último aviso de Meta (`webhook`) y del
último mensaje de un paciente (`mensaje`), sólo la hora y el phone_number_id; si
falla, el mensaje se procesa igual. La tarjeta Estado lo muestra. La página
(`whatsapp.diagnostico`) consulta a Meta en vivo, sólo al abrirla: número,
suscripción de la app a la cuenta y estado de cada plantilla (las dos últimas
necesitan `META_WABA_ID`). Es la lista a seguir al cambiar de número. La prueba
de envío manda `hello_world` o el recordatorio con datos de ejemplo, respeta el
freno y queda en la actividad. Nunca se muestra el token.

**Ajustes de WhatsApp desde administración (`migrations/045`)**: número de la
plataforma, cuenta (WABA), freno y teléfono de avisos se guardan en
`ajustes_plataforma` y **pisan** a los de Railway (`whatsapp.aplicar_ajustes`
cambia `config` en memoria: anda porque hay un solo worker). Vacío = vuelve a
Railway. Se cargan al arrancar desde `scheduler.iniciar()` (los tests la apagan).
«Suscribir la app» hace el POST a `/{WABA}/subscribed_apps` con el token del
servidor. Los avisos de Meta sobre plantillas (`message_template_status_update`,
`template_category_update`) se guardan en `plantillas_meta`, van a la actividad
como «Meta» y le llegan por WhatsApp al teléfono de avisos (con `AVISO_TEMPLATE`).
Hay que tener esos campos suscriptos en el webhook de la app en Meta.
«Probar el bot» (`/admin/whatsapp/simular`) llama a `conversation.responder` con
el teléfono ficticio `app.SIMULADO` y no manda nada por WhatsApp, pero lo que
hace el bot es real; «Deshacer» libera sus turnos futuros sin registrarlos como
cancelación. La charla vive en la sesión del admin.
«Como si fuera otro día» (`scheduler.vista_previa`) arma el resumen diario y la
lista de recordatorios de una cuenta para una fecha y hora dadas, sin mandar ni
marcar nada (`db.turnos_para_recordar` acepta `ahora`). Sin nombres de
pacientes: sólo los últimos 4 dígitos.

**Bandeja, frases y lo que no es texto (`migrations/069`)**: lo que el bot no
reconoce va a `mensajes_pacientes` (por tenant) y se le contesta «Le pasé tu
mensaje a…»; al profesional le llega un WhatsApp sin texto ni nombre sólo si no
tenía otros sin leer. En el panel («Mensajes») lo marca leído o lo contesta
(`/agenda/mensaje/<id>/responder`, texto libre por el número del bot, sólo
dentro de las 24 h). Saludos, «gracias», emojis y stickers no van a la bandeja.
Las frases se reconocen con reglas locales en `conversation.py` (`_normal` saca
tildes y signos): «mi turno», «no voy a poder ir» (pregunta, nunca cancela sin
la palabra *cancelar*), «quiero un turno el martes» (filtra el día pero muestra
el número de la lista completa, para seguir sin estado), «cambiar el turno».
«2 Juan» reserva; «10 minutos tarde» no (`_parece_nombre`). Nada del texto sale
a servicios externos. El webhook ya no descarta lo que no es texto
(`app._no_texto` → `conversation.responder_no_texto`): audio y video piden que
lo escriban (queda la marca en la bandeja, nunca el archivo); foto o PDF de
alguien con transferencia pendiente (`db.pago_esperando_de`) se baja de Meta
(`whatsapp.bajar_media`, hasta 2 MB) y entra como comprobante por
`pagos.subir_comprobante`; las reacciones no se contestan. Todo mensaje
entrante se marca leído (`whatsapp.marcar_leido`).

**Listas, reprogramar y «¿con quién es?» (`migrations/070`)**: `conversation`
puede devolver `Respuesta` (un `str` con `.interactivo`); `whatsapp.enviar_respuesta`
manda la lista o los botones y, si Meta no los acepta, el texto de siempre
(que sigue explicando cómo hacerlo escribiendo). Los horarios van como filas
`reservar:<turno>` (`conversation.elegir_horario` lo busca en los libres de
ahora) y «cambiar el turno» como `mover:<turno>:<nuevo>`, que resuelve
`app._mover_desde_whatsapp`: los ids vienen del teléfono del paciente, así que
se revisa que el turno sea de ese teléfono, el plazo y que el horario siga
libre. La web y WhatsApp comparten `app._mover_por_paciente`. En el número
compartido, si el paciente tiene turnos con más de un profesional
(`db.tenants_de_paciente`, sin tenant_id a propósito, como
`tenant_por_paciente`), `app._tenant_del_mensaje` pregunta con botones (o
«con Micaela» por escrito), guarda el mensaje pendiente en
`elecciones_whatsapp` y lo contesta al elegir; la elección dura 2 h desde el
último mensaje.

**Asistente del profesional (`asistente_profesional.py`)**: el alta (admin y
`scripts/crear_tenant.py`) pide el WhatsApp del profesional (`aviso_telefono`,
`app.whatsapp_de_alta`: 10 dígitos → 549…) y le manda la bienvenida con
`scheduler.avisar_profesional`. Cuando escribe desde ese número, el webhook
(`app._respuesta_al_profesional`, después de LIBERAR / ME LO TOMO) le contesta
«hoy», «mañana», «semana», «libres», «pagos», «deudas», «mensajes», «link» y
«ayuda»: sólo horas, cantidades y montos, nunca nombres ni teléfonos de pacientes.
Sólo lee. Si el texto no es un pedido suyo devuelve None y sigue como paciente
(así prueba su propio bot). `db.tenants_por_aviso` no recibe tenant_id (lo
averigua, como `tenant_por_paciente`); con el mismo número en varias cuentas
contesta por cada una; con número propio, sólo esa cuenta.

**Plantillas y sus botones (`plantillas.py`)**: `CATALOGO` tiene texto, categoría,
{{n}}, botones y nombre sugerido de cada plantilla; /admin/whatsapp lo muestra («Cómo
crear cada plantilla») y `test_conversation.py` manda cada botón al bot y revisa que
haga lo de `QUE_HACE` (si cambia una frase reconocida, se rompe un botón ya aprobado).
«Sí, confirmo» sin reserva web esperando confirma el próximo turno
(`conversation._confirmar_proximo_turno`, `db.marcar_confirmado`); «¿Qué debo?» lista
las sesiones pasadas sin pagar de ese teléfono (`db.pendientes_de_pago_de`). Botón de
link: `whatsapp.enviar_plantilla(..., boton_url=)` agrega el componente `button/url`
índice 0; con `PAGO_PENDIENTE_BOTON=1` el token del pago va en el botón (texto con
{{1}} profesional, {{2}} cuándo) y con `PAGO_RECHAZADO_BOTON=1` el slug (texto sólo
con {{1}}). Sin esas variables el link sigue en el texto.

**Celulares argentinos y el 131030**: se manda con el 9 (549…), que es lo
normal. Si Meta contesta 131030 («no está en la lista de autorizados»),
`whatsapp._mandar` reintenta una vez con `con_15` (54 + característica + 15 +
número): así guarda Meta los destinatarios autorizados del número de prueba.
La característica sale de `_CARACTERISTICAS_3` (11 = 2 dígitos, el resto 4).

El costo de eso: cuando varios tenants comparten un número, el
`phone_number_id` del webhook **ya no identifica a nadie**. El tenant se
resuelve por el paciente — `db.tenant_por_paciente(telefono)` busca el turno
más reciente de ese teléfono y devuelve su tenant. Es **la única función de
`db.py` sin `tenant_id` explícito, y es deliberado**: es justamente la que lo
averigua. No copiarla como precedente.

Dos consecuencias que hay que tener presentes al vender esto:

- Un paciente que **nunca reservó** y le escribe al número compartido no se
  puede rutear a nadie: el bot le contesta una vez por día cómo reservar
  (`RESPUESTA_SIN_DESTINO`, sin nombrar a ningún profesional) y queda en
  `mensajes_sin_destino` (`migrations/046`: últimos 4 dígitos + hash, nunca el
  número ni el texto; se borra a los 30 días), que administración ve en
  «Escribieron sin turno». El texto se edita ahí (`ajustes_plataforma`,
  clave `respuesta_sin_destino`; vacío = el de fábrica) y `{link}` se cambia
  por la página pública sólo si hay UN profesional activo sin número propio y
  con slug (`app._link_sin_destino`). Por eso el camino de
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
`scripts/crear_tenant.py`) que habilita `consultorio.fibot.ar/<slug>`: una
página sin login donde cualquiera ve los horarios libres y reserva dejando
nombre y WhatsApp — sin escribirle nada al bot primero. Es el link que el
profesional comparte en su firma, Instagram, etc.

Reusa exactamente los mismos `db.turnos_libres` / `db.turno_por_id` /
`db.reservar_turno` que ya usaba la conversación de WhatsApp — la reserva
pública no es un camino paralelo, es la misma operación atómica (filtra por
`estado = 'libre'` en el UPDATE) con otra puerta de entrada. Un tenant sin
`slug` cargado simplemente no tiene página pública; sigue andando por
WhatsApp y panel como siempre.

`/` es la portada del sitio para futuros pacientes (`templates/inicio.html`,
página suelta, no extiende `base.html`): diseño de Claude Design (proyecto «Home», archivo `Home.dc.html`), con «Para mí / Para mi hijo/a» que cambia frases, temas (`directorio.TEMAS_ADULTOS` / `TEMAS_CHICOS`), profesionales e ilustraciones (`static/portada/<tema>.webp`; si falta una, queda la figura de color). buscador (tema, modalidad, obra social →
`/turnos`), temas que llevan a `/turnos?tema=…`, hasta `INICIO_PROFESIONALES` del
directorio (primero los que toman turnos y tienen foto), cómo funciona,
modalidades, preguntas frecuentes y el bloque «¿Sos profesional?» → `/sumate`.
Si la base falla, portada, /turnos y sitemap salen sin profesionales
(`app._directorio`). El chequeo de vida es `/health`, no `/`.

Directorio (`directorio.py`): `visibles` (con slug y sin `en_directorio = false`)
y `filtrar` para `/turnos?tema=&modalidad=&obra=`. Los temas (`TEMAS`) se
reconocen por palabras sueltas, sin tildes, en enfoques, descripción, sobre mí,
título y rubro (psicopedagogía cae en «Infancia»): el perfil no tiene lista
cerrada de temas a propósito. Valores desconocidos no filtran. Con modalidad u
obra social la página va `noindex`; con tema sólo, es indexable (canonical).

`/sumate` (`migrations/060`, tabla `interesados`, de la plataforma como
`admins`): formulario para profesionales; pide nombre y WhatsApp o mail, tiene un
campo trampa (`web`) para robots y cuenta 5 envíos por IP cada 15 minutos en
`login_intentos` (`sumate:<ip>`). Avisa con `whatsapp.avisar_admin` y aparece en
«Quieren sumarse» de /admin («Ya lo contacté»). El alta sigue siendo a mano.
`sumate` está en `SLUGS_RESERVADOS`.

Estilo del sitio para pacientes (Claude Design: `Profesionales`, `Perfil`, `Para profesionales`):
`templates/_sitio.html` (letras, paleta clara y oscura con `:root:root` porque base define sus
colores después; el verde no se pisa si hay `color_pagina`) y `_sitio_macros.html` (`barra`, `pie`).
Lo usan `/turnos`, `/sumate`, `/<slug>` y `publico_reservar/revisar/confirmado`; siguen extendiendo
base (menú «Aa», oscuro). `/turnos` filtra además por `especialidad` (rubro psicología /
psicopedagogía), `semana=1` (primer libre en 7 días) y `orden` (próximo turno por defecto, o nombre);
cada tarjeta muestra hasta 4 próximos libres (`app._proximos_libres`, 60 s en memoria) que llevan
directo a reservar, o a la semana si hay varios tipos de turno. En `/<slug>` el calendario va a la
derecha: se elige el día y se ven sus horarios (sin JS, todos los días). Las fotos van por
`/<slug>/foto`, nunca como data URL en listados.

SEO: `/robots.txt` (cierra panel, admin, login y los links con token) y
`/sitemap.xml` (portada, /turnos, /sumate, /privacidad y cada `/<slug>` del
directorio). La portada lleva JSON-LD `WebSite`; `/<slug>` lleva descripción,
canonical, Open Graph (la portada como imagen, si hay) y JSON-LD `Person`
(`app.perfil_ld`). La foto es un data URL y no sirve como `og:image`. `base.html`
tiene `{% block head %}` para esto.

`/turnos` es la puerta de entrada para pacientes: lista los profesionales
activos con `slug` y cada uno lleva a su calendario. `/<slug>` muestra una
semana (lunes a domingo, en la zona del profesional, `?semana=AAAA-MM-DD`) con
los horarios libres; sin semana elegida abre la del próximo horario libre. En el
panel, «Tu página de turnos» copia el link, lo comparte por WhatsApp y da un QR
(`/agenda/mi-pagina-qr.svg`). `turnos` está en `SLUGS_RESERVADOS`.

Perfil público (`migrations/047`): `tenants.foto` (data URL PNG/JPG, mismo
criterio que la firma), `descripcion`, `modalidades_publicas` (presencial /
virtual / ambas: con ambas elige el paciente) y `en_directorio` (si es false no
aparece en /turnos, pero su link sigue andando). Se cargan en «Tu página de
turnos» del panel.
Perfil completo (`migrations/058`): `titulo`, `ubicacion`, `sobre_mi`,
`enfoques` (lista, `MAX_ENFOQUES`), `formacion` (renglones, `MAX_FORMACION`),
`mas_de_mi` (lo humano) y `obras_sociales`, se guardan con `/agenda/presentacion`
(aparte de `/agenda/perfil`, que es la configuración de reservas). La portada vive
en la tabla `portadas` (no en `tenants`, que se lee en cada pedido), hasta
`PORTADA_MAX`, y se sirve en `/<slug>/portada?v=<portada_version>` con caché larga.
El panel achica foto (512) y portada (1600×400, recorte centrado) en el navegador
antes de subirlas. Sin foto, la página muestra la inicial.

Vista (tema y letra, `migrations/059`): `base.html` define los colores como
variables en `:root` y las redefine en `:root[data-modo="oscuro"]` (y en
`data-modo="auto"` con `prefers-color-scheme: dark`). `app._vista()` (global
`vista_pagina`; no llamarlo `vista`, el panel ya usa esa variable) decide
`data-tema` (claro / oscuro / auto / noche) y `data-letra` (normal / grande /
enorme): con sesión de profesional manda `tenants.preferencias_vista`, si no las
cookies. «noche» = oscuro de 20 a 8 h del dispositivo (script en el `<head>`,
antes de pintar). El menú «Aa» abajo a la derecha cambia todo, guarda cookies y,
con sesión, `POST /agenda/vista` (CSRF por `<meta name="csrf">`).
Nada de colores fijos en templates que extienden base: usar `--card`,
`--green-solid`/`--on-green` (fondo verde con texto), `--warn`, `--warn-bg`,
`--danger-bg`, etc. Recibo e impresión quedan siempre claros.
`tenants.color_pagina` (`app.COLORES_PAGINA`: claro y oscuro de cada paleta) pisa
el verde en las páginas del paciente con `_color_pagina.html` (incluido en
publico*, pago, agendar*, espera_listo). Foto y portada pasan por un recorte
(`<dialog id="recorte">`: arrastrar y acercar) antes de subirse.
«Oscuro de noche» usa `data-noche="D-H"` (`preferencias_vista.noche` o la cookie
`noche`, `app._horas_noche` valida; por defecto 20-8; cruza la medianoche si D > H).
Compartir: `/<slug>` lleva Open Graph (`{% block meta %}` de base): título,
descripción e imagen = `/<slug>/foto` (la foto data URL servida como imagen), o la
portada, o el ícono. App instalable: `/manifest.webmanifest` (inicio `/agenda`),
íconos en `static/`, y `/sw.js`, que se registra sólo con sesión y **no guarda
páginas** (hay datos de pacientes): sólo `/sin-conexion`, una página suelta sin
sesión. «Tené el panel como una app» en Ajustes usa `beforeinstallprompt`;
en iPhone, instrucciones de Safari. `static` y `sin-conexion` están en
`SLUGS_RESERVADOS`.

Confirmación por WhatsApp de la reserva web: con `CONFIRMACION_TEMPLATE`
(plantilla con botones «Sí, confirmo» / «Cancelar», `{{1}}` profesional,
`{{2}}` cuándo) se le manda al reservar y se marca `confirmacion_enviada_at`.
El botón llega al webhook como `type: button` y se trata como texto; «sí» o
el botón confirman la próxima reserva que esperaba confirmación
(`db.confirmar_proximo`) y la respuesta lleva `/agendar/<token_calendario>`:
Google Calendar o `.ics`, sin datos del paciente, `noindex`. El token es la
credencial. Los tres campos van en `db._LIBERADO` y no viajan al reprogramar.
En el panel, la sesión muestra «✓ confirmó» o «sin confirmar».
Si a las `scheduler.HORAS_PARA_RECORDAR` (6) no confirmó, `_recordar_confirmaciones`
manda la misma plantilla UNA vez más (`confirmacion_recordada_at`, `migrations/048`),
sólo entre las 9 y las 21 y si falta más de 1 hora; el resumen diario cuenta las
reservas web sin confirmar. Desde `/agendar/<token>` el paciente cancela
(`/cancelar`, queda como cancelación del paciente) o cambia el horario
(`/cambiar`: el mismo calendario semanal, sólo horarios libres, con
`db.reprogramar_turno`); el turno nuevo se lleva el token, queda confirmado y
marcado `reserva_nueva` para que el profesional se entere. Esas rutas no piden
CSRF (no hay sesión; el token es la credencial): ojo, `/agendar` empieza con
`/agenda` y `verificar_csrf` lo excluye a propósito.
`tenants.cancelar_hasta_horas` (`migrations/049`, default 24, 0 = hasta que
empieza) limita cancelar/cambiar desde la web (`app._puede_modificar`, se chequea
también en los POST); después, «escribile directamente». Al cancelar o cambiar
desde la web se avisa al profesional en el momento (`scheduler.avisar_profesional`,
con `AVISO_TEMPLATE`, sin nombres). La página del profesional tiene «Anotate en
la lista de espera» (`/<slug>/lista-espera`): entra a `lista_espera` con
`origen = 'web'`, no se duplica por teléfono y se avisa al profesional; ofrecer
el horario sigue siendo a mano desde el panel, salvo que active la opción
automática (abajo).

**Cancelaciones del paciente (`cancelaciones.py`, `migrations/050`)**: web y bot
pasan por `cancelaciones.tras_cancelar`, y los dos respetan el plazo
(`cancelaciones.puede_modificar`). Con `tenants.al_cancelar = 'preguntar'` el
horario queda retenido (`turnos.hueco = 'pendiente'`): libre pero fuera de
`turnos_disponibles` (la vista filtra `hueco is null`) y rechazado al
reservarlo por id. El profesional decide en la tarjeta «Horarios que te
cancelaron» o contestando LIBERAR / ME LO TOMO desde su `aviso_telefono` (el
webhook lo intercepta antes de rutear al paciente; `db.tenant_por_aviso`).
«Me lo tomo» = `'propio'`. Con `ofrecer_espera_auto`, cada horario que queda
libre de verdad se le ofrece al primero de la lista que hoy no recibió otro.
`eventos_pagina` cuenta visitas (sin bots, sólo la semana inicial), reservas,
cancelaciones, cambios y anotados de la web: «Este mes» en «Tu página de
turnos». `hueco` va en `db._LIBERADO` y reservar lo limpia.
`migrations/051`: `tenants.hueco_vence_horas` (0 = nunca) + `hueco_por_defecto`
(liberar / tomar): `cancelaciones.vencer_huecos`, desde el tick del scheduler,
aplica lo elegido a los retenidos con `turnos.hueco_desde` vencido y avisa.
`tenants.pausado` + `mensaje_pausa`: la página pública no muestra horarios (la
lista de espera queda abierta), reservar por id y cambiar de horario se
rechazan, el bot no lista ni reserva, y /turnos lo muestra pausado. Lo ya
reservado no se toca. Los bloqueos aceptan horas (`hora_desde`/`hora_hasta`):
mismo `bloqueos.desde/hasta`, sólo cambia cómo se cargan y se muestran.
`migrations/052`: `pausado_hasta` (la pausa se levanta sola ese día desde el
scheduler, `cancelaciones.reactivar_si_toca`, y se le ofrece el próximo horario a
la lista de espera). `horario_semanal` (lista de {dia 0-6, desde, hasta}, hasta
`MAX_FRANJAS` por día) + `horario_semanas`: `scheduler.generar_desde_horario` crea
los libres con `crear_turnos_libres` (idempotente) al guardar y una vez por día.
Un bloqueo (feriado, vacaciones) cierra el horario a pacientes, bot y página; el panel agenda y reprograma igual (`ignorar_bloqueo=True` en `reservar_turno`/`reprogramar_turno`) y muestra «cerrado a pacientes». La serie sigue salteando los días bloqueados.
`turnos.abierto` (`migrations/062`) es la excepción de un horario: se ofrece aunque el día esté bloqueado (la vista `turnos_disponibles` y `reservar_turno` lo respetan; el feriado no se borra, así se vuelve a cargar solo). «Abrir este día» marca todos sus libres. Agendar o pasar una sesión a un día bloqueado avisa con `app._avisar_si_bloqueado`. El resumen diario avisa 3 días antes y el día anterior si un bloqueo cae sobre sesiones (`scheduler._bloqueos_con_sesiones`). En el mes, los feriados (motivo `Feriado: `) usan `--feriado`. Abrir el día acepta `hora_desde`/`hora_hasta` (sólo los libres que entran enteros) y `avisar_espera` (`cancelaciones.ofrecer_a_toda_la_espera`, la misma que usa el fin de una pausa). `/agenda/dia/<fecha>/pasar` mueve las sesiones futuras del día +7 días a la misma hora local con `reprogramar_turno` respetando bloqueos; las que no entran quedan y se listan. El mes tiene un menú «⋯» por día (bloquear, abrir, desbloquear sólo si el bloqueo es de ese día); esos forms mandan `mes` y `_volver_a_agenda` vuelve a ese mes.
`tenants.puentes` (`migrations/063`): los días puente (tipo `puente` de la API, o «Puente» en el nombre embebido, `feriados.es_puente`) siguen ese ajuste aparte de `feriados`.
«Sacar» en un libre (`db.sacar_libre`) lo borra y lo anota en `horarios_sacados`
(`migrations/061`): `generar_desde_horario` saltea esos inicios, así no reaparece.
Generarlo a mano (`/agenda/generar`, marca `turnos.a_mano`) lo vuelve a habilitar.
Al guardar el horario, `scheduler.limpiar_fuera_de_horario` borra los libres
futuros que ya no entran en él; nunca toca reservados, `hueco` ni `a_mano`.
`tipos_turno` (lista de {nombre, minutos}): con más de uno el paciente elige en
la página; el calendario sólo muestra horarios donde entra (`app._cabe`: si dura
más que el horario, tiene que seguir en libres consecutivos), el servidor lo
vuelve a chequear (`_entra`), y la reserva guarda `turnos.tipo` y alarga `fin`
(la vista ya oculta los libres que se pisan). Cambiar de horario conserva la
duración y el tipo (`reprogramar_turno` copia `tipo`). El bot reserva el tipo
de siempre.
`migrations/053`: `tenants.descanso_minutos`: los horarios arrancan cada
duración + descanso (45 + 15 desde 9:15 → 9:15, 10:15…, terminan en punto); lo
usan el horario semanal y la generación a mano, y `_cabe` deja que un turno largo
salte el descanso hasta el horario siguiente. Cada franja del horario puede fijar
la modalidad: queda en `turnos.solo_modalidad` (del HORARIO, no va en
`_LIBERADO`; la vista la expone) y manda sobre lo que elija el paciente. Los tipos
pueden tener `precio` (se carga como `valor` al reservar). `scheduler.cuando_de`
agrega «(tipo de N min)», así sale en confirmación y recordatorios. La página del
paciente tiene vista mensual (`?vista=mes&mes=AAAA-MM`, `_mes_libre`).

### Cobro de las reservas web (`pagos.py`, `migrations/054`)

`tenants.cobro_web`: no / mercadopago / transferencia / ambos; `cobro_modo`
total o seña (`sena_monto`); el precio sale del tipo o de `precio_sesion`. Sin
precio o sin medio que funcione (`pagos.metodos`), no se cobra. Con cobro, la
reserva web queda `turnos.pago_web = 'esperando'` (no se confirma ni se recuerda)
y se crea una fila en `pagos` (token = credencial de `/pago/<token>` y
`external_reference` en Mercado Pago). Mercado Pago: cada profesional conecta su
cuenta por OAuth (`/agenda/mercadopago/conectar` → `/mercadopago/vuelta`, con
`state` en la sesión); el token va cifrado con Fernet derivado de
`FLASK_SECRET_KEY` (cambiarla obliga a reconectar) y se renueva solo. El cobro es
Checkout Pro con `notification_url=/webhook/mercadopago?t=<tenant>`; NUNCA se
confía en el aviso: `revisar_pago_mp` consulta el pago con el token del
profesional y compara referencia, profesional y monto. `aprobar` es idempotente;
si el turno ya no estaba apartado queda `aprobado_tarde` y se le avisa al
profesional para devolverlo. Transferencia: el paciente sube el comprobante
(PNG/JPG/PDF por firma de bytes, 2 MB, se borra al resolver) y el profesional
acepta o rechaza en «Pagos para revisar». `pagos.vencer` (scheduler) libera lo no
pagado: 30 min con Mercado Pago, 2 h para subir comprobante. Variables:
`MP_CLIENT_ID`, `MP_CLIENT_SECRET` (de la app de la plataforma en Mercado Pago
Developers, con redirect `https://<dominio>/mercadopago/vuelta`) y `MP_PRUEBA=1`
para credenciales y checkout de prueba.
Las cuentas de prueba de Mercado Pago no tienen Client Secret, así que el OAuth
no se puede probar con ellas. Con `MP_PRUEBA=1` el panel muestra además «Access
Token de la cuenta vendedor de prueba» (`pagos.conectar_con_token`): lo valida con
`/users/me` y lo guarda cifrado igual que el OAuth (sin refresh). Sin `MP_PRUEBA`
la ruta lo rechaza. Las devoluciones por API tampoco andan con tokens de cuentas
de prueba (401 «Unauthorized use of live credentials»): `devolver` lo explica.
**La app de Mercado Pago de la plataforma es «FiBOT - Consultorios - Pagos» (App ID 2884635343502809)**, creada como «Plataforma de e-commerce → Otra plataforma», con la URL de redireccionamiento `https://consultorio.fibot.ar/mercadopago/vuelta`, PKCE en «No» y permisos read/write/offline_access. `MP_CLIENT_ID`/`MP_CLIENT_SECRET` en Railway son los suyos. La app anterior («FiBOT Consultorio», 8592070181994665) mostraba «La aplicación no está preparada para conectarse a Mercado Pago» antes de pedir permiso, aun con todo bien configurado y después de cambiarle el tipo a plataforma; con una app nueva anduvo a la primera (probado el 04/11/2026: conectar, cobrar y devolver con una cuenta de tercero). Si vuelve a pasar, crear una app nueva directamente como plataforma. Los avisos de pago no dependen del panel: cada cobro manda su `notification_url` (`/webhook/mercadopago?t=<tenant>`).
El cobro (`pagos.preferencia`) manda lo que pide la medición de calidad: `statement_descriptor` (`pagos.descriptor_resumen`: nombre del profesional sin tildes, hasta 22), `items.description`, `items.category_id = services`, `payer.name`/`surname` (sólo el nombre que dejó al reservar, `_comprador`) y `X-Idempotency-Key`. Email opcional al reservar (`migrations/064`): sólo cuando la reserva se paga (`pagos.activo`), queda en `pagos.paciente_email` (nunca en la ficha), va como `payer.email` y al aprobar `pagos.mandar_recibo` le manda el link del recibo por mail (Resend + `DOMINIO_PRINCIPAL`).
Conexión caída (`tenants.mp_caido_at`, `migrations/064`): si Mercado Pago contesta 401/403 (o `invalid_grant` al renovar), `pagos.marcar_caida` deja de ofrecer Mercado Pago (`metodos`), avisa UNA vez al profesional y el panel pide reconectar; `pagos.verificar_conexion` lo chequea una vez por día desde el tick (`/users/me`). Reconectar (`_guardar_tokens`) o desconectar la limpia. También avisa a administración (`whatsapp.avisar_admin`, con el nombre del profesional). /admin muestra por cuenta si Mercado Pago está conectado, cortado o no (`todos_los_tenants` trae `mp_user_id`/`mp_caido_at`, nunca el token). En el Estado de /admin, «Mandarme un mail de prueba» (`/admin/mail/prueba`, `mail.enviar_detalle`) dice si Resend aceptó o por qué no. El recibo (`recibos.py`: `datos`, `foto_valida`, `pdf` con fpdf2 y fuentes base latin-1) lleva la foto del perfil si es PNG/JPG embebida; `/pago/<token>/recibo.pdf` lo baja y `mandar_recibo` lo adjunta al mail (Resend `attachments`). Si el PDF falla, el mail sale igual con el link. Antes de dar por cortada la conexión, `_rechazo_confirmado` vuelve a preguntar `/users/me` (2 intentos con pausa, `_pausa`) y la renovación del token se reintenta una vez. /admin suma lo cobrado por profesional en el mes elegido (`?cobros=YYYY-MM`, `db.cobros_por_tenant`, neto de devoluciones, por medio; sólo montos) y `uso.csv` trae las mismas columnas. `migrations/065`: `tenants.recibo_cuit` (validado con el dígito verificador, `cuit_valido`) y `recibo_domicilio`, opcionales, salen en el recibo (página y PDF); `resumen_cobros_mes` marca el resumen mensual: `pagos.mandar_resumen_mensual` (desde el tick, día 1 desde las 9) manda al mail del profesional lo cobrado el mes anterior con el detalle en PDF (`recibos.pdf_resumen`), una vez por mes y sólo si cobró algo. `migrations/066`: `pagos.recibo_numero` correlativo por profesional (`db.asignar_numero_recibo` al aprobar, índice único; el recibo muestra 0001…; los viejos siguen con el código), `tenants.resumen_cobros_mail` (apagar el resumen desde avisos). Al entrar un pago por Mercado Pago, `_avisar_pago_mp` le manda WhatsApp al profesional con el monto y la sesión (sin el nombre del paciente). `/agenda/pagos-web.xlsx?mes=&nombres=1`: los pagos web del mes para el contador (sin nombres salvo que se pidan). `migrations/067`: `tenants.aviso_cada_pago` (casilla en avisos; apagado, `_avisar_pago_mp` no manda nada). Al rechazar un comprobante, `pagos.avisar_rechazo` le manda un mail al paciente (si dejó email) con el link a la página para volver a reservar. /admin muestra arriba de la tabla el tablero del mes de toda la plataforma (`_tablero_cobros`: total neto, pagos, profesionales que cobraron de cuántas cuentas, por medio y % contra el mes anterior), un gráfico de barras de los últimos 6 meses (`_serie_cobros`) y la lista de cuentas con comprobantes sin revisar hace más de 24 h (`db.comprobantes_demorados`, `migrations/068`: `pagos.comprobante_at`, sólo nombre de la cuenta y cantidad). Al subir un comprobante se pide el email del paciente si no lo dejó al reservar. Rechazar acepta un motivo de `pagos.MOTIVOS_RECHAZO` (otro valor se descarta) que va en el mail; con `PAGO_RECHAZADO_TEMPLATE` ({{1}} profesional, {{2}} link de la página) también sale WhatsApp al teléfono del turno (`avisar_rechazo_whatsapp`); sin plantilla, sólo mail.
La vuelta del OAuth explica cada falla (`?error=` de Mercado Pago, `access_denied` = canceló; `state` vencido u otro navegador; canje rechazado con su motivo) con `app.MP_QUE_PROBAR` y anota el código limpio (`_codigo_mp`) en la actividad, nunca datos de la cuenta.
El bot cobra igual que la página: `conversation._cobrar_reserva` (tras `reservar_turno`, si
`pagos.activo` y hay precio) carga `valor`, llama a `pagos.iniciar` y contesta con el link
`/pago/<token>` y un botón «Pagar» (`interactive` `cta_url`, `whatsapp.enviar_boton_link`;
si falla, el texto con el link). Vence y se libera con el mismo `pagos.vencer`.
Conectar (OAuth o token) activa el cobro (`app._tras_conectar_mp`: no → mercadopago,
transferencia → ambos) y avisa si falta el precio.
`revisar_pago_mp` también sincroniza devoluciones y contracargos hechos desde
Mercado Pago (`_sincronizar_devolucion`: `refunded`, `charged_back` o
`transaction_amount_refunded`), sólo si el id coincide con `pagos.mp_pago_id`;
es idempotente y el contracargo se avisa al profesional. Con el pago devuelto
entero, las sesiones que siguen reservadas con ese `pago_id` pasan a
`pago = 'pendiente'` y `pago_web = 'devuelto'` (`migrations/057`): siguen
reservadas, el profesional decide.
`/agenda/novedades` devuelve `db.version_panel` (firma de pagos y reservas
nuevas, sin datos); el panel pregunta cada 30 s con la pestaña visible y, si
cambió, muestra «Hubo novedades · Actualizar» (no recarga solo).
Varias sesiones desde la web (mismo día y hora, 4 u 8 semanas): sólo con cobro
activo (se paga todo junto). `app._serie` busca los libres exactos; se reservan
los que haya (con `frecuencia = 'semanal'` y la ficha del primero) y un solo
`pagos` cubre todos.

`migrations/055`: `tenants.feriados` ('bloquear' por defecto / 'atender').
`feriados.py` trae los de Argentina embebidos (fuente argentinadatos.com) y una
vez por día los actualiza de la API; `scheduler.sincronizar_feriados` los carga
como bloqueos con motivo «Feriado: …» (idempotente; con 'atender' los borra).
`tenants.sedes` ([{nombre, direccion}], hasta `app.MAX_SEDES`): cada franja del horario elige
consultorio y queda en `turnos.sede` (del horario, como `solo_modalidad`; la
vista la expone). Se muestra en el calendario con más de una sede, en el
formulario con la dirección, y `cuando_de` agrega «en X (dirección)» si no es
virtual. `/agenda/imprimir?semana=` es la hoja de la semana: hora, paciente,
tipo, modalidad o consultorio y pago; sin notas ni teléfonos.

Devoluciones, recibo y aviso (`migrations/056`): `turnos.pago_id` dice qué pago
cubre la reserva (va en `_LIBERADO` y viaja al reprogramar si ya estaba
pagada). `pagos.al_cancelar` aplica la política (`tenants.devolucion_modo`:
todo / menos_sena / nada, si cancela con al menos `devolucion_horas`); una
serie se reparte en partes iguales. Con Mercado Pago devuelve sola
(`pagos.devolver`: `/v1/payments/{id}/refunds` con `X-Idempotency-Key`); si
falla, o fue transferencia, o canceló el profesional, suma `pagos.a_devolver` y
aparece en «Pagos para revisar» con «Devolver» / «Ya lo devolví». `devuelto`
acumula; al llegar al monto, estado `devuelto`. Recibo: `/pago/<token>/recibo`
(no es factura; usa `pagos.paciente_nombre` y `pagos.sesiones`, guardados al
crear el pago, así sobrevive a la cancelación). «Lo que entró por la web» del
panel es `app._cobros_web` del mes mirado. `pagos.avisar_por_vencer` (tick) le
manda una vez al paciente `PAGO_PENDIENTE_TEMPLATE` ({{1}} profesional, {{2}}
cuándo, {{3}} link) cuando queda un tercio del plazo; sin plantilla o sin
`DOMINIO_PRINCIPAL` no manda.

Panel: lo de abajo del calendario son `<details class="card plegable"
data-plegable>` agrupados (Tu agenda / Tu página y cobros / Tu cuenta); lo que
pide acción (pagos para revisar, horarios cancelados) queda siempre abierto
arriba. Cada sección recuerda si quedó abierta (localStorage, sólo comodidad) y
el `#id` de la URL la abre. Horario (`MAX_FRANJAS` por día, sin pisarse),
tipos (`MAX_TIPOS`) y consultorios (`MAX_SEDES`) muestran los renglones
cargados y «+ otro…» destapa el siguiente.

Reservar tiene doble confirmación: el primer POST (sin `confirmado=1`) sólo
muestra `publico_revisar.html` con profesional, día, hora, nombre y WhatsApp, sin
tomar el horario; el segundo reserva. Todas las pantallas dicen con quién es el
turno.
En `/<slug>` (con JS) todo pasa dentro de la tarjeta: tocar un horario abre el
formulario y los dos POST van a las mismas rutas con `X-Parcial: 1`, que
devuelven sólo el pedazo (`_reserva_revisar.html`, `_reserva_lista.html` o
`_reserva_error.html` con 409; `data-otro` = ese horario ya no sirve). Con cobro
la respuesta trae `X-Ir-A` con `/pago/<token>`. Las páginas completas incluyen
los mismos pedazos, y sin JS (o si el fetch falla) se sigue por ellas.
Nombre y WhatsApp se recuerdan en el navegador (`_recordar_datos.html`, localStorage
`fibot_paciente`, sólo esos dos datos y sin el profesional) en la reserva y la lista
de espera: casilla «Recordar mis datos en este dispositivo» (tildada; destildarla
borra) y «No soy yo». Los campos llevan `data-recordar`.

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

### Seguimiento de sesiones: asistencia, pago, reprogramar (`migrations/004`)

El panel (`/agenda`) muestra una semana por vez (`?semana=YYYY-MM-DD`, con
‹ ›) y, sobre cada sesión reservada, deja marcar **si vino**, **si pagó** y
**pasarla a otro día**. Arriba de todo, "Pendientes de cobro": sesiones ya
pasadas con `pago = 'pendiente'`. También se puede **agendar a un paciente a
mano** en un horario libre (los pacientes fijos no van a reservar cada semana
por la página pública) — es el mismo `db.reservar_turno` atómico.

- `asistencia` (NULL / `vino` / `no_vino`), `pago` (`pendiente` / `pagado` /
  `no_cobra`) y `reprogramado_desde` describen a la **reserva**, no al
  horario. Cuando un turno vuelve a `libre` se limpian junto con `paciente_*`
  (`db._LIBERADO`) — si no, el próximo que reserve ese horario heredaría el
  "pagó" del anterior. `recordatorio_enviado` también se resetea ahí.
- `db.reprogramar_turno` **toma el horario nuevo primero** (UPDATE filtrado
  por `estado = 'libre'`) y recién después libera el viejo: si el nuevo
  estaba ocupado, no se tocó nada. Si a esa hora no había turno generado, lo
  crea. El pago viaja con la sesión; la asistencia no. Se rechaza si se pisa
  con otra sesión reservada, aunque no coincida el `inicio`. Si la sesión
  queda en el futuro, se le avisa al paciente en el momento (tilde en el panel,
  `REPROGRAMADO_TEMPLATE`).
- Un horario libre que se pisa con una sesión sigue en `libre`, pero
  `db.turnos_libres` lee de la vista `turnos_disponibles` (`migrations/005`) y
  no lo ofrece; `db.reservar_turno` también lo rechaza. Vuelve solo si la
  sesión se mueve. El chequeo no es atómico: dos reservas al mismo milisegundo
  sobre horarios solapados podrían entrar, y sólo existen horarios solapados
  después de pasar una sesión a una hora fuera de la grilla.
- `reprogramado_desde` guarda el **horario** viejo, no un id al turno viejo:
  ese horario queda libre y puede tomarlo otro paciente.
- `db.crear_turnos_libres` usa `ignore_duplicates=True` (ON CONFLICT DO
  NOTHING): antes, regenerar una semana con sesiones reservadas las pisaba
  con `estado = 'libre'`.

### Calendario, valor de sesión y pacientes fijos (`migrations/006`)

`/agenda` entra por el **mes** (`?mes=YYYY-MM`), tipo Google Calendar; tocar un
día abre la **semana** (`?semana=...`), que es donde están las acciones de cada
sesión, en cascada y con Asistió arriba de todo.

- `turnos.valor` (pesos enteros) y `turnos.frecuencia` describen a la reserva:
  van en `db._LIBERADO` y viajan al reprogramar.
- **Semanal/quincenal no es una serie**: agendar crea N filas sueltas por
  adelantado (`MAX_SESIONES_FIJAS`), y cada una se mueve o cancela sola. Las
  fechas ocupadas se saltean y se avisan. No se renuevan solas: el panel y el
  resumen diario avisan cuando quedan 2 o menos (`fechas.sesiones_que_quedan`)
  y se renuevan con un botón.

### Fichas de pacientes (`migrations/007`)

El panel es un tablero: calendario y pendientes arriba, generar horarios y
fichas abajo. Cada ficha (`app._fichas`) se arma con las sesiones de ese
teléfono; lo guardado aparte es lo que anota el profesional (`pacientes`,
`paciente_personas`, `paciente_fechas`, `migrations/008`). Al tocarla se abre en
un popup (`<dialog>`) con notas, fechas con cuenta regresiva y cómo le pegan,
personas y mascotas.

El popup se divide en cinco pestañas: Terapia, Sesiones, Crisis, Personas (con
las fechas) y Datos. Próxima sesión y deuda quedan siempre a la vista arriba.
Abre en Terapia, y al guardar vuelve a la pestaña donde estaba.

### Psicopedagogía (`rubros.py`, `migrations/021`)

No es otro deploy: `tenants.rubro` elige textos y pestañas de la ficha. Con
«psicopedagogía», Crisis pasa a Escuela (escuela, grado, docente y equipo de
orientación, más los datos de salud y emergencia), Terapia a Trabajo y Personas a
Familia. Las claves internas de las pestañas y las columnas no cambian. Los demás
rubros siguen igual.

### Tests para considerar (`tests_sugeridos.py`)

Sólo en psicopedagogía, en la pestaña Trabajo. Cruza palabras de motivo, objetivos,
cómo viene, notas y notas de sesión con un catálogo por área y edad. Son reglas
locales a propósito: esas notas son de menores y no van a ningún servicio externo.
Muestra la palabra que disparó cada área. Las edades son orientativas.
`paciente_tests` (`migrations/026`) registra los tomados: no se sugieren hasta que
pasan los meses del test en `CATALOGO`, y después vuelven con la fecha anterior.
`tenants.plazos_tests` (`migrations/027`) guarda sólo los plazos que el profesional
cambió, así un plazo de fábrica nuevo le llega a quien no lo tocó.

`paciente_tests.resultados` (`migrations/028`, `resultados_tests.py`) guarda los
puntajes YA corregidos que carga la profesional: el sistema no corrige porque los
baremos son de las editoriales (Pearson, TEA, Paidós) y no se pueden incluir sin
licencia. `evolucion()` compara la misma escala y tipo entre tomas del mismo test;
bajo el umbral de `TIPOS` (~1/3 de desvío) es «se mantuvo». Es descriptiva a
propósito («subió/bajó», nunca «mejoró»): en cuestionarios de síntomas subir es peor.

### Fluidez lectora (`fluidez.py`, `migrations/029`)

`textos_lectura` son los textos de cada profesional (nunca incluir textos de tests:
son de las editoriales). `templates/fluidez.html` cronometra y marca errores en el
navegador; al guardar manda palabras leídas, índices de error y segundos, y el
servidor recalcula todo desde el texto (`fluidez.validar`). `lecturas` guarda el
título y las palabras erradas, no el id del texto: borrar un texto no borra lecturas.
Mide palabras correctas por minuto; sin normas por grado a propósito.

### Cuestionarios por link (`cuestionarios.py`, `migrations/030`)

PHQ-9 y GAD-7 con el texto oficial «Spanish for Argentina» de phqscreeners.com
(uso libre, sin permiso). `rubros.py` decide cuáles ofrece cada rubro. El link
público `/cuestionario/<token>` no usa sesión: el token aleatorio ES la
credencial, sirve una vez (`responder_cuestionario` filtra `respondido_at is
null`) y vence a los 7 días. La página pública nunca muestra el nombre ni el
teléfono del paciente. Pregunta 9 del PHQ-9 > 0 → `alerta`: rojo en la ficha y
teléfonos de ayuda (135, 911) al paciente. Antes de sumar un instrumento,
confirmar licencia Y traducción castellana (SNAP-IV y SCARED siguen en duda).
`cuestionarios.ABIERTOS` (entrevista de familia y de adulto, docente) usan el
mismo link pero son preguntas abiertas de redacción propia: no se corrigen.
`cuestionarios.para` (`migrations/031`) es la referencia que ve quien responde.

Cuestionarios propios (`migrations/033`): `plantillas_cuestionario` por tenant;
al mandar el link se copia la plantilla a `cuestionarios.plantilla` (instrumento
`propio`), así editarla no cambia lo respondido. `cuestionarios.definicion(fila)`
unifica fábrica, abiertos y propios. `MODELO_SCARED` trae SÓLO la clave de
corrección verificada (UPMC): los ítems van vacíos y los pega la profesional.
NUNCA cargar texto de ítems de tests de terceros sin licencia explícita.
`version` (dentro del jsonb) separa padres/chico/docente de un mismo cuestionario;
`comparar_versiones` las pone lado a lado en la ficha. Exportar/importar usa
`formato: fibot-cuestionario` y al importar todo pasa por `leer_plantilla`, nunca
se guarda el JSON tal cual.
`todas:<nombre>` en el form de la ficha crea un link por versión completa
(`cuestionarios.familias`). `comparar_versiones` suma «antes» con la toma previa.
`biblioteca_cuestionarios` (`migrations/035`) es la ÚNICA tabla que se lee entre
tenants: sólo lo que cada profesional compartió. Por defecto sin texto de ítems
(`para_biblioteca`); con texto exige declarar que es propio o de uso libre.
Agregar desde la biblioteca pasa por `importar` y queda como copia del tenant.
`tenants.moderador` (`migrations/036`, se marca a mano por SQL) habilita ocultar y
mostrar entradas de la biblioteca: lo oculto sólo lo ven moderadores y la autora.

### Entrega por link (`migrations/036`)

`entregas` guarda el HTML de la ficha completa al generar el link (lo que se
entrega es lo que se revisó). `GET /entrega/<token>` sólo muestra el aviso: los
previsualizadores de WhatsApp y del mail hacen GET y no deben consumirlo.
`POST` lo abre UNA vez (`abrir_entrega` filtra `abierta_at is null`). Respuestas
con `no-store`, `no-referrer` y `noindex`.
`entregas.dni_hash` (`migrations/037`) es sha256(token:dni), nunca el DNI; a los
`MAX_INTENTOS_DNI` fallidos queda bloqueada. El resumen diario cuenta las abiertas
en las últimas 24 horas.

### Administración (`/admin`, `migrations/037`)

Cuenta aparte de los profesionales (`admins`, se crea con `scripts/crear_admin.py`:
la clave la escribe quien lo corre). `session["admin_id"]` nunca convive con
`tenant_id`. Ve cuentas, uso de 30 días (sólo cuentas: nunca datos de pacientes),
da de alta profesionales, los activa/desactiva y modera la biblioteca. Una cuenta
desactivada pierde también la sesión abierta (`login_required` lo chequea).
`SLUGS_RESERVADOS` está en app.py y en `scripts/crear_tenant.py`: mantenerlos iguales.
El doble factor (TOTP, `seguridad.py`, sin dependencias) es OBLIGATORIO para admin:
sin `totp_secreto` sólo se puede entrar a `/admin/doble-factor`. Tras la clave, la
sesión queda en `admin_pendiente` hasta el código; `totp_ultimo_paso` impide reusarlo.
`/admin/uso.csv?mes=AAAA-MM` baja el uso del mes (con BOM y «;» para Excel).
`sesion_version` (`migrations/039`, tenants y admins): subirla cierra todas las
sesiones de la cuenta; cambiar la clave la sube y deja viva la sesión actual.
`tenants.ultimo_bloqueo` se marca cuando el límite bloquea al usuario: aviso en el
panel (7 días) y en el resumen (24 horas). En la máquina de Nacho no hay `python`:
los scripts van con `railway run .venv/bin/python scripts/...`.

### Sesiones, QR y actividad (`migrations/040`)

Cada login de profesional crea una fila en `sesiones` (`_abrir_sesion`); la cookie
sólo lleva `sesion_id` y `login_required` exige que la fila exista (cerrar = borrar
la fila; para tenants ya no se usa `sesion_version`). Dispositivo = «Chrome en
Windows», nunca el user agent entero ni la IP.
Entrar con QR: la compu pide `/login/qr` (token en el QR + nonce en SU sesión), el
celular logueado abre `/login/qr/<token>` y aprueba (POST con CSRF), la compu
consulta `/estado` y recién ahí se le abre la sesión, una sola vez. Del token y del
nonce sólo se guardan hashes. Los QR se dibujan con `segno` en el servidor: el del
doble factor lleva el secreto y nunca puede salir a un servicio externo.
Doble factor opcional para profesionales (`tenants.totp_secreto`), mismo esquema
que admin. `enlaces_clave`: link de 24 h que genera admin (no hay mail todavía).
`actividad` registra ingresos, bloqueos, claves, sesiones, altas y moderación:
nunca datos de pacientes.

### Mail, limpieza y llaves de acceso (`migrations/041`)

`mail.py` manda por Resend (`RESEND_API_KEY` + `MAIL_REMITENTE` en Railway); sin
ellas no se manda nada y «¿Te olvidaste la clave?» sólo explica que lo pida a
admin. La respuesta del pedido es siempre la misma (no dice si la cuenta existe)
y cada pedido cuenta en `login_intentos` (`olvido:`). Link de 1 hora.
`scheduler._limpiar` corre una vez por día: QR, links y sesiones vencidas,
intentos viejos, copias de entregas de más de 90 días, links de cuestionario sin
responder y actividad de más de un año. Nunca borra respuestas ni fichas.
Llaves de acceso con `webauthn` (py_webauthn): se guarda sólo la clave pública
(`llaves_acceso`), se exige verificación del usuario (huella/cara/PIN), y la
llave reemplaza clave + doble factor. rp_id = el host del pedido: si cambia el
dominio, las llaves registradas dejan de servir. `templates/_llaves_js.html`
convierte base64url sin librerías. Probado con firma real usando un
autenticador de software (`soft-webauthn`, con la marca de verificación puesta).
Admin tiene sus propias llaves (`admin_llaves`, `migrations/042`): entran a /admin
sin clave ni código. Las rutas `/admin/login/*` no piden CSRF (no hay sesión).
`_avisar_cuenta` manda un mail de seguridad (llave nueva, clave cambiada, doble
factor) si hay proveedor y la profesional cargó su mail: sin IP ni pacientes.
`DOMINIO_PRINCIPAL` (ej. consultorio.fibot.ar): cualquier otro host redirige con
308, salvo `/webhook` (Meta sigue apuntando a la URL de Railway) y `/health`.
Cargarla recién cuando el dominio tenga certificado; las llaves creadas en la
dirección vieja dejan de servir.
El QR también entra a administración (`migrations/043`: `qr_logins.tipo` =
'panel' | 'admin'; un QR de un lado nunca abre el otro). Crear y consultar:
`/admin/login/qr` (sin sesión ni CSRF); aprobar: `/admin/qr/<token>` (sesión de
admin + CSRF). `_estado_plataforma` arma la tarjeta «Estado» de /admin: dice si
las variables están cargadas, nunca sus valores.
Las llaves se probaron en Chrome real (sin ventana, autenticador virtual de
DevTools) contra la app local con la base en memoria de las pruebas: registrar
y entrar, para profesional y para admin.

### Límite de intentos de login (`migrations/038`)

`login_intentos` cuenta fallos por usuario (`panel:`, `admin:`, `totp:`; 5) y por IP
(`ip:`; 20, porque un consultorio comparte IP): bloquea 15 minutos. La IP sale de
ProxyFix `x_for=1` (la que agrega el proxy de Railway, no la del cliente).

### Hoja de registro, informe y retest

`templates/registro.html` cuenta aciertos/errores y tiempo por subprueba y guarda
por la misma ruta que «Cargar resultados» (como `bruto`), sumando lo que ya había.
`informe.py` arma el borrador con reglas locales (nada sale a servicios externos)
y `templates/informe.html` se edita en pantalla. «Guardar en la ficha» manda el
contenido como JSON (`informe.leer_guardado` lo limpia) a `informes`
(`migrations/032`): cada guardado es una versión nueva, nunca se pisa.
`plantillas_informe` (`migrations/034`) guarda sólo títulos de secciones;
`informe.AUTOMATICAS` decide qué títulos se completan solos. `tenants.firma_imagen`
es un data URL PNG/JPG validado por los primeros bytes (nunca SVG: es HTML).
`templates/_firma.html` la pone en informes y en la ficha completa
(`/agenda/paciente/ficha-completa`), que incluye notas: es para entregar a mano. `tests_sugeridos.
para_repetir` avisa durante `VENTANA_AVISO` días desde que vence el plazo; el
resumen de WhatsApp cuenta sólo los que vencen hoy (sale una vez por día).

### Una ficha por persona (`migrations/022`)

Una ficha es teléfono + `ficha`, no sólo teléfono: dos hermanos pueden usar el
WhatsApp del mismo adulto. `db.clave_de` arma esa identidad y es lo que viaja en
los forms; `paciente_telefono` sigue siendo el número al que se escribe. Nunca
agrupar datos de un paciente por teléfono solo.

`db.ficha_para` decide al reservar, en panel, página pública y bot: mismo nombre,
misma ficha; otro nombre, ficha aparte. `db.mismo_nombre` pide que el nombre corto
esté entero en el largo: «Nina» es «Nina Acosta», «Juan Pablo» no es «Juan Ignacio».
Dos nombres idénticos no se pueden deducir: en el panel se tilda «Es otra persona»
y la serie y la renovación pasan la ficha explícita; el bot pregunta y nunca elige.
La página pública no puede preguntar sin mostrar nombres de pacientes: reserva en la
ficha más reciente con `turnos.revisar_ficha` (`migrations/024`), y el profesional
la confirma o la mueve desde «Para mirar». El resumen diario suma cuántas hay, sin
nombres. Un error se corrige con «Es de
otra persona» en la sesión. `pacientes.nombre` (`migrations/023`) recuerda de quién
es una ficha sin sesiones: otro nombre no la hereda y esa persona la recupera al
volver. Si tiene algo anotado se ve en el desplegable «Sin sesiones», y también
sirve de destino para pasarle sesiones. Si sólo tiene el nombre, no se muestra. Sin nombre, el bot pide uno si hay más de una ficha.

Pacientes sin WhatsApp: en el panel el número es opcional al agendar. Sin número,
`paciente_telefono` lleva una clave inventada `sin-<hex>` (`db.SIN_WHATSAPP`, nunca
todos dígitos) y la ficha es `''`. El mismo nombre completo idéntico reusa la clave
(`db.clave_sin_whatsapp`), «Es otra persona» abre otra. `whatsapp.py` no manda
nada a algo que no sean dígitos (devuelve True: resuelto, no se reintenta), así
que recordatorios, avisos y cobros se saltean solos. Antes de decir «le avisamos»
o mostrar un botón de WhatsApp, mirar `db.tiene_whatsapp`. Desde la ficha
(pestaña Datos) se le carga el número: `db.cargar_whatsapp` pasa todas sus filas
de `TABLAS_DEL_PACIENTE` al número nuevo (ficha aparte si ese número ya tiene
pacientes). Una tabla nueva con `paciente_telefono` va en esa lista.

Cancelar por WhatsApp nunca adivina: con turnos de dos personas en el teléfono,
o con un nombre que no coincide, el bot los lista y no cancela nada hasta recibir
`cancelar Lola`.

### Cómo se usa (`templates/ayuda.html`)

Guía dentro del panel, adaptada al rubro. Si cambia un flujo o un texto visible,
se actualiza acá en el mismo PR.
Arriba tiene «Recorrido por tu panel»: GIFs de cada parte (`static/ayuda/*.gif`,
celular de 412 px, datos inventados). Se graban con `scripts/guia/` (la app con la
base en memoria de `test_seguimiento.py` en 127.0.0.1:8770 + Chrome sin ventana por
DevTools; ver el docstring de `servidor.py`). Si cambia mucho una pantalla, se
vuelve a grabar esa escena. `test_inicio.py` revisa que cada GIF exista y pese
menos de 800 KB, y que ningún `var(--x)` quede pegado a restos de un color (así
quedó la ficha transparente desde #97: `var(--card)df5`).

### Reservas nuevas (`migrations/025`)

Una reserva sin ficha explícita (página pública o bot) lleva `reserva_nueva`: se ve
en la tarjeta «Reservas nuevas» hasta marcarla vista. El scheduler la avisa por
WhatsApp a `aviso_telefono` en el siguiente tick, con fecha y sin nombres. Lo que
agenda el profesional pasa ficha explícita y no se marca.

### Hoy

`app._hoy` arma, arriba del panel, una tarjeta por sesión del día con deuda,
fechas de los próximos 14 días y el inicio de las notas. Abre la ficha.

### Resumen diario (`migrations/009`)

`scheduler._resumen_diario` le manda al profesional, desde las 8, un WhatsApp
por día con **sólo cuentas**: ningún nombre ni dato de paciente sale por ahí. El
detalle está en el panel. Se marca antes de enviar, para no mandarlo dos veces.

### Cobros (`migrations/010`)

`app._cobros` resume el mes que se está mirando (plata, sesiones dadas, ausencias,
pacientes y comparación con el mes anterior); «Quién debe» es histórico. El
recordatorio de pago sale **sólo** cuando el profesional toca el botón de la
ficha, con el monto calculado en el servidor y una vez por día como máximo.

### Lista de espera (`migrations/011`)

Al cancelar desde el panel se avisa si hay gente esperando. Ofrecer el próximo
horario libre es manual, persona por persona, y manda el link de la página
pública: quien está en la lista quizá nunca tuvo turno y el bot no lo reconoce.

### Sesiones virtuales (`migrations/012`)

`turnos.modalidad` describe a la reserva: va en `db._LIBERADO` y viaja al
reprogramar y renovar. El link del profesional entra en el parámetro `{{2}}` de
las plantillas de recordatorio y cambio (`scheduler.cuando_de`).

### Obra social y constancia (`migrations/013`)

La ficha guarda obra social y número de afiliado. La constancia es una página
imprimible de las sesiones pagas de un mes: **no es factura** y nunca lleva
notas ni fechas personales. Va por POST para no dejar el teléfono en la URL.

### Nota de cada sesión (`migrations/014`)

`turnos.nota` describe a la reserva: va en `db._LIBERADO` y viaja al reprogramar.
Se escribe desde la semana o desde «Últimas sesiones» en la ficha, y la tarjeta
de Hoy muestra la última para repasar antes de atender. Es lo más sensible del
sistema: nunca sale por WhatsApp, constancias ni exportaciones.

### Motivo y objetivos (`migrations/015`)

Motivo de consulta, qué se está trabajando, cómo viene y las notas van en la
pestaña Terapia, en su propio form. `/agenda/paciente` sólo guarda los campos que recibe.

### Datos de crisis (`migrations/016`)

Una persona de la ficha se puede marcar como contacto de emergencia (con
teléfono); esos contactos, psiquiatra, medicación y qué hacer van en la pestaña
Crisis del popup. Son datos de salud: no salen del panel.

### Alta y pausa (`migrations/017`)

`pacientes.estado` cierra un tratamiento sin borrar la ficha: sale de «Por
renovar», «Para mirar», fechas y resumen diario, pero la deuda se sigue viendo.
Liberar sus sesiones futuras es opcional. La ficha pasa a un desplegable aparte.

### Ausencias y cancelaciones (`migrations/018`)

Cancelar borra al paciente del turno, así que `cancelaciones` guarda el hecho con
quién lo hizo (`origen`). Sólo se registra si el llamador pasa `origen`:
reprogramar y cerrar un tratamiento no cuentan. La ficha muestra faltas y
cancelaciones de los últimos 90 días; desde 3 entra en «Para mirar».

### Exportar a Excel

`/agenda/exportar` baja sesiones y resumen de un mes para el contador. Por defecto
va sin nombres; nunca lleva teléfonos ni notas. Las celdas de texto se fuerzan a
texto: el nombre viene de una página pública y podría ser una fórmula.

### Aumento de honorarios

`/agenda/aumento` cambia, por % o valor fijo, el valor de las sesiones sin pagar
desde una fecha, sólo de los pacientes tildados. Lo pasado o cobrado no se toca.

### Días bloqueados (`migrations/019`)

Un bloqueo no cancela nada: saca los horarios de `turnos_disponibles`,
`db.reservar_turno` y `reprogramar_turno` lo rechazan, y el panel lista las
sesiones que quedan adentro para que el profesional avise o las mueva. `hasta` es
exclusivo (medianoche local del día siguiente).

### Planilla para obra social (`migrations/020`)

`/agenda/obra-social` arma una página imprimible por obra social y mes con
pacientes, afiliado y fechas de las sesiones marcadas como asistidas; avisa las
que quedaron sin marcar. Sin teléfonos, montos ni notas. La matrícula se carga en
Ajustes y sale también en la constancia. No es una liquidación: el formato de
cada obra social puede pedir otra cosa, hay que validarlo con el profesional.

### CSRF del panel

`app.verificar_csrf` exige `csrf_token` en todo POST bajo `/agenda`. Cualquier
form nuevo del panel lleva `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">`.
El webhook (firma de Meta) y la reserva pública (sin sesión) no lo usan.

## Lo que NO tiene esta v1 (a propósito)

- **Cobro integrado**: no hay integración con MercadoPago ni nadie. El panel
  sólo **registra** cuánto vale la sesión y si el paciente pagó (lo carga el
  profesional a mano); el sistema no cobra. El canon a los profesionales
  también se cobra a mano, fuera del sistema — "como todo se hace a mano, se
  cobra primero y se arma después".
- **Historia clínica formal / diagnóstico**: fuera de alcance. Las notas de la
  ficha y de cada sesión existen por pedido explícito, pero son información sensible bajo secreto
  profesional. **Antes de cargar pacientes reales** hace falta: base propia
  (hoy comparte `vencimetro`), cifrar notas y fechas, sacar la clave `demo/demo`,
  backups y consulta legal. Hasta entonces, sólo datos ficticios.
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
