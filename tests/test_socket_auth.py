from models import db
from movie_theater import socketio

from test_rooms import add_ready_media, create_room


def test_unauthenticated_socket_connection_is_rejected(app, client):
    socket_client = socketio.test_client(app, flask_test_client=client)
    assert not socket_client.is_connected()


def test_socket_room_join_allows_read_only_nonmember(app, client, register):
    register(email="owner@example.com")
    code = create_room(client)
    client.post("/logout")
    register(email="outsider@example.com")

    socket_client = socketio.test_client(app, flask_test_client=client)
    socket_client.emit("join", {"code": code})
    received = socket_client.get_received()
    state_event = next(event for event in received if event["name"] == "state_sync")
    assert state_event["args"][0]["capabilities"] == []
    socket_client.disconnect()


def test_socket_playback_state_is_persisted(app, client, register):
    register()
    code = create_room(client)
    add_ready_media(app, code)
    socket_client = socketio.test_client(app, flask_test_client=client)
    socket_client.emit("join", {"code": code})
    socket_client.get_received()
    socket_client.emit(
        "play",
        {"code": code, "position": 12.5, "expected_playback_version": 1},
    )

    from models import WatchRoom

    with app.app_context():
        db.session.remove()
        room = db.session.execute(
            db.select(WatchRoom).where(WatchRoom.code == code)
        ).scalar_one()
        assert room.playing is True
        assert room.position == 12.5
    socket_client.disconnect()
