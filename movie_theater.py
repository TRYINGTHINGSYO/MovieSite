"""Movie Theater Watch Party — Flask + SocketIO synced video rooms."""

from __future__ import annotations

import mimetypes
import os
import re
import secrets
import string
import uuid
from pathlib import Path

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_ROOT = BASE_DIR / "uploads"

# Help browsers pick the right decoder for common containers
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/mp4", ".m4v")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("video/ogg", ".ogv")
mimetypes.add_type("video/ogg", ".ogg")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("video/x-msvideo", ".avi")
mimetypes.add_type("video/mp2t", ".ts")
mimetypes.add_type("video/mp2t", ".m2ts")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", secrets.token_hex(16))
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB

# Threading mode: use HTTP long-polling (Werkzeug can't do real WebSockets)
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    logger=False,
    engineio_logger=False,
)

CODE_ALPHABET = string.ascii_uppercase + string.digits
CODE_LEN = 5

# In-memory session store — cleared on server restart
sessions: dict[str, dict] = {}
# sid -> session code (for disconnect cleanup)
sid_to_code: dict[str, str] = {}


def generate_code() -> str:
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        if code not in sessions:
            return code


def new_session() -> dict:
    return {
        "queue": [],
        "current": None,
        "playing": False,
        "position": 0.0,
        "viewer_count": 0,
    }


def public_state(session: dict) -> dict:
    return {
        "queue": session["queue"],
        "current": session["current"],
        "playing": session["playing"],
        "position": session["position"],
        "viewer_count": session["viewer_count"],
    }


def ensure_upload_dir(code: str) -> Path:
    path = UPLOAD_ROOT / code
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/create", methods=["POST"])
def create_session():
    code = generate_code()
    sessions[code] = new_session()
    ensure_upload_dir(code)
    return redirect(url_for("session_room", code=code))


@app.route("/join", methods=["POST"])
def join_session():
    raw = (request.form.get("code") or "").strip().upper()
    code = re.sub(r"[^A-Z0-9]", "", raw)
    if not code or code not in sessions:
        flash("No session found for that code. Check the invite and try again.")
        return redirect(url_for("landing"))
    return redirect(url_for("session_room", code=code))


@app.route("/session/<code>")
def session_room(code: str):
    code = code.upper()
    if code not in sessions:
        flash("That session has ended or never existed.")
        return redirect(url_for("landing"))
    return render_template("room.html", code=code)


@app.route("/upload/<code>", methods=["POST"])
def upload_video(code: str):
    code = code.upper()
    if code not in sessions:
        return {"error": "Session not found"}, 404

    if "file" not in request.files:
        return {"error": "No file provided"}, 400

    file = request.files["file"]
    if not file or not file.filename:
        return {"error": "Empty filename"}, 400

    # Accept any video extension / container the browser can send
    original = secure_filename(file.filename) or "video"
    stem = Path(original).stem or "video"
    ext = Path(original).suffix.lower()
    video_id = uuid.uuid4().hex[:10]
    stored_name = f"{video_id}_{stem}{ext}"

    folder = ensure_upload_dir(code)
    file.save(folder / stored_name)

    item = {
        "id": video_id,
        "name": original,
        "url": url_for("serve_upload", code=code, filename=stored_name),
    }
    session = sessions[code]
    session["queue"].append(item)

    # First item in the queue: select it and start playback for everyone
    if session["current"] is None:
        session["current"] = 0
        session["playing"] = True
        session["position"] = 0.0

    socketio.emit("queue_updated", public_state(session), room=code)
    return {"ok": True, "item": item, "state": public_state(session)}


@app.route("/uploads/<code>/<path:filename>")
def serve_upload(code: str, filename: str):
    code = code.upper()
    folder = UPLOAD_ROOT / code
    mime, _ = mimetypes.guess_type(filename)
    # conditional=True enables Accept-Ranges / 206 so <video> can seek
    response = send_from_directory(
        folder,
        filename,
        mimetype=mime or "application/octet-stream",
        conditional=True,
    )
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


# ---------------------------------------------------------------------------
# Socket.IO events
# ---------------------------------------------------------------------------


