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
