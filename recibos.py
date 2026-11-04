"""El recibo del pago web (no es factura): los datos, para la página
`/pago/<token>/recibo` y para el PDF que va adjunto al mail del paciente.

Sólo lo que ya muestra la página: profesional, nombre del paciente, sesiones,
medio y monto. Nunca teléfono, notas ni nada de la ficha.
"""
import base64
import io
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
_FOTO = re.compile(r"data:image/(png|jpeg);base64,([A-Za-z0-9+/=]+)")


def pesos(v) -> str:
    return f"$ {int(v or 0):,}".replace(",", ".")


def foto_valida(tenant: dict) -> str | None:
    """La foto del perfil sólo si es una imagen PNG/JPG embebida como la guarda el panel."""
    foto = tenant.get("foto") or ""
    return foto if _FOTO.fullmatch(foto) else None


def datos(tenant: dict, pago: dict) -> dict:
    tz = ZoneInfo(tenant.get("timezone") or "America/Argentina/Buenos_Aires")
    sesiones = [datetime.fromisoformat(str(x)).astimezone(tz) for x in pago.get("sesiones") or []]
    return {
        "sesiones": [f"{DIAS[d.weekday()]} {d:%d/%m/%Y %H:%M}" for d in sesiones],
        "fecha": datetime.fromisoformat(str(pago.get("resuelto") or pago["creado"])).astimezone(tz).strftime("%d/%m/%Y"),
        "numero": str(pago["id"]).replace("-", "")[:8].upper(),
        "foto": foto_valida(tenant),
    }


def _l1(texto) -> str:
    """Las fuentes base del PDF son latin-1: lo que no entra se reemplaza."""
    texto = str(texto or "").replace("—", "-").replace("–", "-").replace("“", '"').replace("”", '"').replace("’", "'")
    return texto.encode("latin-1", "replace").decode("latin-1")


def pdf(tenant: dict, pago: dict) -> bytes | None:
    """El mismo recibo de la página, en PDF. None si no se pudo armar
    (el mail sale igual, con el link)."""
    try:
        from fpdf import FPDF
    except ImportError:
        logger.warning("Falta fpdf2: el recibo va sin PDF")
        return None
    try:
        d = datos(tenant, pago)
        doc = FPDF(format="A4")
        doc.set_margins(20, 20, 20)
        doc.add_page()
        x_texto = 20
        if d["foto"]:
            try:
                crudo = base64.b64decode(_FOTO.fullmatch(d["foto"]).group(2))
                doc.image(io.BytesIO(crudo), x=20, y=20, w=18, h=18)
                x_texto = 42
            except Exception:
                logger.info("La foto no entró en el PDF del recibo", exc_info=True)
        doc.set_xy(x_texto, 22)
        doc.set_font("Helvetica", "B", 13)
        doc.cell(0, 6, _l1(tenant.get("nombre")), new_x="LMARGIN", new_y="NEXT")
        if tenant.get("titulo"):
            doc.set_x(x_texto)
            doc.set_font("Helvetica", "", 10)
            doc.cell(0, 5, _l1(tenant["titulo"]), new_x="LMARGIN", new_y="NEXT")
        doc.set_y(44)
        doc.set_font("Helvetica", "B", 18)
        doc.cell(0, 9, "Recibo de pago", new_x="LMARGIN", new_y="NEXT")
        doc.set_font("Helvetica", "", 10)
        doc.set_text_color(90, 88, 79)
        doc.cell(0, 6, _l1(f"N.º {d['numero']} · {d['fecha']}"), new_x="LMARGIN", new_y="NEXT")
        doc.set_text_color(28, 27, 25)
        doc.ln(4)

        concepto = ("Sesiones" if len(d["sesiones"]) > 1 else "Sesión") + (" (seña)" if (tenant.get("cobro_modo") or "total") == "sena" else "")
        medio = "Mercado Pago" if pago.get("metodo") == "mercadopago" else "Transferencia"
        if pago.get("mp_pago_id"):
            medio += f" (operación {pago['mp_pago_id']})"
        profesional = tenant.get("nombre") or ""
        if tenant.get("matricula"):
            profesional += f"\nMatrícula {tenant['matricula']}"
        filas = [("Profesional", profesional)]
        if pago.get("paciente_nombre"):
            filas.append(("Paciente", pago["paciente_nombre"]))
        filas += [("Concepto", "\n".join([concepto, *d["sesiones"]])), ("Medio de pago", medio)]
        for nombre, valor in filas:
            _fila(doc, nombre, valor)
        _fila(doc, "Pagado", pesos(pago.get("monto")), negrita=True)
        if pago.get("devuelto"):
            _fila(doc, "Devuelto", pesos(pago["devuelto"]))
        doc.ln(6)
        doc.set_font("Helvetica", "", 9)
        doc.set_text_color(90, 88, 79)
        doc.multi_cell(0, 5, "Este recibo es un comprobante del pago del turno. No es una factura.")
        return bytes(doc.output())
    except Exception:
        logger.exception("No se pudo armar el PDF del recibo")
        return None


def _fila(doc, nombre: str, valor: str, negrita: bool = False) -> None:
    y = doc.get_y()
    doc.set_font("Helvetica", "", 10)
    doc.set_text_color(90, 88, 79)
    doc.set_xy(20, y + 2)
    doc.cell(60, 6, _l1(nombre))
    doc.set_text_color(28, 27, 25)
    doc.set_font("Helvetica", "B" if negrita else "", 12 if negrita else 10)
    doc.set_xy(80, y + 2)
    doc.multi_cell(110, 6, _l1(valor), new_x="LMARGIN", new_y="NEXT")
    doc.set_draw_color(200, 200, 200)
    doc.line(20, doc.get_y() + 2, 190, doc.get_y() + 2)
    doc.set_y(doc.get_y() + 2)
