"""Movie Theater Watch Party — Flask + SocketIO + Mux synced rooms."""

from __future__ import annotations

import os
import re
import secrets
import string
import uuid
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
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from models import RoomMembership, RoomVideo, User, WatchRoom, db

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
    return bool(
        room
        and current_user.is_authenticated
        and db.session.get(RoomMembership, (room.id, current_user.id))
    )


def current_index(room: WatchRoom) -> int | None:
    if room.current_video_id is None:
        return None
    return next(
        (index for index, video in enumerate(room.videos) if video.id == room.current_video_id),
        None,
    )


def public_state(room: WatchRoom) -> dict:
    return {
        "queue": [video.to_public() for video in room.videos],
        "current": current_index(room),
        "playing": room.playing,
        "position": room.position,
        "viewer_count": viewer_counts.get(room.code, 0),
        "mux": mux_configured(),
    }


def find_queue_item(room: WatchRoom, video_id: str) -> RoomVideo | None:
    return next((video for video in room.videos if video.id == video_id), None)


def hls_url(playback_id: str) -> str:
    return f"https://stream.mux.com/{playback_id}.m3u8"


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


def refresh_mux_item(item: RoomVideo) -> RoomVideo:
    """Poll Mux upload/asset and update a queue item in place."""
    if not mux_configured():
        return item
    if item.status == "ready" and item.playback_id and item.url:
        return item

    try:
        if item.mux_upload_id and not item.asset_id:
            resp = requests.get(
                f"{MUX_API}/uploads/{item.mux_upload_id}",
                auth=mux_auth(),
                timeout=30,
            )
            if resp.ok:
                data = resp.json().get("data") or {}
                status = data.get("status")
                if status == "asset_created" and data.get("asset_id"):
                    item.asset_id = data["asset_id"]
                    item.status = "processing"
                elif status in ("errored", "cancelled", "timed_out"):
                    item.status = "error"
                    item.error = f"Upload {status}"
                    return item

        if item.asset_id:
            resp = requests.get(
                f"{MUX_API}/assets/{item.asset_id}",
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
                        item.playback_id = playback_id
                        item.url = hls_url(playback_id)
                        item.status = "ready"
                        item.duration = data.get("duration")
                    else:
                        item.status = "processing"
                elif asset_status == "errored":
                    item.status = "error"
                    errors = data.get("errors") or {}
                    messages = errors.get("messages") if isinstance(errors, dict) else None
                    item.error = (
                        messages[0]
                        if isinstance(messages, list) and messages
                        else "Mux processing failed"
                    )
                else:
                    item.status = "processing"
    except requests.RequestException as exc:
        item.error = str(exc)

    return item


def remove_watched_item(room: WatchRoom, index: int) -> str | None:
    if index < 0 or index >= len(room.videos):
        return None
    finished = room.videos[index]
    asset_id = finished.asset_id
    db.session.delete(finished)
    return asset_id


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
    if not user_can_access(room):
        return {"error": "Forbidden"}, 403
    if not mux_configured():
        return {
            "error": (
                "Mux is not configured. Set MUX_TOKEN_ID and MUX_TOKEN_SECRET "
                "on the Railway service (Mux → Settings → Access Tokens), then redeploy."
            )
        }, 503

    body = request.get_json(silent=True) or {}
    original = secure_filename(body.get("filename") or "video") or "video"
    video_id = uuid.uuid4().hex[:12]
    cors_origin = os.environ.get("MUX_CORS_ORIGIN", "*").strip() or "*"

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
                    "passthrough": f"{room.code}:{video_id}",
                },
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        return {"error": f"Mux request failed: {exc}"}, 502

    if not resp.ok:
        return {"error": mux_error_message(resp), "status": resp.status_code}, 502

    data = (resp.json() or {}).get("data") or {}
    upload_url = data.get("url")
    mux_upload_id = data.get("id")
    if not upload_url or not mux_upload_id:
        return {"error": "Mux did not return an upload URL"}, 502

    try:
        room = lock_room(room.id)
        next_order = db.session.scalar(
            select(func.coalesce(func.max(RoomVideo.sort_order), -1)).where(
                RoomVideo.room_id == room.id
            )
        ) + 1
        item = RoomVideo(
            id=video_id,
            room_id=room.id,
            name=original,
            sort_order=next_order,
            status="uploading",
            mux_upload_id=mux_upload_id,
            created_by_id=current_user.id,
        )
        db.session.add(item)
        if room.current_video_id is None:
            room.current_video = item
            room.playing = True
            room.position = 0.0
        db.session.commit()
    except Exception:
        db.session.rollback()
        delete_mux_upload(mux_upload_id)
        raise

    socketio.emit("queue_updated", public_state(room), room=room.code)
    return {
        "ok": True,
        "video_id": video_id,
        "upload_url": upload_url,
        "state": public_state(room),
    }


