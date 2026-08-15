from unittest.mock import Mock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from authorization import (
    ALL_PERMISSIONS,
    AuthorizationError,
    Permission,
    actor_for_user,
    grant_permission,
    permissions_for,
    revoke_permission,
)
from media_sources import public_source, room_media_to_public
from models import (
    BrowserClient,
    BrowserLocalSource,
    DirectUrlSource,
    MediaAsset,
    MediaCleanupJob,
    MediaSource,
    MuxMediaSource,
    QueueEntry,
    RoomMedia,
    RoomMembership,
    User,
    WatchRoom,
    db,
)
from movie_theater import socketio
from room_commands import (
    ResourceNotFoundError,
    VersionConflictError,
    add_saved_media_to_queue,
    complete_current_queue_entry,
    create_mux_media,
    remove_queue_entry,
    reorder_queue,
    update_playback,
)

from test_rooms import add_ready_media, create_room


def test_owner_is_implicit_and_new_member_has_no_permissions(app, client, register):
    register(email="owner@example.com")
    code = create_room(client)
    member_client = app.test_client()
    member_client.post(
        "/register",
        data={"email": "member@example.com", "password": "correct-horse"},
    )
    member_client.post("/join", data={"code": code})

    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        owner = db.session.scalar(select(User).where(User.email == "owner@example.com"))
        member = db.session.scalar(select(User).where(User.email == "member@example.com"))

        assert permissions_for(room, actor_for_user(owner)) == ALL_PERMISSIONS
        assert permissions_for(room, actor_for_user(member)) == frozenset()

        grant_permission(
            room.id,
            actor_for_user(owner),
            member.id,
            Permission.CONTROL_PLAYBACK,
        )
        db.session.commit()
        assert permissions_for(room, actor_for_user(member)) == {
            Permission.CONTROL_PLAYBACK
        }

        assert revoke_permission(
            room.id,
            actor_for_user(owner),
            member.id,
            Permission.CONTROL_PLAYBACK,
        )
        db.session.commit()
        assert permissions_for(room, actor_for_user(member)) == frozenset()


def test_non_owner_cannot_escalate_permissions(app, client, register):
    register(email="owner@example.com")
    code = create_room(client)
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        owner = db.session.get(User, room.owner_id)
        manager = User(email="manager@example.com")
        manager.set_password("correct-horse")
        target = User(email="target@example.com")
        target.set_password("correct-horse")
        room.memberships.extend(
            [RoomMembership(user=manager), RoomMembership(user=target)]
        )
        db.session.add_all([manager, target])
        db.session.flush()
        grant_permission(
            room.id,
            actor_for_user(owner),
            manager.id,
            Permission.MANAGE_MEMBERS,
        )
        db.session.commit()

        with pytest.raises(AuthorizationError):
            grant_permission(
                room.id,
                actor_for_user(manager),
                target.id,
                Permission.MANAGE_ROOM,
            )
        db.session.rollback()


def test_stale_connected_socket_reauthorizes_after_grant_and_revoke(
    app, client, register
):
    register(email="owner@example.com")
    code = create_room(client)
    add_ready_media(app, code)

    member_client = app.test_client()
    member_client.post(
        "/register",
        data={"email": "member@example.com", "password": "correct-horse"},
    )
    member_client.post("/join", data={"code": code})
    forbidden_upload = member_client.post(
        f"/api/mux/create-upload/{code}", json={"filename": "nope.mp4"}
    )
    assert forbidden_upload.status_code == 403
    socket_client = socketio.test_client(app, flask_test_client=member_client)
    socket_client.emit("join", {"code": code})
    socket_client.get_received()

    socket_client.emit(
        "play", {"code": code, "position": 10, "expected_playback_version": 1}
    )
    assert any(
        event["name"] == "error" and event["args"][0]["code"] == "forbidden"
        for event in socket_client.get_received()
    )

    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        owner = db.session.get(User, room.owner_id)
        member = db.session.scalar(select(User).where(User.email == "member@example.com"))
        grant_permission(
            room.id,
            actor_for_user(owner),
            member.id,
            Permission.CONTROL_PLAYBACK,
        )
        db.session.commit()

    socket_client.emit(
        "play", {"code": code, "position": 10, "expected_playback_version": 1}
    )
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        assert room.playing is True
        assert room.position == 10
        owner = db.session.get(User, room.owner_id)
        member = db.session.scalar(select(User).where(User.email == "member@example.com"))
        revoke_permission(
            room.id,
            actor_for_user(owner),
            member.id,
            Permission.CONTROL_PLAYBACK,
        )
        db.session.commit()

    socket_client.emit(
        "pause", {"code": code, "position": 11, "expected_playback_version": 2}
    )
    assert any(event["name"] == "error" for event in socket_client.get_received())
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        assert room.playing is True
        assert room.position == 10
    socket_client.disconnect()


