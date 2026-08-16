from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from authorization import (
    Actor,
    AuthorizationError,
    Permission,
    lock_room_for,
    permissions_for,
)
from direct_urls import (
    DirectUrlError,
    PLAYABLE_PROBE_RESULTS,
    validate_direct_url,
    validate_probe_result,
)
from link_extract import EXTRACTOR_DIRECT, EXTRACTOR_YT_DLP, extract_clip
from models import QueueEntry, RoomMedia, RoomRequest, WatchRoom, db
from room_commands import (
    CommandError,
    ResourceNotFoundError,
    add_saved_media_to_queue,
    complete_current_queue_entry,
    create_direct_media,
    defer_queue_entry,
    new_id,
    queue_entry_for,
    remove_queue_entry,
    select_queue_entry,
    update_playback,
)


REQUEST_TTL = timedelta(minutes=15)
MAX_PENDING_PER_ROOM = 50
MAX_SEEK_SECONDS = 7 * 24 * 60 * 60
REQUEST_TYPES = frozenset(
    {
        "PLAY",
        "PAUSE",
        "SEEK",
        "NEXT",
        "SELECT_MEDIA",
        "ADD_DIRECT_URL",
        "ADD_SAVED_MEDIA",
        "REMOVE_QUEUE_ENTRY",
        "MOVE_TO_END",
    }
)
ACTION_PERMISSION = {
    "PLAY": Permission.CONTROL_PLAYBACK,
    "PAUSE": Permission.CONTROL_PLAYBACK,
    "SEEK": Permission.CONTROL_PLAYBACK,
    "NEXT": Permission.CONTROL_PLAYBACK,
    "SELECT_MEDIA": Permission.CONTROL_PLAYBACK,
    "ADD_DIRECT_URL": Permission.ADD_MEDIA,
    "ADD_SAVED_MEDIA": Permission.MANAGE_QUEUE,
    "REMOVE_QUEUE_ENTRY": Permission.MANAGE_QUEUE,
    "MOVE_TO_END": Permission.MANAGE_QUEUE,
}


class RequestValidationError(CommandError):
    pass


class RequestConflictError(CommandError):
    pass


def request_to_public(item: RoomRequest) -> dict:
    public_status = (
        "expired" if item.status == "pending" and _expired(item) else item.status
    )
    return {
        "id": item.id,
        "room_id": item.room_id,
        "requester_kind": item.requester_kind,
        "requester_identity": item.requester_key,
        "requester_label": item.requester_label,
        "request_type": item.request_type,
        "payload": item.payload,
        "status": public_status,
        "client_request_id": item.client_request_id,
        "created_at": _iso(item.created_at),
        "expires_at": _iso(item.expires_at),
        "resolved_by_id": item.resolved_by_id,
        "resolved_at": _iso(item.resolved_at),
    }


def visible_requests(room: WatchRoom, actor: Actor) -> list[RoomRequest]:
    expire_pending_requests(room.id)
    statement = select(RoomRequest).where(RoomRequest.room_id == room.id)
    if Permission.REVIEW_REQUESTS not in permissions_for(room, actor):
        statement = statement.where(RoomRequest.requester_key == actor.key)
    items = list(
        db.session.scalars(
            statement.order_by(RoomRequest.created_at.desc(), RoomRequest.id.desc())
        ).all()
    )
    latest = []
    seen = set()
    for item in items:
        if item.requester_key in seen:
            continue
        seen.add(item.requester_key)
        latest.append(item)
    return latest


def create_room_request(
    room_id: int,
    actor: Actor,
    requester_label: str,
    request_type: object,
    payload: object,
    client_request_id: object,
    *,
    require_https: bool,
) -> tuple[RoomRequest, bool]:
    room = lock_room_for(room_id, actor)
    normalized_type = str(request_type or "").upper()
    if normalized_type not in REQUEST_TYPES:
        raise RequestValidationError("Unsupported request type")
    client_id = _client_id(client_request_id)

    required = ACTION_PERMISSION[normalized_type]
    if required in permissions_for(room, actor):
        raise RequestValidationError(
            "You already have permission to perform this action directly"
        )

    validated_payload = validate_request_payload(
        room,
        normalized_type,
        payload,
        require_https=require_https,
    )
    existing = db.session.scalar(
        select(RoomRequest).where(
            RoomRequest.room_id == room.id,
            RoomRequest.requester_key == actor.key,
            RoomRequest.client_request_id == client_id,
        )
    )
    if existing is not None:
        if (
            existing.request_type != normalized_type
            or (existing.payload or {}) != validated_payload
        ):
            raise RequestConflictError(
                "client_request_id was already used for a different request"
            )
        return existing, False

    expire_pending_requests(room.id)
    now = datetime.now(UTC)
    pending_for_identity = list(
        db.session.scalars(
            select(RoomRequest).where(
                RoomRequest.room_id == room.id,
                RoomRequest.requester_key == actor.key,
                RoomRequest.status == "pending",
            )
        ).all()
    )
    for previous in pending_for_identity:
        previous.status = "dismissed"
        previous.resolved_at = now
    if pending_for_identity:
        db.session.flush()
    pending_for_room = db.session.scalar(
        select(func.count(RoomRequest.id)).where(
            RoomRequest.room_id == room.id,
            RoomRequest.status == "pending",
        )
    )
    if pending_for_room >= MAX_PENDING_PER_ROOM:
        raise RequestValidationError("This room has too many pending requests")

    item = RoomRequest(
        id=new_id(),
        room_id=room.id,
        requester_kind=actor.kind,
        requester_key=actor.key,
        requester_user_id=actor.user_id,
        requester_label=(requester_label or "Guest")[:80],
        request_type=normalized_type,
        payload=validated_payload,
        status="pending",
        client_request_id=client_id,
        created_at=now,
        expires_at=now + REQUEST_TTL,
    )
    db.session.add(item)
    db.session.flush()
    return item, True


