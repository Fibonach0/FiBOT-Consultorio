"""Cuestionarios que el paciente completa por link y el sistema corrige.

Sólo instrumentos de uso libre. PHQ-9 y GAD-7: texto de la versión oficial
«Spanish for Argentina» de phqscreeners.com (Pfizer), que dice «No es
necesario ningún permiso para reproducir, traducir, mostrar o distribuir».
Se corrigió un error de tipeo del original («engería» → «energía»).

Cortes de severidad: los de los autores (Kroenke 2001, Spitzer 2006).
SNAP-IV y SCARED quedan afuera hasta confirmar licencia y traducción.
"""

import secrets

OPCIONES = ["Nunca", "Varios días", "Más de la mitad de los días", "Casi todos los días"]
FUENTE = ("Desarrollada por los doctores Robert L. Spitzer, Janet B. Williams, Kurt Kroenke y colegas "
          "con una donación de Pfizer Inc. No es necesario ningún permiso para reproducir, traducir, "
          "mostrar o distribuir.")

INSTRUMENTOS = {
    "phq9": {
        "nombre": "PHQ-9",
        "que_mide": "depresión",
        "consigna": "Durante las últimas dos semanas, ¿con qué frecuencia tuvo molestias por cualquiera de los siguientes problemas?",
        "items": [
            "Poco interés o placer en hacer cosas",
            "Sentirse decaído/a, deprimido/a, o desesperanzado/a",
            "Dificultad para dormir o permanecer dormido o dormir demasiado",
            "Sentirse cansado/a o con poca energía",
            "Con poco apetito o comer demasiado",
            "Sentirse mal consigo mismo o sentir que uno es un fracaso o que le ha fallado a su familia o a sí mismo",
            "Dificultad para concentrarse en cosas, tales como leer el diario o ver la televisión",
            "¿Se ha movido o hablado más lentamente que otras personas lo notaron? O por el contrario — "
            "ha estado más inquieto/a e intranquilo/a, moviéndose más de lo habitual",
            "Pensamientos de que usted estaría mejor muerto/a, o de hacerse daño a sí mismo/a de alguna manera",
        ],
        # (desde, etiqueta): el puntaje cae en el último corte que alcanza.
        "cortes": [(0, "mínima"), (5, "leve"), (10, "moderada"), (15, "moderadamente severa"), (20, "severa")],
        "item_alerta": 8,  # el 9: ideas de muerte o de hacerse daño
        "dificultad": {
            "pregunta": "Si usted tuvo molestias por alguno de los problemas mencionados, ¿cuánta dificultad le "
                        "causaron estos problemas para hacer su trabajo, encargarse de las tareas del hogar o "
                        "llevarse bien con otras personas?",
            "opciones": ["Ninguna dificultad", "Un poco de dificultad", "Mucha dificultad", "Extremada dificultad"],
        },
    },
    "gad7": {
        "nombre": "GAD-7",
        "que_mide": "ansiedad",
        "consigna": "Durante las últimas dos semanas, ¿con qué frecuencia sintió molestias por los siguientes problemas?",
        "items": [
            "Sentirse nervioso/a, ansioso/a o con los nervios de punta",
            "No poder dejar de preocuparse o no poder controlar la preocupación",
            "Preocuparse demasiado por diferentes cosas",
            "Dificultad para relajarse",
            "Estar tan inquieto/a que es difícil permanecer sentado/a quieto/a",
            "Enojarse o irritarse con facilidad",
            "Sentirse con miedo como si algo terrible fuera a suceder",
        ],
        "cortes": [(0, "mínima"), (5, "leve"), (10, "moderada"), (15, "severa")],
        "item_alerta": None,
        "dificultad": None,
    },
}

