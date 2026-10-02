"""RC9 durable Gmail reconciliation fence and provider-observed identity."""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("effects", sa.Column("provider_history_start_id", sa.String(length=255), nullable=True))
    op.add_column("effects", sa.Column("provider_effect_reference", sa.String(length=128), nullable=True))
    op.add_column("effects", sa.Column("provider_thread_id", sa.String(length=255), nullable=True))
    op.add_column("effects", sa.Column("provider_rfc_message_id", sa.String(length=512), nullable=True))

def downgrade():
    op.drop_column("effects", "provider_rfc_message_id")
    op.drop_column("effects", "provider_thread_id")
    op.drop_column("effects", "provider_effect_reference")
    op.drop_column("effects", "provider_history_start_id")
