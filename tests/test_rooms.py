from sqlalchemy import select

from models import (
    MediaAsset,
    MediaSource,
    MuxMediaSource,
    QueueEntry,
    RoomMedia,
    RoomMembership,
    User,
    WatchRoom,
    db,
)


def create_room(client, name="Friday Films"):
    response = client.post("/create", data={"name": name})
    assert response.status_code == 302
    return response.headers["Location"].rsplit("/", 1)[-1]


def add_ready_media(app, code, count=1):
    entry_ids = []
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        user = db.session.get(User, room.owner_id)
        for index in range(count):
            asset = MediaAsset(
                id=f"media-{index}",
                title=f"Video {index}",
                duration=120 + index,
                created_by=user,
            )
            source = MediaSource(
                id=f"source-{index}",
                asset=asset,
                source_type="mux_upload",
                status="ready",
            )
            mux = MuxMediaSource(
                source=source,
                upload_id=f"upload-{index}",
                asset_id=f"asset-{index}",
                playback_id=f"playback-{index}",
            )
            room_media = RoomMedia(
                id=f"saved-{index}",
                room=room,
                asset=asset,
                added_by=user,
            )
            entry = QueueEntry(
                id=f"video-{index}",
                room=room,
                room_media=room_media,
                position=index,
                added_by=user,
            )
            db.session.add_all([asset, source, mux, room_media, entry])
            entry_ids.append(entry.id)
        db.session.flush()
        if entry_ids and room.current_queue_entry_id is None:
            room.current_queue_entry_id = entry_ids[0]
            room.queue_version += count
            room.playback_version += 1
        db.session.commit()
    return entry_ids


def test_database_starts_empty_for_each_test(app):
    with app.app_context():
        assert db.session.scalar(select(User)) is None
        assert str(db.engine.url) == "sqlite://"


def test_room_creation_persists_owner_membership_and_dashboard(app, client, register):
    register()
    code = create_room(client)

    with app.app_context():
        db.session.remove()
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        user = db.session.scalar(select(User))
        assert room.name == "Friday Films"
        assert room.owner_id == user.id
        assert db.session.get(RoomMembership, (room.id, user.id)) is not None

    dashboard = client.get("/rooms")
    assert b"Friday Films" in dashboard.data
    assert code.encode() in dashboard.data
    assert client.get(f"/session/{code}").status_code == 200


def test_joining_room_saves_idempotent_membership(app, client, register):
    register(email="owner@example.com")
    code = create_room(client)
    client.post("/logout")
    register(email="guest@example.com")

    invite = client.get(f"/session/{code}")
    assert invite.status_code == 200
    assert b"Requests" in invite.data

    first = client.post("/join", data={"code": code.lower()})
    second = client.post("/join", data={"code": code})
    assert first.headers["Location"].endswith(f"/session/{code}")
    assert second.headers["Location"].endswith(f"/session/{code}")
    assert client.get(f"/session/{code}").status_code == 200

    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        memberships = db.session.scalars(
            select(RoomMembership).where(RoomMembership.room_id == room.id)
        ).all()
        assert len(memberships) == 2


def test_dashboard_only_lists_joined_rooms(app, client, register):
    register(email="owner@example.com")
    owned_code = create_room(client, "Owner Room")
    client.post("/logout")
    register(email="other@example.com")
    other_code = create_room(client, "Other Room")
    client.post("/logout")

    client.post(
        "/login",
        data={"email": "owner@example.com", "password": "correct-horse"},
    )
    dashboard = client.get("/rooms")
    assert owned_code.encode() in dashboard.data
    assert other_code.encode() not in dashboard.data


def test_playback_state_survives_database_session_reload(app, client, register):
    register()
    code = create_room(client)
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        room.playing = True
        room.position = 42.25
        db.session.commit()
        db.session.remove()
        restored = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        assert restored.playing is True
        assert restored.position == 42.25


def test_room_and_mux_routes_require_authentication(client):
    assert client.post("/create").status_code == 302
    assert client.post("/join", data={"code": "ABCDEFGH"}).status_code == 302
    assert client.get("/session/ABCDEFGH").status_code == 302
    response = client.post("/api/mux/create-upload/ABCDEFGH", json={})
    assert response.status_code == 401
    assert response.json == {"error": "Authentication required"}
