"""Bitácora de WhatsApp para el equipo: clientes nuevos y antiguos por día, sus conversaciones y los
mensajes que fallaron. Fechas en hora de Colombia."""

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session
from app.db.models import (
    Contact,
    Conversation,
    Lead,
    Message,
    MessageDirection,
    WebhookEvent,
    contact_handle,
)
from app.security import require_admin_api_key

router = APIRouter(prefix="/admin/activity", tags=["admin-activity"], dependencies=[Depends(require_admin_api_key)])

BOGOTA = ZoneInfo("America/Bogota")


def _day_bounds(day: date | None) -> tuple[date, datetime, datetime]:
    day = day or datetime.now(BOGOTA).date()
    start = datetime.combine(day, time.min, tzinfo=BOGOTA)
    return day, start, start + timedelta(days=1)


def _contact_row(contact: Contact, inbound: int, last_message_at: datetime | None) -> dict:
    return {
        "id": str(contact.id),
        "name": contact.profile_name,
        "whatsapp": contact_handle(contact),
        "wa_id": contact.wa_id,
        "user_id": contact.user_id,
        "first_seen_at": contact.first_seen_at.astimezone(BOGOTA).isoformat(),
        "inbound_messages": inbound,
        "last_message_at": last_message_at.astimezone(BOGOTA).isoformat() if last_message_at else None,
        "needs_human": contact.needs_human,
    }


@router.get("/daily")
async def daily_activity(
    day: date | None = Query(default=None, description="YYYY-MM-DD en hora de Colombia; por defecto hoy"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Quién escribió ese día, separando clientes nuevos (primer mensaje ese día) de antiguos."""
    day, start, end = _day_bounds(day)

    inbound = (
        select(
            Conversation.contact_id.label("contact_id"),
            func.count(Message.id).label("inbound"),
            func.max(Message.created_at).label("last_at"),
        )
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Message.direction == MessageDirection.inbound,
            Message.created_at >= start,
            Message.created_at < end,
        )
        .group_by(Conversation.contact_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Contact, inbound.c.inbound, inbound.c.last_at)
            .join(inbound, inbound.c.contact_id == Contact.id)
            .order_by(inbound.c.last_at.desc())
        )
    ).all()

    new_contacts = [_contact_row(c, n, last) for c, n, last in rows if c.first_seen_at >= start]
    returning_contacts = [_contact_row(c, n, last) for c, n, last in rows if c.first_seen_at < start]

    outbound_by_status = {
        delivery_status or "sin_estado": count
        for delivery_status, count in (
            await session.execute(
                select(Message.delivery_status, func.count())
                .where(
                    Message.direction == MessageDirection.outbound,
                    Message.created_at >= start,
                    Message.created_at < end,
                )
                .group_by(Message.delivery_status)
            )
        ).all()
    }
    failed_events = (
        await session.execute(
            select(WebhookEvent.received_at, WebhookEvent.outcome, WebhookEvent.error)
            .where(
                WebhookEvent.received_at >= start,
                WebhookEvent.received_at < end,
                WebhookEvent.outcome.in_(("failed", "partial")),
            )
            .order_by(WebhookEvent.received_at)
        )
    ).all()
    leads = await session.scalar(select(func.count(Lead.id)).where(Lead.created_at >= start, Lead.created_at < end))

    return {
        "day": day.isoformat(),
        "timezone": "America/Bogota",
        "summary": {
            "new_contacts": len(new_contacts),
            "returning_contacts": len(returning_contacts),
            "inbound_messages": sum(n for _, n, _ in rows),
            "outbound_messages": outbound_by_status,
            "leads": leads or 0,
            "processing_failures": len(failed_events),
        },
        "new_contacts": new_contacts,
        "returning_contacts": returning_contacts,
        "processing_failures": [
            {"received_at": at.astimezone(BOGOTA).isoformat(), "outcome": outcome, "error": error}
            for at, outcome, error in failed_events
        ],
    }


@router.get("/contacts")
async def list_contacts(
    limit: int = Query(default=100, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Todos los clientes, del más reciente al más antiguo, con su total de mensajes."""
    inbound = (
        select(
            Conversation.contact_id.label("contact_id"),
            func.count(Message.id).label("inbound"),
            func.max(Message.created_at).label("last_at"),
        )
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Message.direction == MessageDirection.inbound)
        .group_by(Conversation.contact_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Contact, func.coalesce(inbound.c.inbound, 0), inbound.c.last_at)
            .outerjoin(inbound, inbound.c.contact_id == Contact.id)
            .order_by(func.coalesce(inbound.c.last_at, Contact.first_seen_at).desc())
            .limit(limit)
        )
    ).all()
    return [_contact_row(c, n, last) for c, n, last in rows]


@router.get("/contacts/{contact_id}/messages")
async def contact_messages(
    contact_id: uuid.UUID,
    limit: int = Query(default=200, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Historial completo de un cliente (todas sus conversaciones), con el estado de entrega."""
    contact = await session.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    messages = (
        await session.execute(
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(Conversation.contact_id == contact.id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return {
        "contact": _contact_row(contact, sum(m.direction == MessageDirection.inbound for m in messages), None),
        "messages": [
            {
                "at": m.created_at.astimezone(BOGOTA).isoformat(),
                "direction": m.direction.value,
                "type": m.message_type.value,
                "content": m.content,
                "delivery_status": m.delivery_status,
                "delivery_error": m.delivery_error,
                "tools": [call.get("tool") for call in (m.tool_calls or {}).get("calls", [])] or None,
            }
            for m in reversed(messages)
        ],
    }