@socketio.on("join")
def on_join(data):
    code = (data.get("code") or "").upper()
    if code not in sessions:
        emit("error", {"message": "Session not found"})
        return

    join_room(code)
    sid_to_code[request.sid] = code
    sessions[code]["viewer_count"] += 1

    emit("state_sync", public_state(sessions[code]))
    socketio.emit(
        "viewer_count",
        {"count": sessions[code]["viewer_count"]},
        room=code,
    )


@socketio.on("disconnect")
def on_disconnect():
    code = sid_to_code.pop(request.sid, None)
    if not code or code not in sessions:
        return

    sessions[code]["viewer_count"] = max(0, sessions[code]["viewer_count"] - 1)
    leave_room(code)
    socketio.emit(
        "viewer_count",
        {"count": sessions[code]["viewer_count"]},
        room=code,
    )


@socketio.on("play")
def on_play(data):
    code = (data.get("code") or "").upper()
    if code not in sessions:
        return
    position = float(data.get("position", 0))
    sessions[code]["playing"] = True
    sessions[code]["position"] = position
    emit(
        "play",
        {"position": position},
        room=code,
        include_self=False,
    )


@socketio.on("pause")
def on_pause(data):
    code = (data.get("code") or "").upper()
    if code not in sessions:
        return
    position = float(data.get("position", 0))
    sessions[code]["playing"] = False
    sessions[code]["position"] = position
    emit(
        "pause",
        {"position": position},
        room=code,
        include_self=False,
    )


@socketio.on("seek")
def on_seek(data):
    code = (data.get("code") or "").upper()
    if code not in sessions:
        return
    position = float(data.get("position", 0))
    sessions[code]["position"] = position
    emit(
        "seek",
        {"position": position, "playing": sessions[code]["playing"]},
        room=code,
        include_self=False,
    )


@socketio.on("select_video")
def on_select_video(data):
    code = (data.get("code") or "").upper()
    if code not in sessions:
        return

    index = data.get("index")
    try:
        index = int(index)
    except (TypeError, ValueError):
        return

    session = sessions[code]
    if index < 0 or index >= len(session["queue"]):
        return

    session["current"] = index
    session["playing"] = False
    session["position"] = 0.0
    emit(
        "video_selected",
        public_state(session),
        room=code,
    )


@socketio.on("video_ended")
def on_video_ended(data):
    code = (data.get("code") or "").upper()
    if code not in sessions:
        return

    session = sessions[code]
    if session["current"] is None:
        return

    # Idempotent: ignore duplicate ended events for the same queue index
    ended_index = data.get("index", session["current"])
    try:
        ended_index = int(ended_index)
    except (TypeError, ValueError):
        ended_index = session["current"]

    if ended_index != session["current"]:
        return

    nxt = session["current"] + 1
    if nxt < len(session["queue"]):
        session["current"] = nxt
        session["playing"] = True
        session["position"] = 0.0
    else:
        session["playing"] = False
        session["position"] = 0.0

    emit("video_selected", public_state(session), room=code)


@socketio.on("sync_position")
def on_sync_position(data):
    """Host/any client quietly reports position for late joiners."""
    code = (data.get("code") or "").upper()
    if code not in sessions:
        return
    sessions[code]["position"] = float(data.get("position", 0))
    sessions[code]["playing"] = bool(data.get("playing", False))


if __name__ == "__main__":
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    print("Movie Theater Watch Party — http://127.0.0.1:5000")
    # Waitress: many worker threads so Socket.IO long-polls don't starve /uploads
    # video requests (Werkzeug's debugger often serializes them in practice).
    try:
        from waitress import serve

        print("Serving with Waitress (threaded) — Ctrl+C to stop")
        serve(
            app,
            host="0.0.0.0",
            port=5000,
            threads=32,
            channel_timeout=120,
            ident="GroupTheater",
        )
    except ImportError:
        print("Waitress not installed; falling back to Werkzeug (pip install waitress)")
        socketio.run(
            app,
            host="0.0.0.0",
            port=5000,
            debug=False,
            use_reloader=False,
            allow_unsafe_werkzeug=True,
            threaded=True,
        )
