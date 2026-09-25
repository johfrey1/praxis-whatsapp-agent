"""Definición de tools (function calling) que Claude puede invocar y su ejecución.

El agente NUNCA responde datos de cursos/horarios/documentos de memoria: todo pasa
por estas herramientas, que consultan Strapi (solo lectura) o la base de datos propia.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Contact, Conversation
from app.config import get_settings
from app.services import conversation_service, document_service, lead_service, payment_service
from app.strapi.client import StrapiClient
from app.whatsapp.client import WhatsAppClient
from app.wompi.client import WompiClient

settings = get_settings()

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
        "description": "Guarda los datos de contacto de un prospecto interesado en inscribirse. Llama a esta "
        "herramienta en cuanto tengas al menos nombre y (teléfono o email); puedes volver a llamarla "
        "más adelante en la misma conversación para completar datos adicionales.",
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
        "description": "Deriva la conversación a un asesor humano (contratos, facturas, quejas, o petición "
        "explícita de hablar con una persona). Notifica al equipo de ventas/atención.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "Motivo breve de la escalación"}},
            "required": ["reason"],
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
        lead = await lead_service.create_lead(
            ctx.session,
            contact_id=ctx.contact.id,
            full_name=tool_input.get("full_name"),
            phone=tool_input.get("phone") or ctx.contact.wa_id,
            email=tool_input.get("email"),
            program_interest=tool_input.get("program_interest"),
            preferred_schedule=tool_input.get("preferred_schedule"),
            comments=tool_input.get("comments"),
        )
        return {"status": "saved", "lead_id": str(lead.id)}

    if name == "escalate_to_human":
        await conversation_service.escalate_conversation(ctx.session, ctx.conversation, ctx.contact)
        await conversation_service.notify_staff(
            ctx.whatsapp_client, ctx.contact, f"Conversación escalada: {tool_input.get('reason', 'sin motivo')}"
        )
        return {"status": "escalated"}

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
        return {
            "status": "link_created",
            "payment_url": payment.payment_url,
            "reference": payment.reference,
            "expires_at": payment.expires_at.isoformat(),
        }

    if name == "get_payment_status":
        payments = await payment_service.list_contact_payments(ctx.session, ctx.contact)
        return {
            "payments": [
                {
                    "reference": p.reference,
                    "contract_number": p.contract_number,
                    "status": p.status.value,
                    "amount": payment_service.format_cop(p.amount_in_cents),
                    "payment_url": p.payment_url if p.status.value == "pending" else None,
                    "created_at": p.created_at.isoformat(),
                }
                for p in payments
            ]
        }

    return {"error": f"unknown_tool:{name}"}
