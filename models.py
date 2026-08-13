from __future__ import annotations

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, UniqueConstraint, func
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
    current_video_id = db.Column(
        db.String(32),
        db.ForeignKey(
            "room_videos.id",
            name="fk_watch_rooms_current_video_id",
            ondelete="SET NULL",
            use_alter=True,
        ),
    )
    playing = db.Column(db.Boolean, nullable=False, default=False, server_default="false")
    position = db.Column(db.Float, nullable=False, default=0.0, server_default="0")
    playback_updated_at = db.Column(db.DateTime(timezone=True))
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


class RoomVideo(db.Model):
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
