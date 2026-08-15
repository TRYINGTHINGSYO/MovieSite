from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from authorization import (
    Actor,
    AuthorizationError,
    Permission,
    lock_room_for,
    require_permission,
)
from media_sources import BROWSER_LOCAL
from models import (
    BrowserClient,
    BrowserLocalSource,
    MediaAsset,
    MediaSource,
    RoomMedia,
    WatchRoom,
    db,
)
from room_commands import CommandError, new_id


BROWSER_TOKEN_PATTERN = re.compile(r"[a-f0-9]{64}")
STORAGE_KEY_PATTERN = re.compile(r"[a-f0-9]{64}")
MAX_LOCAL_MEDIA_BYTES = (1 << 53) - 1
MAX_LOCAL_DURATION_SECONDS = 7 * 24 * 60 * 60
MAX_BROWSER_CLIENTS_PER_USER = 32


class BrowserClientProofError(AuthorizationError):
    pass


class BrowserLocalConflictError(CommandError):
    pass


def browser_token_digest(token: object) -> str:
    if not isinstance(token, str) or not BROWSER_TOKEN_PATTERN.fullmatch(token):
        raise BrowserClientProofError("A valid browser client proof is required")
    return hashlib.sha256(bytes.fromhex(token)).hexdigest()


def register_browser_client(user_id: int, token: object) -> BrowserClient:
    digest = browser_token_digest(token)
    browser_client = db.session.scalar(
        select(BrowserClient).where(
            BrowserClient.user_id == user_id,
            BrowserClient.client_key == digest,
        )
    )
    now = datetime.now(UTC)
    if browser_client is None:
        active_clients = db.session.scalar(
            select(func.count(BrowserClient.id)).where(
                BrowserClient.user_id == user_id,
                BrowserClient.revoked_at.is_(None),
            )
        )
        if active_clients >= MAX_BROWSER_CLIENTS_PER_USER:
            raise CommandError("Browser client limit reached for this account")
        browser_client = BrowserClient(
            id=new_id(),
            user_id=user_id,
            client_key=digest,
            last_seen_at=now,
        )
        try:
            with db.session.begin_nested():
                db.session.add(browser_client)
                db.session.flush()
        except IntegrityError:
            # Multiple tabs can register the same durable token concurrently.
            # Treat the unique-key race as the same idempotent registration.
            browser_client = db.session.scalar(
                select(BrowserClient).where(
                    BrowserClient.user_id == user_id,
                    BrowserClient.client_key == digest,
                )
            )
            if browser_client is None:
                raise
            if browser_client.revoked_at is not None:
                raise BrowserClientProofError("This browser client has been revoked")
            browser_client.last_seen_at = now
    elif browser_client.revoked_at is not None:
        raise BrowserClientProofError("This browser client has been revoked")
    else:
        browser_client.last_seen_at = now
    return browser_client


def prove_browser_client(user_id: int, token: object) -> BrowserClient:
    digest = browser_token_digest(token)
    browser_client = db.session.scalar(
        select(BrowserClient).where(
            BrowserClient.user_id == user_id,
            BrowserClient.client_key == digest,
            BrowserClient.revoked_at.is_(None),
        )
    )
    if browser_client is None:
        raise BrowserClientProofError("Browser client proof was rejected")
    return browser_client


