"""Solicitudes de estudiantes actuales (consultas, cartera, paz y salvo, recibos…).

El agente recoge cédula, teléfono, correo y la petición, y se envían al número de
atención a estudiantes (STUDENT_REQUEST_NUMBERS) para que un asesor la gestione.
"""

import re
from dataclasses import dataclass

from app.config import get_settings
from app.db.models import Contact
from app.logging_config import get_logger
from app.services.lead_service import is_valid_email
from app.whatsapp.client import WhatsAppClient

settings = get_settings()
logger = get_logger(__name__)

REQUEST_TYPES = {
    "consulta": "Consulta general",
    "pagos": "Pagos",
    "cartera": "Cartera",
    "paz_y_salvo": "Paz y salvo",
    "recibo": "Recibo de pago",
    "congelamiento": "Congelamiento de contrato",
    "extension_contrato": "Extensión de contrato por fecha de caducidad",
    "retoma": "Retoma de contrato vencido",
    "queja_reclamo_sugerencia": "Queja, reclamo o sugerencia",
    "reporte_datacredito": "Reporte a DataCrédito",
    "otro": "Otro",
}


class StudentRequestValidationError(ValueError):
    """Dato inválido; el mensaje indica qué pedirle de nuevo al estudiante."""


@dataclass
class StudentRequest:
    request_type: str
    national_id: str
    phone: str
    email: str
    request: str
    full_name: str | None = None


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def normalize_phone(raw: str) -> str | None:
    """Teléfono en formato E.164 sin '+', o None si no es válido.

    Acepta celular (3xx) o fijo (60x) colombiano de 10 dígitos, con o sin 57 adelante, y números
    de otros países solo si vienen con '+'. Un "3xx" con dígitos de más se rechaza en vez de
    confundirlo con un indicativo extranjero (p. ej. 32324434433 no es Bélgica)."""
    digits = _digits(raw)
    national = digits[2:] if len(digits) == 12 and digits.startswith("57") else digits
    if len(national) == 10 and national.startswith(("3", "60")):
        return f"57{national}"
    if (raw or "").strip().startswith("+") and not digits.startswith("57") and 8 <= len(digits) <= 15:
        return digits
    return None


def build_request(data: dict) -> StudentRequest:
    national_id = _digits(data.get("national_id", ""))
    if not 5 <= len(national_id) <= 12:
        raise StudentRequestValidationError("national_id: la cédula debe tener entre 5 y 12 dígitos")

    phone = normalize_phone(data.get("phone", ""))
    if phone is None:
        raise StudentRequestValidationError(
            "phone: el teléfono no es válido (celular colombiano de 10 dígitos que empiece por 3, "
            "fijo de 10 dígitos que empiece por 60, o número internacional con +indicativo)"
        )

    email = (data.get("email") or "").strip()
    if not is_valid_email(email):
        raise StudentRequestValidationError("email: el correo no es válido")

    request = (data.get("request") or "").strip()
    if not request:
        raise StudentRequestValidationError("request: falta la petición del estudiante")

    request_type = data.get("request_type") if data.get("request_type") in REQUEST_TYPES else "otro"
    full_name = (data.get("full_name") or "").strip() or None
    return StudentRequest(request_type, national_id, phone, email, request, full_name)


def format_request_notification(req: StudentRequest, contact: Contact) -> str:
    lines = [
        f"📥 Solicitud de estudiante: {REQUEST_TYPES[req.request_type]}",
        f"👤 {req.full_name or contact.profile_name or 'Sin nombre'}",
        f"🪪 Cédula: {req.national_id}",
        f"📱 Teléfono: +{req.phone}",
        f"✉️ {req.email}",
        f"💬 WhatsApp: +{contact.wa_id}",
        f"📝 Petición: {req.request}",
    ]
    return "\n".join(lines)


async def send_request(whatsapp_client: WhatsAppClient, req: StudentRequest, contact: Contact) -> str:
    """Envía la solicitud a los números de atención. Devuelve el texto enviado."""
    text = format_request_notification(req, contact)
    numbers = settings.student_request_numbers_list
    if not numbers:
        logger.warning("no_student_request_numbers_configured", wa_id=contact.wa_id)
        raise RuntimeError("no_student_request_numbers_configured")

    delivered = 0
    for number in numbers:
        try:
            response = await whatsapp_client.send_staff_message(to=number, text=text)
            delivered += 1
            wa_message_id = ((response or {}).get("messages") or [{}])[0].get("id")
            logger.info("student_request_accepted", to=number, wa_message_id=wa_message_id)
        except Exception:
            logger.exception("student_request_send_failed", to=number)
    if delivered == 0:
        raise RuntimeError("student_request_not_delivered")
    logger.info("student_request_sent", wa_id=contact.wa_id, request_type=req.request_type)
    return text
