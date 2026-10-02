"""allow run-scoped evidence for pre-action business observations

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    import sqlalchemy as sa
    with op.batch_alter_table("runs") as b:
        b.add_column(sa.Column("workflow_type", sa.String(length=64), nullable=True))
        b.add_column(sa.Column("subject_customer_id", sa.String(length=255), nullable=True))
        b.add_column(sa.Column("subject_renewal_id", sa.String(length=255), nullable=True))
        b.add_column(sa.Column("goal_contract_json", sa.Text(), nullable=True))
        b.add_column(sa.Column("snapshot_json", sa.Text(), nullable=True))
    with op.batch_alter_table("evidence") as b:
        b.alter_column("action_id", nullable=True)
        b.alter_column("effect_id", nullable=True)
    op.create_index(
        "uq_run_evidence_source",
        "evidence",
        ["run_id", "evidence_type", "source_ref"],
        unique=True,
    )


def downgrade():
    op.drop_index("uq_run_evidence_source", table_name="evidence")
    # Downgrade is intentionally fail-closed if run-scoped evidence exists.
    bind = op.get_bind()
    count = bind.exec_driver_sql(
        "SELECT COUNT(*) FROM evidence WHERE action_id IS NULL OR effect_id IS NULL"
    ).scalar()
    if count:
        raise RuntimeError("cannot downgrade 0005 while run-scoped evidence exists")
    with op.batch_alter_table("evidence") as b:
        b.alter_column("action_id", nullable=False)
        b.alter_column("effect_id", nullable=False)
    with op.batch_alter_table("runs") as b:
        b.drop_column("snapshot_json")
        b.drop_column("goal_contract_json")
        b.drop_column("subject_renewal_id")
        b.drop_column("subject_customer_id")
        b.drop_column("workflow_type")
