from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from unittest.mock import Mock
import re

import pytest
from sqlalchemy import func, select

from authorization import Permission
from direct_urls import DirectUrlError, validate_direct_url
from models import (
    DirectUrlSource,
    MediaAsset,
    MediaSource,
    QueueEntry,
    RoomCommandReceipt,
    RoomMedia,
    RoomRequest,
    User,
    WatchRoom,
    db,
)
from movie_theater import socketio
from test_rooms import add_ready_media, create_room


def register_user(client, email, display_name=None):
    return client.post(
        "/register",
        data={
            "email": email,
            "password": "correct-horse",
            "display_name": display_name or email.split("@", 1)[0],
        },
    )


def save_direct(client, code, title="Direct feature", url="https://media.example.com/movie.mp4"):
    response = client.post(
        f"/api/rooms/{code}/media/direct-url",
        json={"title": title, "url": url, "probe_result": "playable"},
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:video/mp4;base64,AAAA",
        "file:///tmp/movie.mp4",
        "ftp://example.com/movie.mp4",
        "gopher://example.com/movie.mp4",
        "https://user:secret@example.com/movie.mp4",
        "https://localhost/movie.mp4",
        "https://video.local/movie.mp4",
        "https://media.internal/movie.mp4",
        "https://router/movie.mp4",
        "https://127.0.0.1/movie.mp4",
        "https://10.1.2.3/movie.mp4",
        "https://169.254.1.2/movie.mp4",
        "https://[::1]/movie.mp4",
        "https://2130706433/movie.mp4",
        "https://0177.0.0.1/movie.mp4",
        "https://127.1/movie.mp4",
        "https://0x7f.0.0.1/movie.mp4",
        "https://example.com:99999/movie.mp4",
        "https://exa\n mple.com/movie.mp4",
        "\nhttps://example.com/movie.mp4",
        "https://example.com\\@127.0.0.1/movie.mp4",
        "not-an-absolute-url",
    ],
)
def test_direct_url_validator_rejects_unsafe_targets(url):
    with pytest.raises(DirectUrlError):
        validate_direct_url(url, require_https=False)


def test_direct_url_validator_enforces_production_https_and_size():
    with pytest.raises(DirectUrlError, match="HTTPS"):
        validate_direct_url("http://media.example.com/movie.mp4", require_https=True)
    with pytest.raises(DirectUrlError, match="too long"):
        validate_direct_url("https://example.com/" + "a" * 5000, require_https=False)
    result = validate_direct_url(
        "HTTPS://Media.Example.COM:443/path/video.mp4#fragment",
        require_https=True,
    )
    assert result.normalized == "https://media.example.com:443/path/video.mp4"


def test_privileged_direct_url_save_is_separate_and_never_fetches_server_side(
    app, client, register
):
    register(email="owner@example.com")
    code = create_room(client)
    with patch("movie_theater.requests.get") as get, patch(
        "movie_theater.requests.post"
    ) as post:
        result = save_direct(client, code)
    get.assert_not_called()
    post.assert_not_called()
    state = result["state"]
    assert state["queue"] == []
    assert len(state["library"]) == 1
    assert state["library"][0]["id"] == result["room_media_id"]
    source = state["library"][0]["sources"][0]
    assert source["source_type"] == "direct_url"
    assert source["status"] == "ready"
    assert source["probe_result"] == "playable"

    with app.app_context():
        direct = db.session.scalar(select(DirectUrlSource))
        assert direct.normalized_url == "https://media.example.com/movie.mp4"
        assert db.session.scalar(select(func.count(QueueEntry.id))) == 0


@pytest.mark.parametrize(
    "probe_result",
    ["not_probed", "unsupported_format", "network_or_cors_failure", "unavailable"],
)
def test_direct_url_save_requires_a_successful_browser_probe_or_extract(
    app, client, register, probe_result
):
    register()
    code = create_room(client)
    with patch("movie_theater.extract_clip") as extract:
        extract.side_effect = DirectUrlError(
            "Could not extract a playable clip from that link"
        )
        response = client.post(
            f"/api/rooms/{code}/media/direct-url",
            json={
                "title": "Rejected probe",
                "url": "https://media.example.com/rejected.mp4",
                "probe_result": probe_result,
            },
        )
    assert response.status_code == 400
    extract.assert_called_once()
    with app.app_context():
        assert db.session.scalar(select(func.count(MediaAsset.id))) == 0


