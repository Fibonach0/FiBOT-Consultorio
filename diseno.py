"""Diseño propio de la página de cada profesional (`/<slug>`).

Dos niveles:
1. Estilo: un JSON chico (colores, letras, bordes, foto, portada) que se
   convierte en variables de CSS. Se revisa el contraste para que se pueda leer.
2. CSS propio: lo que la profesional (o su IA) escriba, sólo para su página.
   Se revisa antes de guardar: nada que cargue cosas de otros sitios (url(),
   @import, image-set…), nada de HTML (<) ni escapes (\\), tamaño máximo.
   Encima, la página va con una Content-Security-Policy que bloquea pedidos a
   otros sitios, por si algo se escapa.

Nunca hay HTML ni JavaScript propio: la página vive en el mismo dominio que el
panel, y un script podría leer datos de pacientes o lo que escribe quien reserva.
"""
import json
import re

CSS_MAX = 20000

FUENTES = {
    "Newsreader": "serif", "Lora": "serif", "Playfair Display": "serif", "Merriweather": "serif",
    "Fraunces": "serif", "Libre Baskerville": "serif",
    "Hanken Grotesk": "sans-serif", "DM Sans": "sans-serif", "Nunito": "sans-serif", "Work Sans": "sans-serif",
    "Inter": "sans-serif", "Poppins": "sans-serif", "Quicksand": "sans-serif", "Montserrat": "sans-serif",
}
# Las que ya carga _sitio.html.
FUENTES_DE_BASE = {"Newsreader", "Hanken Grotesk"}
BORDES = {"rectos": (3, 3), "suaves": (10, 12), "redondos": (999, 22)}  # (botones, cajas)
FOTOS = ("rectangulo", "circulo", "cuadrada")
PORTADAS = {"baja": "6/1", "normal": "4/1", "alta": "5/2", "sin": None}
COLORES = ("principal", "acento", "fondo", "superficie", "texto")
# Módulos de la página: las secciones de abajo se pueden reordenar u ocultar, y
# el calendario puede ir a la izquierda o a la derecha (y primero en el celular).
MODULOS = {"sobre": "Sobre mí", "temas": "Temas que trabajo", "formacion": "Formación y obras sociales",
           "mas": "Un poco más de mí", "preguntas": "Preguntas frecuentes", "donde": "Dónde atiendo"}
LADOS = ("derecha", "izquierda")
EN_CELULAR = ("perfil_primero", "calendario_primero")

ORIGINAL = {"principal": "#2f3a2c", "acento": "#9a5c3e", "fondo": "#f4efe6", "superficie": "#fbf8f2",
            "texto": "#2a2620", "letra_titulos": "Newsreader", "letra_texto": "Hanken Grotesk",
            "bordes": "redondos", "foto": "rectangulo", "portada": "normal",
            "calendario": "derecha", "en_celular": "perfil_primero", "orden": list(MODULOS), "ocultar": []}

# Las partes de la página, con el nombre que puede usar el CSS. Es un contrato:
# si se cambia el HTML de /<slug>, estas clases tienen que seguir existiendo
# (test_seguimiento lo revisa).
PARTES = [
    (".s-barra", "la barra de arriba, con tu inicial y tu nombre (.s-marca, .s-marca .logo)"),
    (".pf-portada", "la imagen ancha de arriba"),
    (".pf-cabeza", "tu foto (.pf-foto), tu nombre (h1), título (.s-rotulo), frase (.pf-frase) y dónde atendés (.pf-datos)"),
    (".pf-secciones", "las secciones de abajo; cada una es un <section data-modulo=\"…\"> con su <h2>"),
    (".pf-sobre", "el texto de «Sobre mí»"),
    (".pf-temas", "los temas que trabajás (cada uno es un <li>)"),
    (".pf-dos", "formación (.pf-renglones) y obras sociales (.pf-renglon)"),
    (".pf-mas", "«Un poco más de mí»"),
    (".pf-preguntas", "las preguntas frecuentes (cada una es un <details> con <summary>)"),
    (".pf-lado", "la columna de la derecha con el calendario"),
    (".pf-caja", "la caja del calendario y la reserva"),
    (".pf-dias", "los días de la semana (cada uno es un <button>; el elegido tiene aria-pressed=\"true\")"),
    (".pf-horas", "los horarios libres del día (cada uno es un <a>)"),
    (".pf-form", "el formulario de reserva (.s-campo son los campos, .s-boton el botón)"),
    (".pf-espera", "«Anotate en la lista de espera»"),
    (".s-pie", "el pie con los teléfonos de urgencia (no se puede ocultar)"),
]