def test_stable_queue_operations_preserve_saved_media(app, client, register):
    register()
    code = create_room(client)
    add_ready_media(app, code, count=2)

    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        owner = db.session.get(User, room.owner_id)
        actor = actor_for_user(owner)
        original_queue_version = room.queue_version
        duplicate, original_room_media_id = room.queue_entries[0], room.queue_entries[0].room_media_id

        room, repeated = add_saved_media_to_queue(
            room.id,
            actor,
            original_room_media_id,
            expected_queue_version=original_queue_version,
            queue_entry_id="repeat-entry",
        )
        db.session.commit()
        assert repeated.room_media_id == duplicate.room_media_id
        assert db.session.query(MediaAsset).count() == 2
        assert [entry.id for entry in room.queue_entries] == [
            "video-0",
            "video-1",
            "repeat-entry",
        ]

        reorder_queue(
            room.id,
            actor,
            ["repeat-entry", "video-1", "video-0"],
            expected_queue_version=room.queue_version,
        )
        db.session.commit()
        assert [entry.id for entry in room.queue_entries] == [
            "repeat-entry",
            "video-1",
            "video-0",
        ]

        before_remove_version = room.queue_version
        remove_queue_entry(
            room.id,
            actor,
            "repeat-entry",
            expected_queue_version=before_remove_version,
        )
        db.session.commit()
        assert db.session.get(RoomMedia, original_room_media_id) is not None
        assert db.session.get(MediaAsset, "media-0") is not None
        assert db.session.get(MuxMediaSource, "source-0") is not None

        complete_current_queue_entry(
            room.id,
            actor,
            "video-0",
            expected_playback_version=room.playback_version,
        )
        db.session.commit()
        assert db.session.get(QueueEntry, "video-0") is None
        assert db.session.get(MediaAsset, "media-0") is not None
        assert db.session.get(MuxMediaSource, "source-0") is not None
        assert db.session.scalar(select(MediaCleanupJob)) is None


def test_queue_and_playback_versions_reject_stale_commands(app, client, register):
    register()
    code = create_room(client)
    add_ready_media(app, code, count=2)

    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        actor = actor_for_user(db.session.get(User, room.owner_id))
        stale_queue_version = room.queue_version
        reorder_queue(
            room.id,
            actor,
            ["video-1", "video-0"],
            expected_queue_version=stale_queue_version,
        )
        db.session.commit()
        with pytest.raises(VersionConflictError):
            reorder_queue(
                room.id,
                actor,
                ["video-0", "video-1"],
                expected_queue_version=stale_queue_version,
            )
        db.session.rollback()

        room = db.session.get(WatchRoom, room.id)
        stale_playback_version = room.playback_version
        update_playback(
            room.id,
            actor,
            "play",
            4.5,
            expected_playback_version=stale_playback_version,
        )
        db.session.commit()
        with pytest.raises(VersionConflictError):
            update_playback(
                room.id,
                actor,
                "pause",
                5,
                expected_playback_version=stale_playback_version,
            )
        db.session.rollback()
        room = db.session.get(WatchRoom, room.id)
        assert room.playing is True
        assert room.position == 4.5


def test_serialized_queue_additions_receive_unique_positions(app, client, register):
    register()
    code = create_room(client)
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        actor = actor_for_user(db.session.get(User, room.owner_id))
        create_mux_media(room.id, actor, "First", queue_entry_id="first")
        db.session.commit()
        create_mux_media(room.id, actor, "Second", queue_entry_id="second")
        db.session.commit()
        positions = db.session.scalars(
            select(QueueEntry.position)
            .where(QueueEntry.room_id == room.id)
            .order_by(QueueEntry.position)
        ).all()
        assert positions == [0, 1]
        assert db.session.get(WatchRoom, room.id).queue_version == 2