def test_direct_url_no_seek_is_ready_and_malformed_json_is_rejected(
    app, client, register
):
    register()
    code = create_room(client)
    malformed = client.post(
        f"/api/rooms/{code}/media/direct-url",
        json=["not", "an", "object"],
    )
    assert malformed.status_code == 400
    saved = client.post(
        f"/api/rooms/{code}/media/direct-url",
        json={
            "title": "No seeking",
            "url": "https://media.example.com/live.mp4",
            "probe_result": "playable_no_seek",
        },
    )
    assert saved.status_code == 201
    source = saved.get_json()["state"]["library"][0]["sources"][0]
    assert source["status"] == "ready"
    assert source["probe_result"] == "playable_no_seek"
    assert source["error"] is None


def test_member_and_guest_cannot_save_direct_url(app, client, register):
    register(email="owner@example.com")
    code = create_room(client)
    member = app.test_client()
    register_user(member, "member@example.com")
    member.post("/join", data={"code": code})
    assert member.post(
        f"/api/rooms/{code}/media/direct-url",
        json={"title": "No", "url": "https://example.com/no.mp4"},
    ).status_code == 403

    guest = app.test_client()
    assert guest.get(f"/session/{code}").status_code == 200
    assert guest.post(
        f"/api/rooms/{code}/media/direct-url",
        json={"title": "No", "url": "https://example.com/no.mp4"},
    ).status_code == 403


def test_add_media_only_member_can_save_mux_without_mutating_queue(
    app, client, register
):
    register(email="owner@example.com")
    code = create_room(client)
    member = app.test_client()
    register_user(member, "member@example.com")
    member.post("/join", data={"code": code})
    with app.app_context():
        member_id = db.session.scalar(select(User.id).where(User.email == "member@example.com"))
    client.post(
        f"/api/rooms/{code}/permissions",
        json={"user_id": member_id, "permission": "ADD_MEDIA", "enabled": True},
    )
    mux_response = Mock(
        ok=True,
        status_code=201,
        json=lambda: {"data": {"id": "upload-save-only", "url": "https://upload.example"}},
    )
    with patch("movie_theater.mux_configured", return_value=True), patch(
        "movie_theater.requests.post", return_value=mux_response
    ):
        saved = member.post(
            f"/api/mux/create-upload/{code}",
            json={"filename": "saved.mp4", "save_only": True},
        )
        combined_queue_change = member.post(
            f"/api/mux/create-upload/{code}",
            json={"filename": "queued.mp4", "save_only": False},
        )
    assert saved.status_code == 200
    assert saved.get_json()["queue_entry_id"] is None
    assert len(saved.get_json()["state"]["library"]) == 1
    assert saved.get_json()["state"]["queue"] == []
    assert combined_queue_change.status_code == 400
    assert "separate operations" in combined_queue_change.get_json()["error"]


