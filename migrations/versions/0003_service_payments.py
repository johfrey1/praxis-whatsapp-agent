"""trámites con costo: concepto y nombre del estudiante en payment_requests

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-03

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payment_requests", sa.Column("concept", sa.String(30), nullable=False, server_default="cuota"))
    op.add_column("payment_requests", sa.Column("student_name", sa.String(255), nullable=True))
    op.alter_column("payment_requests", "account_number", existing_type=sa.String(40), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE payment_requests SET account_number = '-' WHERE account_number IS NULL")
    op.alter_column("payment_requests", "account_number", existing_type=sa.String(40), nullable=False)
    op.drop_column("payment_requests", "student_name")
    op.drop_column("payment_requests", "concept")
