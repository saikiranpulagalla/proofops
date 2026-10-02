"""V0.9 API boundary ownership and idempotency.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("owner_actor", sa.String(length=255), nullable=True))
    op.create_table(
        "api_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("route", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("actor", "route", "idempotency_key", name="uq_api_request_actor_route_key"),
    )


def downgrade():
    op.drop_table("api_requests")
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("owner_actor")