def test_queue_http_flow_is_versioned_stable_and_preserves_media(app, client, register):
    register(email="owner@example.com")
    code = create_room(client)
    first_media = save_direct(client, code, "First")["room_media_id"]
    second_media = save_direct(
        client, code, "Second", "https://media.example.com/second.mp4"
    )["room_media_id"]

    first = client.post(
        f"/api/rooms/{code}/queue",
        json={"room_media_id": first_media, "expected_queue_version": 0},
    )
    assert first.status_code == 201
    first_entry = first.get_json()["queue_entry_id"]
    second = client.post(
        f"/api/rooms/{code}/queue",
        json={"room_media_id": second_media, "expected_queue_version": 1},
    )
    second_entry = second.get_json()["queue_entry_id"]
    assert len(first_entry) == len(second_entry) == 32

    reordered = client.put(
        f"/api/rooms/{code}/queue/order",
        json={
            "queue_entry_ids": [second_entry, first_entry],
            "expected_queue_version": 2,
        },
    )
    assert reordered.status_code == 200
    assert [item["id"] for item in reordered.get_json()["state"]["queue"]] == [
        second_entry,
        first_entry,
    ]
    stale = client.put(
        f"/api/rooms/{code}/queue/order",
        json={
            "queue_entry_ids": [first_entry, second_entry],
            "expected_queue_version": 2,
        },
    )
    assert stale.status_code == 409
    assert stale.get_json()["state"]["queue_version"] == 3

    removed = client.delete(
        f"/api/rooms/{code}/queue/{first_entry}",
        json={"expected_queue_version": 3},
    )
    assert removed.status_code == 200
    with app.app_context():
        assert db.session.get(RoomMedia, first_media) is not None
        assert db.session.scalar(select(func.count(MediaAsset.id))) == 2
        assert db.session.scalar(select(func.count(MediaSource.id))) == 2


def test_remove_current_and_clear_upcoming_update_authoritative_versions(
    app, client, register
):
    register()
    code = create_room(client)
    entry_ids = add_ready_media(app, code, count=3)
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        queue_version = room.queue_version
        playback_version = room.playback_version
    removed = client.delete(
        f"/api/rooms/{code}/queue/{entry_ids[0]}",
        json={"expected_queue_version": queue_version},
    )
    state = removed.get_json()["state"]
    assert state["current_id"] == entry_ids[1]
    assert state["queue_version"] == queue_version + 1
    assert state["playback_version"] == playback_version + 1
    cleared = client.delete(
        f"/api/rooms/{code}/queue/upcoming",
        json={"expected_queue_version": state["queue_version"]},
    )
    assert [item["id"] for item in cleared.get_json()["state"]["queue"]] == [
        entry_ids[1]
    ]
    with app.app_context():
        assert db.session.scalar(select(func.count(MediaAsset.id))) == 3


def test_removing_last_selected_entry_stops_instead_of_replaying_previous(
    app, client, register
):
    register()
    code = create_room(client)
    entry_ids = add_ready_media(app, code, count=3)
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        room.current_queue_entry_id = entry_ids[-1]
        room.playing = True
        queue_version = room.queue_version
        db.session.commit()

    removed = client.delete(
        f"/api/rooms/{code}/queue/{entry_ids[-1]}",
        json={"expected_queue_version": queue_version},
    )
    assert removed.status_code == 200
    state = removed.get_json()["state"]
    assert state["current_id"] is None
    assert state["playing"] is False
    assert [entry["id"] for entry in state["queue"]] == entry_ids[:-1]
    with app.app_context():
        assert db.session.scalar(select(func.count(MediaAsset.id))) == 3


def test_owner_permission_http_updates_active_socket_and_revoke_is_immediate(
    app, client, register
):
    register(email="owner@example.com")
    code = create_room(client)
    add_ready_media(app, code)
    member = app.test_client()
    register_user(member, "member@example.com", "Member")
    member.post("/join", data={"code": code})
    socket_client = socketio.test_client(app, flask_test_client=member)
    socket_client.emit("room:join", {"code": code})
    initial = next(
        event["args"][0]
        for event in socket_client.get_received()
        if event["name"] == "room:state"
    )
    assert initial["capabilities"] == []

    with app.app_context():
        member_id = db.session.scalar(
            select(User.id).where(User.email == "member@example.com")
        )
    granted = client.post(
        f"/api/rooms/{code}/permissions",
        json={
            "user_id": member_id,
            "permission": "CONTROL_PLAYBACK",
            "enabled": True,
        },
    )
    assert granted.status_code == 200
    updates = socket_client.get_received()
    permission_state = next(
        event["args"][0]
        for event in updates
        if event["name"] == "permissions:updated"
    )
    assert "CONTROL_PLAYBACK" in permission_state["capabilities"]

    socket_client.emit(
        "playback:command",
        {
            "code": code,
            "action": "play",
            "position": 0,
            "expected_playback_version": permission_state["playback_version"],
            "client_action_id": "grantplay001",
        },
    )
    assert not any(event["name"] == "error" for event in socket_client.get_received())

    revoked = client.post(
        f"/api/rooms/{code}/permissions",
        json={
            "user_id": member_id,
            "permission": "CONTROL_PLAYBACK",
            "enabled": False,
        },
    )
    assert revoked.status_code == 200
    socket_client.get_received()
    socket_client.emit(
        "playback:command",
        {
            "code": code,
            "action": "pause",
            "position": 1,
            "expected_playback_version": revoked.get_json()["state"]["playback_version"],
            "client_action_id": "revokepause01",
        },
    )
    assert any(
        event["name"] == "error" and event["args"][0]["code"] == "forbidden"
        for event in socket_client.get_received()
    )

    socket_client.disconnect()


