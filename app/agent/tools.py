"""Definición de tools (function calling) que Claude puede invocar y su ejecución.

El agente NUNCA responde datos de cursos/horarios/documentos de memoria: todo pasa
por estas herramientas, que consultan Strapi (solo lectura) o la base de datos propia.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Contact, Conversation, MessageDirection, MessageType
from app.config import get_settings
from app.logging_config import get_logger
from app.services import (
    conversation_service,
    document_service,
    lead_service,
    payment_service,
    student_request_service,
)
from app.strapi.client import StrapiClient
from app.whatsapp.client import WhatsAppClient
from app.wompi.client import WompiClient

settings = get_settings()
logger = get_logger(__name__)

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "get_class_schedules",
        "description": "Consulta horarios de clases disponibles en la plataforma académica. Úsalo cuando "
        "el usuario pregunte por horarios, días o disponibilidad de clases.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Texto libre para filtrar (nivel, día, curso). Opcional."}
            },
        },
    },
    {
        "name": "get_teachers",
        "description": "Lista los profesores registrados en la plataforma académica.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_courses",
        "description": "Lista categorías/programas de cursos disponibles en la academia.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_documents",
        "description": "Lista los documentos (brochures, temarios, listas de precios, etc.) disponibles para "
        "enviar por WhatsApp. Úsalo antes de ofrecer o enviar un documento.",
        "input_schema": {
            "type": "object",
            "properties": {"category": {"type": "string", "description": "Categoría a filtrar. Opcional."}},
        },
    },
    {
        "name": "send_document",
        "description": "Envía por WhatsApp al usuario actual un documento previamente listado con "
        "list_documents. Usa el 'id' exacto devuelto por list_documents.",
        "input_schema": {
            "type": "object",
            "properties": {"document_id": {"type": "string", "description": "UUID del documento a enviar."}},
            "required": ["document_id"],
        },
    },
    {
        "name": "save_lead",
        "description": "Guarda un prospecto y avisa de inmediato a los asesores para que lo contacten. "
        "Llámala cuando tengas nombre completo y correo. program_interest = lo que quiere lograr con el "
        "inglés, en sus palabras. Devuelve invalid_email si el correo no es válido.",
        "input_schema": {
            "type": "object",
            "properties": {
                "full_name": {"type": "string"},
                "phone": {"type": "string"},
                "email": {"type": "string"},
                "program_interest": {"type": "string", "description": "Curso/nivel de interés"},
                "preferred_schedule": {"type": "string"},
                "comments": {"type": "string"},
            },
        },
    },
    {
        "name": "escalate_to_human",
        "description": "Deriva la conversación a un asesor humano (quejas fuertes, urgencias, o petición "
        "explícita de hablar con una persona). Notifica al equipo de ventas/atención.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Motivo breve de la escalación"},
                "is_student": {
                    "type": "boolean",
                    "description": "true si la persona ya es estudiante matriculado de Praxis (MODO SERVICIO); "
                    "false si es un prospecto o no está claro. Los estudiantes van al equipo de atención "
                    "a estudiantes y los prospectos a los asesores.",
                },
            },
            "required": ["reason", "is_student"],
        },
    },
    {
        "name": "submit_student_request",
        "description": "Envía la solicitud de un estudiante actual (consulta, pagos, cartera, paz y salvo, "
        "recibo de pago u otra) al equipo de atención a estudiantes. Llámala SOLO cuando tengas los cuatro "
        "datos: cédula, teléfono, correo y la petición. Si devuelve invalid_data, pide de nuevo el dato "
        "indicado en detail.",
        "input_schema": {
            "type": "object",
            "properties": {
                "request_type": {
                    "type": "string",
                    "enum": list(student_request_service.REQUEST_TYPES),
                    "description": "Tipo de solicitud",
                },
                "national_id": {"type": "string", "description": "Número de cédula del estudiante"},
                "phone": {"type": "string", "description": "Teléfono de contacto que dio el estudiante"},
                "email": {"type": "string", "description": "Correo electrónico del estudiante"},
                "request": {
                    "type": "string",
                    "description": "La petición completa del estudiante, con todos los detalles que dio "
                    "(contrato, cuenta, fechas, valores…)",
                },
                "full_name": {"type": "string", "description": "Nombre completo, si lo dio"},
            },
            "required": ["request_type", "national_id", "phone", "email", "request"],
        },
    },
    {
        "name": "create_installment_payment_link",
        "description": "Genera un link de pago de Wompi para que un estudiante pague una cuota. El monto NO "
        "se pide: el estudiante lo escribe en el link. Requiere los tres datos: cédula, número de "
        "contrato y número de cuenta. Si devuelve invalid_data, pide de nuevo el dato indicado.",
        "input_schema": {
            "type": "object",
            "properties": {
                "national_id": {"type": "string", "description": "Cédula del titular del contrato"},
                "contract_number": {"type": "string", "description": "Número de contrato"},
                "account_number": {"type": "string", "description": "Número de cuenta"},
            },
            "required": ["national_id", "contract_number", "account_number"],
        },
    },
    {
        "name": "create_service_payment_link",
        "description": "Genera el link de pago (valor fijo, lo define la academia) de un trámite de un "
        "estudiante antiguo: sábana de notas, paz y salvo o certificado. Requiere cédula, número de "
        "contrato y nombres y apellidos completos. Cuando Wompi confirma el pago, el requerimiento le llega "
        "al gestor de estudiantes y una copia a quien pagó. Si devuelve invalid_data, pide de nuevo el dato "
        "indicado en detail.",
        "input_schema": {
            "type": "object",
            "properties": {
                "concept": {
                    "type": "string",
                    "enum": list(payment_service.SERVICE_CONCEPTS),
                    "description": "sabana_notas, paz_y_salvo o certificado",
                },
                "national_id": {"type": "string", "description": "Cédula del estudiante"},
                "contract_number": {"type": "string", "description": "Número de contrato"},
                "full_name": {"type": "string", "description": "Nombres y apellidos completos del estudiante"},
            },
            "required": ["concept", "national_id", "contract_number", "full_name"],
        },
    },
    {
        "name": "get_payment_status",
        "description": "Consulta el estado de los últimos pagos de cuota de este usuario (pendiente, "
        "aprobado, rechazado). Úsalo si pregunta si su pago ya se registró.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


@dataclass
class ToolContext:
    session: AsyncSession
    contact: Contact
    conversation: Conversation
    whatsapp_client: WhatsAppClient
    strapi_client: StrapiClient
    wompi_client: WompiClient


def _summarize_entries(entries: list[dict], fields: list[str]) -> list[dict]:
    summarized = []
    for entry in entries:
        attrs = entry.get("attributes", entry)
        summarized.append({"id": entry.get("id"), **{f: attrs.get(f) for f in fields}})
    return summarized


async def execute_tool(name: str, tool_input: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if name == "get_class_schedules":
        entries = await ctx.strapi_client.list_class_schedules(
            {"level": tool_input["query"]} if tool_input.get("query") else None
        )
        return {"schedules": _summarize_entries(entries, ["level", "day", "start_time", "end_time", "modality"])}

    if name == "get_teachers":
        entries = await ctx.strapi_client.list_teachers()
        return {"teachers": _summarize_entries(entries, ["name", "specialty"])}

    if name == "get_courses":
        entries = await ctx.strapi_client.list_categories()
        return {"courses": _summarize_entries(entries, ["name", "description"])}

    if name == "list_documents":
        documents = await document_service.list_active_documents(ctx.session, tool_input.get("category"))
        return {
            "documents": [
                {"id": str(d.id), "title": d.title, "description": d.description, "category": d.category}
                for d in documents
            ]
        }

    if name == "send_document":
        document = await document_service.get_document(ctx.session, uuid.UUID(tool_input["document_id"]))
        if document is None or not document.is_active:
            return {"error": "document_not_found"}
        await document_service.send_document_to_contact(ctx.session, ctx.whatsapp_client, document, ctx.contact.wa_id)
        return {"status": "sent", "title": document.title}

    if name == "save_lead":
        email = (tool_input.get("email") or "").strip()
        if email and not lead_service.is_valid_email(email):
            return {"error": "invalid_email"}
        lead = await lead_service.create_lead(
            ctx.session,
            contact_id=ctx.contact.id,
            full_name=tool_input.get("full_name"),
            phone=tool_input.get("phone") or ctx.contact.wa_id,
            email=email or None,
            program_interest=tool_input.get("program_interest"),
            preferred_schedule=tool_input.get("preferred_schedule"),
            comments=tool_input.get("comments"),
        )
        await conversation_service.notify_staff(
            ctx.whatsapp_client, ctx.contact, lead_service.format_lead_notification(lead)
        )
        return {"status": "saved", "lead_id": str(lead.id)}

    if name == "escalate_to_human":
        await conversation_service.escalate_conversation(ctx.session, ctx.conversation, ctx.contact)
        await conversation_service.notify_staff(
            ctx.whatsapp_client,
            ctx.contact,
            f"Conversación escalada: {tool_input.get('reason', 'sin motivo')}",
            student=bool(tool_input.get("is_student")),
        )
        return {"status": "escalated"}

    if name == "submit_student_request":
        try:
            request = student_request_service.build_request(tool_input)
        except student_request_service.StudentRequestValidationError as exc:
            return {"error": "invalid_data", "detail": str(exc)}
        try:
            text = await student_request_service.send_request(ctx.whatsapp_client, request, ctx.contact)
        except Exception:
            logger.exception("student_request_failed", wa_id=ctx.contact.wa_id)
            return {"error": "tool_failed"}
        await conversation_service.record_message(
            ctx.session,
            ctx.conversation,
            direction=MessageDirection.outbound,
            message_type=MessageType.system,
            content=f"[Solicitud enviada a atención a estudiantes]\n{text}",
        )
        return {"status": "request_sent"}

    if name == "create_installment_payment_link":
        if not settings.payments_enabled:
            return {"error": "payments_disabled"}
        try:
            payment = await payment_service.create_installment_payment(
                ctx.session,
                ctx.wompi_client,
                ctx.contact,
                ctx.conversation,
                national_id=tool_input.get("national_id", ""),
                contract_number=tool_input.get("contract_number", ""),
                account_number=tool_input.get("account_number", ""),
            )
        except payment_service.PaymentValidationError as exc:
            return {"error": "invalid_data", "detail": str(exc)}
        try:
            await payment_service.send_payment_button(
                ctx.session, ctx.whatsapp_client, payment, ctx.contact.wa_id, ctx.conversation
            )
            return {
                "status": "payment_button_sent",
                "reference": payment.reference,
                "expires_at": payment.expires_at.isoformat(),
            }
        except Exception:
            # Si Meta rechaza el botón, Claude envía el link como texto.
            logger.exception("payment_button_failed", reference=payment.reference)
            return {
                "status": "link_created",
                "payment_url": payment.payment_url,
                "reference": payment.reference,
                "expires_at": payment.expires_at.isoformat(),
            }

    if name == "create_service_payment_link":
        if not settings.payments_enabled:
            return {"error": "payments_disabled"}
        try:
            payment = await payment_service.create_service_payment(
                ctx.session,
                ctx.wompi_client,
                ctx.contact,
                ctx.conversation,
                concept=tool_input.get("concept", ""),
                national_id=tool_input.get("national_id", ""),
                contract_number=tool_input.get("contract_number", ""),
                student_name=tool_input.get("full_name", ""),
            )
        except payment_service.PaymentValidationError as exc:
            return {"error": "invalid_data", "detail": str(exc)}
        fee = payment_service.format_cop(settings.service_fee_cop * 100)
        try:
            await payment_service.send_payment_button(
                ctx.session, ctx.whatsapp_client, payment, ctx.contact.wa_id, ctx.conversation
            )
            return {"status": "payment_button_sent", "amount": fee, "reference": payment.reference}
        except Exception:
            logger.exception("payment_button_failed", reference=payment.reference)
            return {"status": "link_created", "amount": fee, "payment_url": payment.payment_url}

    if name == "get_payment_status":
        payments = await payment_service.list_contact_payments(ctx.session, ctx.contact)
        return {
            "payments": [
                {
                    "reference": p.reference,
                    "contract_number": p.contract_number,
                    "concept": p.concept,
                    "status": p.status.value,
                    "amount": payment_service.format_cop(p.amount_in_cents),
                    "payment_url": p.payment_url if p.status.value == "pending" else None,
                    "created_at": p.created_at.isoformat(),
                }
                for p in payments
            ]
        }

    return {"error": f"unknown_tool:{name}"}
