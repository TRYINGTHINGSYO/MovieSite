import hashlib

from sqlalchemy import func, select

from models import (
    BrowserClient,
    BrowserLocalSource,
    MediaAsset,
    MediaSource,
    QueueEntry,
    RoomMedia,
    User,
    db,
)
from movie_theater import socketio
from test_rooms import create_room


TOKEN_A = "a" * 64
TOKEN_B = "b" * 64
STORAGE_KEY = "c" * 64


def browser_headers(token=TOKEN_A):
    return {"X-Browser-Client-Token": token}


def register_browser(client, token=TOKEN_A):
    return client.post(
        "/api/browser-clients/register",
        json={},
        headers=browser_headers(token),
    )


def local_payload(**overrides):
    payload = {
        "storage_key": STORAGE_KEY,
        "original_filename": "movie.mp4",
        "mime_type": "video/mp4",
        "byte_size": 123456,
        "duration": 321.5,
    }
    payload.update(overrides)
    return payload


def create_local(client, code, token=TOKEN_A, **overrides):
    return client.post(
        f"/api/rooms/{code}/media/browser-local",
        json=local_payload(**overrides),
        headers=browser_headers(token),
    )


def test_browser_registration_stores_digest_and_is_idempotent(
    app, client, register
):
    register()
    first = register_browser(client)
    second = register_browser(client)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()["browser_client_id"] == second.get_json()[
        "browser_client_id"
    ]

    with app.app_context():
        browser = db.session.scalar(select(BrowserClient))
        assert browser.client_key == hashlib.sha256(bytes.fromhex(TOKEN_A)).hexdigest()
        assert browser.client_key != TOKEN_A
        assert db.session.scalar(select(func.count(BrowserClient.id))) == 1


def test_browser_registration_requires_auth_token_and_strict_body(
    app, client, register
):
    assert register_browser(client).status_code == 401
    register()
    assert client.post("/api/browser-clients/register", json={}).status_code == 403
    assert register_browser(client, "not-a-token").status_code == 403
    assert client.post(
        "/api/browser-clients/register",
        json={"fingerprint": "forbidden"},
        headers=browser_headers(),
    ).status_code == 400


def test_guest_cannot_register_browser_local_media(app, client, register):
    register()
    code = create_room(client)
    guest = app.test_client()
    assert guest.get(f"/session/{code}").status_code == 200
    assert create_local(guest, code).status_code == 401


def test_local_source_registration_is_idempotent_and_does_not_queue(
    app, client, register
):
    register()
    code = create_room(client)
    browser_id = register_browser(client).get_json()["browser_client_id"]

    first = create_local(client, code)
    second = create_local(client, code)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.get_json()["created"] is True
    assert second.get_json()["created"] is False
    assert first.get_json()["media_asset_id"] == second.get_json()["media_asset_id"]
    assert first.get_json()["source_id"] == second.get_json()["source_id"]

    source_payload = first.get_json()["state"]["library"][0]["sources"][0]
    assert source_payload["availability"] == "AVAILABLE_THIS_BROWSER"
    assert source_payload["storage_key"] == STORAGE_KEY
    with app.app_context():
        assert db.session.scalar(select(func.count(MediaAsset.id))) == 1
        assert db.session.scalar(select(func.count(MediaSource.id))) == 1
        assert db.session.scalar(select(func.count(RoomMedia.id))) == 1
        assert db.session.scalar(select(func.count(QueueEntry.id))) == 0
        local = db.session.scalar(select(BrowserLocalSource))
        assert local.browser_client_id == browser_id
        assert local.storage_key == STORAGE_KEY
        assert local.source.status == "ready"
        assert local.source.byte_size == 123456


def test_local_source_rejects_forged_proof_storage_key_and_metadata_reuse(
    app, client, register
):
    register()
    code = create_room(client)
    register_browser(client)

    assert create_local(client, code, TOKEN_B).status_code == 403
    assert create_local(client, code, storage_key="../../movie.mp4").status_code == 400
    assert create_local(client, code).status_code == 201
    assert create_local(client, code, byte_size=999).status_code == 409
    assert create_local(client, code, extra_field="nope").status_code == 400


def test_one_browser_cannot_claim_another_browser_on_same_account(
    app, client, register
):
    register()
    code = create_room(client)
    register_browser(client, TOKEN_A)
    register_browser(client, TOKEN_B)
    created = create_local(client, code, TOKEN_A).get_json()

    wrong_browser = client.post(
        f"/api/rooms/{code}/media/browser-local/{created['source_id']}/availability",
        json={"available": False},
        headers=browser_headers(TOKEN_B),
    )
    assert wrong_browser.status_code == 403
    with app.app_context():
        assert db.session.get(MediaSource, created["source_id"]).status == "ready"


def test_cross_account_browser_proof_and_source_ownership_are_rejected(
    app, client, register
):
    register(email="owner@example.com")
    code = create_room(client)
    register_browser(client, TOKEN_A)
    created = create_local(client, code, TOKEN_A).get_json()

    other = app.test_client()
    other.post(
        "/register",
        data={
            "email": "other@example.com",
            "password": "correct-horse",
            "display_name": "Other",
        },
    )
    assert register_browser(other, TOKEN_A).status_code == 200
    assert create_local(other, code, TOKEN_A, storage_key="d" * 64).status_code == 403
    assert other.post(
        f"/api/rooms/{code}/media/browser-local/{created['source_id']}/availability",
        json={"available": False},
        headers=browser_headers(TOKEN_A),
    ).status_code == 403


