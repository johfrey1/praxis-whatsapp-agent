import re
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


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email.strip()))


def format_lead_notification(lead: Lead) -> str:
    """Aviso para el asesor con lo necesario para llamar ya, sin abrir otra herramienta."""
    lines = ["Nuevo prospecto: contactar ya 📞", f"👤 {lead.full_name or 'Sin nombre'}"]
    if lead.email:
        lines.append(f"✉️ {lead.email}")
    if lead.phone:
        lines.append(f"📱 +{lead.phone.lstrip('+')}")
    if lead.program_interest:
        lines.append(f"🎯 Quiere: {lead.program_interest}")
    if lead.comments:
        lines.append(f"📝 {lead.comments}")
    return "\n".join(lines)
