from __future__ import annotations

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, func
from werkzeug.security import check_password_hash, generate_password_hash


db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(80))
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    owned_rooms = db.relationship(
        "WatchRoom",
        back_populates="owner",
        foreign_keys="WatchRoom.owner_id",
        passive_deletes="all",
    )
    memberships = db.relationship(
        "RoomMembership",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class WatchRoom(db.Model):
    __tablename__ = "watch_rooms"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(8), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    owner_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # Stage 1 compatibility. The Stage 2 expand migration deliberately keeps this
    # column/table until the later contract migration.
    current_video_id = db.Column(
        db.String(32),
        db.ForeignKey(
            "room_videos.id",
            name="fk_watch_rooms_current_video_id",
            ondelete="SET NULL",
            use_alter=True,
        ),
    )
    current_queue_entry_id = db.Column(
        db.String(32),
        db.ForeignKey(
            "queue_entries.id",
            name="fk_watch_rooms_current_queue_entry_id",
            ondelete="SET NULL",
            use_alter=True,
        ),
    )
    playing = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    position = db.Column(db.Float, nullable=False, default=0.0, server_default="0")
    playback_updated_at = db.Column(db.DateTime(timezone=True))
    queue_version = db.Column(
        db.Integer, nullable=False, default=0, server_default="0"
    )
    playback_version = db.Column(
        db.Integer, nullable=False, default=0, server_default="0"
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    archived_at = db.Column(db.DateTime(timezone=True))

    owner = db.relationship("User", back_populates="owned_rooms", foreign_keys=[owner_id])
    memberships = db.relationship(
        "RoomMembership",
        back_populates="room",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    library_items = db.relationship(
        "RoomMedia",
        back_populates="room",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    queue_entries = db.relationship(
        "QueueEntry",
        back_populates="room",
        cascade="all, delete-orphan",
        foreign_keys="QueueEntry.room_id",
        order_by="QueueEntry.position",
        passive_deletes=True,
    )
    current_queue_entry = db.relationship(
        "QueueEntry", foreign_keys=[current_queue_entry_id], post_update=True
    )

    # Stage 1 compatibility relationships.
    videos = db.relationship(
        "RoomVideo",
        back_populates="room",
        cascade="all, delete-orphan",
        foreign_keys="RoomVideo.room_id",
        order_by="RoomVideo.sort_order",
        passive_deletes=True,
    )
    current_video = db.relationship(
        "RoomVideo", foreign_keys=[current_video_id], post_update=True
    )


class RoomMembership(db.Model):
    __tablename__ = "room_memberships"

    room_id = db.Column(
        db.Integer,
        db.ForeignKey("watch_rooms.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    joined_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    room = db.relationship("WatchRoom", back_populates="memberships")
    user = db.relationship("User", back_populates="memberships")
    permissions = db.relationship(
        "RoomMemberPermission",
        back_populates="membership",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class MediaAsset(db.Model):
    __tablename__ = "media_assets"

    id = db.Column(db.String(32), primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    duration = db.Column(db.Float)
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    deleted_at = db.Column(db.DateTime(timezone=True))

    created_by = db.relationship("User")
    sources = db.relationship(
        "MediaSource",
        back_populates="asset",
        cascade="all, delete-orphan",
        order_by="MediaSource.priority, MediaSource.created_at",
        passive_deletes=True,
    )
    room_links = db.relationship("RoomMedia", back_populates="asset")


class MediaSource(db.Model):
    __tablename__ = "media_sources"
    __table_args__ = (
        CheckConstraint("priority >= 0", name="ck_media_sources_priority_nonnegative"),
        CheckConstraint("byte_size IS NULL OR byte_size >= 0", name="ck_media_sources_byte_size_nonnegative"),
    )

    id = db.Column(db.String(32), primary_key=True)
    media_asset_id = db.Column(
        db.String(32),
        db.ForeignKey("media_assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_type = db.Column(db.String(32), nullable=False, index=True)
    status = db.Column(db.String(24), nullable=False)
    mime_type = db.Column(db.String(127))
    byte_size = db.Column(db.BigInteger)
    priority = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    error = db.Column(db.Text)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    deleted_at = db.Column(db.DateTime(timezone=True))

    asset = db.relationship("MediaAsset", back_populates="sources")
    mux = db.relationship(
        "MuxMediaSource",
        back_populates="source",
        cascade="all, delete-orphan",
        uselist=False,
        passive_deletes=True,
    )
    direct_url = db.relationship(
        "DirectUrlSource",
        back_populates="source",
        cascade="all, delete-orphan",
        uselist=False,
        passive_deletes=True,
    )
    browser_local = db.relationship(
        "BrowserLocalSource",
        back_populates="source",
        cascade="all, delete-orphan",
        uselist=False,
        passive_deletes=True,
    )


class MuxMediaSource(db.Model):
    __tablename__ = "mux_media_sources"

    source_id = db.Column(
        db.String(32),
        db.ForeignKey("media_sources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    upload_id = db.Column(db.String(255), unique=True)
    asset_id = db.Column(db.String(255), unique=True)
    playback_id = db.Column(db.String(255), unique=True)

    source = db.relationship("MediaSource", back_populates="mux")


class DirectUrlSource(db.Model):
    __tablename__ = "direct_url_sources"
    __table_args__ = (
        CheckConstraint(
            "probe_result IN ('not_probed', 'playable', 'playable_no_seek', "
            "'unsupported_format', 'network_or_cors_failure', 'unavailable')",
            name="ck_direct_url_sources_probe_result",
        ),
    )

    source_id = db.Column(
        db.String(32),
        db.ForeignKey("media_sources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    original_url = db.Column(db.Text, nullable=False)
    normalized_url = db.Column(db.Text, nullable=False)
    observed_content_type = db.Column(db.String(127))
    probe_result = db.Column(
        db.String(32), nullable=False, default="not_probed", server_default="not_probed"
    )
    last_probed_at = db.Column(db.DateTime(timezone=True))

    source = db.relationship("MediaSource", back_populates="direct_url")


class BrowserClient(db.Model):
    __tablename__ = "browser_clients"
    __table_args__ = (UniqueConstraint("user_id", "client_key"),)

    id = db.Column(db.String(32), primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    client_key = db.Column(db.String(64), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at = db.Column(db.DateTime(timezone=True))
    revoked_at = db.Column(db.DateTime(timezone=True))

    user = db.relationship("User")
    local_sources = db.relationship(
        "BrowserLocalSource", back_populates="browser_client"
    )


class BrowserLocalSource(db.Model):
    __tablename__ = "browser_local_sources"
    __table_args__ = (UniqueConstraint("browser_client_id", "storage_key"),)

    source_id = db.Column(
        db.String(32),
        db.ForeignKey("media_sources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    browser_client_id = db.Column(
        db.String(32),
        db.ForeignKey("browser_clients.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    storage_key = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255))
    last_confirmed_at = db.Column(db.DateTime(timezone=True))

    source = db.relationship("MediaSource", back_populates="browser_local")
    browser_client = db.relationship("BrowserClient", back_populates="local_sources")


class RoomMedia(db.Model):
    __tablename__ = "room_media"
    __table_args__ = (
        UniqueConstraint("room_id", "media_asset_id"),
        UniqueConstraint("room_id", "id", name="uq_room_media_room_id_id"),
    )

    id = db.Column(db.String(32), primary_key=True)
    room_id = db.Column(
        db.Integer,
        db.ForeignKey("watch_rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    media_asset_id = db.Column(
        db.String(32),
        db.ForeignKey("media_assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    added_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    room = db.relationship("WatchRoom", back_populates="library_items")
    asset = db.relationship("MediaAsset", back_populates="room_links")
    added_by = db.relationship("User")
    queue_entries = db.relationship(
        "QueueEntry",
        back_populates="room_media",
        cascade="all, delete-orphan",
        foreign_keys="QueueEntry.room_media_id",
        passive_deletes=True,
    )


class QueueEntry(db.Model):
    __tablename__ = "queue_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["room_id", "room_media_id"],
            ["room_media.room_id", "room_media.id"],
            name="fk_queue_entries_room_media_room",
            ondelete="CASCADE",
        ),
        UniqueConstraint("room_id", "position"),
        CheckConstraint("position >= 0", name="ck_queue_entries_position_nonnegative"),
    )

    id = db.Column(db.String(32), primary_key=True)
    room_id = db.Column(
        db.Integer,
        db.ForeignKey("watch_rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    room_media_id = db.Column(
        db.String(32),
        db.ForeignKey("room_media.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position = db.Column(db.Integer, nullable=False)
    added_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    room = db.relationship(
        "WatchRoom", back_populates="queue_entries", foreign_keys=[room_id]
    )
    room_media = db.relationship(
        "RoomMedia",
        back_populates="queue_entries",
        foreign_keys=[room_media_id],
        overlaps="queue_entries",
    )
    added_by = db.relationship("User")


class RoomMemberPermission(db.Model):
    __tablename__ = "room_member_permissions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["room_id", "user_id"],
            ["room_memberships.room_id", "room_memberships.user_id"],
            ondelete="CASCADE",
        ),
    )

    room_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, primary_key=True)
    permission = db.Column(db.String(40), primary_key=True)
    granted_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    granted_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    membership = db.relationship("RoomMembership", back_populates="permissions")
    granted_by = db.relationship("User")


class RoomRequest(db.Model):
    __tablename__ = "room_requests"
    __table_args__ = (
        UniqueConstraint("room_id", "requester_key", "client_request_id"),
        CheckConstraint(
            "requester_kind IN ('user', 'guest')",
            name="ck_room_requests_requester_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'dismissed', 'expired')",
            name="ck_room_requests_status",
        ),
    )

    id = db.Column(db.String(32), primary_key=True)
    room_id = db.Column(
        db.Integer,
        db.ForeignKey("watch_rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    requester_kind = db.Column(db.String(12), nullable=False)
    requester_key = db.Column(db.String(96), nullable=False)
    requester_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL")
    )
    requester_label = db.Column(db.String(80), nullable=False)
    request_type = db.Column(db.String(40), nullable=False)
    payload = db.Column(db.JSON, nullable=False, default=dict)
    status = db.Column(
        db.String(16), nullable=False, default="pending", server_default="pending"
    )
    client_request_id = db.Column(db.String(32), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at = db.Column(db.DateTime(timezone=True))
    resolved_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL")
    )
    resolved_at = db.Column(db.DateTime(timezone=True))

    room = db.relationship("WatchRoom")
    requester_user = db.relationship("User", foreign_keys=[requester_user_id])
    resolved_by = db.relationship("User", foreign_keys=[resolved_by_id])


class RoomCommandReceipt(db.Model):
    """Durable idempotency receipt for client-originated room commands."""

    __tablename__ = "room_command_receipts"
    __table_args__ = (
        UniqueConstraint("room_id", "actor_key", "client_action_id"),
        CheckConstraint(
            "actor_kind IN ('user', 'guest')",
            name="ck_room_command_receipts_actor_kind",
        ),
    )

    id = db.Column(db.String(32), primary_key=True)
    room_id = db.Column(
        db.Integer,
        db.ForeignKey("watch_rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_kind = db.Column(db.String(12), nullable=False)
    actor_key = db.Column(db.String(96), nullable=False)
    client_action_id = db.Column(db.String(64), nullable=False)
    command_type = db.Column(db.String(40), nullable=False)
    result = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    room = db.relationship("WatchRoom")


class MediaCleanupJob(db.Model):
    __tablename__ = "media_cleanup_jobs"
    __table_args__ = (
        UniqueConstraint("media_source_id", "operation"),
        CheckConstraint("attempts >= 0", name="ck_media_cleanup_jobs_attempts_nonnegative"),
    )

    id = db.Column(db.String(32), primary_key=True)
    media_source_id = db.Column(
        db.String(32), db.ForeignKey("media_sources.id", ondelete="SET NULL")
    )
    source_type = db.Column(db.String(32), nullable=False)
    operation = db.Column(
        db.String(24), nullable=False, default="delete", server_default="delete"
    )
    remote_id = db.Column(db.String(255))
    status = db.Column(
        db.String(16), nullable=False, default="pending", server_default="pending"
    )
    attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = db.Column(db.DateTime(timezone=True))
    last_error = db.Column(db.Text)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    media_source = db.relationship("MediaSource")


class ProviderWebhookEvent(db.Model):
    __tablename__ = "provider_webhook_events"
    __table_args__ = (UniqueConstraint("provider", "event_id"),)

    id = db.Column(db.String(32), primary_key=True)
    provider = db.Column(db.String(32), nullable=False)
    event_id = db.Column(db.String(255), nullable=False)
    event_type = db.Column(db.String(80), nullable=False)
    received_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at = db.Column(db.DateTime(timezone=True))


class RoomVideo(db.Model):
    """Stage 1 compatibility model retained until the contract migration."""

    __tablename__ = "room_videos"
    __table_args__ = (
        UniqueConstraint("room_id", "sort_order"),
        CheckConstraint("sort_order >= 0", name="ck_room_videos_sort_order_nonnegative"),
    )

    id = db.Column(db.String(32), primary_key=True)
    room_id = db.Column(
        db.Integer,
        db.ForeignKey("watch_rooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = db.Column(db.String(255), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False)
    mux_upload_id = db.Column(db.String(255), unique=True)
    asset_id = db.Column(db.String(255))
    playback_id = db.Column(db.String(255))
    url = db.Column(db.Text)
    duration = db.Column(db.Float)
    error = db.Column(db.Text)
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    room = db.relationship("WatchRoom", back_populates="videos", foreign_keys=[room_id])
    created_by = db.relationship("User")

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "mux_upload_id": self.mux_upload_id,
            "asset_id": self.asset_id,
            "playback_id": self.playback_id,
            "url": self.url,
            "duration": self.duration,
            "error": self.error,
        }
