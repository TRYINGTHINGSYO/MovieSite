"""add users and persistent watch rooms

Revision ID: ea92f8b3bf52
Revises:
Create Date: 2026-08-12 19:00:00.764285
"""

from alembic import op
import sqlalchemy as sa


revision = "ea92f8b3bf52"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=80), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "watch_rooms",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("current_video_id", sa.String(length=32), nullable=True),
        sa.Column("playing", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("position", sa.Float(), server_default="0", nullable=False),
        sa.Column("playback_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_watch_rooms_code", "watch_rooms", ["code"], unique=True)

    op.create_table(
        "room_videos",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("mux_upload_id", sa.String(length=255), nullable=True),
        sa.Column("asset_id", sa.String(length=255), nullable=True),
        sa.Column("playback_id", sa.String(length=255), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("duration", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["room_id"], ["watch_rooms.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "sort_order >= 0", name="ck_room_videos_sort_order_nonnegative"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mux_upload_id"),
        sa.UniqueConstraint("room_id", "sort_order"),
    )
    op.create_index("ix_room_videos_room_id", "room_videos", ["room_id"])

    with op.batch_alter_table("watch_rooms") as batch_op:
        batch_op.create_foreign_key(
            "fk_watch_rooms_current_video_id",
            "room_videos",
            ["current_video_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "room_memberships",
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["room_id"], ["watch_rooms.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("room_id", "user_id"),
    )


def downgrade():
    op.drop_table("room_memberships")
    with op.batch_alter_table("watch_rooms") as batch_op:
        batch_op.drop_constraint("fk_watch_rooms_current_video_id", type_="foreignkey")
    op.drop_index("ix_room_videos_room_id", table_name="room_videos")
    op.drop_table("room_videos")
    op.drop_index("ix_watch_rooms_code", table_name="watch_rooms")
    op.drop_table("watch_rooms")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
