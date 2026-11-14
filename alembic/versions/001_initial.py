"""Create project and investigation tables.

Revision ID: 001_initial
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
    )
    op.create_table(
        "investigations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), unique=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("report", sa.JSON()),
        sa.Column("error_code", sa.String(length=64)),
    )
    op.create_index("ix_investigations_project_id", "investigations", ["project_id"])
    op.create_index("ix_investigations_status", "investigations", ["status"])


def downgrade() -> None:
    op.drop_index("ix_investigations_status", table_name="investigations")
    op.drop_index("ix_investigations_project_id", table_name="investigations")
    op.drop_table("investigations")
    op.drop_table("projects")