def test_nonowner_cannot_self_grant_over_http(app, client, register):
    register(email="owner@example.com")
    code = create_room(client)
    member = app.test_client()
    register_user(member, "member@example.com")
    member.post("/join", data={"code": code})
    with app.app_context():
        member_id = db.session.scalar(select(User.id).where(User.email == "member@example.com"))
    response = member.post(
        f"/api/rooms/{code}/permissions",
        json={"user_id": member_id, "permission": "ADD_MEDIA", "enabled": True},
    )
    assert response.status_code == 403


def test_permission_updates_require_strict_boolean_and_owner_stays_implicit(
    app, client, register
):
    register(email="owner@example.com")
    code = create_room(client)
    member = app.test_client()
    register_user(member, "member@example.com")
    member.post("/join", data={"code": code})
    with app.app_context():
        owner_id = db.session.scalar(select(User.id).where(User.email == "owner@example.com"))
        member_id = db.session.scalar(select(User.id).where(User.email == "member@example.com"))

    ambiguous = client.post(
        f"/api/rooms/{code}/permissions",
        json={"user_id": member_id, "permission": "ADD_MEDIA", "enabled": "false"},
    )
    assert ambiguous.status_code == 400
    owner_change = client.post(
        f"/api/rooms/{code}/permissions",
        json={"user_id": owner_id, "permission": "ADD_MEDIA", "enabled": False},
    )
    assert owner_change.status_code == 403
    owner_state = client.get(f"/api/rooms/{code}/state").get_json()["state"]
    assert set(owner_state["capabilities"]) == {permission.value for permission in Permission}
    member_state = member.get(f"/api/rooms/{code}/state").get_json()["state"]
    assert member_state["capabilities"] == []


def test_guest_session_identity_socket_and_request_flow(app, client, register):
    register(email="owner@example.com")
    code = create_room(client)
    add_ready_media(app, code)
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        room.playing = True
        db.session.commit()

    guest = app.test_client()
    assert guest.get(f"/session/{code}").status_code == 200
    with guest.session_transaction() as guest_session:
        first_identity = guest_session["guest_id"]
    assert guest.get(f"/session/{code}").status_code == 200
    with guest.session_transaction() as guest_session:
        assert guest_session["guest_id"] == first_identity

    guest_socket = socketio.test_client(app, flask_test_client=guest)
    guest_socket.emit("room:join", {"code": code})
    state = next(
        event["args"][0]
        for event in guest_socket.get_received()
        if event["name"] == "room:state"
    )
    assert state["identity"]["kind"] == "guest"
    assert state["capabilities"] == []
    assert state["queue"]
    assert guest.delete(
        f"/api/rooms/{code}/queue/{state['queue'][0]['id']}",
        json={"expected_queue_version": state["queue_version"]},
    ).status_code == 403
    guest_socket.emit(
        "playback:command",
        {
            "code": code,
            "action": "pause",
            "position": 1,
            "expected_playback_version": state["playback_version"],
            "client_action_id": "guestpause01",
        },
    )
    assert any(
        event["name"] == "error" and event["args"][0]["code"] == "forbidden"
        for event in guest_socket.get_received()
    )

    created = guest.post(
        f"/api/rooms/{code}/requests",
        json={
            "request_type": "PAUSE",
            "payload": {},
            "client_request_id": "guestrequest01",
        },
    )
    assert created.status_code == 201
    request_id = created.get_json()["request"]["id"]
    approved = client.post(
        f"/api/rooms/{code}/requests/{request_id}/resolve",
        json={"resolution": "approved"},
    )
    assert approved.status_code == 200
    assert approved.get_json()["request"]["status"] == "approved"
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        assert room.playing is False
    assert client.post(
        f"/api/rooms/{code}/requests/{request_id}/resolve",
        json={"resolution": "approved"},
    ).status_code == 409
    assert guest.post(
        f"/api/rooms/{code}/requests/{request_id}/resolve",
        json={"resolution": "dismissed"},
    ).status_code == 403
    guest_socket.disconnect()


