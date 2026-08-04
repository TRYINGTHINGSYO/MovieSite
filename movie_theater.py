"""Movie Theater Watch Party — Flask + SocketIO + Mux synced rooms."""

from __future__ import annotations

import os
import re
import secrets
import string
import uuid

import requests
from flask import Flask, flash, redirect, render_template, request, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", secrets.token_hex(16))

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

sessions: dict[str, dict] = {}
sid_to_code: dict[str, str] = {}
# video_id -> session code (for Mux status polling)
video_index: dict[str, str] = {}

MUX_TOKEN_ID = os.environ.get("MUX_TOKEN_ID", "").strip()
MUX_TOKEN_SECRET = os.environ.get("MUX_TOKEN_SECRET", "").strip()
MUX_API = "https://api.mux.com/video/v1"


def mux_configured() -> bool:
    return bool(MUX_TOKEN_ID and MUX_TOKEN_SECRET)


def mux_auth() -> tuple[str, str]:
    return (MUX_TOKEN_ID, MUX_TOKEN_SECRET)


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
        "mux": mux_configured(),
    }


def find_queue_item(session: dict, video_id: str) -> dict | None:
    for item in session["queue"]:
        if item.get("id") == video_id:
            return item
    return None


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


def refresh_mux_item(item: dict) -> dict:
    """Poll Mux upload/asset and update queue item fields in place."""
    if not mux_configured():
        return item
    if item.get("status") == "ready" and item.get("url"):
        return item

    upload_id = item.get("mux_upload_id")
    asset_id = item.get("asset_id")

    try:
        if upload_id and not asset_id:
            resp = requests.get(
                f"{MUX_API}/uploads/{upload_id}",
                auth=mux_auth(),
                timeout=30,
            )
            if resp.ok:
                data = resp.json().get("data") or {}
                status = data.get("status")
                if status == "asset_created" and data.get("asset_id"):
                    asset_id = data["asset_id"]
                    item["asset_id"] = asset_id
                    item["status"] = "processing"
                elif status in ("errored", "cancelled", "timed_out"):
                    item["status"] = "error"
                    item["error"] = f"Upload {status}"
                    return item

        if asset_id:
            resp = requests.get(
                f"{MUX_API}/assets/{asset_id}",
                auth=mux_auth(),
                timeout=30,
            )
            if resp.ok:
                data = resp.json().get("data") or {}
                asset_status = data.get("status")
                if asset_status == "ready":
                    playback_ids = data.get("playback_ids") or []
                    public = next(
                        (p for p in playback_ids if p.get("policy") == "public"),
                        playback_ids[0] if playback_ids else None,
                    )
                    if public and public.get("id"):
                        item["playback_id"] = public["id"]
                        item["url"] = hls_url(public["id"])
                        item["status"] = "ready"
                        item["duration"] = data.get("duration")
                elif asset_status == "errored":
                    item["status"] = "error"
                    errs = data.get("errors") or {}
                    item["error"] = errs.get("messages", ["Mux processing failed"])[0]
                else:
                    item["status"] = "processing"
    except requests.RequestException as exc:
        item["error"] = str(exc)

    return item


def remove_watched_item(session: dict, index: int) -> None:
    """Drop a finished queue item and delete its Mux asset."""
    if index < 0 or index >= len(session["queue"]):
        return
    finished = session["queue"].pop(index)
    delete_mux_asset(finished.get("asset_id"))
    video_index.pop(finished.get("id", ""), None)


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


@app.route("/")
def landing():
    return render_template("landing.html", mux_ready=mux_configured())


@app.route("/create", methods=["POST"])
def create_session():
    code = generate_code()
    sessions[code] = new_session()
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
    return render_template(
        "room.html",
        code=code,
        mux_ready=mux_configured(),
    )


