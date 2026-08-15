from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sqlalchemy import select

from models import (
    RoomMemberPermission,
    RoomMembership,
    User,
    WatchRoom,
    db,
)


class Permission(str, Enum):
    CONTROL_PLAYBACK = "CONTROL_PLAYBACK"
    ADD_MEDIA = "ADD_MEDIA"
    MANAGE_QUEUE = "MANAGE_QUEUE"
    MANAGE_MEDIA = "MANAGE_MEDIA"
    MANAGE_MEMBERS = "MANAGE_MEMBERS"
    MANAGE_ROOM = "MANAGE_ROOM"
    REVIEW_REQUESTS = "REVIEW_REQUESTS"


ALL_PERMISSIONS = frozenset(Permission)
OWNER_ONLY_GRANTS = frozenset({Permission.MANAGE_MEMBERS, Permission.MANAGE_ROOM})


class AuthorizationError(Exception):
    pass


class RoomUnavailableError(Exception):
    pass


@dataclass(frozen=True)
class Actor:
    kind: str
    user_id: int | None = None
    guest_id: str | None = None
    browser_client_id: str | None = None

    @property
    def is_user(self) -> bool:
        return self.kind == "user" and self.user_id is not None

    @property
    def is_guest(self) -> bool:
        return self.kind == "guest" and bool(self.guest_id)

    @property
    def key(self) -> str:
        if self.is_user:
            return f"user:{self.user_id}"
        if self.is_guest:
            return f"guest:{self.guest_id}"
        return "anonymous"


def actor_for_user(
    user: User | object, browser_client_id: str | None = None
) -> Actor:
    if not getattr(user, "is_authenticated", False):
        return Actor(kind="anonymous")
    return Actor(
        kind="user",
        user_id=int(getattr(user, "id")),
        browser_client_id=browser_client_id,
    )


def actor_for_guest(guest_id: str, browser_client_id: str | None = None) -> Actor:
    return Actor(
        kind="guest", guest_id=guest_id, browser_client_id=browser_client_id
    )


def membership_for(room_id: int, user_id: int | None) -> RoomMembership | None:
    if user_id is None:
        return None
    return db.session.get(RoomMembership, (room_id, user_id))


def can_view_room(room: WatchRoom | None, actor: Actor) -> bool:
    return bool(
        room
        and room.archived_at is None
        and (actor.is_user or actor.is_guest)
    )


def permissions_for(room: WatchRoom, actor: Actor) -> frozenset[Permission]:
    if actor.is_user and actor.user_id == room.owner_id:
        return ALL_PERMISSIONS
    if not actor.is_user or membership_for(room.id, actor.user_id) is None:
        return frozenset()

    values = db.session.scalars(
        select(RoomMemberPermission.permission).where(
            RoomMemberPermission.room_id == room.id,
            RoomMemberPermission.user_id == actor.user_id,
        )
    ).all()
    valid = []
    for value in values:
        try:
            valid.append(Permission(value))
        except ValueError:
            continue
    return frozenset(valid)


def require_permission(room: WatchRoom, actor: Actor, permission: Permission) -> None:
    if permission not in permissions_for(room, actor):
        raise AuthorizationError(f"{permission.value} permission required")


def lock_room_for(
    room_id: int,
    actor: Actor,
    permission: Permission | None = None,
    *,
    allow_archived: bool = False,
) -> WatchRoom:
    room = db.session.scalar(
        select(WatchRoom)
        .where(WatchRoom.id == room_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if room is None or (room.archived_at is not None and not allow_archived):
        raise RoomUnavailableError("Room not found or inactive")
    if not can_view_room(room, actor) and actor.user_id != room.owner_id:
        raise AuthorizationError("Room access denied")
    if permission is not None:
        require_permission(room, actor, permission)
    return room


def grant_permission(
    room_id: int,
    actor: Actor,
    target_user_id: int,
    permission: Permission,
) -> RoomMemberPermission:
    room = lock_room_for(room_id, actor, Permission.MANAGE_MEMBERS)
    _validate_permission_change(room, actor, target_user_id, permission)
    existing = db.session.get(
        RoomMemberPermission, (room.id, target_user_id, permission.value)
    )
    if existing:
        return existing
    grant = RoomMemberPermission(
        room_id=room.id,
        user_id=target_user_id,
        permission=permission.value,
        granted_by_id=actor.user_id,
    )
    db.session.add(grant)
    db.session.flush()
    return grant


def revoke_permission(
    room_id: int,
    actor: Actor,
    target_user_id: int,
    permission: Permission,
) -> bool:
    room = lock_room_for(room_id, actor, Permission.MANAGE_MEMBERS)
    _validate_permission_change(room, actor, target_user_id, permission)
    grant = db.session.get(
        RoomMemberPermission, (room.id, target_user_id, permission.value)
    )
    if grant is None:
        return False
    db.session.delete(grant)
    db.session.flush()
    return True


def _validate_permission_change(
    room: WatchRoom,
    actor: Actor,
    target_user_id: int,
    permission: Permission,
) -> None:
    if target_user_id == room.owner_id:
        raise AuthorizationError("The room owner permissions are implicit")
    if membership_for(room.id, target_user_id) is None:
        raise AuthorizationError("Target user is not a room member")
    if actor.user_id == target_user_id and actor.user_id != room.owner_id:
        raise AuthorizationError("Members cannot change their own permissions")
    if actor.user_id == room.owner_id:
        return
    actor_permissions = permissions_for(room, actor)
    if permission in OWNER_ONLY_GRANTS or permission not in actor_permissions:
        raise AuthorizationError("Cannot grant or revoke that permission")
