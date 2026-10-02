"""bind completion evidence to renewal identity

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("evidence") as b:
        b.add_column(sa.Column("renewal_id", sa.String(length=255), nullable=True))

    # Best-effort backfill from the canonical action payload introduced in 0002.
    # SQLite JSON1 is not guaranteed everywhere, so leave legacy rows nullable if
    # extraction is unavailable; completion now fails closed for scoped contracts.
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute("""
            UPDATE evidence e
            SET renewal_id = (a.payload_json::jsonb ->> 'renewal_id')
            FROM actions a
            WHERE e.action_id = a.id AND e.renewal_id IS NULL
        """)
    elif dialect == "sqlite":
        try:
            op.execute("""
                UPDATE evidence
                SET renewal_id = (
                    SELECT json_extract(actions.payload_json, '$.renewal_id')
                    FROM actions WHERE actions.id = evidence.action_id
                )
                WHERE renewal_id IS NULL
            """)
        except Exception:
            # Legacy evidence without identity remains intentionally untrusted.
            pass


def downgrade():
    with op.batch_alter_table("evidence") as b:
        b.drop_column("renewal_id")
