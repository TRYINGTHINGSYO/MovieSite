"""Movie Theater Watch Party — Flask + SocketIO + Mux synced rooms."""

from __future__ import annotations

import os
import re
import secrets
import string
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import requests
from flask import Flask, Response, flash, redirect, render_template, request, session, stream_with_context, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_migrate import Migrate
from flask_socketio import SocketIO, disconnect, emit, join_room, leave_room
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from authorization import (
    ALL_PERMISSIONS,
    Actor,
    AuthorizationError,
    Permission,
    RoomUnavailableError,
    actor_for_guest,
    actor_for_user,
    can_view_room,
    grant_permission,
    lock_room_for,
    permissions_for,
    require_permission,
    revoke_permission,
)
from browser_local import (
    BrowserClientProofError,
    BrowserLocalConflictError,
    create_browser_local_media,
    prove_browser_client,
    register_browser_client,
    update_browser_local_availability,
)
from direct_urls import (
    DirectUrlError,
    PLAYABLE_PROBE_RESULTS,
    validate_direct_url,
    validate_probe_result,
)
from link_extract import (
    EXTRACTOR_DIRECT,
    EXTRACTOR_YT_DLP,
    extract_clip,
    forwarded_stream_headers,
    open_media_stream,
)
from media_sources import queue_entry_to_public, room_media_to_public
from models import (
    BrowserClient,
    MediaSource,
    MuxMediaSource,
    QueueEntry,
    RoomCommandReceipt,
    RoomMedia,
    RoomMembership,
    RoomMemberPermission,
    RoomRequest,
    User,
    WatchRoom,
    db,
)
from request_commands import (
    RequestConflictError,
    RequestValidationError,
    create_room_request,
    request_to_public,
    resolve_room_request,
    visible_requests,
)
from room_commands import (
    CommandError,
    ResourceNotFoundError,
    VersionConflictError,
    add_saved_media_to_queue,
    clear_upcoming_queue,
    complete_current_queue_entry,
    create_direct_media,
    create_mux_media,
    mux_source_for_entry,
    new_id,
    queue_entries_for,
    queue_entry_for,
    remove_queue_entry,
    reorder_queue,
    schedule_mux_cleanup,
    select_queue_entry,
    update_playback,
)

app = Flask(__name__)
on_railway = bool(
    os.environ.get("RAILWAY_ENVIRONMENT_ID") or os.environ.get("RAILWAY_ENVIRONMENT")
)
if on_railway:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
secret_key = os.environ.get("SECRET_KEY")
if on_railway and not secret_key:
    raise RuntimeError("SECRET_KEY must be set in production")
app.config["SECRET_KEY"] = secret_key or secrets.token_hex(16)
database_url = os.environ.get("DATABASE_URL")
if on_railway and not database_url:
    raise RuntimeError("DATABASE_URL must be set in production")
database_url = database_url or "sqlite:///movie_theater.db"
if database_url.startswith("postgres://"):
    database_url = "postgresql+psycopg://" + database_url.removeprefix("postgres://")
elif database_url.startswith("postgresql://"):
    database_url = "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["WTF_CSRF_TIME_LIMIT"] = None
app.config["MAX_CONTENT_LENGTH"] = int(
    os.environ.get("MAX_CONTENT_LENGTH", str(64 * 1024))
)
app.config["RATELIMIT_STORAGE_URI"] = os.environ.get(
    "RATELIMIT_STORAGE_URI", "memory://"
)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
secure_cookie = os.environ.get("SESSION_COOKIE_SECURE")
app.config["SESSION_COOKIE_SECURE"] = (
    secure_cookie.lower() in ("1", "true", "yes")
    if secure_cookie is not None
    else on_railway or os.environ.get("FLASK_ENV") == "production"
)
direct_url_https = os.environ.get("DIRECT_URL_REQUIRE_HTTPS")
app.config["DIRECT_URL_REQUIRE_HTTPS"] = bool(
    on_railway
    or os.environ.get("FLASK_ENV") == "production"
    or (
        direct_url_https is not None
        and direct_url_https.lower() in ("1", "true", "yes")
    )
)

db.init_app(app)
Migrate(app, db)
CSRFProtect(app)
limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri=app.config["RATELIMIT_STORAGE_URI"],
    default_limits=[],
)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please sign in to continue."


@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/api/"):
        return {"error": "Authentication required"}, 401
    flash(login_manager.login_message)
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