def test_duplicate_registration_recovers_after_add_media_is_revoked(
    app, client, register
):
    register(email="owner@example.com")
    code = create_room(client)
    member = app.test_client()
    member.post(
        "/register",
        data={
            "email": "member@example.com",
            "password": "correct-horse",
            "display_name": "Member",
        },
    )
    assert member.post("/join", data={"code": code}).status_code == 302
    with app.app_context():
        member_id = db.session.scalar(
            select(User.id).where(User.email == "member@example.com")
        )

    grant = client.post(
        f"/api/rooms/{code}/permissions",
        json={
            "user_id": member_id,
            "permission": "ADD_MEDIA",
            "enabled": True,
        },
    )
    assert grant.status_code == 200
    assert register_browser(member).status_code == 200
    created = create_local(member, code)
    assert created.status_code == 201

    revoke = client.post(
        f"/api/rooms/{code}/permissions",
        json={
            "user_id": member_id,
            "permission": "ADD_MEDIA",
            "enabled": False,
        },
    )
    assert revoke.status_code == 200
    duplicate = create_local(member, code)
    assert duplicate.status_code == 200
    assert duplicate.get_json()["created"] is False
    assert duplicate.get_json()["media_asset_id"] == created.get_json()[
        "media_asset_id"
    ]
    assert create_local(member, code, storage_key="d" * 64).status_code == 403


def test_missing_local_data_preserves_logical_media_and_can_be_restored(
    app, client, register
):
    register()
    code = create_room(client)
    register_browser(client)
    created = create_local(client, code).get_json()
    source_id = created["source_id"]
    media_asset_id = created["media_asset_id"]
    room_media_id = created["room_media_id"]
    path = f"/api/rooms/{code}/media/browser-local/{source_id}/availability"

    missing = client.post(
        path,
        json={"available": False},
        headers=browser_headers(),
    )
    assert missing.status_code == 200
    source_payload = missing.get_json()["state"]["library"][0]["sources"][0]
    assert source_payload["availability"] == "LOCAL_DATA_MISSING"
    assert source_payload["storage_key"] == STORAGE_KEY
    hidden_missing = client.get(f"/api/rooms/{code}/state").get_json()["state"]
    hidden_source = hidden_missing["library"][0]["sources"][0]
    assert hidden_source["availability"] == "LOCAL_DATA_MISSING"
    assert "storage_key" not in hidden_source

    restored = client.post(
        path,
        json={"available": True},
        headers=browser_headers(),
    )
    assert restored.status_code == 200
    assert restored.get_json()["state"]["library"][0]["sources"][0][
        "availability"
    ] == "AVAILABLE_THIS_BROWSER"
    with app.app_context():
        assert db.session.get(MediaAsset, media_asset_id) is not None
        assert db.session.get(RoomMedia, room_media_id) is not None
        assert db.session.get(MediaSource, source_id).status == "ready"


def test_storage_key_hidden_without_proof_and_available_on_authenticated_socket(
    app, client, register
):
    register()
    code = create_room(client)
    register_browser(client)
    created = create_local(client, code).get_json()
    queued = client.post(
        f"/api/rooms/{code}/queue",
        json={
            "room_media_id": created["room_media_id"],
            "expected_queue_version": 0,
        },
        headers=browser_headers(),
    )
    assert queued.status_code == 201

    without_proof = client.get(f"/api/rooms/{code}/state").get_json()["state"]
    hidden_source = without_proof["library"][0]["sources"][0]
    assert "storage_key" not in hidden_source
    assert hidden_source["availability"] == "LOCAL_OWNER_OFFLINE"
    assert "storage_key" not in without_proof["queue"][0]

    with_proof = client.get(
        f"/api/rooms/{code}/state", headers=browser_headers()
    ).get_json()["state"]
    assert with_proof["queue"][0]["storage_key"] == STORAGE_KEY

    socket_client = socketio.test_client(
        app,
        flask_test_client=client,
        auth={"browser_client_token": TOKEN_A},
    )
    assert socket_client.is_connected()
    socket_client.emit("room:join", {"code": code})
    state = next(
        event["args"][0]
        for event in socket_client.get_received()
        if event["name"] == "room:state"
    )
    local_source = state["library"][0]["sources"][0]
    assert local_source["storage_key"] == STORAGE_KEY
    assert local_source["availability"] == "AVAILABLE_THIS_BROWSER"
    socket_client.disconnect()


def test_filename_is_sanitized_and_availability_payload_is_strict(
    app, client, register
):
    register()
    code = create_room(client)
    register_browser(client)
    created = create_local(
        client,
        code,
        original_filename='<img src=x onerror="alert(1)">.mp4',
    )
    assert created.status_code == 201
    item = created.get_json()["state"]["library"][0]
    assert "<" not in item["name"]
    assert "onerror=" not in item["name"]
    assert client.post(
        f"/api/rooms/{code}/media/browser-local/{created.get_json()['source_id']}/availability",
        json={"available": "false"},
        headers=browser_headers(),
    ).status_code == 400
