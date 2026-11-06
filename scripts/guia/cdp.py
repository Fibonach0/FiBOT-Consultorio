"""Chrome sin ventana por DevTools: navegar, tocar, desplazar y sacar cuadros para los GIF de la guía."""
import base64, io, json, subprocess, tempfile, time, urllib.request
import websocket
from PIL import Image

B = "http://127.0.0.1:8770"


class Navegador:
    def __init__(self, ancho=1100, alto=720, movil=False):
        self.perfil = tempfile.mkdtemp()
        self.proc = subprocess.Popen(["google-chrome", "--headless=new", "--remote-debugging-port=9344", f"--user-data-dir={self.perfil}",
                                      "--no-first-run", "--disable-gpu", "--hide-scrollbars", "--remote-allow-origins=http://127.0.0.1:9344",
                                      "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                pest = json.load(urllib.request.urlopen("http://127.0.0.1:9344/json"))
                break
            except Exception:
                time.sleep(0.2)
        self.ws = websocket.create_connection(next(p for p in pest if p["type"] == "page")["webSocketDebuggerUrl"], origin="http://127.0.0.1:9344")
        self.n = 0
        self.cdp("Page.enable"); self.cdp("Runtime.enable")
        self.cdp("Emulation.setDeviceMetricsOverride", width=ancho, height=alto, deviceScaleFactor=1, mobile=movil)
        self.cdp("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "light"}])
        self.cuadros = []

    def cdp(self, metodo, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": metodo, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.n:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result", {})

    def js(self, expr):
        r = self.cdp("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"])
        return r.get("result", {}).get("value")

    def ir(self, url, espera=1.2):
        self.cdp("Page.navigate", url=url if url.startswith("http") else B + url)
        time.sleep(espera)
        self._cursor()

    def _cursor(self):
        # Un puntero visible para que se entienda qué se toca.
        self.js("""(()=>{if(document.getElementById('_cur'))return;const c=document.createElement('div');c.id='_cur';
          c.style.cssText='position:fixed;z-index:99999;width:26px;height:26px;border-radius:50%;background:rgba(220,60,40,.35);border:2px solid rgba(200,40,20,.9);pointer-events:none;left:-60px;top:-60px;transition:left .35s,top .35s,transform .15s';
          document.body.appendChild(c);})()""")

    def cuadro(self, veces=1, ms=None):
        png = base64.b64decode(self.cdp("Page.captureScreenshot", format="png")["data"])
        img = Image.open(io.BytesIO(png)).convert("RGB")
        for _ in range(veces):
            self.cuadros.append((img, ms))

    def pausa(self, segundos):
        self.cuadro(1, int(segundos * 1000))

    def scroll_a(self, selector, extra=-70, pasos=6):
        destino = self.js(f"(()=>{{const e=document.querySelector({json.dumps(selector)});return e?e.getBoundingClientRect().top+scrollY+({extra}):null}})()")
        if destino is None:
            raise RuntimeError("no está " + selector)
        self.scroll_hasta(destino, pasos)

    def scroll_hasta(self, y, pasos=6):
        desde = self.js("scrollY")
        for i in range(1, pasos + 1):
            self.js(f"scrollTo(0,{desde + (y - desde) * i / pasos})")
            time.sleep(0.05)
            self.cuadro(1, 110)

    def tocar(self, selector, espera=0.9, abajo=False):
        self._cursor()
        r = self.js(f"(()=>{{const e=document.querySelector({json.dumps(selector)});if(!e)return null;e.scrollIntoView({{block:'{'end' if abajo else 'center'}'}});"
                    f"const b=e.getBoundingClientRect();return [b.left+Math.min(b.width/2,60),b.top+b.height/2]}})()")
        if r is None:
            raise RuntimeError("no está " + selector)
        x, y = r
        self.js(f"(()=>{{const c=document.getElementById('_cur');c.style.left='{x - 13}px';c.style.top='{y - 13}px'}})()")
        time.sleep(0.4)
        self.cuadro(1, 500)
        self.js("document.getElementById('_cur').style.transform='scale(.7)'")
        self.cuadro(1, 150)
        self.js(f"document.querySelector({json.dumps(selector)}).click()")
        time.sleep(espera)
        self._cursor()
        try:
            self.js(f"(()=>{{const c=document.getElementById('_cur');c.style.transition='none';c.style.left='{x - 13}px';c.style.top='{y - 13}px';c.style.transform=''}})()")
        except RuntimeError:
            pass
        self.cuadro(1, 900)

    def escribir(self, selector, texto):
        for i in range(1, len(texto) + 1, max(1, len(texto) // 6)):
            self.js(f"document.querySelector({json.dumps(selector)}).value={json.dumps(texto[:i])}")
            self.cuadro(1, 90)
        self.js(f"document.querySelector({json.dumps(selector)}).value={json.dumps(texto)}")
        self.cuadro(1, 400)

    def guardar(self, ruta, ancho=360):
        cuadros = self.cuadros
        self.cuadros = []
        if not cuadros:
            return
        w0, h0 = cuadros[0][0].size
        alto = int(h0 * ancho / w0)
        imgs = [c.resize((ancho, alto), Image.LANCZOS) for c, _ in cuadros]
        duraciones = [ms or 120 for _, ms in cuadros]
        duraciones[-1] = max(duraciones[-1], 2200)
        # Paleta común a todo el GIF: menos peso y sin parpadeo de colores.
        muestra = Image.new("RGB", (ancho, alto * min(len(imgs), 6)))
        for i, im in enumerate(imgs[:: max(1, len(imgs) // 6)][:6]):
            muestra.paste(im, (0, alto * i))
        paleta = muestra.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
        q = [im.quantize(palette=paleta, dither=Image.Dither.NONE) for im in imgs]
        q[0].save(ruta, save_all=True, append_images=q[1:], duration=duraciones, loop=0, optimize=True, disposal=1)

    def cerrar(self):
        try:
            self.ws.close()
        finally:
            self.proc.kill()


def entrar(nav):
    nav.ir("/login")
    nav.js("document.getElementById('usuario').value='guia';document.getElementById('clave').value='clave-local-guia';document.querySelector('form').submit()")
    time.sleep(1.5)
    return nav.js("location.pathname")


def marcar(nav, selector, texto="", n=0, nombre="g"):
    """Marca el n-ésimo elemento que cumple el selector y contiene el texto; devuelve su selector."""
    ok = nav.js(f"""(()=>{{document.querySelectorAll('[data-guia="{nombre}"]').forEach(e=>e.removeAttribute('data-guia'));
      const l=[...document.querySelectorAll({json.dumps(selector)})].filter(e=>e.textContent.includes({json.dumps(texto)}));
      if(!l[{n}])return false; l[{n}].setAttribute('data-guia','{nombre}'); return true}})()""")
    if not ok:
        raise RuntimeError(f"no está {selector} con «{texto}»")
    return f'[data-guia="{nombre}"]'
