"""Cada escena de «Recorrido por tu panel» (templates/ayuda.html). Ver servidor.py."""
import os, sys, time, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import Navegador, entrar, marcar
SAL = sys.argv[1]
SOLO = sys.argv[2:]
os.makedirs(SAL, exist_ok=True)


def panel(nav):
    nav.ir("/agenda", 1.5)
    nav.pausa(1.6)
    nav.scroll_a("#hoy, .hoy-sesion", extra=-90, pasos=5)
    nav.pausa(1.4)
    nav.tocar(marcar(nav, ".hoy-sesion, .ficha", "", 0), espera=1.2)
    nav.pausa(1.5)


def calendario(nav):
    nav.ir("/agenda", 1.5)
    nav.scroll_a(".tablero", extra=-20, pasos=5)
    nav.pausa(1.5)
    nav.tocar(marcar(nav, "a.cal-ir", "Tomás"), espera=1.8)
    nav.pausa(1.2)
    nav.tocar(marcar(nav, "summary", "Acciones"), espera=0.8)
    nav.pausa(1.8)


def ficha(nav):
    nav.ir("/agenda", 1.5)
    nav.tocar(marcar(nav, ".hoy-sesion, .ficha", "", 0), espera=1.8)
    nav.pausa(1.2)
    for pestana in ("Sesiones", "Crisis", "Personas", "Datos"):
        try:
            nav.tocar(marcar(nav, "dialog[open] [role=tab], dialog[open] .pestania, dialog[open] button", pestana), espera=0.9)
        except RuntimeError:
            pass
    nav.pausa(1.2)


def mensajes_pagos(nav):
    nav.ir("/agenda", 1.5)
    nav.scroll_a("#para-resolver", pasos=6)
    nav.pausa(2.2)
    nav.tocar("#bt-pagos-revisar", espera=0.8)
    nav.pausa(2.2)
    nav.tocar("#bt-reservas-nuevas", espera=0.8)
    nav.pausa(1.6)


def pestania(p, id_, *subs):
    """Una pestaña de /ajustes, bajando hasta la tarjeta y mostrándola."""
    def escena(nav):
        nav.ir(f"/ajustes?p={p}", 1.5)
        nav.pausa(1.2)
        nav.scroll_a(f"#{id_}", pasos=6)
        nav.pausa(1.4)
        for s in subs:
            try:
                nav.tocar(marcar(nav, f"#{id_} summary", s), espera=0.8)
                nav.pausa(1.2)
            except RuntimeError:
                traceback.print_exc()
        nav.scroll_hasta(nav.js(f"document.querySelector('#{id_}').getBoundingClientRect().top+scrollY+300"), 4)
        nav.pausa(1.6)
    return escena


def ajustes(nav):
    nav.ir("/ajustes", 1.5)
    nav.pausa(1.4)
    for p in ("pagina", "cobros", "cancelaciones", "avisos"):
        nav.tocar(f"#tab-{p}", espera=0.7)
        nav.pausa(1.0)
    nav.tocar("#tab-cancelaciones", espera=0.6)
    nav.js("(()=>{const s=document.querySelector('select[name=al_cancelar]');s.value='preguntar';s.dispatchEvent(new Event('change',{bubbles:true}))})()")
    nav.pausa(1.8)
    nav.tocar("#guardar-todo", espera=1.6)
    nav.pausa(1.8)


def cobros(nav):
    nav.ir("/ajustes?p=cobros", 1.5)
    nav.pausa(1.4)
    for nombre, valor in (("cobro_web", "no"), ("cobro_web", "ambos"), ("cobro_modo", "sena")):
        nav.js(f"(()=>{{const s=document.querySelector('select[name={nombre}]');s.value='{valor}';s.dispatchEvent(new Event('change',{{bubbles:true}}))}})()")
        nav.pausa(1.5)
    nav.tocar("#tab-cancelaciones", espera=0.7)
    nav.pausa(1.8)


def buscador(nav):
    nav.ir("/agenda", 1.5)
    nav.pausa(1.0)
    nav.tocar("[data-buscar-abrir]", espera=0.6)
    for texto in ("cam", "camila"):
        nav.js(f"(()=>{{const q=document.getElementById('buscador-q');q.value='{texto}';q.dispatchEvent(new Event('input'))}})()")
        nav.pausa(0.5)
    nav.pausa(1.2)
    for texto in ("pre", "precio"):
        nav.js(f"(()=>{{const q=document.getElementById('buscador-q');q.value='{texto}';q.dispatchEvent(new Event('input'))}})()")
        nav.pausa(0.5)
    nav.pausa(1.2)
    for texto in ("vac", "vacaciones"):
        nav.js(f"(()=>{{const q=document.getElementById('buscador-q');q.value='{texto}';q.dispatchEvent(new Event('input'))}})()")
        nav.pausa(0.5)
    nav.pausa(1.0)
    nav.js("document.getElementById('buscador-q').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))")
    time.sleep(1.6)
    nav.pausa(2.0)


def seccion(id_, *subs):
    def escena(nav):
        nav.ir("/agenda", 1.5)
        nav.scroll_a(f"#{id_}", pasos=6)
        nav.tocar(f"#{id_} > summary", espera=0.8)
        nav.pausa(1.4)
        for s in subs:
            try:
                nav.tocar(marcar(nav, f"#{id_} summary", s), espera=0.8)
                nav.pausa(1.2)
            except RuntimeError:
                traceback.print_exc()
        nav.scroll_hasta(nav.js(f"document.querySelector('#{id_}').getBoundingClientRect().top+scrollY+260"), 4)
        nav.pausa(1.6)
    return escena


def pagina_paciente(nav):
    nav.cdp("Network.clearBrowserCookies")
    nav.ir("/laura-demo", 2)
    nav.pausa(1.8)
    nav.tocar(marcar(nav, "a", "Sesión · 45"), espera=1.6)
    nav.tocar(marcar(nav, "a", "›"), espera=1.6)
    nav.tocar(marcar(nav, "button", "Mar"), espera=0.8)
    nav.tocar(marcar(nav, "a[href*='/reservar/']", ""), espera=1.2)
    nav.pausa(0.8)
    nav.escribir("form:not([hidden]) input[name=nombre]", "Pablo Ejemplo")
    nav.escribir("form:not([hidden]) input[name=telefono]", "54 9 11 5555 0000")
    nav.tocar(marcar(nav, "button", "Reservar"), espera=1.5)
    nav.pausa(2.5)


ESCENAS = {"panel": panel, "calendario": calendario, "ficha": ficha, "mensajes-pagos": mensajes_pagos,
           "horario": pestania("agenda", "horario"), "mi-pagina": pestania("pagina", "mi-pagina", "Código QR"),
           "cobros": cobros, "ajustes": ajustes, "buscador": buscador, "espera": seccion("espera"),
           "bloqueos": seccion("bloqueos"), "pagina-paciente": pagina_paciente}
for nombre, escena in ESCENAS.items():
    if SOLO and nombre not in SOLO:
        continue
    nav = Navegador(ancho=412, alto=800, movil=True)
    try:
        if nombre != "pagina-paciente":
            entrar(nav)
        escena(nav)
        nav.guardar(f"{SAL}/{nombre}.gif")
        nav.cuadros = []
        print(nombre, os.path.getsize(f"{SAL}/{nombre}.gif") // 1024, "KB")
    except Exception:
        print("FALLÓ", nombre)
        traceback.print_exc()
    finally:
        nav.cerrar()
