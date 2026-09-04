"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-04

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # create_type=False: los tipos se crean explícitamente abajo (checkfirst=True); si se deja el
    # default, create_table() los vuelve a crear sin checkfirst y falla con "type already exists".
    conversation_status = postgresql.ENUM(
        "active", "escalated", "closed", name="conversation_status", create_type=False
    )
    message_direction = postgresql.ENUM("inbound", "outbound", name="message_direction", create_type=False)
    message_type = postgresql.ENUM(
        "text", "interactive", "document", "image", "audio", "system", name="message_type", create_type=False
    )
    lead_status = postgresql.ENUM(
        "new", "contacted", "enrolled", "discarded", name="lead_status", create_type=False
    )

    bind = op.get_bind()
    conversation_status.create(bind, checkfirst=True)
    message_direction.create(bind, checkfirst=True)
    message_type.create(bind, checkfirst=True)
    lead_status.create(bind, checkfirst=True)

    op.create_table(
        "contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("wa_id", sa.String(32), nullable=False, unique=True),
        sa.Column("profile_name", sa.String(255), nullable=True),
        sa.Column("is_new", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("needs_human", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("human_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_contacts_wa_id", "contacts", ["wa_id"], unique=True)

    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("contacts.id"), nullable=False),
        sa.Column("status", conversation_status, nullable=False, server_default="active"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_conversations_contact_id", "conversations", ["contact_id"])

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("wa_message_id", sa.String(128), nullable=True, unique=True),
        sa.Column("direction", message_direction, nullable=False),
        sa.Column("message_type", message_type, nullable=False),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("media_id", sa.String(128), nullable=True),
        sa.Column("tool_calls", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index("ix_messages_wa_message_id", "messages", ["wa_message_id"], unique=True)

    op.create_table(
        "leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("contacts.id"), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("program_interest", sa.String(255), nullable=True),
        sa.Column("preferred_schedule", sa.String(255), nullable=True),
        sa.Column("comments", sa.Text, nullable=True),
        sa.Column("status", lead_status, nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_leads_contact_id", "leads", ["contact_id"])

    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("storage_path", sa.String(500), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("file_size_bytes", sa.Integer, nullable=False),
        sa.Column("whatsapp_media_id", sa.String(128), nullable=True),
        sa.Column("whatsapp_media_uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_documents_category", "documents", ["category"])


def downgrade() -> None:
    op.drop_table("documents")
    op.drop_table("leads")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("contacts")

    bind = op.get_bind()
    postgresql.ENUM(name="lead_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="message_type").drop(bind, checkfirst=True)
    postgresql.ENUM(name="message_direction").drop(bind, checkfirst=True)
    postgresql.ENUM(name="conversation_status").drop(bind, checkfirst=True)
