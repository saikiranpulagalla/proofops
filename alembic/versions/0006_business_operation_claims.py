"""serialize concurrent business operations before planning

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "business_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operation_key", sa.String(length=255), nullable=False),
        sa.Column("owner_run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("operation_key", name="uq_business_operation_key"),
    )


def downgrade():
    op.drop_table("business_operations")