# Preguntas abiertas: no se corrigen, se leen. Redacción propia.
ABIERTOS = {
    "entrevista_familia": {
        "nombre": "Entrevista inicial",
        "que_mide": "para la familia",
        "consigna": "Contanos lo que sepas; si alguna pregunta no aplica, dejala en blanco.",
        "preguntas": [
            "¿Quién completa este cuestionario y qué vínculo tiene con el chico o la chica?",
            "¿Por qué consultan? ¿Qué les preocupa hoy?",
            "¿Desde cuándo lo notan? ¿Quién lo notó primero (la familia, la escuela, otro profesional)?",
            "Embarazo, parto y primeros años: ¿hubo algo para destacar (prematurez, internaciones, demoras para caminar o hablar)?",
            "¿Cómo es su salud en general? ¿Tiene algún diagnóstico, medicación o controles de vista o audición?",
            "¿A qué escuela va, en qué grado y cómo le va? ¿Repitió o cambió de escuela?",
            "¿Cómo es un día de tarea en casa? ¿Quién lo ayuda y cuánto tarda?",
            "¿Con quién vive? ¿Hubo cambios importantes en la familia últimamente?",
            "¿Qué le gusta hacer? ¿En qué se destaca?",
            "¿Hizo antes evaluaciones o tratamientos (psicopedagogía, fonoaudiología, psicología)? ¿Cuáles?",
            "¿Algo más que quieran contar?",
        ],
    },
    "docente": {
        "nombre": "Cuestionario para el docente",
        "que_mide": "para la escuela",
        "consigna": "Gracias por tomarse el tiempo. Lo que cuente queda entre usted y la profesional.",
        "preguntas": [
            "¿Qué grado o materia tiene a cargo y desde cuándo conoce al alumno o la alumna?",
            "¿Cómo es su lectura en voz alta y su comprensión de textos?",
            "¿Cómo es su escritura (letra, ortografía, armado de textos)?",
            "¿Cómo le va en matemática (cálculo, resolución de problemas)?",
            "¿Cómo es su atención y su ritmo de trabajo en clase? ¿Termina las tareas?",
            "¿Cómo se lleva con sus compañeros y con los adultos?",
            "¿Qué estrategias le dieron resultado en el aula?",
            "¿Qué es lo que más le preocupa de su aprendizaje hoy?",
            "¿Algo más que quiera agregar?",
        ],
    },
    "entrevista_adulto": {
        "nombre": "Antes de la primera sesión",
        "que_mide": "entrevista inicial",
        "consigna": "Respondé lo que quieras; lo que no, lo hablamos en la sesión.",
        "preguntas": [
            "¿Qué te trae a consulta?",
            "¿Desde cuándo te pasa?",
            "¿Hiciste terapia antes? ¿Cómo te fue?",
            "¿Tomás alguna medicación o estás en algún tratamiento médico o psiquiátrico?",
            "¿Con quién vivís y con quién contás cuando las cosas se ponen difíciles?",
            "¿Qué te gustaría lograr con la terapia?",
            "¿Algo más que quieras que sepa antes de empezar?",
        ],
    },
}

MAX_RESPUESTA = 2000
MAX_PARA = 40
DIAS_VIGENCIA = 7


def nombre_de(clave: str) -> str | None:
    inst = INSTRUMENTOS.get(clave) or ABIERTOS.get(clave)
    return inst["nombre"] if inst else None


def es_abierto(clave: str) -> bool:
    return clave in ABIERTOS


def leer_abierto(clave: str, form) -> tuple[dict | None, str | None]:
    respuestas = [(form.get(f"pregunta-{i}") or "").strip()[:MAX_RESPUESTA]
                  for i in range(len(ABIERTOS[clave]["preguntas"]))]
    if not any(respuestas):
        return None, "Respondé al menos una pregunta."
    return {"respuestas": respuestas}, None


def nuevo_token() -> str:
    return secrets.token_urlsafe(24)


def severidad(clave: str, puntaje: int) -> str:
    etiqueta = ""
    for desde, nombre in INSTRUMENTOS[clave]["cortes"]:
        if puntaje >= desde:
            etiqueta = nombre
    return etiqueta


def corregir(clave: str, form) -> tuple[dict | None, str | None]:
    """Respuestas del formulario público → puntaje, severidad y alerta."""
    inst = INSTRUMENTOS[clave]
    respuestas = []
    for i in range(len(inst["items"])):
        valor = form.get(f"item-{i}")
        if valor not in {"0", "1", "2", "3"}:
            return None, "Falta responder alguna pregunta."
        respuestas.append(int(valor))
    dificultad = None
    if inst["dificultad"]:
        dificultad = form.get("dificultad") or None
        if dificultad is not None and dificultad not in inst["dificultad"]["opciones"]:
            return None, "Esa respuesta no es válida."
    puntaje = sum(respuestas)
    alerta = inst["item_alerta"] is not None and respuestas[inst["item_alerta"]] > 0
    return {"respuestas": respuestas, "puntaje": puntaje, "severidad": severidad(clave, puntaje),
            "alerta": alerta, "dificultad": dificultad}, None


# ---------------------------------------------------------------------------
# Cuestionarios propios: la profesional carga preguntas, opciones y cómo se
# corrigen. El sistema no trae texto de tests de terceros: cada profesional
# carga su material bajo su responsabilidad.

