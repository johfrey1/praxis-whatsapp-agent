import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Lead, LeadStatus

# Un prospecto abierto en este lapso se completa en vez de crear otro: el bot llama save_lead cada vez
# que el cliente corrige o agrega un dato, y antes eso dejaba varios registros y varios avisos.
REUSE_WINDOW = timedelta(days=30)
_OPEN_STATUSES = (LeadStatus.new, LeadStatus.contacted)
# Si cambia alguno de estos, el asesor necesita saberlo (tiene otra forma de contactarlo).
_CONTACT_FIELDS = ("phone", "email")


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


async def _open_lead(session: AsyncSession, contact_id: uuid.UUID) -> Lead | None:
    result = await session.execute(
        select(Lead)
        .where(
            Lead.contact_id == contact_id,
            Lead.status.in_(_OPEN_STATUSES),
            Lead.created_at >= datetime.now(timezone.utc) - REUSE_WINDOW,
        )
        .order_by(Lead.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def known_phone(session: AsyncSession, contact_id: uuid.UUID) -> str | None:
    lead = await _open_lead(session, contact_id)
    return lead.phone if lead is not None else None


async def save_lead(session: AsyncSession, contact_id: uuid.UUID, **fields: str | None) -> tuple[Lead, bool, bool]:
    """Un prospecto por cliente. Devuelve (prospecto, es_nuevo, avisar): se avisa si es nuevo o si cambió
    su teléfono o correo; un nombre corregido o un comentario más no amerita otro aviso."""
    lead = await _open_lead(session, contact_id)
    if lead is None:
        return await create_lead(session, contact_id, **fields), True, True

    notify = False
    for key, value in fields.items():
        if value and value != getattr(lead, key):
            notify = notify or key in _CONTACT_FIELDS
            setattr(lead, key, value)
    await session.flush()
    return lead, False, notify


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email.strip()))


def normalize_phone(raw: str | None) -> str | None:
    """Teléfono para que el asesor llame, en E.164 sin '+' (573001112233), o None si no sirve.

    Acepta celulares colombianos (3001112233, +57 300 111 2233), fijos con indicativo (601…) y
    números extranjeros escritos con '+' e indicativo de país."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10 and digits[0] in "36":
        return f"57{digits}"
    if len(digits) == 12 and digits.startswith("57") and digits[2] in "36":
        return digits
    if raw.strip().startswith("+") and not digits.startswith("57") and 8 <= len(digits) <= 15:
        return digits
    return None


def format_lead_notification(lead: Lead, *, updated: bool = False) -> str:
    """Aviso para el asesor con lo necesario para llamar ya, sin abrir otra herramienta."""
    title = "Prospecto actualizado: datos de contacto nuevos 📞" if updated else "Nuevo prospecto: contactar ya 📞"
    lines = [title, f"👤 {lead.full_name or 'Sin nombre'}"]
    if lead.email:
        lines.append(f"✉️ {lead.email}")
    if lead.phone:
        lines.append(f"📱 +{lead.phone.lstrip('+')}")
    if lead.program_interest:
        lines.append(f"🎯 Quiere: {lead.program_interest}")
    if lead.comments:
        lines.append(f"📝 {lead.comments}")
    return "\n".join(lines)
