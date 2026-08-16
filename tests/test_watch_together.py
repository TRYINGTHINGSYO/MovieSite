from sqlalchemy import select

from models import MediaReview, QueueEntry, User, WatchRoom, db
from movie_theater import socketio
from test_rooms import add_ready_media, create_room


def _join_state(app, client, code):
    socket_client = socketio.test_client(app, flask_test_client=client)
    socket_client.emit("room:join", {"code": code})
    state = next(
        event["args"][0]
        for event in socket_client.get_received()
        if event["name"] == "room:state"
    )
    return socket_client, state


def test_selecting_a_queue_item_starts_playback_for_everyone(app, client, register):
    register()
    code = create_room(client)
    entry_ids = add_ready_media(app, code, count=2)
    socket_client, state = _join_state(app, client, code)
    assert state["playing"] is False
    assert "playback_base" in state
    assert "server_now" in state

    socket_client.emit(
        "playback:command",
        {
            "code": code,
            "action": "select",
            "queue_entry_id": entry_ids[1],
            "expected_playback_version": state["playback_version"],
            "client_action_id": "selectstart01",
        },
    )
    updated = next(
        event["args"][0]
        for event in socket_client.get_received()
        if event["name"] == "playback:updated"
    )
    assert updated["current_id"] == entry_ids[1]
    assert updated["playing"] is True
    assert updated["playback_base"] == 0.0
    socket_client.disconnect()


def test_next_removes_the_finished_clip_and_starts_the_following(
    app, client, register
):
    register()
    code = create_room(client)
    entry_ids = add_ready_media(app, code, count=2)
    socket_client, state = _join_state(app, client, code)
    socket_client.emit(
        "playback:command",
        {
            "code": code,
            "action": "next",
            "expected_playback_version": state["playback_version"],
            "client_action_id": "finishnext001",
        },
    )
    updated = next(
        event["args"][0]
        for event in socket_client.get_received()
        if event["name"] == "playback:updated"
    )
    assert [item["id"] for item in updated["queue"]] == [entry_ids[1]]
    assert updated["current_id"] == entry_ids[1]
    assert updated["playing"] is True
    with app.app_context():
        assert db.session.get(QueueEntry, entry_ids[0]) is None
    socket_client.disconnect()


def test_defer_moves_current_to_the_end_and_starts_the_next_clip(
    app, client, register
):
    register()
    code = create_room(client)
    entry_ids = add_ready_media(app, code, count=3)
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        queue_version = room.queue_version
    response = client.post(
        f"/api/rooms/{code}/queue/{entry_ids[0]}/defer",
        json={"expected_queue_version": queue_version},
    )
    assert response.status_code == 200, response.get_json()
    state = response.get_json()["state"]
    assert [item["id"] for item in state["queue"]] == [
        entry_ids[1],
        entry_ids[2],
        entry_ids[0],
    ]
    assert state["current_id"] == entry_ids[1]
    assert state["playing"] is True
    assert state["playback_base"] == 0.0


def test_guest_can_request_watch_later_and_owner_approval_defers(
    app, client, register
):
    register()
    code = create_room(client)
    entry_ids = add_ready_media(app, code, count=2)
    guest = app.test_client()
    assert guest.get(f"/session/{code}").status_code == 200
    created = guest.post(
        f"/api/rooms/{code}/requests",
        json={
            "request_type": "MOVE_TO_END",
            "payload": {"queue_entry_id": entry_ids[0]},
            "client_request_id": "watchlater01",
        },
    )
    assert created.status_code == 201, created.get_json()
    request_id = created.get_json()["request"]["id"]
    approved = client.post(
        f"/api/rooms/{code}/requests/{request_id}/resolve",
        json={"resolution": "approved"},
    )
    assert approved.status_code == 200, approved.get_json()
    state = approved.get_json()["state"]
    assert [item["id"] for item in state["queue"]] == [entry_ids[1], entry_ids[0]]
    assert state["current_id"] == entry_ids[1]
    assert state["playing"] is True


def test_group_reviews_upsert_one_rating_per_person(app, client, register):
    register(display_name="Host")
    code = create_room(client)
    add_ready_media(app, code)
    state = client.get(f"/api/rooms/{code}/state").get_json()["state"]
    room_media_id = state["library"][0]["id"]
    assert state["library"][0]["reviews"]["count"] == 0

    first = client.post(
        f"/api/rooms/{code}/media/{room_media_id}/reviews",
        json={"rating": 5, "comment": "Loved it"},
    )
    assert first.status_code == 200, first.get_json()
    reviews = first.get_json()["state"]["library"][0]["reviews"]
    assert reviews["count"] == 1
    assert reviews["average"] == 5
    assert reviews["mine"]["rating"] == 5
    assert reviews["mine"]["comment"] == "Loved it"

    guest = app.test_client()
    assert guest.get(f"/session/{code}").status_code == 200
    guest_review = guest.post(
        f"/api/rooms/{code}/media/{room_media_id}/reviews",
        json={"rating": 4},
    )
    assert guest_review.status_code == 200, guest_review.get_json()
    guest_reviews = guest_review.get_json()["state"]["library"][0]["reviews"]
    assert guest_reviews["count"] == 2
    assert guest_reviews["average"] == 4.5
    assert guest_reviews["mine"]["rating"] == 4

    updated = client.post(
        f"/api/rooms/{code}/media/{room_media_id}/reviews",
        json={"rating": 3, "comment": "Rewatched"},
    )
    assert updated.status_code == 200
    host_reviews = updated.get_json()["state"]["library"][0]["reviews"]
    assert host_reviews["count"] == 2
    assert host_reviews["average"] == 3.5
    assert host_reviews["mine"]["rating"] == 3
    assert host_reviews["mine"]["comment"] == "Rewatched"
    with app.app_context():
        assert db.session.scalar(select(MediaReview)) is not None
        assert db.session.query(MediaReview).count() == 2
        owner = db.session.scalar(select(User).where(User.email == "viewer@example.com"))
        assert owner is not None


def test_host_soft_sync_keeps_everyone_aligned_without_version_bump(
    app, client, register
):
    register()
    code = create_room(client)
    add_ready_media(app, code)
    socket_client, state = _join_state(app, client, code)
    playback_version = state["playback_version"]
    socket_client.emit(
        "sync_position",
        {"code": code, "position": 12.5, "playing": True},
    )
    updated = next(
        event["args"][0]
        for event in socket_client.get_received()
        if event["name"] == "playback:updated"
    )
    assert updated["playback_version"] == playback_version
    assert updated["playback_base"] == 12.5
    assert updated["playing"] is True
    socket_client.disconnect()