MAX_ITEMS = 120
MAX_OPCIONES = 7
MAX_SUBESCALAS = 12
MAX_TEXTO_ITEM = 400

# SCARED (Birmaher et al., 41 ítems, 8 a 18 años): SOLO la clave de corrección,
# verificada con la hoja de puntuación de UPMC/UH Rainbow («a score of 7+ is
# positive», total «>= 25»). Las fuentes discrepan entre el ítem 25 y el 35 en
# separación; va el 25 porque las subescalas suman 41 ítems y el total es su
# suma: con el 35 el 25 quedaría huérfano y el 35 contaría dos veces.
# Los ítems van vacíos: los pega la profesional desde su copia.
# La versión para padres y la del chico tienen la misma corrección.
VERSIONES_SCARED = {"scared_chico": "Chico/a", "scared_padres": "Padres"}

MODELO_SCARED = {
    "nombre": "SCARED",
    "que_mide": "ansiedad infantil",
    "consigna": "",
    "opciones": [{"texto": "Nunca o casi nunca", "valor": 0}, {"texto": "A veces", "valor": 1},
                 {"texto": "Seguido", "valor": 2}],
    "items": [""] * 41,
    "subescalas": [
        {"nombre": "Pánico o síntomas somáticos", "items": [1, 6, 9, 12, 15, 18, 19, 22, 24, 27, 30, 34, 38], "corte": 7},
        {"nombre": "Ansiedad generalizada", "items": [5, 7, 14, 21, 23, 28, 33, 35, 37], "corte": 9},
        {"nombre": "Ansiedad de separación", "items": [4, 8, 13, 16, 20, 25, 29, 31], "corte": 5},
        {"nombre": "Ansiedad social", "items": [3, 10, 26, 32, 39, 40, 41], "corte": 8},
        {"nombre": "Evitación escolar", "items": [2, 11, 17, 36], "corte": 3},
    ],
    "corte_total": 25,
    "fuente": "SCARED: Birmaher, Khetarpal, Cully, Brent y McKenzie, Western Psychiatric Institute and Clinic, "
              "University of Pittsburgh (1995).",
}


def _entero(texto: str) -> int:
    return int(str(texto).strip())


def leer_plantilla(form, conservar_vacias: bool = False) -> tuple[dict | None, str | None]:
    """Formulario de Ajustes → plantilla limpia. Ítems: uno por renglón.
    Opciones: «texto = valor» por renglón. Subescalas: «nombre: 1, 6, 9 | corte»."""
    nombre = (form.get("nombre") or "").strip()[:60]
    if not nombre:
        return None, "El cuestionario necesita un nombre."
    items = [x.strip()[:MAX_TEXTO_ITEM] for x in (form.get("items") or "").splitlines()]
    # conservar_vacias: al importar un SCARED sin pegar, los 41 lugares vacíos cuentan.
    while items and not items[-1] and not conservar_vacias:
        items.pop()
    if not 1 <= len(items) <= MAX_ITEMS:
        return None, f"Cargá entre 1 y {MAX_ITEMS} preguntas, una por renglón."
    opciones = []
    for renglon in (form.get("opciones") or "").splitlines():
        if not renglon.strip():
            continue
        texto, _, valor = renglon.rpartition("=")
        try:
            opciones.append({"texto": texto.strip()[:60], "valor": _entero(valor)})
        except ValueError:
            return None, f"La opción «{renglon.strip()}» tiene que terminar en «= número»."
        if not opciones[-1]["texto"]:
            return None, "Cada opción necesita un texto antes del «=»."
    if not 2 <= len(opciones) <= MAX_OPCIONES:
        return None, f"Cargá entre 2 y {MAX_OPCIONES} opciones de respuesta."
    subescalas = []
    for renglon in (form.get("subescalas") or "").splitlines():
        if not renglon.strip():
            continue
        try:
            nom, _, resto = renglon.partition(":")
            numeros, _, corte = resto.partition("|")
            lista = [_entero(n) for n in numeros.replace(";", ",").split(",") if n.strip()]
            sub = {"nombre": nom.strip()[:60], "items": lista, "corte": _entero(corte) if corte.strip() else None}
        except ValueError:
            return None, f"No entiendo la subescala «{renglon.strip()}». Usá «Nombre: 1, 6, 9 | 7»."
        if not sub["nombre"] or not sub["items"]:
            return None, f"No entiendo la subescala «{renglon.strip()}». Usá «Nombre: 1, 6, 9 | 7»."
        if any(n < 1 for n in sub["items"]):
            return None, f"En «{sub['nombre']}» las preguntas se numeran desde 1."
        if max(sub["items"]) > len(items):
            return None, (f"Hay {len(items)} preguntas, pero «{sub['nombre']}» usa la {max(sub['items'])}. "
                          "¿Faltan renglones al pegar las preguntas?")
        subescalas.append(sub)
    if len(subescalas) > MAX_SUBESCALAS:
        return None, f"Hasta {MAX_SUBESCALAS} subescalas."
    corte_txt = (form.get("corte_total") or "").strip()
    try:
        corte_total = _entero(corte_txt) if corte_txt else None
    except ValueError:
        return None, "El corte del total tiene que ser un número."
    return {"nombre": nombre, "version": (form.get("version") or "").strip()[:30],
            "que_mide": (form.get("que_mide") or "").strip()[:60],
            "consigna": (form.get("consigna") or "").strip()[:600], "opciones": opciones, "items": items,
            "subescalas": subescalas, "corte_total": corte_total,
            "fuente": (form.get("fuente") or "").strip()[:400]}, None