def create_browser_local_media(
    room_id: int,
    actor: Actor,
    browser_client: BrowserClient,
    *,
    storage_key: object,
    original_filename: object,
    mime_type: object,
    byte_size: object,
    duration: object = None,
) -> tuple[
    WatchRoom,
    RoomMedia,
    MediaSource,
    BrowserLocalSource,
    bool,
]:
    room = lock_room_for(room_id, actor)
    if (
        not actor.is_user
        or actor.browser_client_id != browser_client.id
        or browser_client.user_id != actor.user_id
    ):
        raise BrowserClientProofError("Browser client ownership was rejected")
    if browser_client.revoked_at is not None:
        raise BrowserClientProofError("This browser client has been revoked")

    clean_storage_key = _storage_key(storage_key)
    clean_filename = _filename(original_filename)
    clean_mime_type = _mime_type(mime_type)
    clean_byte_size = _byte_size(byte_size)
    clean_duration = _duration(duration)

    existing = db.session.scalar(
        select(BrowserLocalSource).where(
            BrowserLocalSource.browser_client_id == browser_client.id,
            BrowserLocalSource.storage_key == clean_storage_key,
        )
    )
    if existing is not None:
        source = existing.source
        room_media = db.session.scalar(
            select(RoomMedia).where(
                RoomMedia.room_id == room.id,
                RoomMedia.media_asset_id == source.media_asset_id,
            )
        )
        if room_media is None:
            raise BrowserLocalConflictError(
                "That browser storage key is already registered elsewhere"
            )
        if (
            source.byte_size != clean_byte_size
            or (source.mime_type or "") != clean_mime_type
            or (existing.original_filename or "") != clean_filename
        ):
            raise BrowserLocalConflictError(
                "That browser storage key was reused with different metadata"
            )
        existing.last_confirmed_at = datetime.now(UTC)
        source.status = "ready"
        source.error = None
        return room, room_media, source, existing, False

    require_permission(room, actor, Permission.ADD_MEDIA)
    asset = MediaAsset(
        id=new_id(),
        title=clean_filename,
        duration=clean_duration,
        created_by_id=actor.user_id,
    )
    source = MediaSource(
        id=new_id(),
        asset=asset,
        source_type=BROWSER_LOCAL,
        status="ready",
        mime_type=clean_mime_type,
        byte_size=clean_byte_size,
    )
    local_source = BrowserLocalSource(
        source=source,
        browser_client_id=browser_client.id,
        storage_key=clean_storage_key,
        original_filename=clean_filename,
        last_confirmed_at=datetime.now(UTC),
    )
    room_media = RoomMedia(
        id=new_id(),
        room_id=room.id,
        asset=asset,
        added_by_id=actor.user_id,
    )
    db.session.add_all([asset, source, local_source, room_media])
    db.session.flush()
    return room, room_media, source, local_source, True


def update_browser_local_availability(
    room_id: int,
    actor: Actor,
    browser_client: BrowserClient,
    source_id: str,
    *,
    available: object,
) -> tuple[WatchRoom, MediaSource, BrowserLocalSource]:
    room = lock_room_for(room_id, actor)
    if not isinstance(available, bool):
        raise CommandError("available must be a boolean")
    source = db.session.scalar(
        select(MediaSource)
        .join(RoomMedia, RoomMedia.media_asset_id == MediaSource.media_asset_id)
        .where(
            RoomMedia.room_id == room.id,
            MediaSource.id == source_id,
            MediaSource.source_type == BROWSER_LOCAL,
            MediaSource.deleted_at.is_(None),
        )
    )
    if source is None or source.browser_local is None:
        raise BrowserClientProofError("Browser-local source ownership was rejected")
    local_source = source.browser_local
    if (
        not actor.is_user
        or actor.browser_client_id != browser_client.id
        or browser_client.user_id != actor.user_id
        or local_source.browser_client_id != browser_client.id
    ):
        raise BrowserClientProofError("Browser-local source ownership was rejected")

    if available:
        source.status = "ready"
        source.error = None
        local_source.last_confirmed_at = datetime.now(UTC)
    else:
        source.status = "missing"
        source.error = "Local data is missing on its owning browser"
    db.session.flush()
    return room, source, local_source


def _storage_key(value: object) -> str:
    if not isinstance(value, str) or not STORAGE_KEY_PATTERN.fullmatch(value):
        raise CommandError("A valid opaque storage key is required")
    return value


def _filename(value: object) -> str:
    if not isinstance(value, str):
        raise CommandError("Local media filename must be a string")
    if len(value) > 255 or any(ord(character) < 32 for character in value):
        raise CommandError("Invalid local media filename")
    filename = secure_filename(value.strip())[:255]
    if not filename:
        raise CommandError("Invalid local media filename")
    return filename


def _mime_type(value: object) -> str:
    if value in (None, ""):
        return "application/octet-stream"
    if not isinstance(value, str) or len(value) > 127:
        raise CommandError("Invalid local media MIME type")
    mime_type = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*", mime_type):
        raise CommandError("Invalid local media MIME type")
    if not (mime_type.startswith("video/") or mime_type == "application/octet-stream"):
        raise CommandError("Local media must be a video")
    return mime_type


def _byte_size(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CommandError("Local media byte size must be an integer")
    if value <= 0 or value > MAX_LOCAL_MEDIA_BYTES:
        raise CommandError("Invalid local media byte size")
    return value


def _duration(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise CommandError("Invalid local media duration")
    duration = float(value)
    if (
        not math.isfinite(duration)
        or duration <= 0
        or duration > MAX_LOCAL_DURATION_SECONDS
    ):
        raise CommandError("Invalid local media duration")
    return duration
