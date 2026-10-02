"""durably bind run initialization input

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("initial_request_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "initial_request_json")