def test_pause_request_approval_uses_effective_authoritative_position(
    app, client, register
):
    register()
    code = create_room(client)
    add_ready_media(app, code)
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        room.position = 5
        room.playing = True
        room.playback_updated_at = datetime.now(UTC) - timedelta(seconds=20)
        db.session.commit()

    guest = app.test_client()
    guest.get(f"/session/{code}")
    request_item = guest.post(
        f"/api/rooms/{code}/requests",
        json={
            "request_type": "PAUSE",
            "payload": {},
            "client_request_id": "pauseeffective1",
        },
    ).get_json()["request"]
    approved = client.post(
        f"/api/rooms/{code}/requests/{request_item['id']}/resolve",
        json={"resolution": "approved"},
    )
    assert approved.status_code == 200
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        assert room.playing is False
        assert 24 <= room.position <= 30


def test_guest_request_requires_csrf_when_enabled(app, client, register):
    register()
    code = create_room(client)
    guest = app.test_client()
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        page = guest.get(f"/session/{code}")
        token_match = re.search(
            rb'<meta name="csrf-token" content="([^"]+)"', page.data
        )
        assert token_match
        body = {
            "request_type": "PAUSE",
            "payload": {},
            "client_request_id": "guestcsrf001",
        }
        assert guest.post(f"/api/rooms/{code}/requests", json=body).status_code == 400
        allowed = guest.post(
            f"/api/rooms/{code}/requests",
            json=body,
            headers={"X-CSRFToken": token_match.group(1).decode()},
        )
        assert allowed.status_code == 201
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_guest_socket_room_code_failures_are_bounded_across_reconnects(
    app, client, register
):
    register()
    code = create_room(client)
    guest = app.test_client()
    guest.get(f"/session/{code}")
    socket_client = socketio.test_client(app, flask_test_client=guest)
    for index in range(10):
        socket_client.emit("room:join", {"code": f"BAD{index:05d}"})
        if not socket_client.is_connected():
            break
    assert not socket_client.is_connected()

    reconnected = socketio.test_client(app, flask_test_client=guest)
    assert reconnected.is_connected()
    reconnected.emit("room:join", {"code": "NOPE0000"})
    assert not reconnected.is_connected()


def test_request_endpoint_has_per_identity_rate_limit(app, client, register):
    register()
    code = create_room(client)
    guest = app.test_client()
    guest.get(f"/session/{code}")
    app.config["RATELIMIT_ENABLED"] = True
    from movie_theater import limiter

    limiter.reset()
    try:
        for index in range(30):
            response = guest.post(
                f"/api/rooms/{code}/requests",
                json={
                    "request_type": "INVALID",
                    "payload": {},
                    "client_request_id": f"ratelimit{index:08d}",
                },
            )
            assert response.status_code == 400
        blocked = guest.post(
            f"/api/rooms/{code}/requests",
            json={
                "request_type": "INVALID",
                "payload": {},
                "client_request_id": "ratelimitblocked",
            },
        )
        assert blocked.status_code == 429
    finally:
        app.config["RATELIMIT_ENABLED"] = False
        limiter.reset()


