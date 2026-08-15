"""direct url extractor for yt-dlp playback

Revision ID: d4a1b7c9e2f3
Revises: c3f8e1a2d4b6
"""

from alembic import op
import sqlalchemy as sa


revision = "d4a1b7c9e2f3"
down_revision = "c3f8e1a2d4b6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("direct_url_sources") as batch_op:
        batch_op.add_column(
            sa.Column(
                "extractor",
                sa.String(length=32),
                server_default="direct",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_direct_url_sources_extractor",
            "extractor IN ('direct', 'yt_dlp')",
        )


def downgrade():
    with op.batch_alter_table("direct_url_sources") as batch_op:
        batch_op.drop_constraint(
            "ck_direct_url_sources_extractor", type_="check"
        )
        batch_op.drop_column("extractor")