@app.route("/api/mux/uploaded/<code>/<video_id>", methods=["POST"])
@login_required
def mux_upload_finished(code: str, video_id: str):
    room = room_for_code(code)
    if not room:
        return {"error": "Room not found"}, 404
    if not user_can_access(room):
        return {"error": "Forbidden"}, 403
    item = find_queue_item(room, video_id)
    if not item:
        return {"error": "Video not found"}, 404

    item.status = "processing"
    refresh_mux_item(item)
    db.session.commit()
    socketio.emit("queue_updated", public_state(room), room=room.code)
    return {"ok": True, "item": item.to_public(), "state": public_state(room)}


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

    before = item.status
    refresh_mux_item(item)
    db.session.commit()
    if item.status != before:
        socketio.emit("queue_updated", public_state(room), room=room.code)
    return {"ok": True, "item": item.to_public(), "state": public_state(room)}


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

    for item in room.videos:
        if item.status in ("uploading", "processing"):
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
    room = authorized_socket_room(data)
    if not room:
        return
    position = float(data.get("position", 0))
    room.playing = True
    room.position = position
    room.playback_updated_at = datetime.now(UTC)
    db.session.commit()
    emit("play", {"position": position}, room=room.code, include_self=False)


@socketio.on("pause")
def on_pause(data):
    room = authorized_socket_room(data)
    if not room:
        return
    position = float(data.get("position", 0))
    room.playing = False
    room.position = position
    room.playback_updated_at = datetime.now(UTC)
    db.session.commit()
    emit("pause", {"position": position}, room=room.code, include_self=False)


@socketio.on("seek")
def on_seek(data):
    room = authorized_socket_room(data)
    if not room:
        return
    position = float(data.get("position", 0))
    room.position = position
    room.playback_updated_at = datetime.now(UTC)
    db.session.commit()
    emit(
        "seek",
        {"position": position, "playing": room.playing},
        room=room.code,
        include_self=False,
    )


@socketio.on("select_video")
def on_select_video(data):
    room = authorized_socket_room(data)
    if not room:
        return
    try:
        index = int(data.get("index"))
    except (TypeError, ValueError):
        return
    if index < 0 or index >= len(room.videos):
        return

    room.current_video = room.videos[index]
    room.playing = False
    room.position = 0.0
    room.playback_updated_at = datetime.now(UTC)
    db.session.commit()
    emit("video_selected", public_state(room), room=room.code)


@socketio.on("video_ended")
def on_video_ended(data):
    authorized = authorized_socket_room(data)
    if not authorized:
        return
    room = lock_room(authorized.id)
    index = current_index(room)
    if index is None:
        return
    ended_video_id = data.get("video_id")
    if ended_video_id != room.current_video_id:
        return
    try:
        ended_index = int(data.get("index", index))
    except (TypeError, ValueError):
        ended_index = index
    if ended_index != index:
        return

    remaining = [video for i, video in enumerate(room.videos) if i != ended_index]
    if remaining:
        room.current_video = remaining[min(ended_index, len(remaining) - 1)]
        room.playing = True
    else:
        room.current_video = None
        room.playing = False
    asset_id = remove_watched_item(room, ended_index)
    room.position = 0.0
    room.playback_updated_at = datetime.now(UTC)
    db.session.commit()
    delete_mux_asset(asset_id)
    emit("video_selected", public_state(room), room=room.code)


@socketio.on("sync_position")
def on_sync_position(data):
    room = authorized_socket_room(data)
    if not room:
        return
    room.position = float(data.get("position", 0))
    room.playing = bool(data.get("playing", False))
    room.playback_updated_at = datetime.now(UTC)
    db.session.commit()


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
