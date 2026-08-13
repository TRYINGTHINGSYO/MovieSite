"""Movie Theater Watch Party — Flask + SocketIO + Mux synced rooms."""

from __future__ import annotations

import os
import re
import secrets
import string
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import requests
from flask import Flask, flash, redirect, render_template, request, session, url_for
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
    AuthorizationError,
    Permission,
    RoomUnavailableError,
    actor_for_user,
    can_view_room,
    lock_room_for,
    require_permission,
)
from media_sources import queue_entry_to_public, room_media_to_public
from models import (
    MediaSource,
    MuxMediaSource,
    QueueEntry,
    RoomMembership,
    User,
    WatchRoom,
    db,
)
from room_commands import (
    CommandError,
    ResourceNotFoundError,
    VersionConflictError,
    complete_current_queue_entry,
    create_mux_media,
    mux_source_for_entry,
    queue_entries_for,
    queue_entry_for,
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


def user_can_access(room: WatchRoom | None) -> bool:
    return can_view_room(room, actor_for_user(current_user))


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


def public_state(room: WatchRoom) -> dict:
    entries = queue_entries_for(room.id)
    return {
        "queue": [queue_entry_to_public(entry) for entry in entries],
        "library": [
            room_media_to_public(room_media)
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
    }


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


def refresh_mux_item(item: QueueEntry) -> QueueEntry:
    """Poll Mux and update the reusable media source behind a queue entry."""
    if not mux_configured():
        return item
    source_pair = mux_source_for_entry(item)
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
                        item.room_media.asset.duration = data.get("duration")
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
    return f"user:{user_id}" if user_id else f"ip:{get_remote_address()}"


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
    if not user_can_access(room):
        db.session.add(RoomMembership(room_id=room.id, user_id=current_user.id))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
    return redirect(url_for("session_room", code=code))


@app.route("/session/<code>")
@login_required
def session_room(code: str):
    room = room_for_code(code)
    if not room:
        flash("That room has ended or never existed.")
        return redirect(url_for("saved_rooms"))
    if not user_can_access(room):
        return render_template("join_room.html", room=room), 403
    return render_template(
        "room.html",
        code=room.code,
        room=room,
        mux_ready=mux_configured(),
    )


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

    body = request.get_json(silent=True) or {}
    original = secure_filename(body.get("filename") or "video") or "video"
    try:
        byte_size = int(body.get("size")) if body.get("size") is not None else None
    except (TypeError, ValueError):
        return {"error": "Invalid file size"}, 400
    if byte_size is not None and byte_size < 0:
        return {"error": "Invalid file size"}, 400
    cors_origin = os.environ.get("MUX_CORS_ORIGIN", "*").strip() or "*"

    try:
        room, item, source, _mux = create_mux_media(
            room.id,
            actor_for_user(current_user),
            original,
            byte_size=byte_size,
        )
        db.session.commit()
    except AuthorizationError as exc:
        db.session.rollback()
        return {"error": str(exc)}, 403
    except RoomUnavailableError:
        db.session.rollback()
        return {"error": "Room not found"}, 404
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
        socketio.emit("queue_updated", public_state(room), room=room.code)
        return {"error": f"Mux request failed: {exc}"}, 502

    if not resp.ok:
        source = db.session.get(MediaSource, source.id)
        source.status = "error"
        error_message = mux_error_message(resp)
        source.error = error_message
        db.session.commit()
        socketio.emit("queue_updated", public_state(room), room=room.code)
        return {"error": error_message, "status": resp.status_code}, 502

    data = (resp.json() or {}).get("data") or {}
    upload_url = data.get("url")
    mux_upload_id = data.get("id")
    if not upload_url or not mux_upload_id:
        source = db.session.get(MediaSource, source.id)
        source.status = "error"
        source.error = "Mux did not return an upload URL"
        db.session.commit()
        socketio.emit("queue_updated", public_state(room), room=room.code)
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
    socketio.emit("queue_updated", public_state(room), room=room.code)
    return {
        "ok": True,
        "video_id": item.id,
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
    if not item:
        return {"error": "Video not found"}, 404

    source_pair = mux_source_for_entry(item)
    if source_pair is None:
        return {"error": "Mux source not found"}, 404
    source, _mux = source_pair
    source.status = "processing"
    refresh_mux_item(item)
    db.session.commit()
    socketio.emit("queue_updated", public_state(room), room=room.code)
    return {
        "ok": True,
        "item": queue_entry_to_public(item),
        "state": public_state(room),
    }


@app.route("/api/mux/status/<code>/<video_id>", methods=["GET"])
@login_required
def mux_status(code: str, video_id: str):
    room = room_for_code(code)
    if not room:
        return {"error": "Room not found"}, 404
    if not user_can_access(room):
        return {"error": "Forbidden"}, 403
    item = find_queue_item(room, video_id)
    if not item:
        return {"error": "Video not found"}, 404

    source_pair = mux_source_for_entry(item)
    if source_pair is None:
        return {"error": "Mux source not found"}, 404
    source, _mux = source_pair
    before = source.status
    refresh_mux_item(item)
    db.session.commit()
    if source.status != before:
        socketio.emit("queue_updated", public_state(room), room=room.code)
    return {
        "ok": True,
        "item": queue_entry_to_public(item),
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
    socket_user_id = sid_to_user.get(request.sid)
    current_user_id = current_user.id if current_user.is_authenticated else None
    if socket_user_id is not None and socket_user_id != current_user_id:
        disconnect()
        return None
    code = (data.get("code") or "").upper()
    room = room_for_code(code)
    if not user_can_access(room):
        emit("error", {"message": "Room not found or access denied"})
        return None
    return room


@socketio.on("connect")
def on_connect():
    if not current_user.is_authenticated:
        return False
    sid_to_user[request.sid] = current_user.id
    user_to_sids.setdefault(current_user.id, set()).add(request.sid)


@socketio.on("join")
def on_join(data):
    room = authorized_socket_room(data)
    if not room:
        return

    previous = sid_to_code.get(request.sid)
    if previous == room.code:
        emit("state_sync", public_state(room))
        return
    if previous:
        leave_room(previous)
        viewer_counts[previous] = max(0, viewer_counts.get(previous, 1) - 1)

    join_room(room.code)
    sid_to_code[request.sid] = room.code
    sid_to_user[request.sid] = current_user.id
    user_to_sids.setdefault(current_user.id, set()).add(request.sid)
    viewer_counts[room.code] = viewer_counts.get(room.code, 0) + 1

    for item in queue_entries_for(room.id):
        source_pair = mux_source_for_entry(item)
        if source_pair and source_pair[0].status in ("uploading", "processing"):
            refresh_mux_item(item)
    db.session.commit()

    emit("state_sync", public_state(room))
    socketio.emit(
        "viewer_count", {"count": viewer_counts[room.code]}, room=room.code
    )


@socketio.on("disconnect")
def on_disconnect():
    revoked_sids.discard(request.sid)
    user_id = sid_to_user.pop(request.sid, None)
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


@socketio.on("play")
def on_play(data):
    viewed_room = authorized_socket_room(data)
    if not viewed_room:
        return
    try:
        room = update_playback(
            viewed_room.id,
            actor_for_user(current_user),
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


@socketio.on("pause")
def on_pause(data):
    viewed_room = authorized_socket_room(data)
    if not viewed_room:
        return
    try:
        room = update_playback(
            viewed_room.id,
            actor_for_user(current_user),
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


@socketio.on("seek")
def on_seek(data):
    viewed_room = authorized_socket_room(data)
    if not viewed_room:
        return
    try:
        room = update_playback(
            viewed_room.id,
            actor_for_user(current_user),
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
            actor_for_user(current_user),
            str(entry_id),
            expected_playback_version=_expected_playback_version(data),
        )
        db.session.commit()
    except (AuthorizationError, RoomUnavailableError, CommandError) as exc:
        _emit_socket_command_error(exc)
        return
    emit("video_selected", public_state(room), room=room.code)


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
            actor_for_user(current_user),
            str(entry_id),
            expected_playback_version=_expected_playback_version(data),
        )
        db.session.commit()
    except (AuthorizationError, RoomUnavailableError, CommandError) as exc:
        _emit_socket_command_error(exc)
        return
    emit("video_selected", public_state(room), room=room.code)


@socketio.on("sync_position")
def on_sync_position(data):
    viewed_room = authorized_socket_room(data)
    if not viewed_room:
        return
    try:
        update_playback(
            viewed_room.id,
            actor_for_user(current_user),
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
    if value is None or isinstance(value, bool):
        raise VersionConflictError("expected_playback_version is required")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise VersionConflictError("Invalid expected_playback_version") from exc


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
