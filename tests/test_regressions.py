from unittest.mock import Mock, patch

import itsdangerous
import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from wtforms.validators import ValidationError

from models import (
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
from movie_theater import limiter, lock_room, socketio

from test_rooms import add_ready_media, create_room


def test_multi_video_completion_preserves_saved_mux_media(app, client, register):
    register()
    code = create_room(client)
    add_ready_media(app, code, count=3)

    socket_client = socketio.test_client(app, flask_test_client=client)
    socket_client.emit("join", {"code": code})
    with patch("movie_theater.delete_mux_asset") as delete_asset:
        socket_client.emit(
            "video_ended",
            {
                "code": code,
                "video_id": "video-0",
                "expected_playback_version": 1,
            },
        )

    delete_asset.assert_not_called()
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        assert [(entry.id, entry.position) for entry in room.queue_entries] == [
            ("video-1", 1),
            ("video-2", 2),
        ]
        assert db.session.scalar(select(MediaAsset).where(MediaAsset.id == "media-0"))
        assert db.session.scalar(
            select(MuxMediaSource).where(MuxMediaSource.asset_id == "asset-0")
        )
    socket_client.disconnect()


def test_failed_completion_does_not_delete_mux_asset(app, client, register):
    register()
    code = create_room(client)
    add_ready_media(app, code, count=1)
    socket_client = socketio.test_client(app, flask_test_client=client)
    socket_client.emit("join", {"code": code})

    with (
        patch("movie_theater.db.session.commit", side_effect=IntegrityError("x", {}, None)),
        patch("movie_theater.delete_mux_asset") as delete_asset,
        pytest.raises(IntegrityError),
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
    with app.app_context():
        db.session.rollback()
    socket_client.disconnect()


def test_logout_disconnects_socket_before_it_joins_room(app, client, register):
    register()
    socket_client = socketio.test_client(app, flask_test_client=client)
    assert socket_client.is_connected()
    client.post("/logout")
    assert not socket_client.is_connected()


def test_logout_disconnects_and_revokes_active_socket(app, client, register):
    register()
    code = create_room(client)
    socket_client = socketio.test_client(app, flask_test_client=client)
    socket_client.emit("join", {"code": code})

    client.post("/logout")

    assert not socket_client.is_connected()
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        assert room.playing is False
        assert room.position == 0.0


def test_upload_room_query_compiles_to_postgres_for_update():
    statement = (
        select(WatchRoom)
        .where(WatchRoom.id == 1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    compiled = str(statement.compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE" in compiled


def test_locked_room_refreshes_stale_current_queue_entry(app):
    with app.app_context():
        owner = User(email="owner@example.com")
        owner.set_password("correct-horse")
        room = WatchRoom(code="ABCDEFGH", name="Room", owner=owner)
        room.memberships.append(RoomMembership(user=owner))
        db.session.add(room)
        db.session.commit()
        stale = db.session.get(WatchRoom, room.id)
        asset = MediaAsset(id="media", title="Video", created_by=owner)
        source = MediaSource(
            id="source", asset=asset, source_type="mux_upload", status="ready"
        )
        mux = MuxMediaSource(source=source, playback_id="playback")
        room_media = RoomMedia(
            id="saved", room=room, asset=asset, added_by=owner
        )
        entry = QueueEntry(
            id="first-entry",
            room=room,
            room_media=room_media,
            position=0,
            added_by=owner,
        )
        db.session.add_all([asset, source, mux, room_media, entry])
        db.session.flush()
        db.session.execute(
            WatchRoom.__table__.update()
            .where(WatchRoom.id == room.id)
            .values(current_queue_entry_id=entry.id)
        )
        assert stale.current_queue_entry_id is None
        assert lock_room(room.id).current_queue_entry_id == entry.id


def test_upload_order_query_locks_room_row(app, client, register):
    register()
    code = create_room(client)
    mux_response = Mock(ok=True)
    mux_response.json.return_value = {
        "data": {"url": "https://upload.example", "id": "upload-locked"}
    }
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.upper())

    with app.app_context():
        event.listen(db.engine, "before_cursor_execute", capture)
    try:
        with (
            patch("movie_theater.mux_configured", return_value=True),
            patch("movie_theater.requests.post", return_value=mux_response),
        ):
            assert client.post(
                f"/api/mux/create-upload/{code}", json={"filename": "new.mp4"}
            ).status_code == 200
    finally:
        with app.app_context():
            event.remove(db.engine, "before_cursor_execute", capture)
    # SQLite strips FOR UPDATE; PostgreSQL coverage validates that it blocks.
    assert any("FROM WATCH_ROOMS" in statement for statement in statements)


def test_upload_uses_database_max_order_and_cleans_mux_on_commit_failure(
    app, client, register
):
    register()
    code = create_room(client)
    add_ready_media(app, code, count=2)
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        db.session.delete(room.queue_entries[0])
        db.session.commit()

    mux_response = Mock(ok=True)
    mux_response.json.return_value = {
        "data": {"url": "https://upload.example", "id": "upload-new"}
    }
    with (
        patch("movie_theater.mux_configured", return_value=True),
        patch("movie_theater.requests.post", return_value=mux_response),
    ):
        response = client.post(
            f"/api/mux/create-upload/{code}", json={"filename": "new.mp4"}
        )
    assert response.status_code == 200
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        assert [entry.position for entry in room.queue_entries] == [1]
        assert len(room.library_items) == 3

    failed_response = Mock(ok=True)
    failed_response.json.return_value = {
        "data": {"url": "https://upload.example", "id": "upload-failed"}
    }
    real_commit = db.session.commit
    commit_calls = 0

    def fail_link_commit():
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 2:
            raise IntegrityError("x", {}, None)
        return real_commit()

    with (
        patch("movie_theater.mux_configured", return_value=True),
        patch("movie_theater.requests.post", return_value=failed_response),
        patch("movie_theater.db.session.commit", side_effect=fail_link_commit),
        pytest.raises(IntegrityError),
    ):
        client.post(f"/api/mux/create-upload/{code}", json={"filename": "fail.mp4"})
    with app.app_context():
        db.session.rollback()
        cleanup_job = db.session.scalar(
            select(MediaCleanupJob).where(MediaCleanupJob.remote_id == "upload-failed")
        )
        assert cleanup_job is not None
        assert cleanup_job.status == "pending"


def test_socketio_rejects_cross_origin_handshake(app, client):
    response = client.get(
        "/socket.io/?EIO=4&transport=polling",
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 400


def test_csrf_token_does_not_expire_for_long_lived_room(app, client):
    app.config["WTF_CSRF_ENABLED"] = True
    assert app.config["WTF_CSRF_TIME_LIMIT"] is None
    with client.session_transaction() as browser_session:
        browser_session["csrf_token"] = "stable-token"
    serializer = itsdangerous.URLSafeTimedSerializer(
        app.secret_key, salt="wtf-csrf-token"
    )
    with patch.object(serializer.signer, "get_timestamp", return_value=1):
        token = serializer.dumps("stable-token")
    from flask_wtf.csrf import validate_csrf

    with app.test_request_context():
        from flask import session

        session["csrf_token"] = "stable-token"
        with pytest.raises(ValidationError, match="expired"):
            validate_csrf(token, time_limit=3600)
        validate_csrf(token, time_limit=None)


def test_user_deletion_policy_preserves_other_rooms_and_media(app):
    with app.app_context():
        owner = User(email="owner@example.com")
        owner.set_password("correct-horse")
        uploader = User(email="uploader@example.com")
        uploader.set_password("correct-horse")
        room = WatchRoom(code="ABCDEFGH", name="Room", owner=owner)
        room.memberships.extend(
            [RoomMembership(user=owner), RoomMembership(user=uploader)]
        )
        asset = MediaAsset(id="media", title="Video", created_by=uploader)
        source = MediaSource(
            id="source", asset=asset, source_type="mux_upload", status="ready"
        )
        room_media = RoomMedia(
            id="saved", room=room, asset=asset, added_by=uploader
        )
        queue_entry = QueueEntry(
            id="entry",
            room=room,
            room_media=room_media,
            position=0,
            added_by=uploader,
        )
        db.session.add_all([room, asset, source, room_media, queue_entry])
        db.session.commit()

        db.session.delete(uploader)
        db.session.commit()
        db.session.expire_all()
        assert db.session.scalar(select(MediaAsset)).created_by_id is None
        assert db.session.scalar(select(WatchRoom)).id == room.id

        with pytest.raises(IntegrityError):
            db.session.delete(owner)
            db.session.commit()
        db.session.rollback()


def test_rate_limits_registration_login_join_and_upload(app, client, register):
    app.config["RATELIMIT_ENABLED"] = True
    limiter.reset()
    for index in range(5):
        response = client.post(
            "/register",
            data={"email": "invalid", "password": f"password-{index}"},
        )
        assert response.status_code == 200
    assert client.post(
        "/register", data={"email": "invalid", "password": "password-x"}
    ).status_code == 429

    limiter.reset()
    for _ in range(10):
        assert client.post(
            "/login", data={"email": "nobody@example.com", "password": "wrong"}
        ).status_code == 200
    assert client.post(
        "/login", data={"email": "nobody@example.com", "password": "wrong"}
    ).status_code == 429

    limiter.reset()
    register()
    for _ in range(20):
        assert client.post("/join", data={"code": "NOTFOUND"}).status_code == 302
    assert client.post("/join", data={"code": "NOTFOUND"}).status_code == 429

    limiter.reset()
    code = create_room(client)
    with patch("movie_theater.mux_configured", return_value=False):
        for _ in range(10):
            assert client.post(f"/api/mux/create-upload/{code}", json={}).status_code == 503
        assert client.post(f"/api/mux/create-upload/{code}", json={}).status_code == 429


def test_registration_recovers_from_unique_constraint_race(app, client):
    real_commit = db.session.commit
    calls = 0

    def raced_commit():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise IntegrityError("insert", {}, None)
        return real_commit()

    with patch("movie_theater.db.session.commit", side_effect=raced_commit):
        response = client.post(
            "/register",
            data={"email": "race@example.com", "password": "correct-horse"},
            follow_redirects=True,
        )
    assert response.status_code == 200
    assert b"already exists" in response.data
    with app.app_context():
        db.session.execute(select(User)).scalars().all()