def test_saved_media_cannot_be_queued_into_a_different_room(app, client, register):
    register()
    first_code = create_room(client, "First")
    second_code = create_room(client, "Second")
    add_ready_media(app, first_code)
    with app.app_context():
        second = db.session.scalar(
            select(WatchRoom).where(WatchRoom.code == second_code)
        )
        actor = actor_for_user(db.session.get(User, second.owner_id))
        with pytest.raises(ResourceNotFoundError):
            add_saved_media_to_queue(second.id, actor, "saved-0")
        db.session.rollback()

        db.session.add(
            QueueEntry(
                id="invalid-cross-room-entry",
                room_id=second.id,
                room_media_id="saved-0",
                position=0,
                added_by_id=actor.user_id,
            )
        )
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_source_abstraction_hides_browser_storage_key_from_other_browsers(
    app, client, register
):
    register()
    code = create_room(client)
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        owner = db.session.get(User, room.owner_id)
        browser = BrowserClient(
            id="browser-one", user=owner, client_key="opaque-client-key"
        )
        asset = MediaAsset(id="mixed-media", title="Mixed", created_by=owner)
        direct_source = MediaSource(
            id="direct-source",
            asset=asset,
            source_type="direct_url",
            status="ready",
            priority=1,
        )
        direct = DirectUrlSource(
            source=direct_source,
            original_url="https://media.example/movie.mp4",
            normalized_url="https://media.example/movie.mp4",
        )
        local_source = MediaSource(
            id="local-source",
            asset=asset,
            source_type="browser_local",
            status="ready",
            priority=2,
        )
        local = BrowserLocalSource(
            source=local_source,
            browser_client=browser,
            storage_key="opfs/mixed-media",
            original_filename="movie.mp4",
        )
        room_media = RoomMedia(
            id="mixed-saved", room=room, asset=asset, added_by=owner
        )
        db.session.add_all(
            [browser, asset, direct_source, direct, local_source, local, room_media]
        )
        db.session.commit()

        public_for_other = public_source(local_source)
        public_for_owner = public_source(
            local_source, browser_client_id=browser.id
        )
        assert "storage_key" not in public_for_other
        assert public_for_other["availability"] == "LOCAL_OWNER_OFFLINE"
        assert public_for_owner["storage_key"] == "opfs/mixed-media"
        assert public_for_owner["availability"] == "AVAILABLE_THIS_BROWSER"
        assert room_media_to_public(room_media)["sources"][0]["source_type"] in {
            "direct_url",
            "browser_local",
        }


def test_playback_completion_never_calls_mux_cleanup(app, client, register):
    register()
    code = create_room(client)
    add_ready_media(app, code)
    socket_client = socketio.test_client(app, flask_test_client=client)
    socket_client.emit("join", {"code": code})
    socket_client.get_received()
    with (
        patch("movie_theater.delete_mux_asset") as delete_asset,
        patch("movie_theater.delete_mux_upload") as delete_upload,
    ):
        socket_client.emit(
            "video_ended",
            {
                "code": code,
                "video_id": "video-0",
                "expected_playback_version": 1,
            },
        )
    delete_asset.assert_not_called()
    delete_upload.assert_not_called()
    socket_client.disconnect()


def test_mux_remote_creation_happens_after_durable_media_state(
    app, client, register
):
    register()
    code = create_room(client)

    def observe_durable_state(*_args, **_kwargs):
        source = db.session.scalar(select(MediaSource))
        assert source is not None
        assert source.status == "creating"
        assert db.session.scalar(select(RoomMedia)) is not None
        assert db.session.scalar(select(QueueEntry)) is None
        response = Mock(ok=True)
        response.json.return_value = {
            "data": {"url": "https://upload.example", "id": "mux-upload"}
        }
        return response

    with (
        patch("movie_theater.mux_configured", return_value=True),
        patch("movie_theater.requests.post", side_effect=observe_durable_state),
    ):
        response = client.post(
            f"/api/mux/create-upload/{code}",
            json={"filename": "movie.mp4", "size": 1234},
        )
    assert response.status_code == 200
    with app.app_context():
        source = db.session.scalar(select(MediaSource))
        assert source.status == "uploading"
        assert source.mux.upload_id == "mux-upload"
