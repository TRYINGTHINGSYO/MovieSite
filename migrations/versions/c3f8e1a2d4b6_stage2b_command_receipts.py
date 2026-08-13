"""stage 2b durable command receipts

Revision ID: c3f8e1a2d4b6
Revises: b7d9a4c2e6f1
"""

from alembic import op
import sqlalchemy as sa


revision = "c3f8e1a2d4b6"
down_revision = "b7d9a4c2e6f1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("direct_url_sources") as batch_op:
        batch_op.add_column(
            sa.Column(
                "probe_result",
                sa.String(length=32),
                server_default="not_probed",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_direct_url_sources_probe_result",
            "probe_result IN ('not_probed', 'playable', 'playable_no_seek', "
            "'unsupported_format', 'network_or_cors_failure', 'unavailable')",
        )
    op.create_table(
        "room_command_receipts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("actor_kind", sa.String(length=12), nullable=False),
        sa.Column("actor_key", sa.String(length=96), nullable=False),
        sa.Column("client_action_id", sa.String(length=64), nullable=False),
        sa.Column("command_type", sa.String(length=40), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "actor_kind IN ('user', 'guest')",
            name="ck_room_command_receipts_actor_kind",
        ),
        sa.ForeignKeyConstraint(
            ["room_id"], ["watch_rooms.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "room_id",
            "actor_key",
            "client_action_id",
            name="uq_room_command_receipts_identity_action",
        ),
    )
    with op.batch_alter_table("room_command_receipts") as batch_op:
        batch_op.create_index(
            "ix_room_command_receipts_room_id", ["room_id"], unique=False
        )


def downgrade():
    with op.batch_alter_table("room_command_receipts") as batch_op:
        batch_op.drop_index("ix_room_command_receipts_room_id")
    op.drop_table("room_command_receipts")
    with op.batch_alter_table("direct_url_sources") as batch_op:
        batch_op.drop_constraint(
            "ck_direct_url_sources_probe_result", type_="check"
        )
        batch_op.drop_column("probe_result")
