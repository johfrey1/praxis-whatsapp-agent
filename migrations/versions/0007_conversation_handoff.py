"""aviso al encargado cuando una conversación queda sin terminar

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-21

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("handoff_notified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "handoff_notified_at")