def test_requests_are_validated_idempotent_bounded_and_cross_room_safe(
    app, client, register
):
    register(email="owner@example.com")
    code = create_room(client)
    media = save_direct(client, code)["room_media_id"]
    queued = client.post(
        f"/api/rooms/{code}/queue",
        json={"room_media_id": media, "expected_queue_version": 0},
    ).get_json()
    entry_id = queued["queue_entry_id"]
    other_code = create_room(client, "Other")
    other_media = save_direct(client, other_code, "Other")["room_media_id"]
    other_entry = client.post(
        f"/api/rooms/{other_code}/queue",
        json={"room_media_id": other_media, "expected_queue_version": 0},
    ).get_json()["queue_entry_id"]

    guest = app.test_client()
    guest.get(f"/session/{code}")
    invalid_seek = guest.post(
        f"/api/rooms/{code}/requests",
        json={"request_type": "SEEK", "payload": {"position": float("inf")}, "client_request_id": "invalidseek01"},
    )
    assert invalid_seek.status_code == 400
    string_seek = guest.post(
        f"/api/rooms/{code}/requests",
        json={"request_type": "SEEK", "payload": {"position": "30"}, "client_request_id": "stringseek01"},
    )
    assert string_seek.status_code == 400
    cross_room = guest.post(
        f"/api/rooms/{code}/requests",
        json={"request_type": "REMOVE_QUEUE_ENTRY", "payload": {"queue_entry_id": other_entry}, "client_request_id": "crossroom001"},
    )
    assert cross_room.status_code == 404

    body = {
        "request_type": "REMOVE_QUEUE_ENTRY",
        "payload": {"queue_entry_id": entry_id},
        "client_request_id": "idempotent001",
    }
    first = guest.post(f"/api/rooms/{code}/requests", json=body)
    second = guest.post(f"/api/rooms/{code}/requests", json=body)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.get_json()["request"]["id"] == second.get_json()["request"]["id"]

    latest = None
    for index in range(4):
        response = guest.post(
            f"/api/rooms/{code}/requests",
            json={"request_type": "PAUSE", "payload": {}, "client_request_id": f"pending{index:05d}"},
        )
        assert response.status_code == 201
        latest = response.get_json()["request"]
    replaced = guest.post(
        f"/api/rooms/{code}/requests",
        json={"request_type": "PLAY", "payload": {}, "client_request_id": "pendinglimit1"},
    )
    assert replaced.status_code == 201
    assert replaced.get_json()["request"]["request_type"] == "PLAY"
    owner_state = client.get(f"/api/rooms/{code}/state").get_json()["state"]
    guest_state = guest.get(f"/api/rooms/{code}/state").get_json()["state"]
    assert len(owner_state["requests"]) == 1
    assert owner_state["requests"][0]["id"] == replaced.get_json()["request"]["id"]
    assert owner_state["requests"][0]["status"] == "pending"
    assert len(guest_state["requests"]) == 1
    assert guest_state["requests"][0]["request_type"] == "PLAY"
    with app.app_context():
        pending = db.session.scalars(
            select(RoomRequest).where(
                RoomRequest.status == "pending",
                RoomRequest.room_id == db.session.scalar(
                    select(WatchRoom.id).where(WatchRoom.code == code)
                ),
            )
        ).all()
        assert len(pending) == 1
        assert latest is not None
        assert db.session.get(RoomRequest, latest["id"]).status == "dismissed"


def test_requests_reject_extra_fields_bad_probe_and_idempotency_key_reuse(
    app, client, register
):
    register()
    code = create_room(client)
    guest = app.test_client()
    guest.get(f"/session/{code}")

    extra = guest.post(
        f"/api/rooms/{code}/requests",
        json={
            "request_type": "PLAY",
            "payload": {"admin": True},
            "client_request_id": "strictplay01",
        },
    )
    assert extra.status_code == 400
    bad_id = guest.post(
        f"/api/rooms/{code}/requests",
        json={"request_type": "PLAY", "payload": {}, "client_request_id": 12345678},
    )
    assert bad_id.status_code == 400
    bad_probe = guest.post(
        f"/api/rooms/{code}/requests",
        json={
            "request_type": "ADD_DIRECT_URL",
            "payload": {
                "title": "Bad probe",
                "url": "https://media.example.com/bad.mp4",
                "probe_result": "not-a-probe-result",
            },
            "client_request_id": "badprobe001",
        },
    )
    assert bad_probe.status_code == 400

    first = guest.post(
        f"/api/rooms/{code}/requests",
        json={"request_type": "PLAY", "payload": {}, "client_request_id": "reusedkey001"},
    )
    assert first.status_code == 201
    reused = guest.post(
        f"/api/rooms/{code}/requests",
        json={"request_type": "PAUSE", "payload": {}, "client_request_id": "reusedkey001"},
    )
    assert reused.status_code == 409


