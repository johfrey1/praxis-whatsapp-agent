from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session
from app.db.models import Contact, Lead
from app.security import require_admin_api_key

router = APIRouter(prefix="/admin/leads", tags=["admin-leads"], dependencies=[Depends(require_admin_api_key)])


@router.get("")
async def list_leads(session: AsyncSession = Depends(get_session)) -> list[dict]:
    result = await session.execute(
        select(Lead, Contact).join(Contact, Lead.contact_id == Contact.id).order_by(Lead.created_at.desc())
    )
    return [
        {
            "id": str(lead.id),
            "full_name": lead.full_name,
            "phone": lead.phone,
            "email": lead.email,
            "program_interest": lead.program_interest,
            "preferred_schedule": lead.preferred_schedule,
            "comments": lead.comments,
            "status": lead.status.value,
            "wa_id": contact.wa_id,
            "created_at": lead.created_at.isoformat(),
        }
        for lead, contact in result.all()
    ]
