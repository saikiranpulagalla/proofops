"""RC2 recovery/session hardening.

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("runs") as b:
        b.add_column(sa.Column("api_request_id", sa.Integer(), nullable=True))
        b.create_unique_constraint("uq_runs_api_request_id", ["api_request_id"])

    with op.batch_alter_table("business_operations") as b:
        b.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("api_requests") as b:
        b.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", name="uq_sessions_session_id"),
    )


def downgrade():
    op.drop_table("sessions")
    with op.batch_alter_table("api_requests") as b:
        b.drop_column("lease_expires_at")
    with op.batch_alter_table("business_operations") as b:
        b.drop_column("lease_expires_at")
    with op.batch_alter_table("runs") as b:
        b.drop_constraint("uq_runs_api_request_id", type_="unique")
        b.drop_column("api_request_id")