def test_approved_direct_url_request_reuses_normal_command_without_queueing(
    app, client, register
):
    register()
    code = create_room(client)
    guest = app.test_client()
    guest.get(f"/session/{code}")
    created = guest.post(
        f"/api/rooms/{code}/requests",
        json={
            "request_type": "ADD_DIRECT_URL",
            "payload": {
                "title": "Requested direct media",
                "url": "https://media.example.com/requested.mp4",
                "probe_result": "playable",
            },
            "client_request_id": "directreq001",
        },
    )
    assert created.status_code == 201
    request_id = created.get_json()["request"]["id"]
    with patch("movie_theater.requests.get") as get, patch(
        "movie_theater.requests.post"
    ) as post:
        approved = client.post(
            f"/api/rooms/{code}/requests/{request_id}/resolve",
            json={"resolution": "approved"},
        )
    get.assert_not_called()
    post.assert_not_called()
    assert approved.status_code == 200
    state = approved.get_json()["state"]
    assert len(state["library"]) == 1
    assert state["queue"] == []


@pytest.mark.parametrize("resolution", ["denied", "dismissed"])
def test_request_denial_and_dismissal(app, client, register, resolution):
    register()
    code = create_room(client)
    guest = app.test_client()
    guest.get(f"/session/{code}")
    created = guest.post(
        f"/api/rooms/{code}/requests",
        json={"request_type": "PLAY", "payload": {}, "client_request_id": f"resolve{resolution}1"},
    ).get_json()["request"]
    response = client.post(
        f"/api/rooms/{code}/requests/{created['id']}/resolve",
        json={"resolution": resolution},
    )
    assert response.status_code == 200
    assert response.get_json()["request"]["status"] == resolution


def test_expired_duplicate_and_deleted_resource_approvals_are_safe(app, client, register):
    register()
    code = create_room(client)
    media = save_direct(client, code)["room_media_id"]
    queued = client.post(
        f"/api/rooms/{code}/queue",
        json={"room_media_id": media, "expected_queue_version": 0},
    ).get_json()
    guest = app.test_client()
    guest.get(f"/session/{code}")

    expired = guest.post(
        f"/api/rooms/{code}/requests",
        json={"request_type": "PAUSE", "payload": {}, "client_request_id": "expiredreq01"},
    ).get_json()["request"]
    with app.app_context():
        item = db.session.get(RoomRequest, expired["id"])
        item.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.session.commit()
    response = client.post(
        f"/api/rooms/{code}/requests/{expired['id']}/resolve",
        json={"resolution": "approved"},
    )
    assert response.status_code == 409
    with app.app_context():
        assert db.session.get(RoomRequest, expired["id"]).status == "expired"

    removable = guest.post(
        f"/api/rooms/{code}/requests",
        json={"request_type": "REMOVE_QUEUE_ENTRY", "payload": {"queue_entry_id": queued["queue_entry_id"]}, "client_request_id": "deleteres001"},
    ).get_json()["request"]
    client.delete(
        f"/api/rooms/{code}/queue/{queued['queue_entry_id']}",
        json={"expected_queue_version": queued["state"]["queue_version"]},
    )
    deleted = client.post(
        f"/api/rooms/{code}/requests/{removable['id']}/resolve",
        json={"resolution": "approved"},
    )
    assert deleted.status_code == 404
    with app.app_context():
        assert db.session.get(RoomRequest, removable["id"]).status == "pending"


