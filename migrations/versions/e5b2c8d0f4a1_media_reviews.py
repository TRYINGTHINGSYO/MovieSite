"""media reviews for watched room titles

Revision ID: e5b2c8d0f4a1
Revises: d4a1b7c9e2f3
"""

from alembic import op
import sqlalchemy as sa


revision = "e5b2c8d0f4a1"
down_revision = "d4a1b7c9e2f3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "media_reviews",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("room_media_id", sa.String(length=32), nullable=False),
        sa.Column("actor_kind", sa.String(length=12), nullable=False),
        sa.Column("actor_key", sa.String(length=96), nullable=False),
        sa.Column("reviewer_label", sa.String(length=80), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.String(length=280), nullable=True),
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
        sa.CheckConstraint(
            "rating >= 1 AND rating <= 5", name="ck_media_reviews_rating"
        ),
        sa.CheckConstraint(
            "actor_kind IN ('user', 'guest')",
            name="ck_media_reviews_actor_kind",
        ),
        sa.ForeignKeyConstraint(
            ["room_media_id"], ["room_media.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room_media_id", "actor_key"),
    )
    op.create_index(
        "ix_media_reviews_room_media_id", "media_reviews", ["room_media_id"]
    )


def downgrade():
    op.drop_index("ix_media_reviews_room_media_id", table_name="media_reviews")
    op.drop_table("media_reviews")