_HEX = re.compile(r"#(?:[0-9a-fA-F]{3}){1,2}")
_PROHIBIDO = ("url(", "@import", "image-set(", "image(", "cross-fade(", "element(", "src(", "expression(",
              "javascript:", "behavior:", "-moz-binding", "@font-face", "@namespace", "@charset")
_ARROBAS = {"media", "supports", "keyframes", "-webkit-keyframes", "container"}


# --- Colores ---------------------------------------------------------------

def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def _luz(h: str) -> float:
    def canal(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(h)
    return 0.2126 * canal(r) + 0.7152 * canal(g) + 0.0722 * canal(b)


def contraste(a: str, b: str) -> float:
    la, lb = sorted((_luz(a), _luz(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def mezcla(a: str, b: str, p: float) -> str:
    """p de `a` y el resto de `b`."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x * p + y * (1 - p) for x, y in zip(ra, rb)))


def _sobre(color: str) -> str:
    """Texto que se lee sobre un fondo de ese color."""
    return "#ffffff" if contraste(color, "#ffffff") >= contraste(color, "#111111") else "#111111"


def _suave(texto: str, fondo: str) -> str:
    """El gris de los textos secundarios: lo más claro que siga leyéndose."""
    for p in (0.68, 0.74, 0.8, 0.86, 0.92, 1.0):
        c = mezcla(texto, fondo, p)
        if contraste(c, fondo) >= 4.5:
            return c
    return texto


# --- Leer lo que manda la profesional ----------------------------------------

def leer(dato) -> tuple[dict, list[str]]:
    """Valida el JSON de estilo. Devuelve sólo las claves válidas y los
    problemas en castellano. Las claves que no vienen quedan como el original."""
    if not isinstance(dato, dict):
        return {}, ["El estilo tiene que ser un objeto JSON, entre llaves { }."]
    d, errores = {}, []
    for k in COLORES:
        v = dato.get(k)
        if v in (None, ""):
            continue
        if not isinstance(v, str) or not _HEX.fullmatch(v.strip()):
            errores.append(f"«{k}» tiene que ser un color como #2f5233.")
            continue
        d[k] = _hex(_rgb(v.strip())).lower()
    for k in ("letra_titulos", "letra_texto"):
        v = dato.get(k)
        if v in (None, ""):
            continue
        nombre = next((f for f in FUENTES if isinstance(v, str) and f.lower() == v.strip().lower()), None)
        if not nombre:
            errores.append(f"«{k}»: «{v}» no está entre las letras disponibles ({', '.join(FUENTES)}).")
        else:
            d[k] = nombre
    for k, opciones in (("bordes", BORDES), ("foto", FOTOS), ("portada", PORTADAS),
                        ("calendario", LADOS), ("en_celular", EN_CELULAR)):
        v = dato.get(k)
        if v in (None, ""):
            continue
        if not isinstance(v, str) or v.strip().lower() not in opciones:
            errores.append(f"«{k}» tiene que ser {', '.join(opciones)}.")
        else:
            d[k] = v.strip().lower()
    for k in ("orden", "ocultar"):
        v = dato.get(k)
        if v in (None, ""):
            continue
        if not isinstance(v, list) or any(not isinstance(x, str) or x not in MODULOS for x in v):
            errores.append(f"«{k}» tiene que ser una lista con algunos de: {', '.join(MODULOS)}.")
            continue
        lista = list(dict.fromkeys(v))
        if k == "orden":
            lista += [m for m in MODULOS if m not in lista]
        d[k] = lista
    c = {**ORIGINAL, **d}
    if any(k in d for k in COLORES):
        for frente, fondo, minimo, que in (("texto", "fondo", 4.5, "El texto sobre el fondo"),
                                           ("texto", "superficie", 4.5, "El texto sobre las cajas"),
                                           ("principal", "fondo", 3, "El color principal sobre el fondo"),
                                           ("acento", "fondo", 3, "El acento sobre el fondo")):
            r = contraste(c[frente], c[fondo])
            if r < minimo:
                errores.append(f"{que} casi no se lee (contraste {r:.1f}, hace falta {minimo}). Probá un color más oscuro o más claro.")
    return d, errores


def limpiar_css(css: str) -> tuple[str, list[str], list[str]]:
    """(css limpio, errores, avisos). Con errores no se guarda nada."""
    css = (css or "").replace("\r\n", "\n").strip()
    errores, avisos = [], []
    if not css:
        return "", [], []
    if len(css) > CSS_MAX:
        errores.append(f"El CSS es muy largo: hasta {CSS_MAX // 1000} mil caracteres.")
    if "<" in css:
        errores.append("El CSS no puede tener «<»: nada de HTML ni de <style>. Pegá sólo las reglas.")
    if "\\" in css:
        errores.append("El CSS no puede tener barras invertidas (\\).")
    sin_comentarios = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    if "/*" in sin_comentarios:
        errores.append("Hay un comentario /* sin cerrar.")
    bajo = sin_comentarios.lower()
    for p in _PROHIBIDO:
        if p in bajo:
            errores.append(f"No se puede usar «{p}»: tu página no carga nada de otros sitios (imágenes y letras van por los ajustes).")
    for a in sorted(set(re.findall(r"@([a-z-]+)", bajo)) - _ARROBAS - {"import", "font-face", "namespace", "charset"}):
        errores.append(f"No se puede usar «@{a}». Sí @media, @supports, @keyframes y @container.")
    nivel = 0
    for ch in sin_comentarios:
        nivel += (ch == "{") - (ch == "}")
        if nivel < 0:
            break
    if nivel != 0:
        errores.append("Las llaves { } no cierran bien.")
    limpio = re.sub(r"\s*!\s*important", "", sin_comentarios, flags=re.I)
    if limpio != sin_comentarios:
        avisos.append("Saqué los «!important»: no hacen falta, tu CSS va después del de la página.")
    return limpio.strip(), errores, avisos


def extraer(respuesta: str) -> tuple[dict | None, str | None, list[str]]:
    """De la respuesta de la IA, el JSON de estilo y el CSS. Acepta bloques
    ```json / ```css, o sólo un JSON, o sólo CSS."""
    texto = (respuesta or "").strip()
    estilo, css, errores = None, None, []
    bloques = re.findall(r"```([a-zA-Z]*)\s*\n(.*?)```", texto, flags=re.S)
    if bloques:
        for tipo, cuerpo in bloques:
            tipo = tipo.lower()
            if tipo == "json" or (not tipo and cuerpo.strip().startswith("{") and estilo is None and _json(cuerpo) is not None):
                estilo = _json(cuerpo)
                if estilo is None:
                    errores.append("El bloque JSON no se puede leer: revisá comas y comillas.")
            elif tipo in ("css", ""):
                css = ((css + "\n") if css else "") + cuerpo.strip()
        return estilo, css, errores
    if texto.startswith("{") and _json(texto) is not None:
        return _json(texto), None, []
    if "{" in texto and ":" in texto:
        return None, texto, []
    return None, None, ["No encontré ni el estilo (JSON) ni CSS en lo que pegaste."]


def _json(t: str):
    try:
        return json.loads(t)
    except ValueError:
        return None


# --- Lo que va en la página --------------------------------------------------

def css_de_estilo(d: dict) -> str:
    if not d:
        return ""
    c = {**ORIGINAL, **d}
    reglas = []
    if any(k in d for k in COLORES):
        fondo, texto = c["fondo"], c["texto"]
        v = {
            "--cream": fondo, "--card": c["superficie"], "--paper": c["superficie"],
            "--ink": texto, "--s-texto": mezcla(texto, fondo, .88) if contraste(mezcla(texto, fondo, .88), fondo) >= 4.5 else texto,
            "--muted": _suave(texto, fondo),
            "--rule": mezcla(texto, fondo, .14), "--s-linea-2": mezcla(texto, fondo, .22),
            "--hover": mezcla(texto, fondo, .05), "--chip-bg": mezcla(texto, fondo, .07),
            "--s-arena": mezcla(texto, fondo, .08), "--s-arena-2": mezcla(texto, fondo, .12),
            "--green": c["principal"], "--green-solid": c["principal"], "--on-green": _sobre(c["principal"]),
            "--green-bg": mezcla(c["principal"], c["superficie"], .12), "--green-mid": mezcla(c["principal"], fondo, .6),
            "--s-salvia": mezcla(c["principal"], fondo, .13), "--s-salvia-texto": texto,
            "--s-acento": c["acento"], "--s-rosa": mezcla(c["acento"], fondo, .15), "--s-rosa-texto": texto,
            "--s-sombra": "rgba(0,0,0,.35)",
        }
        esquema = "light" if _luz(fondo) > 0.4 else "dark"
        cuerpo = "; ".join(f"{k}:{x}" for k, x in v.items())
        # Pesa más que :root:root[data-modo="oscuro"] de _sitio.html: los
        # colores elegidos valen en claro y en oscuro.
        reglas.append(f":root:root:root:root {{ {cuerpo}; color-scheme:{esquema}; }}")
    if "letra_titulos" in d or "letra_texto" in d:
        t, x = c["letra_titulos"], c["letra_texto"]
        reglas.append(f":root:root:root:root {{ --serif:'{t}', {'Georgia, serif' if FUENTES[t] == 'serif' else 'system-ui, sans-serif'};"
                      f" --sans:'{x}', {'Georgia, serif' if FUENTES[x] == 'serif' else 'system-ui, -apple-system, sans-serif'}; }}")
    if "bordes" in d:
        b, k = BORDES[c["bordes"]]
        chico = min(k, 10)
        reglas.append(f".s-boton, main .btn, .s-seg, .s-seg a, .s-seg button, .pf-modo .s-seg label, .pf-temas li, .s-chip, .s-etiqueta {{ border-radius:{b}px; }}")
        reglas.append(f".pf-caja, .pf-espera, .pf-portada, .pf-mas, .pf-cuenta div {{ border-radius:{k}px; }}")
        reglas.append(f".s-campo, .pf-tipos a, .pf-dias button, .pf-horas a, .pf-mes .dia, .pf-aviso, .s-marca .logo {{ border-radius:{chico}px; }}")
    if "foto" in d:
        forma = {"rectangulo": ("4/5", "10px"), "circulo": ("1/1", "50%"), "cuadrada": ("1/1", "4px")}[c["foto"]]
        reglas.append(f".pf-foto {{ aspect-ratio:{forma[0]}; border-radius:{forma[1]}; }}")
    if "portada" in d:
        p = PORTADAS[c["portada"]]
        reglas.append(".pf-portada { display:none; }" if p is None else f".pf-portada {{ aspect-ratio:{p}; }}")
    if d.get("calendario") == "izquierda":
        reglas.append(".pf { flex-direction:row-reverse; }")
    if d.get("en_celular") == "calendario_primero":
        reglas.append("@media (max-width:980px) { .pf-lado { order:-1; } }")
    if "orden" in d:
        reglas.append(" ".join(f'.pf-secciones [data-modulo="{m}"] {{ order:{i}; }}' for i, m in enumerate(d["orden"])))
    if d.get("ocultar"):
        reglas.append(", ".join(f'.pf-secciones [data-modulo="{m}"]' for m in d["ocultar"]) + " { display:none; }")
    return "\n".join(reglas)


def fuentes_url(d: dict) -> str | None:
    """Link de Google Fonts para las letras que no carga ya el sitio."""
    faltan = sorted({d.get("letra_titulos"), d.get("letra_texto")} - {None} - FUENTES_DE_BASE)
    if not faltan:
        return None
    familias = "&".join("family=" + f.replace(" ", "+") + ":wght@400;500;600;700" for f in faltan)
    return f"https://fonts.googleapis.com/css2?{familias}&display=swap"


# Va DESPUÉS del CSS propio: el pie con los teléfonos de urgencia y la caja de
# reserva siempre se ven (el CSS propio no puede usar !important).
CSS_FIJO = """
footer.s-pie { display:flex !important; visibility:visible !important; opacity:1 !important; position:static !important;
  transform:none !important; clip-path:none !important; filter:none !important; height:auto !important; max-height:none !important;
  overflow:visible !important; font-size:14px !important; background:Canvas !important; color:CanvasText !important; }
footer.s-pie * { visibility:visible !important; opacity:1 !important; font-size:14px !important; color:CanvasText !important; }
footer.s-pie a { color:LinkText !important; text-decoration:underline !important; }
#horarios, #reserva-caja { visibility:visible !important; opacity:1 !important; }
#horarios { display:block !important; }
"""

# Para quien lleva diseño propio: la página no puede pedir nada a otros sitios
# (salvo las letras de Google Fonts, que ya usa el sitio).
CSP = ("default-src 'self'; img-src 'self' data: blob:; font-src 'self' https://fonts.gstatic.com; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; script-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'self'; form-action 'self'")


def instrucciones(tenant: dict, link: str) -> str:
    """El texto para pegarle a ChatGPT, Claude o la IA que use. Sin datos de
    pacientes: sólo cómo es la página."""
    partes = "\n".join(f"- {sel}: {que}" for sel, que in PARTES)
    quien = tenant.get("titulo") or (tenant.get("rubro") or "profesional de la salud")
    return f"""Quiero que diseñes el estilo de mi página de turnos online. Soy {quien}{(' en ' + tenant['ubicacion']) if tenant.get('ubicacion') else ''}. Mi página es {link} (mirala si podés).

[Contale acá a la IA cómo te imaginás tu página: colores, sensación, qué tipo de pacientes atendés.]

Devolveme DOS bloques de código y nada más:

1) Un bloque ```json con el estilo general. Todas las claves son opcionales:
{{
  "principal": "#2f3a2c",     (botones y links; color hex)
  "acento": "#9a5c3e",        (detalles y la frase destacada)
  "fondo": "#f4efe6",         (fondo de la página)
  "superficie": "#fbf8f2",    (fondo de las cajas)
  "texto": "#2a2620",         (color del texto)
  "letra_titulos": "Newsreader",
  "letra_texto": "Hanken Grotesk",
  "bordes": "redondos",       (rectos, suaves o redondos)
  "foto": "rectangulo",       (rectangulo, circulo o cuadrada)
  "portada": "normal",        (baja, normal, alta o sin)
  "calendario": "derecha",    (derecha o izquierda: de qué lado va el calendario en la compu)
  "en_celular": "perfil_primero",  (perfil_primero o calendario_primero)
  "orden": ["sobre", "temas", "formacion", "mas", "preguntas", "donde"],  (orden de las secciones)
  "ocultar": []               (secciones que no querés mostrar)
}}
Secciones: {', '.join(f'{k} = {v}' for k, v in MODULOS.items())}.
Letras disponibles: {', '.join(FUENTES)}.
El texto tiene que leerse bien sobre el fondo y sobre las cajas (contraste 4.5 o más).

2) Un bloque ```css con ajustes finos (opcional). Reglas:
- Sólo CSS: nada de HTML, <style> ni JavaScript.
- Sin url(), @import, @font-face ni image-set(): la página no puede cargar nada de otros sitios. Las imágenes son mi foto y mi portada, que ya están.
- Sin barras invertidas (\\) ni !important.
- No cambies el texto de la página ni ocultes el pie con los teléfonos de urgencia ni la caja de reserva.
- Que se vea bien en el celular (la mayoría entra desde ahí).
- Podés usar estas variables: --green-solid (principal), --s-acento, --cream (fondo), --card (cajas), --ink (texto), --muted, --rule (líneas), --serif y --sans (letras).

Partes de la página que podés tocar (son clases de CSS):
{partes}
"""