def resolve_room_request(
    room_id: int,
    actor: Actor,
    request_id: str,
    resolution: object,
    *,
    require_https: bool,
) -> tuple[WatchRoom, RoomRequest]:
    room = lock_room_for(room_id, actor, Permission.REVIEW_REQUESTS)
    decision = str(resolution or "").lower()
    if decision not in {"approved", "denied", "dismissed"}:
        raise RequestValidationError("Invalid request resolution")
    item = db.session.scalar(
        select(RoomRequest)
        .where(RoomRequest.room_id == room.id, RoomRequest.id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise ResourceNotFoundError("Request not found")
    if item.status != "pending":
        raise RequestConflictError("Request is no longer pending")
    if _expired(item):
        item.status = "expired"
        item.resolved_at = datetime.now(UTC)
        db.session.flush()
        raise RequestConflictError("Request has expired")

    if decision == "approved":
        _execute_approved_request(
            room,
            actor,
            item,
            require_https=require_https,
        )

    item.status = decision
    item.resolved_by_id = actor.user_id
    item.resolved_at = datetime.now(UTC)
    db.session.flush()
    return room, item


def validate_request_payload(
    room: WatchRoom,
    request_type: str,
    payload: object,
    *,
    require_https: bool,
) -> dict:
    if payload is None:
        body = {}
    elif isinstance(payload, dict):
        body = payload
    else:
        raise RequestValidationError("Request payload must be an object")

    if request_type in {"PLAY", "PAUSE", "NEXT"}:
        _require_keys(body, set())
        return {}
    if request_type == "SEEK":
        _require_keys(body, {"position"})
        return {"position": _seek_position(body.get("position"))}
    if request_type == "SELECT_MEDIA":
        _require_keys(body, {"queue_entry_id"})
        entry_id = _stable_id(body.get("queue_entry_id"), "queue entry")
        if queue_entry_for(room.id, entry_id) is None:
            raise ResourceNotFoundError("Queue entry not found in this room")
        return {"queue_entry_id": entry_id}
    if request_type == "REMOVE_QUEUE_ENTRY":
        _require_keys(body, {"queue_entry_id"})
        entry_id = _stable_id(body.get("queue_entry_id"), "queue entry")
        if queue_entry_for(room.id, entry_id) is None:
            raise ResourceNotFoundError("Queue entry not found in this room")
        return {"queue_entry_id": entry_id}
    if request_type == "MOVE_TO_END":
        _require_keys(body, {"queue_entry_id"})
        entry_id = _stable_id(body.get("queue_entry_id"), "queue entry")
        if queue_entry_for(room.id, entry_id) is None:
            raise ResourceNotFoundError("Queue entry not found in this room")
        return {"queue_entry_id": entry_id}
    if request_type == "ADD_SAVED_MEDIA":
        _require_keys(body, {"room_media_id"})
        room_media_id = _stable_id(body.get("room_media_id"), "saved media")
        exists = db.session.scalar(
            select(RoomMedia.id).where(
                RoomMedia.room_id == room.id, RoomMedia.id == room_media_id
            )
        )
        if exists is None:
            raise ResourceNotFoundError("Saved media not found in this room")
        return {"room_media_id": room_media_id}
    if request_type == "ADD_DIRECT_URL":
        allowed = {"title", "url"}
        if "probe_result" in body:
            allowed.add("probe_result")
        _require_keys(body, allowed)
        if "url" not in body:
            raise RequestValidationError("An absolute media URL is required")
        try:
            validated = validate_direct_url(
                body.get("url"), require_https=require_https
            )
            probe_result = validate_probe_result(body.get("probe_result"))
        except ValueError as exc:
            raise RequestValidationError(str(exc)) from exc
        title = _media_title(body.get("title"))
        return {
            "url": validated.normalized,
            "title": title,
            "probe_result": probe_result,
        }
    raise RequestValidationError("Unsupported request type")


def expire_pending_requests(room_id: int) -> int:
    now = datetime.now(UTC)
    items = db.session.scalars(
        select(RoomRequest).where(
            RoomRequest.room_id == room_id,
            RoomRequest.status == "pending",
            RoomRequest.expires_at.is_not(None),
            RoomRequest.expires_at <= now,
        )
    ).all()
    for item in items:
        item.status = "expired"
        item.resolved_at = now
    if items:
        db.session.flush()
    return len(items)


def _execute_approved_request(
    room: WatchRoom,
    actor: Actor,
    item: RoomRequest,
    *,
    require_https: bool,
) -> None:
    payload = item.payload or {}
    request_type = item.request_type
    if request_type in {"PLAY", "PAUSE", "SEEK"}:
        position = (
            payload["position"]
            if request_type == "SEEK"
            else _authoritative_position(room)
        )
        update_playback(
            room.id,
            actor,
            request_type.lower(),
            position,
            expected_playback_version=room.playback_version,
        )
        return
    if request_type == "NEXT":
        if room.current_queue_entry_id is None:
            raise ResourceNotFoundError("There is no current queue entry")
        complete_current_queue_entry(
            room.id,
            actor,
            room.current_queue_entry_id,
            expected_playback_version=room.playback_version,
        )
        return
    if request_type == "SELECT_MEDIA":
        select_queue_entry(
            room.id,
            actor,
            payload["queue_entry_id"],
            expected_playback_version=room.playback_version,
        )
        return
    if request_type == "ADD_SAVED_MEDIA":
        add_saved_media_to_queue(
            room.id,
            actor,
            payload["room_media_id"],
            expected_queue_version=room.queue_version,
        )
        return
    if request_type == "REMOVE_QUEUE_ENTRY":
        remove_queue_entry(
            room.id,
            actor,
            payload["queue_entry_id"],
            expected_queue_version=room.queue_version,
        )
        return
    if request_type == "MOVE_TO_END":
        defer_queue_entry(
            room.id,
            actor,
            payload["queue_entry_id"],
            expected_queue_version=room.queue_version,
        )
        return
    if request_type == "ADD_DIRECT_URL":
        try:
            validated = validate_direct_url(payload["url"], require_https=require_https)
            probe_result = validate_probe_result(payload.get("probe_result"))
            extractor = EXTRACTOR_DIRECT
            duration = None
            observed_content_type = None
            title = payload["title"]
            if probe_result not in PLAYABLE_PROBE_RESULTS:
                extracted = extract_clip(
                    validated.normalized, require_https=require_https
                )
                probe_result = "playable"
                extractor = EXTRACTOR_YT_DLP
                duration = extracted.duration
                observed_content_type = extracted.content_type
                if title in {None, "", "Direct media"}:
                    title = extracted.title
            create_direct_media(
                room.id,
                actor,
                title,
                validated,
                probe_result=probe_result,
                extractor=extractor,
                duration=duration,
                observed_content_type=observed_content_type,
            )
        except DirectUrlError as exc:
            raise RequestValidationError(str(exc)) from exc
        return
    raise RequestValidationError("Unsupported request type")


def _client_id(value: object) -> str:
    if not isinstance(value, str):
        raise RequestValidationError("A valid client_request_id is required")
    candidate = value
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,32}", candidate):
        raise RequestValidationError("A valid client_request_id is required")
    return candidate


def _stable_id(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RequestValidationError(f"A stable {label} ID is required")
    candidate = value
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", candidate):
        raise RequestValidationError(f"A stable {label} ID is required")
    return candidate


def _require_keys(body: dict, allowed: set[str]) -> None:
    unexpected = set(body) - allowed
    if unexpected:
        raise RequestValidationError(
            f"Unexpected request payload field: {sorted(unexpected)[0]}"
        )


def _media_title(value: object) -> str:
    if value is None or value == "":
        return "Direct media"
    if not isinstance(value, str):
        raise RequestValidationError("Media title must be a string")
    title = value.strip()
    if len(title) > 255:
        raise RequestValidationError("Media title is too long")
    return title or "Direct media"


def _seek_position(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestValidationError("Seek position must be a finite number")
    position = float(value)
    if not math.isfinite(position) or position < 0 or position > MAX_SEEK_SECONDS:
        raise RequestValidationError("Seek position is outside the allowed range")
    return position


def _expired(item: RoomRequest) -> bool:
    if item.expires_at is None:
        return False
    expires_at = item.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= datetime.now(UTC)


def _authoritative_position(room: WatchRoom) -> float:
    position = max(0.0, float(room.position or 0.0))
    if not room.playing or room.playback_updated_at is None:
        return position
    updated_at = room.playback_updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return position + max(0.0, (datetime.now(UTC) - updated_at).total_seconds())


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()
