"""usuarios de WhatsApp sin número (BSUID), estado de entrega y bitácora de webhooks

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-08

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_NEW_MESSAGE_TYPES = ("video", "sticker", "location", "contacts", "reaction", "other")


def upgrade() -> None:
    op.alter_column("contacts", "wa_id", type_=sa.String(160), existing_type=sa.String(32), existing_nullable=False)
    op.add_column("contacts", sa.Column("user_id", sa.String(160), nullable=True))
    op.add_column("contacts", sa.Column("username", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_contacts_user_id", "contacts", ["user_id"])

    op.add_column("messages", sa.Column("delivery_status", sa.String(16), nullable=True))
    op.add_column("messages", sa.Column("delivery_error", sa.Text(), nullable=True))
    for value in _NEW_MESSAGE_TYPES:
        op.execute(f"ALTER TYPE message_type ADD VALUE IF NOT EXISTS '{value}'")

    op.create_table(
        "webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("messages_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("statuses_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outcome", sa.String(20), nullable=False, server_default="received"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_webhook_events_outcome", "webhook_events", ["outcome"])
    op.create_index("ix_webhook_events_received_at", "webhook_events", ["received_at"])
    op.create_index("ix_contacts_first_seen_at", "contacts", ["first_seen_at"])
    op.create_index("ix_messages_created_at", "messages", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_messages_created_at", table_name="messages")
    op.drop_index("ix_contacts_first_seen_at", table_name="contacts")
    op.drop_table("webhook_events")
    op.drop_column("messages", "delivery_error")
    op.drop_column("messages", "delivery_status")
    op.drop_constraint("uq_contacts_user_id", "contacts", type_="unique")
    op.drop_column("contacts", "username")
    op.drop_column("contacts", "user_id")
    op.alter_column("contacts", "wa_id", type_=sa.String(32), existing_type=sa.String(160), existing_nullable=False)
    # Postgres no permite quitar valores de un enum: los nuevos tipos de mensaje se quedan.
