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
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_ROOT = BASE_DIR / "uploads"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

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

# Threading + gthread workers: Socket.IO long-polls must not starve /uploads
# Range fetches (that was the "duration shows, never plays" bug on Railway).
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    logger=False,
    engineio_logger=False,
    ping_timeout=60,
    ping_interval=25,
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


# Max bytes per video response. Open-ended Range requests (bytes=0-) used to
# stream the whole file through Waitress, which buffers generators and can
# send zero bytes for minutes — Chrome shows duration then spins forever.
VIDEO_CHUNK_SIZE = 2 * 1024 * 1024  # 2 MiB


def parse_byte_range(range_header: str, file_size: int) -> tuple[int, int] | None:
    """Parse a single Range request into inclusive (start, end) offsets."""
    match = re.match(r"bytes=(\d*)-(\d*)", (range_header or "").strip())
    if not match or file_size <= 0:
        return None

    start_s, end_s = match.group(1), match.group(2)
    if start_s == "" and end_s == "":
        return None

    if start_s == "":
        # suffix-byte-range-spec: last N bytes
        try:
            suffix = int(end_s)
        except ValueError:
            return None
        if suffix <= 0:
            return None
        start = max(0, file_size - suffix)
        end = file_size - 1
    else:
        try:
            start = int(start_s)
            end = int(end_s) if end_s else file_size - 1
        except ValueError:
            return None
        if start >= file_size:
            return None
        end = min(end, file_size - 1)
        if start > end:
            return None

    return start, end


def guess_video_mime(filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename)
    # Chromium often refuses video/quicktime even when the MOV is H.264.
    # video/mp4 lets it try the MP4 demuxer for compatible .mov files.
    if filename.lower().endswith((".mov", ".m4v")):
        return "video/mp4"
    return mime or "application/octet-stream"


def read_video_slice(path: Path, start: int, end: int) -> bytes:
    length = end - start + 1
    with path.open("rb") as handle:
        handle.seek(start)
        return handle.read(length)


def enqueue_uploaded_video(code: str, original_name: str, stored_path: Path) -> dict:
    """Register a finished upload in the session queue and notify the room."""
    video_id = uuid.uuid4().hex[:10]
    stem = Path(original_name).stem or "video"
    ext = Path(original_name).suffix.lower() or stored_path.suffix.lower()
    stored_name = f"{video_id}_{stem}{ext}"
    folder = ensure_upload_dir(code)
    final_path = folder / stored_name
    stored_path.replace(final_path)

    item = {
        "id": video_id,
        "name": original_name,
        "url": url_for("serve_upload", code=code, filename=stored_name),
    }
    session = sessions[code]
    session["queue"].append(item)

    if session["current"] is None:
        session["current"] = 0
        session["playing"] = True
        session["position"] = 0.0

    socketio.emit("queue_updated", public_state(session), room=code)
    return {"ok": True, "item": item, "state": public_state(session)}


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


