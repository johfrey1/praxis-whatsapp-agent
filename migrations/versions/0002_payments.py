"""pago de cuotas con Wompi

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    payment_status = postgresql.ENUM(
        "pending", "approved", "declined", "voided", "error", name="payment_status", create_type=False
    )
    payment_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "payment_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("contacts.id"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("reference", sa.String(36), nullable=False, unique=True),
        sa.Column("national_id", sa.String(20), nullable=False),
        sa.Column("contract_number", sa.String(40), nullable=False),
        sa.Column("account_number", sa.String(40), nullable=False),
        sa.Column("wompi_payment_link_id", sa.String(64), nullable=False),
        sa.Column("payment_url", sa.String(255), nullable=False),
        sa.Column("status", payment_status, nullable=False, server_default="pending"),
        sa.Column("amount_in_cents", sa.BigInteger, nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("payment_method_type", sa.String(32), nullable=True),
        sa.Column("wompi_transaction_id", sa.String(64), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_payment_requests_contact_id", "payment_requests", ["contact_id"])
    op.create_index("ix_payment_requests_national_id", "payment_requests", ["national_id"])
    op.create_index("ix_payment_requests_contract_number", "payment_requests", ["contract_number"])
    op.create_index(
        "ix_payment_requests_wompi_payment_link_id", "payment_requests", ["wompi_payment_link_id"], unique=True
    )
    op.create_index("ix_payment_requests_wompi_transaction_id", "payment_requests", ["wompi_transaction_id"])

    op.create_table(
        "payment_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "payment_request_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("payment_requests.id"), nullable=True
        ),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("wompi_transaction_id", sa.String(64), nullable=True),
        sa.Column("transaction_status", sa.String(32), nullable=True),
        sa.Column("amount_in_cents", sa.BigInteger, nullable=True),
        sa.Column("environment", sa.String(16), nullable=True),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("event_timestamp", sa.BigInteger, nullable=True),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("verified_transaction", sa.JSON, nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_payment_events_payment_request_id", "payment_events", ["payment_request_id"])
    op.create_index("ix_payment_events_wompi_transaction_id", "payment_events", ["wompi_transaction_id"])


def downgrade() -> None:
    op.drop_table("payment_events")
    op.drop_table("payment_requests")
    postgresql.ENUM(name="payment_status").drop(op.get_bind(), checkfirst=True)
