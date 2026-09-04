import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Document
from app.whatsapp.client import WhatsAppClient

settings = get_settings()

# Meta conserva un media_id subido ~30 días; refrescamos antes por margen de seguridad.
_MEDIA_ID_TTL = timedelta(days=25)


async def save_uploaded_document(
    session: AsyncSession,
    title: str,
    filename: str,
    content: bytes,
    mime_type: str,
    category: str | None = None,
    description: str | None = None,
) -> Document:
    storage_dir = Path(settings.document_storage_path)
    storage_dir.mkdir(parents=True, exist_ok=True)

    unique_name = f"{uuid.uuid4()}_{filename}"
    file_path = storage_dir / unique_name
    file_path.write_bytes(content)

    document = Document(
        title=title,
        description=description,
        category=category,
        filename=filename,
        storage_path=str(file_path),
        mime_type=mime_type,
        file_size_bytes=len(content),
    )
    session.add(document)
    await session.flush()
    return document


async def list_active_documents(session: AsyncSession, category: str | None = None) -> list[Document]:
    stmt = select(Document).where(Document.is_active.is_(True))
    if category:
        stmt = stmt.where(Document.category == category)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_document(session: AsyncSession, document_id: uuid.UUID) -> Document | None:
    return await session.get(Document, document_id)


async def send_document_to_contact(
    session: AsyncSession, whatsapp_client: WhatsAppClient, document: Document, to_wa_id: str
) -> None:
    media_id = document.whatsapp_media_id
    is_stale = (
        document.whatsapp_media_uploaded_at is None
        or datetime.now(timezone.utc) - document.whatsapp_media_uploaded_at > _MEDIA_ID_TTL
    )

    if not media_id or is_stale:
        content = Path(document.storage_path).read_bytes()
        media_id = await whatsapp_client.upload_media(content, document.filename, document.mime_type)
        document.whatsapp_media_id = media_id
        document.whatsapp_media_uploaded_at = datetime.now(timezone.utc)
        await session.flush()

    await whatsapp_client.send_document_by_media_id(
        to=to_wa_id, media_id=media_id, filename=document.filename, caption=document.title
    )
