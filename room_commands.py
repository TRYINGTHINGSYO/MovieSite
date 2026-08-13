from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select

from authorization import Actor, Permission, lock_room_for
from media_sources import MUX_UPLOAD
from models import (
    MediaAsset,
    MediaCleanupJob,
    MediaSource,
    MuxMediaSource,
    QueueEntry,
    RoomMedia,
    WatchRoom,
    db,
)


class CommandError(Exception):
    pass


class VersionConflictError(CommandError):
    pass


class ResourceNotFoundError(CommandError):
    pass


def new_id() -> str:
    return uuid.uuid4().hex


def queue_entries_for(room_id: int) -> list[QueueEntry]:
    return list(
        db.session.scalars(
            select(QueueEntry)
            .where(QueueEntry.room_id == room_id)
            .order_by(QueueEntry.position, QueueEntry.created_at, QueueEntry.id)
        ).all()
    )


def queue_entry_for(room_id: int, entry_id: str) -> QueueEntry | None:
    return db.session.scalar(
        select(QueueEntry).where(
            QueueEntry.room_id == room_id, QueueEntry.id == entry_id
        )
    )


def mux_source_for_entry(entry: QueueEntry) -> tuple[MediaSource, MuxMediaSource] | None:
    for source in entry.room_media.asset.sources:
        if source.source_type == MUX_UPLOAD and source.mux is not None:
            return source, source.mux
    return None


def create_mux_media(
    room_id: int,
    actor: Actor,
    title: str,
    *,
    byte_size: int | None = None,
    asset_id: str | None = None,
    source_id: str | None = None,
    room_media_id: str | None = None,
    queue_entry_id: str | None = None,
) -> tuple[WatchRoom, QueueEntry, MediaSource, MuxMediaSource]:
    room = lock_room_for(room_id, actor, Permission.ADD_MEDIA)
    next_position = (
        db.session.scalar(
            select(func.coalesce(func.max(QueueEntry.position), -1)).where(
                QueueEntry.room_id == room.id
            )
        )
        + 1
    )

    asset = MediaAsset(
        id=asset_id or new_id(),
        title=title,
        created_by_id=actor.user_id,
    )
    source = MediaSource(
        id=source_id or new_id(),
        asset=asset,
        source_type=MUX_UPLOAD,
        status="creating",
        byte_size=byte_size,
    )
    mux = MuxMediaSource(source=source)
    room_media = RoomMedia(
        id=room_media_id or new_id(),
        room_id=room.id,
        asset=asset,
        added_by_id=actor.user_id,
    )
    entry = QueueEntry(
        id=queue_entry_id or new_id(),
        room_id=room.id,
        room_media=room_media,
        position=next_position,
        added_by_id=actor.user_id,
    )
    db.session.add_all([asset, source, mux, room_media, entry])
    db.session.flush()

    room.queue_version += 1
    if room.current_queue_entry_id is None:
        room.current_queue_entry_id = entry.id
        room.playing = True
        room.position = 0.0
        room.playback_updated_at = datetime.now(UTC)
        room.playback_version += 1
    return room, entry, source, mux


def add_saved_media_to_queue(
    room_id: int,
    actor: Actor,
    room_media_id: str,
    *,
    expected_queue_version: int | None = None,
    queue_entry_id: str | None = None,
) -> tuple[WatchRoom, QueueEntry]:
    room = lock_room_for(room_id, actor, Permission.ADD_MEDIA)
    _check_version(room.queue_version, expected_queue_version, "queue")
    room_media = db.session.scalar(
        select(RoomMedia).where(
            RoomMedia.id == room_media_id, RoomMedia.room_id == room.id
        )
    )
    if room_media is None:
        raise ResourceNotFoundError("Saved media not found in this room")
    next_position = (
        db.session.scalar(
            select(func.coalesce(func.max(QueueEntry.position), -1)).where(
                QueueEntry.room_id == room.id
            )
        )
        + 1
    )
    entry = QueueEntry(
        id=queue_entry_id or new_id(),
        room_id=room.id,
        room_media_id=room_media.id,
        position=next_position,
        added_by_id=actor.user_id,
    )
    db.session.add(entry)
    db.session.flush()
    room.queue_version += 1
    if room.current_queue_entry_id is None:
        room.current_queue_entry_id = entry.id
        room.playing = False
        room.position = 0.0
        room.playback_updated_at = datetime.now(UTC)
        room.playback_version += 1
    return room, entry


def reorder_queue(
    room_id: int,
    actor: Actor,
    ordered_entry_ids: list[str],
    *,
    expected_queue_version: int | None,
) -> WatchRoom:
    room = lock_room_for(room_id, actor, Permission.MANAGE_QUEUE)
    _check_version(room.queue_version, expected_queue_version, "queue")
    entries = queue_entries_for(room.id)
    current_ids = [entry.id for entry in entries]
    if len(ordered_entry_ids) != len(set(ordered_entry_ids)):
        raise CommandError("Queue order contains duplicate entry IDs")
    if set(ordered_entry_ids) != set(current_ids):
        raise VersionConflictError("Queue contents changed before reorder")

    by_id = {entry.id: entry for entry in entries}
    temporary_offset = max([entry.position for entry in entries], default=-1) + len(entries) + 1
    for entry in entries:
        entry.position += temporary_offset
    db.session.flush()
    for position, entry_id in enumerate(ordered_entry_ids):
        by_id[entry_id].position = position
    db.session.flush()
    room.queue_version += 1
    return room


