"""pagos por llave Bre-B con comprobante y aprobación

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-03

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "manual_payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("contacts.id"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("reference", sa.String(36), nullable=False, unique=True),
        sa.Column("concept", sa.String(30), nullable=False),
        sa.Column("national_id", sa.String(20), nullable=False),
        sa.Column("contract_number", sa.String(40), nullable=False),
        sa.Column("account_number", sa.String(40), nullable=True),
        sa.Column("student_name", sa.String(255), nullable=True),
        sa.Column("amount_in_cents", sa.BigInteger, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="awaiting_proof"),
        sa.Column("proof_media_id", sa.String(128), nullable=True),
        sa.Column("proof_path", sa.String(500), nullable=True),
        sa.Column("proof_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_manual_payments_contact_id", "manual_payments", ["contact_id"])
    op.create_index("ix_manual_payments_national_id", "manual_payments", ["national_id"])
    op.create_index("ix_manual_payments_contract_number", "manual_payments", ["contract_number"])
    op.create_index("ix_manual_payments_status", "manual_payments", ["status"])


def downgrade() -> None:
    op.drop_table("manual_payments")
