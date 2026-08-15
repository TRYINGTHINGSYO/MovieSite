from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from direct_urls import DirectUrlError
from link_extract import (
    EXTRACTOR_YT_DLP,
    ExtractedClip,
    LinkExtractError,
    _extract_cache,
    _extract_lock,
    extract_clip,
)
from models import DirectUrlSource, MediaAsset, QueueEntry, WatchRoom, db
from test_rooms import create_room


CLIP = ExtractedClip(
    title="Cool Clip",
    duration=12.5,
    playback_url="https://cdn.example.com/clip.mp4",
    http_headers={"User-Agent": "yt-dlp"},
    content_type="video/mp4",
)


@pytest.fixture(autouse=True)
def clear_extract_cache():
    with _extract_lock:
        _extract_cache.clear()
    yield
    with _extract_lock:
        _extract_cache.clear()


class FakeStream:
    def __init__(self, body=b"data", status_code=200):
        self.body = body
        self.status_code = status_code
        self.headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(len(body)),
            "Accept-Ranges": "bytes",
        }
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        self.closed = True


def test_extract_clip_rejects_private_targets_before_ytdlp():
    with patch("link_extract.yt_dlp.YoutubeDL") as ydl:
        with pytest.raises(DirectUrlError, match="Private or local"):
            extract_clip("https://127.0.0.1/movie.mp4", require_https=False)
    ydl.assert_not_called()


def test_extract_clip_reads_progressive_url_and_rejects_hls():
    with patch("link_extract.yt_dlp.YoutubeDL") as ydl_cls:
        ydl = ydl_cls.return_value.__enter__.return_value
        ydl.extract_info.return_value = {
            "title": "Cool Clip",
            "duration": 12,
            "url": "https://cdn.example.com/v.mp4?token=1",
            "ext": "mp4",
            "protocol": "https",
            "http_headers": {"User-Agent": "yt-dlp"},
        }
        clip = extract_clip(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            require_https=True,
        )
        assert clip.title == "Cool Clip"
        assert clip.playback_url == "https://cdn.example.com/v.mp4?token=1"
        assert clip.extractor == EXTRACTOR_YT_DLP

    with _extract_lock:
        _extract_cache.clear()
    with patch("link_extract.yt_dlp.YoutubeDL") as ydl_cls:
        ydl = ydl_cls.return_value.__enter__.return_value
        ydl.extract_info.return_value = {
            "title": "Live",
            "url": "https://cdn.example.com/live.m3u8",
            "protocol": "m3u8_native",
        }
        with pytest.raises(LinkExtractError, match="browser-playable"):
            extract_clip(
                "https://www.youtube.com/watch?v=abcdefghijk",
                require_https=True,
            )


def test_owner_can_extract_link_and_start_playback(app, client, register):
    register()
    code = create_room(client)
    with patch("movie_theater.extract_clip", return_value=CLIP) as extract:
        response = client.post(
            f"/api/rooms/{code}/media/direct-url",
            json={
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "enqueue": True,
            },
        )
    assert response.status_code == 201, response.get_json()
    extract.assert_called_once()
    state = response.get_json()["state"]
    assert len(state["library"]) == 1
    assert len(state["queue"]) == 1
    source = state["queue"][0]
    assert source["name"] == "Cool Clip"
    assert source["duration"] == 12.5
    assert source["extractor"] == "yt_dlp"
    assert source["url"] == f"/api/media/sources/{source['source_id']}/stream"
    assert state["playing"] is True
    assert state["current_id"] == source["id"]
    with app.app_context():
        direct = db.session.scalar(select(DirectUrlSource))
        assert direct.extractor == "yt_dlp"
        assert direct.original_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert db.session.scalar(select(func.count(QueueEntry.id))) == 1
        room = db.session.scalar(select(WatchRoom).where(WatchRoom.code == code))
        assert room.playing is True


def test_playable_probe_still_skips_extract_but_can_play_now(app, client, register):
    register()
    code = create_room(client)
    with patch("movie_theater.extract_clip") as extract, patch(
        "movie_theater.requests.get"
    ) as get:
        response = client.post(
            f"/api/rooms/{code}/media/direct-url",
            json={
                "title": "Direct feature",
                "url": "https://media.example.com/movie.mp4",
                "probe_result": "playable",
                "enqueue": True,
            },
        )
    assert response.status_code == 201, response.get_json()
    extract.assert_not_called()
    get.assert_not_called()
    state = response.get_json()["state"]
    assert state["playing"] is True
    assert state["queue"][0]["url"] == "https://media.example.com/movie.mp4"
    assert state["queue"][0]["extractor"] == "direct"


def test_extracted_stream_is_proxied_for_room_viewers(app, client, register):
    register()
    code = create_room(client)
    with patch("movie_theater.extract_clip", return_value=CLIP):
        created = client.post(
            f"/api/rooms/{code}/media/direct-url",
            json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )
    source_id = created.get_json()["state"]["library"][0]["sources"][0]["source_id"]
    stream = FakeStream()
    with patch("movie_theater.extract_clip", return_value=CLIP), patch(
        "movie_theater.open_media_stream", return_value=stream
    ) as opener:
        response = client.get(
            f"/api/media/sources/{source_id}/stream",
            headers={"Range": "bytes=0-3"},
        )
    assert response.status_code == 200
    assert response.data == b"data"
    assert response.headers["Content-Type"] == "video/mp4"
    opener.assert_called_once()
    assert opener.call_args.kwargs["range_header"] == "bytes=0-3"
    assert stream.closed is True

    missing = client.get("/api/media/sources/missingid000000000000000000000000/stream")
    assert missing.status_code == 404


def test_approved_unprobed_link_request_extracts_without_queueing(
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
                "title": "Requested clip",
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            },
            "client_request_id": "ytrequest001",
        },
    )
    assert created.status_code == 201, created.get_json()
    request_id = created.get_json()["request"]["id"]
    with patch("request_commands.extract_clip", return_value=CLIP) as extract:
        approved = client.post(
            f"/api/rooms/{code}/requests/{request_id}/resolve",
            json={"resolution": "approved"},
        )
    assert approved.status_code == 200, approved.get_json()
    extract.assert_called_once()
    state = approved.get_json()["state"]
    assert len(state["library"]) == 1
    assert state["queue"] == []
    assert state["library"][0]["name"] == "Requested clip"
    assert state["library"][0]["sources"][0]["extractor"] == "yt_dlp"


def test_enqueue_flag_must_be_boolean(app, client, register):
    register()
    code = create_room(client)
    response = client.post(
        f"/api/rooms/{code}/media/direct-url",
        json={
            "url": "https://media.example.com/movie.mp4",
            "probe_result": "playable",
            "enqueue": "yes",
        },
    )
    assert response.status_code == 400
    with app.app_context():
        assert db.session.scalar(select(func.count(MediaAsset.id))) == 0