def remove_queue_entry(
    room_id: int,
    actor: Actor,
    entry_id: str,
    *,
    expected_queue_version: int | None = None,
) -> WatchRoom:
    room = lock_room_for(room_id, actor, Permission.MANAGE_QUEUE)
    _check_version(room.queue_version, expected_queue_version, "queue")
    entry = queue_entry_for(room.id, entry_id)
    if entry is None:
        raise ResourceNotFoundError("Queue entry not found")
    _remove_entry_and_advance(room, entry, continue_playing=room.playing)
    return room


def select_queue_entry(
    room_id: int,
    actor: Actor,
    entry_id: str,
    *,
    expected_playback_version: int | None = None,
) -> WatchRoom:
    room = lock_room_for(room_id, actor, Permission.CONTROL_PLAYBACK)
    _check_version(room.playback_version, expected_playback_version, "playback")
    entry = queue_entry_for(room.id, entry_id)
    if entry is None:
        raise ResourceNotFoundError("Queue entry not found")
    room.current_queue_entry_id = entry.id
    room.playing = False
    room.position = 0.0
    room.playback_updated_at = datetime.now(UTC)
    room.playback_version += 1
    return room


def update_playback(
    room_id: int,
    actor: Actor,
    action: str,
    position: float,
    *,
    expected_playback_version: int | None = None,
    playing: bool | None = None,
) -> WatchRoom:
    room = lock_room_for(room_id, actor, Permission.CONTROL_PLAYBACK)
    _check_version(room.playback_version, expected_playback_version, "playback")
    position = _valid_position(position)
    if action == "play":
        if room.current_queue_entry_id is None:
            raise CommandError("Cannot play an empty queue")
        room.playing = True
    elif action == "pause":
        room.playing = False
    elif action == "seek":
        pass
    elif action == "sync":
        if playing is not None:
            room.playing = bool(playing)
    else:
        raise CommandError("Unknown playback action")
    room.position = position
    room.playback_updated_at = datetime.now(UTC)
    room.playback_version += 1
    return room


def complete_current_queue_entry(
    room_id: int,
    actor: Actor,
    entry_id: str,
    *,
    expected_playback_version: int | None = None,
) -> WatchRoom:
    room = lock_room_for(room_id, actor, Permission.CONTROL_PLAYBACK)
    _check_version(room.playback_version, expected_playback_version, "playback")
    if room.current_queue_entry_id != entry_id:
        raise VersionConflictError("Playback has already advanced")
    entry = queue_entry_for(room.id, entry_id)
    if entry is None:
        raise ResourceNotFoundError("Current queue entry not found")
    _remove_entry_and_advance(room, entry, continue_playing=True)
    return room


def schedule_mux_cleanup(
    media_source_id: str,
    remote_id: str,
    *,
    operation: str = "delete_upload",
) -> MediaCleanupJob:
    existing = db.session.scalar(
        select(MediaCleanupJob).where(
            MediaCleanupJob.media_source_id == media_source_id,
            MediaCleanupJob.operation == operation,
        )
    )
    if existing:
        return existing
    job = MediaCleanupJob(
        id=new_id(),
        media_source_id=media_source_id,
        source_type=MUX_UPLOAD,
        operation=operation,
        remote_id=remote_id,
        status="pending",
    )
    db.session.add(job)
    db.session.flush()
    return job


def _remove_entry_and_advance(
    room: WatchRoom, entry: QueueEntry, *, continue_playing: bool
) -> None:
    entries = queue_entries_for(room.id)
    try:
        removed_index = next(index for index, item in enumerate(entries) if item.id == entry.id)
    except StopIteration as exc:
        raise ResourceNotFoundError("Queue entry not found") from exc
    remaining = [item for item in entries if item.id != entry.id]
    if room.current_queue_entry_id == entry.id:
        if remaining:
            next_entry = remaining[min(removed_index, len(remaining) - 1)]
            room.current_queue_entry_id = next_entry.id
            room.playing = bool(continue_playing)
        else:
            room.current_queue_entry_id = None
            room.playing = False
        room.position = 0.0
        room.playback_updated_at = datetime.now(UTC)
        room.playback_version += 1
    db.session.delete(entry)
    room.queue_version += 1
    db.session.flush()


def _check_version(
    actual: int, expected: int | None, resource_name: str
) -> None:
    if expected is not None and expected != actual:
        raise VersionConflictError(
            f"Stale {resource_name} version: expected {expected}, current {actual}"
        )


def _valid_position(value: float) -> float:
    try:
        position = float(value)
    except (TypeError, ValueError) as exc:
        raise CommandError("Playback position must be a number") from exc
    if not math.isfinite(position) or position < 0:
        raise CommandError("Playback position must be finite and nonnegative")
    return position
