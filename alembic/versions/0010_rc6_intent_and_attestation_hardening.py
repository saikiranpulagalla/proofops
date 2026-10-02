"""bind approval causality and track deployment generations

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.add_column(sa.Column("api_request_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_approvals_api_request_id", "api_requests", ["api_request_id"], ["id"]
        )
        batch.create_unique_constraint("uq_approvals_api_request_id", ["api_request_id"])

    op.create_table(
        "deployment_boots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("deployment_key", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("last_boot_id", sa.String(length=128), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("deployment_key", name="uq_deployment_boots_deployment_key"),
    )

    op.create_table(
        "login_throttles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("throttle_key", sa.String(length=255), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("throttle_key", name="uq_login_throttles_throttle_key"),
    )


def downgrade() -> None:
    op.drop_table("login_throttles")
    op.drop_table("deployment_boots")
    with op.batch_alter_table("approvals") as batch:
        batch.drop_constraint("uq_approvals_api_request_id", type_="unique")
        batch.drop_constraint("fk_approvals_api_request_id", type_="foreignkey")
        batch.drop_column("api_request_id")