def test_reviewer_must_also_hold_action_permission(app, client, register):
    register(email="owner@example.com")
    code = create_room(client)
    add_ready_media(app, code)
    reviewer = app.test_client()
    register_user(reviewer, "reviewer@example.com")
    reviewer.post("/join", data={"code": code})
    with app.app_context():
        reviewer_id = db.session.scalar(select(User.id).where(User.email == "reviewer@example.com"))
    assert client.post(
        f"/api/rooms/{code}/permissions",
        json={"user_id": reviewer_id, "permission": "REVIEW_REQUESTS", "enabled": True},
    ).status_code == 200
    guest = app.test_client()
    guest.get(f"/session/{code}")
    request_item = guest.post(
        f"/api/rooms/{code}/requests",
        json={"request_type": "SEEK", "payload": {"position": 30}, "client_request_id": "reviewseek01"},
    ).get_json()["request"]
    response = reviewer.post(
        f"/api/rooms/{code}/requests/{request_item['id']}/resolve",
        json={"resolution": "approved"},
    )
    assert response.status_code == 403
    with app.app_context():
        assert db.session.get(RoomRequest, request_item["id"]).status == "pending"


def test_generic_playback_command_is_durably_idempotent(app, client, register):
    register()
    code = create_room(client)
    add_ready_media(app, code)
    socket_client = socketio.test_client(app, flask_test_client=client)
    socket_client.emit("room:join", {"code": code})
    state = next(
        event["args"][0]
        for event in socket_client.get_received()
        if event["name"] == "room:state"
    )
    command = {
        "code": code,
        "action": "play",
        "position": 4,
        "expected_playback_version": state["playback_version"],
        "client_action_id": "durableplay001",
    }
    socket_client.emit("playback:command", command)
    socket_client.get_received()
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        version_after_first = room.playback_version
        assert db.session.scalar(select(func.count(RoomCommandReceipt.id))) == 1
    socket_client.emit("playback:command", command)
    duplicate = socket_client.get_received()
    assert any(
        event["name"] == "playback:updated" and event["args"][0].get("duplicate")
        for event in duplicate
    )
    with app.app_context():
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        assert room.playback_version == version_after_first
    socket_client.disconnect()


def test_playback_command_rejects_idempotency_reuse_and_invalid_numbers(
    app, client, register
):
    register()
    code = create_room(client)
    add_ready_media(app, code)
    socket_client = socketio.test_client(app, flask_test_client=client)
    socket_client.emit("room:join", {"code": code})
    state = next(
        event["args"][0]
        for event in socket_client.get_received()
        if event["name"] == "room:state"
    )
    socket_client.emit(
        "playback:command",
        {
            "code": code,
            "action": "play",
            "position": 0,
            "expected_playback_version": state["playback_version"],
            "client_action_id": "reusecommand1",
        },
    )
    first_events = socket_client.get_received()
    updated = next(
        event["args"][0]
        for event in first_events
        if event["name"] == "playback:updated" and "playback_version" in event["args"][0]
    )
    socket_client.emit(
        "playback:command",
        {
            "code": code,
            "action": "pause",
            "position": 1,
            "expected_playback_version": updated["playback_version"],
            "client_action_id": "reusecommand1",
        },
    )
    assert any(
        event["name"] == "error" and event["args"][0]["code"] == "conflict"
        for event in socket_client.get_received()
    )
    socket_client.emit(
        "playback:command",
        {
            "code": code,
            "action": "seek",
            "position": 604801,
            "expected_playback_version": updated["playback_version"],
            "client_action_id": "hugeposition1",
        },
    )
    assert any(
        event["name"] == "error" and event["args"][0]["code"] == "invalid_command"
        for event in socket_client.get_received()
    )
    socket_client.emit(
        "playback:command",
        {
            "code": code,
            "action": "pause",
            "position": 1,
            "expected_playback_version": 1.5,
            "client_action_id": "floatversion1",
        },
    )
    assert any(
        event["name"] == "error" and event["args"][0]["code"] == "conflict"
        for event in socket_client.get_received()
    )
    socket_client.disconnect()