@app.route("/api/mux/create-upload/<code>", methods=["POST"])
def create_mux_upload(code: str):
    """Create a Mux Direct Upload URL for the browser to PUT the file to."""
    code = code.upper()
    if code not in sessions:
        return {"error": "Session not found"}, 404
    if not mux_configured():
        return {
            "error": (
                "Mux is not configured. Set MUX_TOKEN_ID and MUX_TOKEN_SECRET "
                "on the Railway service, then redeploy."
            )
        }, 503

    body = request.get_json(silent=True) or {}
    original = secure_filename(body.get("filename") or "video") or "video"
    video_id = uuid.uuid4().hex[:12]

    # Prefer the request Origin so browser CORS on the signed URL works.
    cors_origin = request.headers.get("Origin") or os.environ.get(
        "PUBLIC_ORIGIN", "*"
    )

    try:
        resp = requests.post(
            f"{MUX_API}/uploads",
            auth=mux_auth(),
            json={
                "cors_origin": cors_origin,
                "timeout": 3600,
                "new_asset_settings": {
                    "playback_policies": ["public"],
                    "video_quality": "basic",
                    "passthrough": f"{code}:{video_id}",
                },
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        return {"error": f"Mux request failed: {exc}"}, 502

    if not resp.ok:
        return {
            "error": "Mux rejected the upload request",
            "detail": resp.text[:500],
        }, 502

    data = (resp.json() or {}).get("data") or {}
    upload_url = data.get("url")
    mux_upload_id = data.get("id")
    if not upload_url or not mux_upload_id:
        return {"error": "Mux did not return an upload URL"}, 502

    item = {
        "id": video_id,
        "name": original,
        "status": "uploading",
        "mux_upload_id": mux_upload_id,
        "asset_id": None,
        "playback_id": None,
        "url": None,
        "error": None,
    }
    session = sessions[code]
    session["queue"].append(item)
    video_index[video_id] = code

    if session["current"] is None:
        session["current"] = len(session["queue"]) - 1
        session["playing"] = True
        session["position"] = 0.0

    socketio.emit("queue_updated", public_state(session), room=code)
    return {
        "ok": True,
        "video_id": video_id,
        "upload_url": upload_url,
        "state": public_state(session),
    }


@app.route("/api/mux/uploaded/<code>/<video_id>", methods=["POST"])
def mux_upload_finished(code: str, video_id: str):
    """Client finished PUTting bytes to Mux — mark processing and poll."""
    code = code.upper()
    if code not in sessions:
        return {"error": "Session not found"}, 404
    item = find_queue_item(sessions[code], video_id)
    if not item:
        return {"error": "Video not found"}, 404

    item["status"] = "processing"
    refresh_mux_item(item)
    socketio.emit("queue_updated", public_state(sessions[code]), room=code)
    return {"ok": True, "item": item, "state": public_state(sessions[code])}


@app.route("/api/mux/status/<code>/<video_id>", methods=["GET"])
def mux_status(code: str, video_id: str):
    code = code.upper()
    if code not in sessions:
        return {"error": "Session not found"}, 404
    item = find_queue_item(sessions[code], video_id)
    if not item:
        return {"error": "Video not found"}, 404

    before = item.get("status")
    refresh_mux_item(item)
    if item.get("status") != before:
        socketio.emit("queue_updated", public_state(sessions[code]), room=code)
    return {"ok": True, "item": item, "state": public_state(sessions[code])}


@app.route("/api/config", methods=["GET"])
def api_config():
    return {"mux": mux_configured()}


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

    # Refresh any processing Mux items for late joiners
    for item in sessions[code]["queue"]:
        if item.get("status") in ("uploading", "processing"):
            refresh_mux_item(item)

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
    emit("play", {"position": position}, room=code, include_self=False)


@socketio.on("pause")
def on_pause(data):
    code = (data.get("code") or "").upper()
    if code not in sessions:
        return
    position = float(data.get("position", 0))
    sessions[code]["playing"] = False
    sessions[code]["position"] = position
    emit("pause", {"position": position}, room=code, include_self=False)


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

    try:
        index = int(data.get("index"))
    except (TypeError, ValueError):
        return

    session = sessions[code]
    if index < 0 or index >= len(session["queue"]):
        return

    session["current"] = index
    session["playing"] = False
    session["position"] = 0.0
    emit("video_selected", public_state(session), room=code)


@socketio.on("video_ended")
def on_video_ended(data):
    code = (data.get("code") or "").upper()
    if code not in sessions:
        return

    session = sessions[code]
    if session["current"] is None:
        return

    ended_index = data.get("index", session["current"])
    try:
        ended_index = int(ended_index)
    except (TypeError, ValueError):
        ended_index = session["current"]

    if ended_index != session["current"]:
        return

    # Delete watched asset to keep Mux / storage lean, then play next.
    remove_watched_item(session, ended_index)

    if session["queue"]:
        # Next item shifted into ended_index after pop
        next_index = min(ended_index, len(session["queue"]) - 1)
        session["current"] = next_index
        session["playing"] = True
        session["position"] = 0.0
    else:
        session["current"] = None
        session["playing"] = False
        session["position"] = 0.0

    emit("video_selected", public_state(session), room=code)


@socketio.on("sync_position")
def on_sync_position(data):
    code = (data.get("code") or "").upper()
    if code not in sessions:
        return
    sessions[code]["position"] = float(data.get("position", 0))
    sessions[code]["playing"] = bool(data.get("playing", False))


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