socket_origins = [
    origin.strip()
    for origin in os.environ.get("SOCKETIO_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

socketio = SocketIO(
    app,
    cors_allowed_origins=socket_origins or None,
    async_mode="threading",
    logger=False,
    engineio_logger=False,
    ping_timeout=60,
    ping_interval=25,
)

CODE_ALPHABET = string.ascii_uppercase + string.digits
CODE_LEN = 8

sid_to_code: dict[str, str] = {}
sid_to_user: dict[str, int] = {}
sid_to_guest: dict[str, str] = {}
sid_to_browser_client: dict[str, str] = {}
sid_to_presence: dict[str, dict] = {}
identity_join_failures: dict[str, list[float]] = {}
user_to_sids: dict[int, set[str]] = {}
revoked_sids: set[str] = set()
viewer_counts: dict[str, int] = {}

MUX_TOKEN_ID = os.environ.get("MUX_TOKEN_ID", "").strip()
MUX_TOKEN_SECRET = os.environ.get("MUX_TOKEN_SECRET", "").strip()
MUX_API = "https://api.mux.com/video/v1"


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


def mux_configured() -> bool:
    return bool(MUX_TOKEN_ID and MUX_TOKEN_SECRET)


def mux_auth() -> tuple[str, str]:
    return (MUX_TOKEN_ID, MUX_TOKEN_SECRET)


def generate_code() -> str:
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        if db.session.scalar(select(WatchRoom.id).where(WatchRoom.code == code)) is None:
            return code


def room_for_code(code: str) -> WatchRoom | None:
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9]{8}", code):
        return None
    return db.session.scalar(
        select(WatchRoom).where(
            WatchRoom.code == code.upper(), WatchRoom.archived_at.is_(None)
        )
    )


def lock_room(room_id: int) -> WatchRoom | None:
    return db.session.scalar(
        select(WatchRoom)
        .where(WatchRoom.id == room_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def ensure_guest_id() -> str:
    guest_id = session.get("guest_id")
    try:
        normalized = str(uuid.UUID(str(guest_id)))
    except (TypeError, ValueError, AttributeError):
        normalized = str(uuid.uuid4())
        session["guest_id"] = normalized
    return normalized


def current_actor(*, create_guest: bool = True) -> Actor:
    if current_user.is_authenticated:
        browser_client_id = None
        token = request.headers.get("X-Browser-Client-Token")
        if token:
            try:
                browser_client_id = prove_browser_client(current_user.id, token).id
            except BrowserClientProofError:
                pass
        return actor_for_user(current_user, browser_client_id)
    guest_id = ensure_guest_id() if create_guest else session.get("guest_id")
    if not guest_id:
        return Actor(kind="anonymous")
    return actor_for_guest(str(guest_id))


def actor_label(actor: Actor) -> str:
    if actor.is_user:
        user = db.session.get(User, actor.user_id)
        if user is not None:
            return (user.display_name or f"Member {user.id}")[:80]
    if actor.is_guest:
        token = str(actor.guest_id).replace("-", "")[:6].upper()
        return f"Guest {token}"
    return "Viewer"


def user_can_access(room: WatchRoom | None) -> bool:
    return can_view_room(room, current_actor())


def current_index(room: WatchRoom) -> int | None:
    if room.current_queue_entry_id is None:
        return None
    entries = queue_entries_for(room.id)
    return next(
        (
            index
            for index, entry in enumerate(entries)
            if entry.id == room.current_queue_entry_id
        ),
        None,
    )


def public_state(room: WatchRoom, actor: Actor | None = None) -> dict:
    actor = actor or current_actor()
    entries = queue_entries_for(room.id)
    capabilities = permissions_for(room, actor)
    requests_for_actor = visible_requests(room, actor)
    return {
        "queue": [
            queue_entry_to_public(
                entry, browser_client_id=actor.browser_client_id
            )
            for entry in entries
        ],
        "library": [
            room_media_to_public(
                room_media, browser_client_id=actor.browser_client_id
            )
            for room_media in room.library_items
            if room_media.asset.deleted_at is None
        ],
        "current": current_index(room),
        "current_id": room.current_queue_entry_id,
        "playing": room.playing,
        "position": effective_position(room),
        "queue_version": room.queue_version,
        "playback_version": room.playback_version,
        "viewer_count": viewer_counts.get(room.code, 0),
        "mux": mux_configured(),
        "identity": {
            "kind": actor.kind,
            "key": actor.key,
            "label": actor_label(actor),
            "is_owner": actor.is_user and actor.user_id == room.owner_id,
        },
        "capabilities": sorted(permission.value for permission in capabilities),
        "people": room_people(room, actor),
        "presence": room_presence(room.code),
        "requests": [request_to_public(item) for item in requests_for_actor],
    }


def room_people(room: WatchRoom, actor: Actor) -> list[dict]:
    result = []
    memberships = db.session.scalars(
        select(RoomMembership)
        .where(RoomMembership.room_id == room.id)
        .order_by(RoomMembership.joined_at, RoomMembership.user_id)
    ).all()
    can_manage = Permission.MANAGE_MEMBERS in permissions_for(room, actor)
    for membership in memberships:
        user = membership.user
        member_actor = actor_for_user(user)
        item = {
            "user_id": user.id,
            "label": (user.display_name or f"Member {user.id}")[:80],
            "is_owner": user.id == room.owner_id,
        }
        if can_manage:
            item["permissions"] = sorted(
                permission.value for permission in permissions_for(room, member_actor)
            )
        result.append(item)
    return result


def room_presence(code: str) -> list[dict]:
    by_identity: dict[str, dict] = {}
    for sid, joined_code in sid_to_code.items():
        if joined_code != code:
            continue
        presence = sid_to_presence.get(sid)
        if presence:
            by_identity[presence["key"]] = dict(presence)
    return sorted(by_identity.values(), key=lambda item: (item["kind"], item["label"]))


def actor_for_sid(sid: str) -> Actor:
    user_id = sid_to_user.get(sid)
    if user_id is not None:
        return Actor(
            kind="user",
            user_id=user_id,
            browser_client_id=sid_to_browser_client.get(sid),
        )
    guest_id = sid_to_guest.get(sid)
    if guest_id:
        return actor_for_guest(guest_id)
    return Actor(kind="anonymous")


def emit_state_to_room(event: str, room: WatchRoom) -> None:
    for sid, joined_code in list(sid_to_code.items()):
        if joined_code == room.code:
            socketio.emit(event, public_state(room, actor_for_sid(sid)), to=sid)


def broadcast_room_updates(room: WatchRoom, *events: str) -> None:
    for event in events:
        emit_state_to_room(event, room)


def effective_position(room: WatchRoom) -> float:
    position = max(0.0, float(room.position or 0.0))
    if not room.playing or room.playback_updated_at is None:
        return position
    updated_at = room.playback_updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    elapsed = max(0.0, (datetime.now(UTC) - updated_at).total_seconds())
    return position + elapsed


def find_queue_item(room: WatchRoom, video_id: str) -> QueueEntry | None:
    return queue_entry_for(room.id, video_id)


def delete_mux_asset(asset_id: str | None) -> None:
    if not asset_id or not mux_configured():
        return
    try:
        requests.delete(
            f"{MUX_API}/assets/{asset_id}",
            auth=mux_auth(),
            timeout=30,
        )
    except requests.RequestException:
        pass


def refresh_mux_item(item: QueueEntry | RoomMedia) -> QueueEntry | RoomMedia:
    """Poll Mux and update the reusable media source behind a queue entry."""
    if not mux_configured():
        return item
    room_media = item.room_media if isinstance(item, QueueEntry) else item
    source_pair = next(
        (
            (source, source.mux)
            for source in room_media.asset.sources
            if source.source_type == "mux_upload" and source.mux is not None
        ),
        None,
    )
    if source_pair is None:
        return item
    source, mux = source_pair
    if source.status == "ready" and mux.playback_id:
        return item

    try:
        if mux.upload_id and not mux.asset_id:
            resp = requests.get(
                f"{MUX_API}/uploads/{mux.upload_id}",
                auth=mux_auth(),
                timeout=30,
            )
            if resp.ok:
                data = resp.json().get("data") or {}
                status = data.get("status")
                if status == "asset_created" and data.get("asset_id"):
                    mux.asset_id = data["asset_id"]
                    source.status = "processing"
                elif status in ("errored", "cancelled", "timed_out"):
                    source.status = "error"
                    source.error = f"Upload {status}"
                    return item

        if mux.asset_id:
            resp = requests.get(
                f"{MUX_API}/assets/{mux.asset_id}",
                auth=mux_auth(),
                timeout=30,
            )
            if resp.ok:
                data = resp.json().get("data") or {}
                asset_status = data.get("status")
                if asset_status == "ready":
                    playback_ids = data.get("playback_ids") or []
                    public = next(
                        (
                            playback
                            for playback in playback_ids
                            if (playback.get("policy") or "").lower() == "public"
                        ),
                        playback_ids[0] if playback_ids else None,
                    )
                    playback_id = (public or {}).get("id")
                    if playback_id:
                        mux.playback_id = playback_id
                        source.status = "ready"
                        room_media.asset.duration = data.get("duration")
                    else:
                        source.status = "processing"
                elif asset_status == "errored":
                    source.status = "error"
                    errors = data.get("errors") or {}
                    messages = errors.get("messages") if isinstance(errors, dict) else None
                    source.error = (
                        messages[0]
                        if isinstance(messages, list) and messages
                        else "Mux processing failed"
                    )
                else:
                    source.status = "processing"
    except requests.RequestException as exc:
        source.error = str(exc)

    return item


def delete_mux_upload(upload_id: str | None) -> None:
    if not upload_id or not mux_configured():
        return
    try:
        requests.delete(
            f"{MUX_API}/uploads/{upload_id}",
            auth=mux_auth(),
            timeout=30,
        )
    except requests.RequestException:
        pass


# HTTP routes


@app.route("/")
def landing():
    if current_user.is_authenticated:
        return redirect(url_for("saved_rooms"))
    return render_template("landing.html", mux_ready=mux_configured())


def rate_limit_key() -> str:
    user_id = session.get("_user_id")
    if user_id:
        return f"user:{user_id}"
    guest_id = session.get("guest_id")
    return f"guest:{guest_id}" if guest_id else f"ip:{get_remote_address()}"


def safe_next_url(target: str | None) -> bool:
    if not target:
        return False
    host = urlparse(request.host_url)
    candidate = urlparse(urljoin(request.host_url, target))
    return candidate.scheme in ("http", "https") and candidate.netloc == host.netloc


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("saved_rooms"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        display_name = (request.form.get("display_name") or "").strip()
        password = request.form.get("password") or ""
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Enter a valid email address.")
        elif len(password) < 10:
            flash("Password must be at least 10 characters.")
        elif db.session.scalar(select(User).where(User.email == email)):
            flash("An account with that email already exists.")
        else:
            user = User(email=email, display_name=display_name[:80] or None)
            user.set_password(password)
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("An account with that email already exists.")
            else:
                login_user(user)
                return redirect(url_for("saved_rooms"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("saved_rooms"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = db.session.scalar(select(User).where(User.email == email))
        if not user or not user.check_password(request.form.get("password") or ""):
            flash("Invalid email or password.")
        else:
            login_user(user)
            target = request.args.get("next")
            return redirect(target if safe_next_url(target) else url_for("saved_rooms"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    user_id = current_user.id
    for sid in user_to_sids.pop(user_id, set()):
        revoked_sids.add(sid)
        socketio.server.disconnect(sid, namespace="/")
    logout_user()
    return redirect(url_for("landing"))


@app.route("/rooms")
@login_required
def saved_rooms():
    memberships = db.session.scalars(
        select(RoomMembership)
        .join(RoomMembership.room)
        .where(
            RoomMembership.user_id == current_user.id,
            WatchRoom.archived_at.is_(None),
        )
        .order_by(WatchRoom.updated_at.desc())
    ).all()
    return render_template("rooms.html", memberships=memberships)


@app.route("/create", methods=["POST"])
@login_required
def create_session():
    code = generate_code()
    name = (request.form.get("name") or "").strip()[:120] or f"Room {code}"
    room = WatchRoom(code=code, name=name, owner_id=current_user.id)
    room.memberships.append(RoomMembership(user_id=current_user.id))
    db.session.add(room)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Could not create a room. Please try again.")
        return redirect(url_for("saved_rooms"))
    return redirect(url_for("session_room", code=code))


@app.route("/join", methods=["POST"])
@login_required
@limiter.limit("20 per minute", key_func=rate_limit_key)
def join_session():
    raw = (request.form.get("code") or "").strip().upper()
    code = re.sub(r"[^A-Z0-9]", "", raw)
    room = room_for_code(code)
    if not room:
        flash("No room found for that code. Check the invite and try again.")
        return redirect(url_for("saved_rooms"))
    membership = db.session.get(RoomMembership, (room.id, current_user.id))
    if membership is None:
        db.session.add(RoomMembership(room_id=room.id, user_id=current_user.id))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
    return redirect(url_for("session_room", code=code))


@app.route("/session/<code>")
@limiter.limit("60 per hour")
def session_room(code: str):
    room = room_for_code(code)
    if not room:
        flash("That room has ended or never existed.")
        return redirect(url_for("saved_rooms" if current_user.is_authenticated else "landing"))
    if not user_can_access(room):
        return {"error": "Room not found or inactive"}, 404
    actor = current_actor()
    return render_template(
        "room.html",
        code=room.code,
        room=room,
        mux_ready=mux_configured(),
        actor=actor,
    )


def expected_version(body: dict, field: str) -> int:
    value = body.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise VersionConflictError(f"{field} is required")
    version = value
    if version < 0:
        raise VersionConflictError(f"Invalid {field}")
    return version


def json_object_body() -> dict:
    body = request.get_json(silent=True)
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise CommandError("JSON request body must be an object")
    return body


def stable_id(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise CommandError(f"A stable {label} ID is required")
    candidate = value
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", candidate):
        raise CommandError(f"A stable {label} ID is required")
    return candidate


def media_title(value: object) -> str:
    if value is None or value == "":
        return "Direct media"
    if not isinstance(value, str):
        raise CommandError("Media title must be a string")
    title = value.strip()
    if len(title) > 255:
        raise CommandError("Media title is too long")
    return title or "Direct media"


def api_command_error(exc: Exception, room: WatchRoom | None = None):
    db.session.rollback()
    if isinstance(exc, VersionConflictError):
        fresh = db.session.get(WatchRoom, room.id) if room is not None else None
        payload = {"error": str(exc)}
        if fresh is not None:
            payload["state"] = public_state(fresh)
        return payload, 409
    if isinstance(exc, RequestConflictError):
        return {"error": str(exc)}, 409
    if isinstance(exc, BrowserLocalConflictError):
        return {"error": str(exc)}, 409
    if isinstance(exc, ResourceNotFoundError):
        return {"error": str(exc)}, 404
    if isinstance(exc, (RequestValidationError, DirectUrlError, CommandError)):
        return {"error": str(exc)}, 400
    if isinstance(exc, AuthorizationError):
        return {"error": str(exc)}, 403
    if isinstance(exc, RoomUnavailableError):
        return {"error": "Room not found or inactive"}, 404
    raise exc


def required_browser_client() -> BrowserClient:
    if not current_user.is_authenticated:
        raise BrowserClientProofError("Authentication is required")
    return prove_browser_client(
        current_user.id,
        request.headers.get("X-Browser-Client-Token"),
    )


@app.route("/api/browser-clients/register", methods=["POST"])
@login_required
@limiter.limit("30 per hour", key_func=rate_limit_key)
def register_current_browser_client():
    try:
        body = json_object_body()
        if body:
            raise CommandError("Browser registration does not accept metadata")
        browser_client = register_browser_client(
            current_user.id,
            request.headers.get("X-Browser-Client-Token"),
        )
        db.session.commit()
    except (AuthorizationError, CommandError) as exc:
        return api_command_error(exc)
    return {"ok": True, "browser_client_id": browser_client.id}


@app.route("/api/rooms/<code>/state", methods=["GET"])
@limiter.limit("120 per hour")
def room_state_api(code: str):
    room = room_for_code(code)
    if not can_view_room(room, current_actor()):
        return {"error": "Room not found or inactive"}, 404
    return {"state": public_state(room)}


@app.route("/api/rooms/<code>/media/browser-local", methods=["POST"])
@login_required
@limiter.limit("20 per hour", key_func=rate_limit_key)
def create_local_media(code: str):
    room = room_for_code(code)
    if room is None:
        return {"error": "Room not found or inactive"}, 404
    try:
        browser_client = required_browser_client()
        actor = actor_for_user(current_user, browser_client.id)
        body = json_object_body()
        allowed_fields = {
            "storage_key",
            "original_filename",
            "mime_type",
            "byte_size",
            "duration",
        }
        if set(body) - allowed_fields:
            raise CommandError("Unexpected browser-local media metadata")
        room, room_media, source, _local_source, created = (
            create_browser_local_media(
                room.id,
                actor,
                browser_client,
                storage_key=body.get("storage_key"),
                original_filename=body.get("original_filename"),
                mime_type=body.get("mime_type"),
                byte_size=body.get("byte_size"),
                duration=body.get("duration"),
            )
        )
        db.session.commit()
    except (AuthorizationError, RoomUnavailableError, CommandError) as exc:
        return api_command_error(exc, room)
    room = db.session.get(WatchRoom, room.id)
    broadcast_room_updates(room, "library:updated", "room:state")
    return {
        "ok": True,
        "created": created,
        "browser_client_id": browser_client.id,
        "media_asset_id": source.media_asset_id,
        "room_media_id": room_media.id,
        "source_id": source.id,
        "state": public_state(room, actor),
    }, 201 if created else 200


@app.route(
    "/api/rooms/<code>/media/browser-local/<source_id>/availability",
    methods=["POST"],
)
@login_required
@limiter.limit("60 per hour", key_func=rate_limit_key)
def update_local_media_availability(code: str, source_id: str):
    room = room_for_code(code)
    if room is None:
        return {"error": "Room not found or inactive"}, 404
    try:
        browser_client = required_browser_client()
        actor = actor_for_user(current_user, browser_client.id)
        body = json_object_body()
        if set(body) != {"available"}:
            raise CommandError("Availability requires exactly one boolean field")
        room, source, _local_source = update_browser_local_availability(
            room.id,
            actor,
            browser_client,
            stable_id(source_id, "browser-local source"),
            available=body.get("available"),
        )
        db.session.commit()
    except (AuthorizationError, RoomUnavailableError, CommandError) as exc:
        return api_command_error(exc, room)
    room = db.session.get(WatchRoom, room.id)
    broadcast_room_updates(room, "library:updated", "room:state")
    return {
        "ok": True,
        "source_id": source.id,
        "state": public_state(room, actor),
    }


@app.route("/api/rooms/<code>/media/direct-url", methods=["POST"])
@limiter.limit("20 per hour", key_func=rate_limit_key)
def create_direct_url_media(code: str):
    room = room_for_code(code)
    if room is None:
        return {"error": "Room not found or inactive"}, 404
    try:
        actor = current_actor()
        require_permission(room, actor, Permission.ADD_MEDIA)
        body = json_object_body()
        allowed_fields = {"title", "url", "probe_result", "enqueue"}
        if set(body) - allowed_fields:
            raise CommandError("Unexpected direct media field")
        enqueue_value = body.get("enqueue")
        if enqueue_value is None:
            enqueue = False
        elif isinstance(enqueue_value, bool):
            enqueue = enqueue_value
        else:
            raise CommandError("enqueue must be a boolean")
        capabilities = permissions_for(room, actor)
        enqueue = enqueue and Permission.MANAGE_QUEUE in capabilities
        play_now = enqueue and Permission.CONTROL_PLAYBACK in capabilities
        validated = validate_direct_url(
            body.get("url"),
            require_https=app.config["DIRECT_URL_REQUIRE_HTTPS"],
        )
        probe_result = validate_probe_result(body.get("probe_result"))
        extracted = None
        extractor = EXTRACTOR_DIRECT
        if probe_result not in PLAYABLE_PROBE_RESULTS:
            extracted = extract_clip(
                validated.normalized,
                require_https=app.config["DIRECT_URL_REQUIRE_HTTPS"],
            )
            probe_result = "playable"
            extractor = EXTRACTOR_YT_DLP
        raw_title = body.get("title")
        if raw_title in (None, "") and extracted is not None:
            title = extracted.title
        else:
            title = media_title(raw_title)
        room, room_media, _source, _direct = create_direct_media(
            room.id,
            actor,
            title,
            validated,
            probe_result=probe_result,
            extractor=extractor,
            duration=extracted.duration if extracted is not None else None,
            observed_content_type=(
                extracted.content_type if extracted is not None else None
            ),
            enqueue=enqueue,
            play_now=play_now,
        )
        db.session.commit()
    except (
        AuthorizationError,
        RoomUnavailableError,
        DirectUrlError,
        CommandError,
    ) as exc:
        return api_command_error(exc, room)
    room = db.session.get(WatchRoom, room.id)
    events = ["library:updated", "room:state"]
    if enqueue:
        events.extend(["queue:updated", "queue_updated", "playback:updated"])
    broadcast_room_updates(room, *events)
    return {
        "ok": True,
        "room_media_id": room_media.id,
        "state": public_state(room),
    }, 201


@app.route("/api/media/sources/<source_id>/stream", methods=["GET", "HEAD"])
def stream_extracted_source(source_id: str):
    try:
        source_key = stable_id(source_id, "media source")
    except CommandError as exc:
        return api_command_error(exc)
    source = db.session.get(MediaSource, source_key)
    direct = source.direct_url if source is not None else None
    if (
        source is None
        or source.deleted_at is not None
        or source.source_type != "direct_url"
        or direct is None
        or direct.extractor != EXTRACTOR_YT_DLP
    ):
        return {"error": "Media source not found"}, 404
    room = db.session.scalar(
        select(WatchRoom)
        .join(RoomMedia, RoomMedia.room_id == WatchRoom.id)
        .where(RoomMedia.media_asset_id == source.media_asset_id)
        .order_by(RoomMedia.created_at, RoomMedia.id)
    )
    actor = current_actor()
    if (
        not can_view_room(room, actor)
        or (source.asset is not None and source.asset.deleted_at is not None)
    ):
        return {"error": "Media source not found"}, 404
    try:
        extracted = extract_clip(
            direct.original_url,
            require_https=app.config["DIRECT_URL_REQUIRE_HTTPS"],
        )
        if request.method == "HEAD":
            upstream = open_media_stream(
                extracted.playback_url,
                headers=extracted.http_headers,
                range_header=request.headers.get("Range"),
                require_https=app.config["DIRECT_URL_REQUIRE_HTTPS"],
            )
            headers = forwarded_stream_headers(upstream)
            status = upstream.status_code
            upstream.close()
            return Response(status=status, headers=headers)
        upstream = open_media_stream(
            extracted.playback_url,
            headers=extracted.http_headers,
            range_header=request.headers.get("Range"),
            require_https=app.config["DIRECT_URL_REQUIRE_HTTPS"],
        )
    except DirectUrlError as exc:
        return api_command_error(exc, room)

    def generate():
        try:
            for chunk in upstream.iter_content(256 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        headers=forwarded_stream_headers(upstream),
        direct_passthrough=True,
    )


@app.route("/api/rooms/<code>/queue", methods=["POST"])
def queue_saved_media(code: str):
    room = room_for_code(code)
    if room is None:
        return {"error": "Room not found or inactive"}, 404
    try:
        actor = current_actor()
        require_permission(room, actor, Permission.MANAGE_QUEUE)
        body = json_object_body()
        room, entry = add_saved_media_to_queue(
            room.id,
            actor,
            stable_id(body.get("room_media_id"), "saved media"),
            expected_queue_version=expected_version(body, "expected_queue_version"),
        )
        db.session.commit()
    except (AuthorizationError, RoomUnavailableError, CommandError) as exc:
        return api_command_error(exc, room)
    broadcast_room_updates(room, "queue:updated", "room:state", "queue_updated")
    return {"ok": True, "queue_entry_id": entry.id, "state": public_state(room)}, 201


@app.route("/api/rooms/<code>/queue/<entry_id>", methods=["DELETE"])
def delete_queue_entry(code: str, entry_id: str):
    room = room_for_code(code)
    if room is None:
        return {"error": "Room not found or inactive"}, 404
    try:
        actor = current_actor()
        require_permission(room, actor, Permission.MANAGE_QUEUE)
        body = json_object_body()
        room = remove_queue_entry(
            room.id,
            actor,
            stable_id(entry_id, "queue entry"),
            expected_queue_version=expected_version(body, "expected_queue_version"),
        )
        db.session.commit()
    except (AuthorizationError, RoomUnavailableError, CommandError) as exc:
        return api_command_error(exc, room)
    broadcast_room_updates(
        room,
        "queue:updated",
        "playback:updated",
        "room:state",
        "video_selected",
    )
    return {"ok": True, "state": public_state(room)}


@app.route("/api/rooms/<code>/queue/order", methods=["PUT"])
def update_queue_order(code: str):
    room = room_for_code(code)
    if room is None:
        return {"error": "Room not found or inactive"}, 404
    try:
        actor = current_actor()
        require_permission(room, actor, Permission.MANAGE_QUEUE)
        body = json_object_body()
        ordered_ids = body.get("queue_entry_ids")
        if not isinstance(ordered_ids, list):
            raise CommandError("queue_entry_ids must be a list of stable IDs")
        ordered_ids = [stable_id(item, "queue entry") for item in ordered_ids]
        room = reorder_queue(
            room.id,
            actor,
            ordered_ids,
            expected_queue_version=expected_version(body, "expected_queue_version"),
        )
        db.session.commit()
    except (AuthorizationError, RoomUnavailableError, CommandError) as exc:
        return api_command_error(exc, room)
    broadcast_room_updates(room, "queue:updated", "room:state", "queue_updated")
    return {"ok": True, "state": public_state(room)}


@app.route("/api/rooms/<code>/queue/upcoming", methods=["DELETE"])
def delete_upcoming_queue(code: str):
    room = room_for_code(code)
    if room is None:
        return {"error": "Room not found or inactive"}, 404
    try:
        actor = current_actor()
        require_permission(room, actor, Permission.MANAGE_QUEUE)
        body = json_object_body()
        room = clear_upcoming_queue(
            room.id,
            actor,
            expected_queue_version=expected_version(body, "expected_queue_version"),
        )
        db.session.commit()
    except (AuthorizationError, RoomUnavailableError, CommandError) as exc:
        return api_command_error(exc, room)
    broadcast_room_updates(room, "queue:updated", "room:state", "queue_updated")
    return {"ok": True, "state": public_state(room)}


@app.route("/api/rooms/<code>/permissions", methods=["POST"])
def update_member_permission(code: str):
    viewed_room = room_for_code(code)
    if viewed_room is None:
        return {"error": "Room not found or inactive"}, 404
    actor = current_actor()
    if not actor.is_user or actor.user_id != viewed_room.owner_id:
        return {"error": "Only the room owner can manage permissions"}, 403
    try:
        body = json_object_body()
        target_user_id = body.get("user_id")
        if isinstance(target_user_id, bool) or not isinstance(target_user_id, int):
            raise ValueError
        permission_value = body.get("permission")
        if not isinstance(permission_value, str):
            raise ValueError
        permission = Permission(permission_value)
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError
        if enabled:
            grant_permission(viewed_room.id, actor, target_user_id, permission)
        else:
            revoke_permission(viewed_room.id, actor, target_user_id, permission)
        db.session.commit()
    except (TypeError, ValueError, CommandError):
        db.session.rollback()
        return {"error": "Invalid member or permission"}, 400
    except (AuthorizationError, RoomUnavailableError) as exc:
        return api_command_error(exc, viewed_room)
    room = db.session.get(WatchRoom, viewed_room.id)
    broadcast_room_updates(room, "permissions:updated", "room:state")
    return {"ok": True, "state": public_state(room)}


@app.route("/api/rooms/<code>/requests", methods=["POST"])
@limiter.limit("30 per hour", key_func=rate_limit_key)
def submit_room_request(code: str):
    room = room_for_code(code)
    actor = current_actor()
    if not can_view_room(room, actor):
        return {"error": "Room not found or inactive"}, 404
    try:
        body = json_object_body()
        item, created = create_room_request(
            room.id,
            actor,
            actor_label(actor),
            body.get("request_type"),
            body.get("payload"),
            body.get("client_request_id"),
            require_https=app.config["DIRECT_URL_REQUIRE_HTTPS"],
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        try:
            item, created = create_room_request(
                room.id,
                actor,
                actor_label(actor),
                body.get("request_type"),
                body.get("payload"),
                body.get("client_request_id"),
                require_https=app.config["DIRECT_URL_REQUIRE_HTTPS"],
            )
            db.session.commit()
        except (AuthorizationError, RoomUnavailableError, CommandError) as exc:
            return api_command_error(exc, room)
    except (AuthorizationError, RoomUnavailableError, CommandError) as exc:
        return api_command_error(exc, room)
    room = db.session.get(WatchRoom, room.id)
    broadcast_room_updates(room, "requests:updated", "room:state")
    return {"ok": True, "created": created, "request": request_to_public(item)}, (201 if created else 200)


@app.route("/api/rooms/<code>/requests/<request_id>/resolve", methods=["POST"])
def resolve_request_api(code: str, request_id: str):
    room = room_for_code(code)
    if room is None:
        return {"error": "Room not found or inactive"}, 404
    try:
        body = json_object_body()
        room, item = resolve_room_request(
            room.id,
            current_actor(),
            stable_id(request_id, "request"),
            body.get("resolution"),
            require_https=app.config["DIRECT_URL_REQUIRE_HTTPS"],
        )
        db.session.commit()
    except RequestConflictError as exc:
        db.session.commit()
        return {"error": str(exc), "state": public_state(room)}, 409
    except (AuthorizationError, RoomUnavailableError, CommandError) as exc:
        return api_command_error(exc, room)
    room = db.session.get(WatchRoom, room.id)
    broadcast_room_updates(
        room,
        "requests:updated",
        "queue:updated",
        "library:updated",
        "playback:updated",
        "room:state",
    )
    return {"ok": True, "request": request_to_public(item), "state": public_state(room)}


def mux_error_message(resp: requests.Response) -> str:
    status = resp.status_code
    body = ""
    try:
        payload = resp.json()
        error = payload.get("error") or {}
        if isinstance(error, dict):
            body = error.get("messages") or error.get("message") or ""
            if isinstance(body, list):
                body = "; ".join(str(message) for message in body)
        if not body:
            body = resp.text[:300]
    except Exception:
        body = resp.text[:300]

    if status in (401, 403):
        return (
            f"Mux auth failed (HTTP {status}). On Railway set MUX_TOKEN_ID and "
            "MUX_TOKEN_SECRET from Mux → Settings → Access Tokens (Video Read+Write). "
            "Do not use the Mux Data environment key. "
            f"Details: {body or 'unauthorized'}"
        )
    return f"Mux rejected the upload (HTTP {status}): {body or resp.reason}"


@app.route("/api/mux/create-upload/<code>", methods=["POST"])
@login_required
@limiter.limit("10 per hour", key_func=rate_limit_key)
def create_mux_upload(code: str):
    room = room_for_code(code)
    if not room:
        return {"error": "Room not found"}, 404
    try:
        require_permission(room, actor_for_user(current_user), Permission.ADD_MEDIA)
    except AuthorizationError as exc:
        return {"error": str(exc)}, 403
    if not mux_configured():
        return {
            "error": (
                "Mux is not configured. Set MUX_TOKEN_ID and MUX_TOKEN_SECRET "
                "on the Railway service (Mux → Settings → Access Tokens), then redeploy."
            )
        }, 503

    try:
        body = json_object_body()
        original = (secure_filename(body.get("filename") or "video") or "video")[:255]
        byte_size = int(body.get("size")) if body.get("size") is not None else None
    except (TypeError, ValueError, CommandError):
        return {"error": "Invalid file size"}, 400
    if byte_size is not None and byte_size < 0:
        return {"error": "Invalid file size"}, 400
    cors_origin = os.environ.get("MUX_CORS_ORIGIN", "*").strip() or "*"

    try:
        save_only = body.get("save_only", True)
        if not isinstance(save_only, bool):
            raise CommandError("save_only must be a boolean")
        if not save_only:
            raise CommandError(
                "Upload and queue are separate operations; save the upload first"
            )
        room, item, source, _mux = create_mux_media(
            room.id,
            actor_for_user(current_user),
            original,
            byte_size=byte_size,
            enqueue=False,
        )
        room_media = source.asset.room_links[0]
        db.session.commit()
    except AuthorizationError as exc:
        db.session.rollback()
        return {"error": str(exc)}, 403
    except RoomUnavailableError:
        db.session.rollback()
        return {"error": "Room not found"}, 404
    except CommandError as exc:
        db.session.rollback()
        return {"error": str(exc)}, 400
    except Exception:
        db.session.rollback()
        raise

    try:
        resp = requests.post(
            f"{MUX_API}/uploads",
            auth=mux_auth(),
            headers={"Content-Type": "application/json"},
            json={
                "cors_origin": cors_origin,
                "timeout": 3600,
                "new_asset_settings": {
                    "playback_policies": ["public"],
                    "video_quality": "basic",
                    "passthrough": f"{room.code}:{source.id}",
                },
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        source = db.session.get(MediaSource, source.id)
        source.status = "error"
        source.error = f"Mux request failed: {exc}"
        db.session.commit()
        broadcast_room_updates(room, "queue_updated", "queue:updated", "room:state")
        return {"error": f"Mux request failed: {exc}"}, 502

    if not resp.ok:
        source = db.session.get(MediaSource, source.id)
        source.status = "error"
        error_message = mux_error_message(resp)
        source.error = error_message
        db.session.commit()
        broadcast_room_updates(room, "queue_updated", "queue:updated", "room:state")
        return {"error": error_message, "status": resp.status_code}, 502

    data = (resp.json() or {}).get("data") or {}
    upload_url = data.get("url")
    mux_upload_id = data.get("id")
    if not upload_url or not mux_upload_id:
        source = db.session.get(MediaSource, source.id)
        source.status = "error"
        source.error = "Mux did not return an upload URL"
        db.session.commit()
        broadcast_room_updates(room, "queue_updated", "queue:updated", "room:state")
        return {"error": "Mux did not return an upload URL"}, 502

    try:
        source = db.session.get(MediaSource, source.id)
        mux = db.session.get(MuxMediaSource, source.id)
        mux.upload_id = mux_upload_id
        source.status = "uploading"
        db.session.commit()
    except Exception:
        db.session.rollback()
        try:
            source = db.session.get(MediaSource, source.id)
            source.status = "error"
            source.error = "Mux upload was created but could not be linked"
            schedule_mux_cleanup(source.id, mux_upload_id)
            db.session.commit()
        except Exception:
            db.session.rollback()
        raise

    room = db.session.get(WatchRoom, room.id)
    broadcast_room_updates(room, "queue_updated", "queue:updated", "room:state")
    return {
        "ok": True,
        "video_id": item.id if item is not None else room_media.id,
        "room_media_id": room_media.id,
        "queue_entry_id": item.id if item is not None else None,
        "upload_url": upload_url,
        "state": public_state(room),
    }


@app.route("/api/mux/uploaded/<code>/<video_id>", methods=["POST"])
@login_required
def mux_upload_finished(code: str, video_id: str):
    room = room_for_code(code)
    if not room:
        return {"error": "Room not found"}, 404
    try:
        room = lock_room_for(
            room.id, actor_for_user(current_user), Permission.ADD_MEDIA
        )
    except AuthorizationError as exc:
        db.session.rollback()
        return {"error": str(exc)}, 403
    item = find_queue_item(room, video_id)
    room_media = item.room_media if item else db.session.scalar(
        select(RoomMedia).where(RoomMedia.room_id == room.id, RoomMedia.id == video_id)
    )
    if room_media is None:
        return {"error": "Video not found"}, 404

    source_pair = next(
        (
            (source, source.mux)
            for source in room_media.asset.sources
            if source.source_type == "mux_upload" and source.mux is not None
        ),
        None,
    )
    if source_pair is None:
        return {"error": "Mux source not found"}, 404
    source, _mux = source_pair
    source.status = "processing"
    refresh_mux_item(room_media)
    db.session.commit()
    broadcast_room_updates(room, "queue_updated", "queue:updated", "room:state")
    return {
        "ok": True,
        "item": (
            queue_entry_to_public(item)
            if item is not None
            else room_media_to_public(room_media)
        ),
        "state": public_state(room),
    }


@app.route("/api/mux/status/<code>/<video_id>", methods=["GET"])
def mux_status(code: str, video_id: str):
    room = room_for_code(code)
    if not room:
        return {"error": "Room not found"}, 404
    if not user_can_access(room):
        return {"error": "Forbidden"}, 403
    item = find_queue_item(room, video_id)
    room_media = item.room_media if item else db.session.scalar(
        select(RoomMedia).where(RoomMedia.room_id == room.id, RoomMedia.id == video_id)
    )
    if room_media is None:
        return {"error": "Video not found"}, 404

    source_pair = next(
        (
            (source, source.mux)
            for source in room_media.asset.sources
            if source.source_type == "mux_upload" and source.mux is not None
        ),
        None,
    )
    if source_pair is None:
        return {"error": "Mux source not found"}, 404
    source, _mux = source_pair
    before = source.status
    refresh_mux_item(room_media)
    db.session.commit()
    if source.status != before:
        broadcast_room_updates(room, "queue_updated", "queue:updated", "room:state")
    return {
        "ok": True,
        "item": (
            queue_entry_to_public(item)
            if item is not None
            else room_media_to_public(room_media)
        ),
        "state": public_state(room),
    }


@app.route("/api/config", methods=["GET"])
def api_config():
    return {"mux": mux_configured()}


@app.route("/api/mux/health", methods=["GET"])
def mux_health():
    if not mux_configured():
        return {
            "ok": False,
            "error": "MUX_TOKEN_ID / MUX_TOKEN_SECRET not set on Railway",
        }, 503
    try:
        resp = requests.get(
            f"{MUX_API}/uploads?limit=1",
            auth=mux_auth(),
            timeout=20,
        )
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}, 502
    if resp.ok:
        return {"ok": True}
    return {"ok": False, "error": mux_error_message(resp)}, 502


# Socket.IO events


def authorized_socket_room(data) -> WatchRoom | None:
    if request.sid in revoked_sids:
        disconnect()
        return None
    if not isinstance(data, dict):
        emit("error", {"message": "Invalid room command", "code": "invalid_command"})
        return None
    connected_actor = actor_for_sid(request.sid)
    request_actor = current_actor()
    if connected_actor.key != request_actor.key:
        disconnect()
        return None
    code = (data.get("code") or "").upper()
    room = room_for_code(code)
    if not can_view_room(room, connected_actor):
        now = time.monotonic()
        if (
            connected_actor.key not in identity_join_failures
            and len(identity_join_failures) >= 10_000
        ):
            identity_join_failures.pop(next(iter(identity_join_failures)))
        failures = [
            recorded
            for recorded in identity_join_failures.get(connected_actor.key, [])
            if now - recorded < 600
        ]
        failures.append(now)
        identity_join_failures[connected_actor.key] = failures
        emit(
            "error",
            {"message": "Room not found or access denied", "code": "room_unavailable"},
        )
        if len(failures) >= 10:
            disconnect()
        return None
    return room


@socketio.on("connect")
def on_connect(auth=None):
    actor = current_actor(create_guest=False)
    if not (actor.is_user or actor.is_guest):
        return False
    if actor.is_user:
        token = auth.get("browser_client_token") if isinstance(auth, dict) else None
        if token:
            try:
                browser_client = prove_browser_client(actor.user_id, token)
            except BrowserClientProofError:
                return False
            sid_to_browser_client[request.sid] = browser_client.id
        sid_to_user[request.sid] = actor.user_id
        user_to_sids.setdefault(actor.user_id, set()).add(request.sid)
    else:
        sid_to_guest[request.sid] = actor.guest_id
    sid_to_presence[request.sid] = {
        "key": actor.key,
        "kind": actor.kind,
        "label": actor_label(actor),
    }


@socketio.on("room:join")
@socketio.on("join")
def on_join(data):
    room = authorized_socket_room(data)
    if not room:
        return

    previous = sid_to_code.get(request.sid)
    if previous == room.code:
        state = public_state(room, actor_for_sid(request.sid))
        emit("state_sync", state)
        emit("room:state", state)
        return
    if previous:
        leave_room(previous)
        viewer_counts[previous] = max(0, viewer_counts.get(previous, 1) - 1)

    join_room(room.code)
    sid_to_code[request.sid] = room.code
    viewer_counts[room.code] = viewer_counts.get(room.code, 0) + 1

    for item in queue_entries_for(room.id):
        source_pair = mux_source_for_entry(item)
        if source_pair and source_pair[0].status in ("uploading", "processing"):
            refresh_mux_item(item)
    db.session.commit()

    state = public_state(room, actor_for_sid(request.sid))
    emit("state_sync", state)
    emit("room:state", state)
    socketio.emit(
        "viewer_count", {"count": viewer_counts[room.code]}, room=room.code
    )
    broadcast_room_updates(room, "presence:updated")


@socketio.on("disconnect")
def on_disconnect():
    revoked_sids.discard(request.sid)
    user_id = sid_to_user.pop(request.sid, None)
    sid_to_guest.pop(request.sid, None)
    sid_to_browser_client.pop(request.sid, None)
    sid_to_presence.pop(request.sid, None)
    if user_id is not None:
        sids = user_to_sids.get(user_id)
        if sids:
            sids.discard(request.sid)
            if not sids:
                user_to_sids.pop(user_id, None)
    code = sid_to_code.pop(request.sid, None)
    if not code:
        return
    viewer_counts[code] = max(0, viewer_counts.get(code, 1) - 1)
    if viewer_counts[code] == 0:
        viewer_counts.pop(code, None)
    leave_room(code)
    socketio.emit(
        "viewer_count", {"count": viewer_counts.get(code, 0)}, room=code
    )
    room = room_for_code(code)
    if room is not None:
        broadcast_room_updates(room, "presence:updated")


@socketio.on("playback:command")
def on_playback_command(data):
    viewed_room = authorized_socket_room(data)
    if not viewed_room:
        return
    actor = actor_for_sid(request.sid)
    client_action_id = str(data.get("client_action_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", client_action_id):
        emit(
            "error",
            {"message": "A valid client_action_id is required", "code": "invalid_command"},
        )
        return
    action = str(data.get("action") or "").lower()
    receipt = db.session.scalar(
        select(RoomCommandReceipt).where(
            RoomCommandReceipt.room_id == viewed_room.id,
            RoomCommandReceipt.actor_key == actor.key,
            RoomCommandReceipt.client_action_id == client_action_id,
        )
    )
    if receipt is not None:
        if receipt.command_type != action:
            emit(
                "error",
                {
                    "message": "client_action_id was already used for a different command",
                    "code": "conflict",
                },
            )
            return
        emit("playback:updated", {**(receipt.result or {}), "duplicate": True})
        return

    try:
        receipt = RoomCommandReceipt(
            id=new_id(),
            room_id=viewed_room.id,
            actor_kind=actor.kind,
            actor_key=actor.key,
            client_action_id=client_action_id,
            command_type=action[:40],
            result={},
        )
        db.session.add(receipt)
        db.session.flush()
        expected = _expected_playback_version(data)
        if action in {"play", "pause", "seek"}:
            room = update_playback(
                viewed_room.id,
                actor,
                action,
                data.get("position", 0),
                expected_playback_version=expected,
            )
        elif action == "select":
            entry_id = stable_id(data.get("queue_entry_id"), "queue entry")
            room = select_queue_entry(
                viewed_room.id,
                actor,
                entry_id,
                expected_playback_version=expected,
            )
        elif action == "next":
            entry_id = viewed_room.current_queue_entry_id
            if not entry_id:
                raise ResourceNotFoundError("There is no current queue entry")
            room = complete_current_queue_entry(
                viewed_room.id,
                actor,
                entry_id,
                expected_playback_version=expected,
            )
        else:
            raise CommandError("Unknown playback command")
        receipt.result = {
            "ok": True,
            "client_action_id": client_action_id,
            "action": action,
            "queue_version": room.queue_version,
            "playback_version": room.playback_version,
            "position": room.position,
            "playing": room.playing,
            "current_id": room.current_queue_entry_id,
        }
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        receipt = db.session.scalar(
            select(RoomCommandReceipt).where(
                RoomCommandReceipt.room_id == viewed_room.id,
                RoomCommandReceipt.actor_key == actor.key,
                RoomCommandReceipt.client_action_id == client_action_id,
            )
        )
        if receipt is None:
            emit("error", {"message": "Duplicate command conflict", "code": "conflict"})
            return
        if receipt.command_type != action:
            emit(
                "error",
                {
                    "message": "client_action_id was already used for a different command",
                    "code": "conflict",
                },
            )
            return
        emit("playback:updated", {**(receipt.result or {}), "duplicate": True})
        return
    except (AuthorizationError, RoomUnavailableError, CommandError) as exc:
        _emit_socket_command_error(exc)
        return

    broadcast_room_updates(
        room,
        "playback:updated",
        "queue:updated",
        "room:state",
        "video_selected",
    )


@socketio.on("play")
def on_play(data):
    viewed_room = authorized_socket_room(data)
    if not viewed_room:
        return
    try:
        room = update_playback(
            viewed_room.id,
            actor_for_sid(request.sid),
            "play",
            data.get("position", 0),
            expected_playback_version=_expected_playback_version(data),
        )
        db.session.commit()
    except (AuthorizationError, RoomUnavailableError, CommandError) as exc:
        _emit_socket_command_error(exc)
        return
    emit(
        "play",
        {"position": room.position, "playback_version": room.playback_version},
        room=room.code,
    )
    broadcast_room_updates(room, "playback:updated")


@socketio.on("pause")
def on_pause(data):
    viewed_room = authorized_socket_room(data)
    if not viewed_room:
        return
    try:
        room = update_playback(
            viewed_room.id,
            actor_for_sid(request.sid),
            "pause",
            data.get("position", 0),
            expected_playback_version=_expected_playback_version(data),
        )
        db.session.commit()
    except (AuthorizationError, RoomUnavailableError, CommandError) as exc:
        _emit_socket_command_error(exc)
        return
    emit(
        "pause",
        {"position": room.position, "playback_version": room.playback_version},
        room=room.code,
    )
    broadcast_room_updates(room, "playback:updated")


@socketio.on("seek")
def on_seek(data):
    viewed_room = authorized_socket_room(data)
    if not viewed_room:
        return
    try:
        room = update_playback(
            viewed_room.id,
            actor_for_sid(request.sid),
            "seek",
            data.get("position", 0),
            expected_playback_version=_expected_playback_version(data),
        )
        db.session.commit()
    except (AuthorizationError, RoomUnavailableError, CommandError) as exc:
        _emit_socket_command_error(exc)
        return
    emit(
        "seek",
        {
            "position": room.position,
            "playing": room.playing,
            "playback_version": room.playback_version,
        },
        room=room.code,
    )
    broadcast_room_updates(room, "playback:updated")


@socketio.on("select_video")
def on_select_video(data):
    viewed_room = authorized_socket_room(data)
    if not viewed_room:
        return
    entry_id = data.get("queue_entry_id") or data.get("video_id")
    if not entry_id:
        emit("error", {"message": "A stable queue entry ID is required"})
        return
    try:
        room = select_queue_entry(
            viewed_room.id,
            actor_for_sid(request.sid),
            str(entry_id),
            expected_playback_version=_expected_playback_version(data),
        )
        db.session.commit()
    except (AuthorizationError, RoomUnavailableError, CommandError) as exc:
        _emit_socket_command_error(exc)
        return
    broadcast_room_updates(room, "video_selected", "playback:updated", "room:state")


@socketio.on("video_ended")
def on_video_ended(data):
    viewed_room = authorized_socket_room(data)
    if not viewed_room:
        return
    entry_id = data.get("queue_entry_id") or data.get("video_id")
    if not entry_id:
        emit("error", {"message": "A stable queue entry ID is required"})
        return
    try:
        room = complete_current_queue_entry(
            viewed_room.id,
            actor_for_sid(request.sid),
            str(entry_id),
            expected_playback_version=_expected_playback_version(data),
        )
        db.session.commit()
    except (AuthorizationError, RoomUnavailableError, CommandError) as exc:
        _emit_socket_command_error(exc)
        return
    broadcast_room_updates(
        room, "video_selected", "queue:updated", "playback:updated", "room:state"
    )


@socketio.on("sync_position")
def on_sync_position(data):
    viewed_room = authorized_socket_room(data)
    if not viewed_room:
        return
    try:
        update_playback(
            viewed_room.id,
            actor_for_sid(request.sid),
            "sync",
            data.get("position", 0),
            expected_playback_version=_expected_playback_version(data),
            playing=bool(data.get("playing", False)),
        )
        db.session.commit()
    except AuthorizationError:
        db.session.rollback()
    except (RoomUnavailableError, CommandError) as exc:
        _emit_socket_command_error(exc)


def _emit_socket_command_error(exc: Exception) -> None:
    db.session.rollback()
    code = "conflict" if isinstance(exc, VersionConflictError) else "forbidden"
    if isinstance(exc, (CommandError, ResourceNotFoundError)) and not isinstance(
        exc, VersionConflictError
    ):
        code = "invalid_command"
    emit("error", {"message": str(exc), "code": code})


def _expected_playback_version(data) -> int:
    value = data.get("expected_playback_version")
    if not isinstance(value, int) or isinstance(value, bool):
        raise VersionConflictError("expected_playback_version is required")
    if value < 0:
        raise VersionConflictError("Invalid expected_playback_version")
    return value


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print(f"Movie Theater Watch Party — http://0.0.0.0:{port}")
    print(f"Mux configured: {mux_configured()}")
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
        log_output=False,
    )