@app.errorhandler(RequestEntityTooLarge)
def too_large(_err):
    return {"error": "File too large for this server."}, 413


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
    """Accept a full file or a single chunk (chunked uploads for Railway limits)."""
    code = code.upper()
    if code not in sessions:
        return {"error": "Session not found"}, 404

    if "file" not in request.files:
        return {"error": "No file provided"}, 400

    file = request.files["file"]
    if not file:
        return {"error": "Empty upload"}, 400

    # Chunked mode — small POSTs stay under Railway's 5-minute body timeout
    upload_id = (request.form.get("upload_id") or "").strip()
    chunk_index = request.form.get("chunk_index")
    chunk_total = request.form.get("chunk_total")
    original = secure_filename(request.form.get("filename") or file.filename or "") or "video"

    if upload_id and chunk_index is not None and chunk_total is not None:
        try:
            index = int(chunk_index)
            total = int(chunk_total)
        except ValueError:
            return {"error": "Invalid chunk metadata"}, 400

        if not re.fullmatch(r"[a-fA-F0-9-]{8,64}", upload_id):
            return {"error": "Invalid upload id"}, 400
        if total < 1 or total > 50_000 or index < 0 or index >= total:
            return {"error": "Invalid chunk range"}, 400

        tmp_dir = ensure_upload_dir(code) / ".parts" / upload_id
        tmp_dir.mkdir(parents=True, exist_ok=True)
        part_path = tmp_dir / f"{index:06d}.part"
        file.save(part_path)

        if index < total - 1:
            return {"ok": True, "received": index, "total": total}

        # Last chunk: assemble in order, then enqueue
        assembled = tmp_dir / "assembled"
        try:
            with assembled.open("wb") as out:
                for i in range(total):
                    part = tmp_dir / f"{i:06d}.part"
                    if not part.is_file():
                        return {"error": f"Missing chunk {i}"}, 400
                    with part.open("rb") as inp:
                        while True:
                            buf = inp.read(1024 * 1024)
                            if not buf:
                                break
                            out.write(buf)
            result = enqueue_uploaded_video(code, original, assembled)
        except Exception:
            try:
                assembled.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        else:
            try:
                for p in tmp_dir.iterdir():
                    p.unlink(missing_ok=True)
                tmp_dir.rmdir()
            except OSError:
                pass
        return result

    # Legacy single-shot upload (small files)
    if not file.filename and not original:
        return {"error": "Empty filename"}, 400
    original = secure_filename(file.filename or original) or "video"
    folder = ensure_upload_dir(code)
    incoming = folder / f".incoming_{uuid.uuid4().hex}"
    file.save(incoming)
    return enqueue_uploaded_video(code, original, incoming)


@app.route("/uploads/<code>/<path:filename>", methods=["GET", "HEAD"])
def serve_upload(code: str, filename: str):
    """Serve uploads with capped byte-range responses.

    Browsers request open-ended ranges (bytes=N-). Streaming those whole
    tails through Waitress often stalls after metadata (duration shows, spinner
    forever). Return at most VIDEO_CHUNK_SIZE bytes of real content per 206 so
    each response finishes immediately; the player requests the next range.
    """
    code = code.upper()
    folder = (UPLOAD_ROOT / code).resolve()
    path = (folder / filename).resolve()
    try:
        path.relative_to(folder)
    except ValueError:
        abort(404)
    if not path.is_file():
        abort(404)

    file_size = path.stat().st_size
    mime = guess_video_mime(filename)
    range_header = request.headers.get("Range")

    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Content-Type": mime,
        "X-Content-Type-Options": "nosniff",
    }

    if file_size == 0:
        headers["Content-Length"] = "0"
        return Response(b"", status=200, headers=headers)

    if range_header:
        parsed = parse_byte_range(range_header, file_size)
        if parsed is None:
            resp = Response(status=416)
            resp.headers["Content-Range"] = f"bytes */{file_size}"
            resp.headers["Cache-Control"] = "no-store"
            return resp
        start, end = parsed
    else:
        # No Range: still cap so a huge file cannot wedge the worker thread.
        start, end = 0, file_size - 1

    # Cap every response — critical for bytes=0- / bytes=N- open ranges.
    end = min(end, start + VIDEO_CHUNK_SIZE - 1, file_size - 1)
    length = end - start + 1

    if request.method == "HEAD":
        headers["Content-Length"] = str(length)
        if range_header or start > 0 or end < file_size - 1:
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            return Response(b"", status=206, headers=headers)
        return Response(b"", status=200, headers=headers)

    data = read_video_slice(path, start, end)
    headers["Content-Length"] = str(len(data))

    # Always 206 when the client asked for a range, or when we truncated.
    if range_header or end < file_size - 1 or start > 0:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        return Response(data, status=206, headers=headers)

    return Response(data, status=200, headers=headers)


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
    port = int(os.environ.get("PORT", "5000"))
    print(f"Movie Theater Watch Party — http://0.0.0.0:{port}")
    # Local/dev fallback. Production on Railway uses gunicorn gthread (see railway.json).
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
        log_output=False,
    )
