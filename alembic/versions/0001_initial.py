"""initial safety-kernel schema

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("goal_text", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table("actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("logical_key", sa.String(length=255), nullable=False, unique=True),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=320), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table("effects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("action_id", sa.Integer(), sa.ForeignKey("actions.id"), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("external_key", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("provider_ref", sa.String(length=255), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("action_id", "attempt_no", name="uq_effect_attempt"),
    )
    op.create_table("approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("action_id", sa.Integer(), sa.ForeignKey("actions.id"), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("nonce", sa.String(length=128), nullable=False, unique=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table("evidence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("effect_id", sa.Integer(), sa.ForeignKey("effects.id"), nullable=False),
        sa.Column("evidence_type", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("expected", sa.Text(), nullable=True),
        sa.Column("observed", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("evidence")
    op.drop_table("approvals")
    op.drop_table("effects")
    op.drop_table("actions")
    op.drop_table("runs")
