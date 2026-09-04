import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session
from app.db.models import Document
from app.security import require_admin_api_key
from app.services import document_service

router = APIRouter(prefix="/admin/documents", tags=["admin-documents"], dependencies=[Depends(require_admin_api_key)])


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_document(
    title: str = Form(...),
    category: str | None = Form(default=None),
    description: str | None = Form(default=None),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    content = await file.read()
    document = await document_service.save_uploaded_document(
        session,
        title=title,
        filename=file.filename or "documento",
        content=content,
        mime_type=file.content_type or "application/octet-stream",
        category=category,
        description=description,
    )
    await session.commit()
    return {"id": str(document.id), "title": document.title, "filename": document.filename}


@router.get("")
async def list_documents(session: AsyncSession = Depends(get_session)) -> list[dict]:
    result = await session.execute(select(Document).order_by(Document.created_at.desc()))
    documents = result.scalars().all()
    return [
        {
            "id": str(d.id),
            "title": d.title,
            "category": d.category,
            "filename": d.filename,
            "is_active": d.is_active,
            "created_at": d.created_at.isoformat(),
        }
        for d in documents
    ]


@router.delete("/{document_id}")
async def deactivate_document(document_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    document = await session.get(Document, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    document.is_active = False
    await session.commit()
    return {"status": "deactivated"}
