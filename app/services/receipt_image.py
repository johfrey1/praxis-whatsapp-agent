"""Genera el comprobante de pago aprobado como imagen PNG (para enviarlo por WhatsApp)."""

import io
import unicodedata
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.db.models import Contact, PaymentRequest

_WIDTH = 900
_MARGIN = 60
_GREEN = (22, 128, 61)
_DARK = (33, 37, 41)
_MUTED = (108, 117, 125)
_LINE = (222, 226, 230)


# DejaVu se instala en la imagen Docker (fonts-dejavu-core); las rutas de macOS son para desarrollo local.
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
]


@lru_cache
def _font_path() -> str | None:
    return next((p for p in _FONT_CANDIDATES if Path(p).exists()), None)


def _font(size: int) -> ImageFont.FreeTypeFont:
    path = _font_path()
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size)


def _t(text: str) -> str:
    """La fuente por defecto de Pillow no trae tildes: sin fuente del sistema, se quitan."""
    if _font_path():
        return text
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _fit(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """Recorta con '…' los valores largos (p. ej. nombres) para que no se salgan de la imagen."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _format_cop(amount_in_cents: int | None) -> str:
    if amount_in_cents is None:
        return "-"
    return "$" + f"{amount_in_cents / 100:,.0f}".replace(",", ".") + " COP"


def render_payment_receipt(payment: PaymentRequest, contact: Contact) -> bytes:
    paid_at = (payment.paid_at or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=-5)))
    rows = [
        ("Valor pagado", _format_cop(payment.amount_in_cents)),
        ("Fecha", paid_at.strftime("%d/%m/%Y %I:%M %p") + " (hora Colombia)"),
        ("Medio de pago", payment.payment_method_type or "-"),
        ("Transacción Wompi", payment.wompi_transaction_id or "-"),
        ("Referencia", payment.reference),
        ("Cédula", payment.national_id),
        ("Contrato", payment.contract_number),
        ("Cuenta", payment.account_number),
        ("Estudiante", f"{contact.profile_name or '-'} (+{contact.wa_id})"),
    ]

    row_height = 62
    height = 330 + row_height * len(rows) + 110
    image = Image.new("RGB", (_WIDTH, height), "white")
    draw = ImageDraw.Draw(image)

    draw.rectangle([0, 0, _WIDTH, 150], fill=_GREEN)
    draw.text((_MARGIN, 38), "Praxis English School", font=_font(44), fill="white")
    draw.text((_MARGIN, 96), "Comprobante de pago de cuota", font=_font(28), fill="white")

    draw.rounded_rectangle([_MARGIN, 185, _WIDTH - _MARGIN, 265], radius=16, fill=(232, 245, 236))
    draw.text((_MARGIN + 30, 205), "PAGO APROBADO", font=_font(38), fill=_GREEN)

    y = 300
    for label, value in rows:
        draw.text((_MARGIN, y), _t(label), font=_font(24), fill=_MUTED)
        draw.text((_MARGIN + 290, y - 2), _fit(draw, _t(value), _font(28), _WIDTH - 2 * _MARGIN - 290), font=_font(28), fill=_DARK)
        y += row_height
        draw.line([_MARGIN, y - 16, _WIDTH - _MARGIN, y - 16], fill=_LINE, width=2)

    draw.text(
        (_MARGIN, y + 20),
        _t("Pago procesado por Wompi. Confirmado automáticamente por el asistente de WhatsApp."),
        font=_font(20),
        fill=_MUTED,
    )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
