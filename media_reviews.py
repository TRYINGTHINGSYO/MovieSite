from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from authorization import Actor, can_view_room
from models import MediaReview, RoomMedia, WatchRoom, db
from room_commands import CommandError, ResourceNotFoundError, new_id


MAX_REVIEW_COMMENT = 280


def upsert_media_review(
    room_id: int,
    actor: Actor,
    reviewer_label: str,
    room_media_id: str,
    *,
    rating: object,
    comment: object = None,
) -> MediaReview:
    room = db.session.get(WatchRoom, room_id)
    if room is None or not can_view_room(room, actor):
        raise ResourceNotFoundError("Saved media not found in this room")
    if not actor.is_user and not actor.is_guest:
        raise CommandError("Sign in or join as a guest to leave a review")
    room_media = db.session.scalar(
        select(RoomMedia).where(
            RoomMedia.id == room_media_id, RoomMedia.room_id == room.id
        )
    )
    if room_media is None:
        raise ResourceNotFoundError("Saved media not found in this room")
    score = _rating(rating)
    text = _comment(comment)
    existing = db.session.scalar(
        select(MediaReview).where(
            MediaReview.room_media_id == room_media.id,
            MediaReview.actor_key == actor.key,
        )
    )
    if existing is None:
        existing = MediaReview(
            id=new_id(),
            room_media_id=room_media.id,
            actor_kind=actor.kind,
            actor_key=actor.key,
            reviewer_label=(reviewer_label or "Viewer")[:80],
            user_id=actor.user_id,
            rating=score,
            comment=text,
        )
        db.session.add(existing)
    else:
        existing.rating = score
        existing.comment = text
        existing.reviewer_label = (reviewer_label or existing.reviewer_label)[:80]
        existing.updated_at = datetime.now(UTC)
    db.session.flush()
    return existing


def reviews_payload(room_media: RoomMedia, actor_key: str | None = None) -> dict:
    items = list(room_media.reviews or [])
    items.sort(key=lambda item: ((item.updated_at or item.created_at), item.id), reverse=True)
    count = len(items)
    average = round(sum(item.rating for item in items) / count, 1) if count else None
    mine = next((item for item in items if item.actor_key == actor_key), None)
    return {
        "average": average,
        "count": count,
        "mine": review_to_public(mine) if mine else None,
        "latest": [review_to_public(item) for item in items[:5]],
    }


def review_to_public(item: MediaReview | None) -> dict | None:
    if item is None:
        return None
    return {
        "id": item.id,
        "label": item.reviewer_label,
        "rating": item.rating,
        "comment": item.comment or "",
    }


def _rating(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CommandError("Rating must be a whole number from 1 to 5")
    if value < 1 or value > 5:
        raise CommandError("Rating must be a whole number from 1 to 5")
    return value


def _comment(value: object) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise CommandError("Review comment must be a string")
    text = value.strip()
    if len(text) > MAX_REVIEW_COMMENT:
        raise CommandError("Review comment is too long")
    return text or None