def completa(plantilla: dict) -> bool:
    return all(x.strip() for x in plantilla["items"])


def corregir_propio(plantilla: dict, form) -> tuple[dict | None, str | None]:
    valores = []
    for i in range(len(plantilla["items"])):
        try:
            idx = int(form.get(f"item-{i}") or "")
            valores.append(plantilla["opciones"][idx]["valor"])
        except (ValueError, IndexError):
            return None, "Falta responder alguna pregunta."
    total = sum(valores)
    subescalas = []
    for s in plantilla["subescalas"]:
        puntaje = sum(valores[n - 1] for n in s["items"])
        subescalas.append({"nombre": s["nombre"], "puntaje": puntaje, "corte": s["corte"],
                           "sobre_corte": s["corte"] is not None and puntaje >= s["corte"]})
    corte = plantilla.get("corte_total")
    if corte is not None:
        severidad = "sobre el corte" if total >= corte else "debajo del corte"
    else:
        severidad = ""
    return {"respuestas": valores, "puntaje": total, "severidad": severidad, "alerta": False,
            "detalle": subescalas}, None


def definicion(fila: dict) -> dict | None:
    """Lo necesario para mostrar y corregir una fila de `cuestionarios`, sea
    de fábrica, abierta o propia (la propia viaja como copia en la fila)."""
    clave = fila["instrumento"]
    if clave == "propio" and fila.get("plantilla"):
        p = fila["plantilla"]
        maximo = max(o["valor"] for o in p["opciones"]) * len(p["items"])
        nombre = p["nombre"] + (f" · {p['version']}" if p.get("version") else "")
        return {"tipo": "propio", "nombre": nombre, "que_mide": p.get("que_mide") or "propio",
                "consigna": p.get("consigna") or "", "items": p["items"],
                "opciones": [o["texto"] for o in p["opciones"]], "dificultad": None,
                "fuente": p.get("fuente") or None, "maximo": maximo}
    if clave in INSTRUMENTOS:
        i = INSTRUMENTOS[clave]
        return {**i, "tipo": "puntaje", "opciones": OPCIONES, "fuente": FUENTE, "maximo": 3 * len(i["items"])}
    if clave in ABIERTOS:
        return {**ABIERTOS[clave], "tipo": "abierto", "fuente": None}
    return None


FORMATO = "fibot-cuestionario"
MAX_IMPORTAR = 200_000


def a_formulario(c: dict) -> dict:
    """Plantilla → campos del formulario de Ajustes (y de la importación)."""
    return {
        "nombre": c.get("nombre") or "", "version": c.get("version") or "", "que_mide": c.get("que_mide") or "",
        "consigna": c.get("consigna") or "", "fuente": c.get("fuente") or "",
        "items": "\n".join(c.get("items") or []),
        "opciones": "\n".join(f"{o['texto']} = {o['valor']}" for o in c.get("opciones") or []),
        "subescalas": "\n".join(f"{x['nombre']}: {', '.join(map(str, x['items']))}"
                                 + (f" | {x['corte']}" if x.get("corte") is not None else "")
                                 for x in c.get("subescalas") or []),
        "corte_total": "" if c.get("corte_total") is None else str(c["corte_total"]),
    }


def exportar(c: dict) -> dict:
    return {"formato": FORMATO, "version_formato": 1,
            **{k: c.get(k) for k in ("nombre", "version", "que_mide", "consigna", "opciones", "items",
                                     "subescalas", "corte_total", "fuente")}}


