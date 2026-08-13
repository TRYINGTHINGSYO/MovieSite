"""stage 2a media queue and permissions

Revision ID: b7d9a4c2e6f1
Revises: ea92f8b3bf52
Create Date: 2026-08-12 21:15:00
"""

from alembic import context, op
import sqlalchemy as sa


revision = "b7d9a4c2e6f1"
down_revision = "ea92f8b3bf52"
branch_labels = None
depends_on = None


def upgrade():
    _validate_stage1_provider_ids()

    op.create_table(
        "browser_clients",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("client_key", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "client_key"),
    )

    op.create_table(
        "media_assets",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("duration", sa.Float(), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "media_sources",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("media_asset_id", sa.String(length=32), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("mime_type", sa.String(length=127), nullable=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=True),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "byte_size IS NULL OR byte_size >= 0",
            name="ck_media_sources_byte_size_nonnegative",
        ),
        sa.CheckConstraint(
            "priority >= 0", name="ck_media_sources_priority_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["media_asset_id"], ["media_assets.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_media_sources_media_asset_id",
        "media_sources",
        ["media_asset_id"],
    )
    op.create_index(
        "ix_media_sources_source_type", "media_sources", ["source_type"]
    )

    op.create_table(
        "mux_media_sources",
        sa.Column("source_id", sa.String(length=32), nullable=False),
        sa.Column("upload_id", sa.String(length=255), nullable=True),
        sa.Column("asset_id", sa.String(length=255), nullable=True),
        sa.Column("playback_id", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"], ["media_sources.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("source_id"),
        sa.UniqueConstraint("asset_id"),
        sa.UniqueConstraint("playback_id"),
        sa.UniqueConstraint("upload_id"),
    )

    op.create_table(
        "direct_url_sources",
        sa.Column("source_id", sa.String(length=32), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("observed_content_type", sa.String(length=127), nullable=True),
        sa.Column("last_probed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"], ["media_sources.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("source_id"),
    )

    op.create_table(
        "browser_local_sources",
        sa.Column("source_id", sa.String(length=32), nullable=False),
        sa.Column("browser_client_id", sa.String(length=32), nullable=False),
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=True),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["browser_client_id"], ["browser_clients.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["media_sources.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("source_id"),
        sa.UniqueConstraint("browser_client_id", "storage_key"),
    )
    op.create_index(
        "ix_browser_local_sources_browser_client_id",
        "browser_local_sources",
        ["browser_client_id"],
    )

    op.create_table(
        "room_media",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("media_asset_id", sa.String(length=32), nullable=False),
        sa.Column("added_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["added_by_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["media_asset_id"], ["media_assets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["room_id"], ["watch_rooms.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room_id", "media_asset_id"),
        sa.UniqueConstraint("room_id", "id", name="uq_room_media_room_id_id"),
    )
    op.create_index("ix_room_media_media_asset_id", "room_media", ["media_asset_id"])
    op.create_index("ix_room_media_room_id", "room_media", ["room_id"])

    op.create_table(
        "queue_entries",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("room_media_id", sa.String(length=32), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("added_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "position >= 0", name="ck_queue_entries_position_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["added_by_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["room_id"], ["watch_rooms.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["room_media_id"], ["room_media.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["room_id", "room_media_id"],
            ["room_media.room_id", "room_media.id"],
            name="fk_queue_entries_room_media_room",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room_id", "position"),
    )
    op.create_index("ix_queue_entries_room_id", "queue_entries", ["room_id"])
    op.create_index(
        "ix_queue_entries_room_media_id", "queue_entries", ["room_media_id"]
    )

    with op.batch_alter_table("watch_rooms") as batch_op:
        batch_op.add_column(
            sa.Column("current_queue_entry_id", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("queue_version", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column(
                "playback_version", sa.Integer(), server_default="0", nullable=False
            )
        )

    op.create_table(
        "room_member_permissions",
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("permission", sa.String(length=40), nullable=False),
        sa.Column("granted_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["room_id", "user_id"],
            ["room_memberships.room_id", "room_memberships.user_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("room_id", "user_id", "permission"),
    )

    op.create_table(
        "room_requests",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("requester_kind", sa.String(length=12), nullable=False),
        sa.Column("requester_key", sa.String(length=96), nullable=False),
        sa.Column("requester_user_id", sa.Integer(), nullable=True),
        sa.Column("requester_label", sa.String(length=80), nullable=False),
        sa.Column("request_type", sa.String(length=40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("client_request_id", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_id", sa.Integer(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "requester_kind IN ('user', 'guest')",
            name="ck_room_requests_requester_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'dismissed', 'expired')",
            name="ck_room_requests_status",
        ),
        sa.ForeignKeyConstraint(
            ["requester_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["room_id"], ["watch_rooms.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room_id", "requester_key", "client_request_id"),
    )
    op.create_index("ix_room_requests_room_id", "room_requests", ["room_id"])

    op.create_table(
        "media_cleanup_jobs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("media_source_id", sa.String(length=32), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("operation", sa.String(length=24), server_default="delete", nullable=False),
        sa.Column("remote_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
            "attempts >= 0", name="ck_media_cleanup_jobs_attempts_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["media_source_id"], ["media_sources.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("media_source_id", "operation"),
    )

    op.create_table(
        "provider_webhook_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "event_id"),
    )

    _backfill_stage1_media()

    with op.batch_alter_table("watch_rooms") as batch_op:
        batch_op.create_foreign_key(
            "fk_watch_rooms_current_queue_entry_id",
            "queue_entries",
            ["current_queue_entry_id"],
            ["id"],
            ondelete="SET NULL",
        )


def _backfill_stage1_media():
    mux_predicate = (
        "mux_upload_id IS NOT NULL OR asset_id IS NOT NULL "
        "OR playback_id IS NOT NULL OR url IS NULL"
    )
    op.execute(
        sa.text(
            "INSERT INTO media_assets "
            "(id, title, duration, created_by_id, created_at, updated_at) "
            "SELECT id, name, duration, created_by_id, created_at, updated_at "
            "FROM room_videos"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO media_sources "
            "(id, media_asset_id, source_type, status, priority, error, created_at, updated_at) "
            "SELECT id, id, CASE WHEN "
            + mux_predicate
            + " THEN 'mux_upload' ELSE 'direct_url' END, "
            "status, 0, error, created_at, updated_at FROM room_videos"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO mux_media_sources (source_id, upload_id, asset_id, playback_id) "
            "SELECT id, mux_upload_id, asset_id, playback_id FROM room_videos WHERE "
            + mux_predicate
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO direct_url_sources (source_id, original_url, normalized_url) "
            "SELECT id, url, url FROM room_videos WHERE NOT ("
            + mux_predicate
            + ")"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO room_media (id, room_id, media_asset_id, added_by_id, created_at) "
            "SELECT id, room_id, id, created_by_id, created_at FROM room_videos"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO queue_entries "
            "(id, room_id, room_media_id, position, added_by_id, created_at) "
            "SELECT id, room_id, id, sort_order, created_by_id, created_at FROM room_videos"
        )
    )
    op.execute(
        sa.text(
            "UPDATE watch_rooms SET current_queue_entry_id = current_video_id "
            "WHERE current_video_id IS NOT NULL"
        )
    )


def _validate_stage1_provider_ids():
    if context.is_offline_mode():
        return
    connection = op.get_bind()
    for column_name in ("asset_id", "playback_id"):
        duplicate = connection.execute(
            sa.text(
                f"SELECT {column_name} FROM room_videos "
                f"WHERE {column_name} IS NOT NULL GROUP BY {column_name} "
                "HAVING COUNT(*) > 1 LIMIT 1"
            )
        ).scalar_one_or_none()
        if duplicate is not None:
            raise RuntimeError(
                f"Stage 2A migration blocked: duplicate Mux {column_name} "
                f"{duplicate!r} must be resolved before backfill"
            )


def downgrade():
    _validate_safe_downgrade()
    with op.batch_alter_table("watch_rooms") as batch_op:
        batch_op.drop_constraint(
            "fk_watch_rooms_current_queue_entry_id", type_="foreignkey"
        )
        batch_op.drop_column("playback_version")
        batch_op.drop_column("queue_version")
        batch_op.drop_column("current_queue_entry_id")

    op.drop_table("provider_webhook_events")
    op.drop_table("media_cleanup_jobs")
    op.drop_index("ix_room_requests_room_id", table_name="room_requests")
    op.drop_table("room_requests")
    op.drop_table("room_member_permissions")
    op.drop_index("ix_queue_entries_room_media_id", table_name="queue_entries")
    op.drop_index("ix_queue_entries_room_id", table_name="queue_entries")
    op.drop_table("queue_entries")
    op.drop_index("ix_room_media_room_id", table_name="room_media")
    op.drop_index("ix_room_media_media_asset_id", table_name="room_media")
    op.drop_table("room_media")
    op.drop_index(
        "ix_browser_local_sources_browser_client_id",
        table_name="browser_local_sources",
    )
    op.drop_table("browser_local_sources")
    op.drop_table("direct_url_sources")
    op.drop_table("mux_media_sources")
    op.drop_index("ix_media_sources_source_type", table_name="media_sources")
    op.drop_index("ix_media_sources_media_asset_id", table_name="media_sources")
    op.drop_table("media_sources")
    op.drop_table("media_assets")
    op.drop_table("browser_clients")


def _validate_safe_downgrade():
    if context.is_offline_mode():
        return
    connection = op.get_bind()
    checks = (
        (
            "new Stage 2 media exists",
            "SELECT 1 FROM media_assets ma LEFT JOIN room_videos rv ON rv.id = ma.id "
            "WHERE rv.id IS NULL LIMIT 1",
        ),
        (
            "legacy media was removed from the Stage 2 queue or library",
            "SELECT 1 FROM room_videos rv "
            "LEFT JOIN media_assets ma ON ma.id = rv.id "
            "LEFT JOIN room_media rm ON rm.id = rv.id "
            "LEFT JOIN queue_entries qe ON qe.id = rv.id "
            "WHERE ma.id IS NULL OR rm.id IS NULL OR qe.id IS NULL LIMIT 1",
        ),
        (
            "queue ordering or room ownership changed",
            "SELECT 1 FROM queue_entries qe JOIN room_videos rv ON rv.id = qe.id "
            "WHERE qe.room_id <> rv.room_id OR qe.position <> rv.sort_order LIMIT 1",
        ),
        (
            "the current queue selection changed",
            "SELECT 1 FROM watch_rooms WHERE "
            "COALESCE(current_queue_entry_id, '') <> COALESCE(current_video_id, '') LIMIT 1",
        ),
        (
            "media source state changed",
            "SELECT 1 FROM room_videos rv "
            "JOIN media_assets ma ON ma.id = rv.id "
            "JOIN media_sources ms ON ms.id = rv.id "
            "LEFT JOIN mux_media_sources mux ON mux.source_id = ms.id "
            "WHERE ms.status <> rv.status "
            "OR COALESCE(ms.error, '') <> COALESCE(rv.error, '') "
            "OR COALESCE(ma.duration, -1) <> COALESCE(rv.duration, -1) "
            "OR (ms.source_type = 'mux_upload' AND ("
            "COALESCE(mux.upload_id, '') <> COALESCE(rv.mux_upload_id, '') "
            "OR COALESCE(mux.asset_id, '') <> COALESCE(rv.asset_id, '') "
            "OR COALESCE(mux.playback_id, '') <> COALESCE(rv.playback_id, '')"
            ")) LIMIT 1",
        ),
        (
            "an asset has additional or missing sources",
            "SELECT 1 FROM media_assets ma JOIN room_videos rv ON rv.id = ma.id "
            "WHERE (SELECT COUNT(*) FROM media_sources ms "
            "WHERE ms.media_asset_id = ma.id) <> 1 LIMIT 1",
        ),
        (
            "member permissions exist",
            "SELECT 1 FROM room_member_permissions LIMIT 1",
        ),
        ("room requests exist", "SELECT 1 FROM room_requests LIMIT 1"),
        ("browser clients exist", "SELECT 1 FROM browser_clients LIMIT 1"),
        ("cleanup jobs exist", "SELECT 1 FROM media_cleanup_jobs LIMIT 1"),
    )
    for reason, statement in checks:
        if connection.execute(sa.text(statement)).first() is not None:
            raise RuntimeError(
                "Stage 2A downgrade blocked because "
                + reason
                + ". Restore a pre-Stage-2 backup or explicitly migrate the new data."
            )
