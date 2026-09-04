import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Lead


async def create_lead(
    session: AsyncSession,
    contact_id: uuid.UUID,
    full_name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    program_interest: str | None = None,
    preferred_schedule: str | None = None,
    comments: str | None = None,
) -> Lead:
    lead = Lead(
        contact_id=contact_id,
        full_name=full_name,
        phone=phone,
        email=email,
        program_interest=program_interest,
        preferred_schedule=preferred_schedule,
        comments=comments,
    )
    session.add(lead)
    await session.flush()
    return lead