def importar(crudo: bytes) -> tuple[dict | None, str | None]:
    """Archivo exportado por otra profesional → plantilla validada con las
    mismas reglas que el formulario (nunca se guarda el JSON tal cual)."""
    import json
    if len(crudo) > MAX_IMPORTAR:
        return None, "El archivo es demasiado grande."
    try:
        datos = json.loads(crudo.decode("utf-8"))
        if not isinstance(datos, dict) or datos.get("formato") != FORMATO:
            raise ValueError
        # Los ítems vacíos (como el SCARED sin pegar) se conservan en su lugar.
        items = [str(x).replace("\n", " ") for x in datos.get("items") or []]
        form = a_formulario({**datos, "items": items})
        # Un renglón vacío al final se pierde al partir el texto: el espacio lo
        # conserva y leer_plantilla lo vuelve a dejar vacío.
        form["items"] = "\n".join(x.strip() or " " for x in items)
    except (ValueError, UnicodeDecodeError, TypeError, KeyError, AttributeError):
        return None, "Ese archivo no es un cuestionario exportado de FiBOT."
    return leer_plantilla(form, conservar_vacias=True)


def comparar_versiones(grupos: list[dict]) -> list[dict]:
    """Versiones de un mismo cuestionario propio (p. ej. SCARED de padres y
    del chico): la última respuesta de cada una, lado a lado por subescala.
    grupos: los de la ficha, cada uno con su «base», «version» y respuestas
    de la más nueva a la más vieja."""
    por_base: dict[str, list[dict]] = {}
    for g in grupos:
        if g.get("base") and g.get("version"):
            por_base.setdefault(g["base"].casefold(), []).append(g)
    comparaciones = []
    for gs in por_base.values():
        if len(gs) < 2:
            continue
        gs = sorted(gs, key=lambda g: g["version"].casefold())
        ultimas = [g["respuestas"][0] for g in gs]
        anteriores = [g["respuestas"][1] if len(g["respuestas"]) > 1 else None for g in gs]
        nombres = []
        for r in ultimas:
            for d in r.get("detalle") or []:
                if d["nombre"] not in nombres:
                    nombres.append(d["nombre"])
        filas = []
        for nombre in nombres:
            celdas = [next((dict(d) for d in r.get("detalle") or [] if d["nombre"] == nombre), None) for r in ultimas]
            for celda, ant in zip(celdas, anteriores):
                previa = next((d for d in (ant or {}).get("detalle") or [] if d["nombre"] == nombre), None)
                if celda and previa:
                    celda["antes"] = previa["puntaje"]
            filas.append({"nombre": nombre, "celdas": celdas,
                          "corte": next((c["corte"] for c in celdas if c and c["corte"] is not None), None),
                          "difieren": len({bool(c and c["sobre_corte"]) for c in celdas}) > 1})
        filas.append({"nombre": "Total", "celdas": [{"puntaje": r["puntaje"], "sobre_corte": r.get("severidad") == "sobre el corte",
                                                     **({"antes": a["puntaje"]} if a else {})}
                                                    for r, a in zip(ultimas, anteriores)],
                      "corte": None, "difieren": len({r.get("severidad") for r in ultimas}) > 1})
        comparaciones.append({"nombre": gs[0]["base"], "versiones": [g["version"] for g in gs],
                              "fechas": [r["_fecha"] for r in ultimas], "filas": filas,
                              "fechas_antes": [a["_fecha"] if a else None for a in anteriores]})
    return comparaciones


def familias(plantillas: list[dict]) -> dict[str, tuple[str, list[str]]]:
    """Cuestionarios propios con dos o más versiones completas, para mandar
    todos los links en un paso: {nombre en minúsculas: (nombre, [versiones])}."""
    grupos: dict[str, tuple[str, list[str]]] = {}
    for p in plantillas:
        c = p["contenido"]
        if completa(c):
            nombre, vs = grupos.setdefault(c["nombre"].casefold(), (c["nombre"], []))
            vs.append(c.get("version") or "sin versión")
    return {k: v for k, v in grupos.items() if len(v[1]) >= 2}


def para_biblioteca(c: dict, con_preguntas: bool) -> dict:
    """Copia para compartir. Sin preguntas viajan los lugares vacíos y la
    corrección, como el modelo SCARED: quien lo agrega pega su texto."""
    copia = {k: c.get(k) for k in ("nombre", "version", "que_mide", "consigna", "opciones", "items",
                                   "subescalas", "corte_total", "fuente")}
    if not con_preguntas:
        copia["items"] = [""] * len(c["items"])
        copia["consigna"] = ""
    return copia
