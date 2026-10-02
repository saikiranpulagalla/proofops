"""Add exact API mutation provenance for RC7 recovery.

Revision ID: 0011
Revises: 0010
"""

from alembic import op
import sqlalchemy as sa


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical RC6 effects/revocations have unknown origin.  Leave them NULL rather
    # than reconstructing causality from timestamps.
    with op.batch_alter_table("effects") as batch:
        batch.add_column(sa.Column("api_request_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_effects_api_request_id", "api_requests", ["api_request_id"], ["id"])
    with op.batch_alter_table("approvals") as batch:
        batch.add_column(sa.Column("rejected_api_request_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_approvals_rejected_api_request_id", "api_requests", ["rejected_api_request_id"], ["id"])
        batch.create_unique_constraint("uq_approvals_rejected_api_request_id", ["rejected_api_request_id"])


def downgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.drop_constraint("uq_approvals_rejected_api_request_id", type_="unique")
        batch.drop_constraint("fk_approvals_rejected_api_request_id", type_="foreignkey")
        batch.drop_column("rejected_api_request_id")
    with op.batch_alter_table("effects") as batch:
        batch.drop_constraint("fk_effects_api_request_id", type_="foreignkey")
        batch.drop_column("api_request_id")
