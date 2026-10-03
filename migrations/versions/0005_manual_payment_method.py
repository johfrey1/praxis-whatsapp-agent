"""medio de pago en manual_payments (breb, nequi, daviplata)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-03

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("manual_payments", sa.Column("method", sa.String(20), nullable=False, server_default="breb"))


def downgrade() -> None:
    op.drop_column("manual_payments", "method")
