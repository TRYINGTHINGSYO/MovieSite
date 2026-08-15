from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from urllib.parse import urljoin

import requests
import yt_dlp
from yt_dlp.utils import DownloadError

from direct_urls import (
    MAX_EXTRACTED_URL_LENGTH,
    DirectUrlError,
    ValidatedDirectUrl,
    validate_direct_url,
)


EXTRACTOR_DIRECT = "direct"
EXTRACTOR_YT_DLP = "yt_dlp"
EXTRACT_CACHE_TTL_SECONDS = 8 * 60
PROGRESSIVE_FORMAT = (
    "b[ext=mp4][protocol^=http][protocol!*=m3u8][protocol!*=dash]"
    "/b[protocol^=http][protocol!*=m3u8][protocol!*=dash]"
    "/b"
)
STREAM_HEADERS = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "content-disposition",
)


class LinkExtractError(DirectUrlError):
    pass


@dataclass(frozen=True)
class ExtractedClip:
    title: str
    duration: float | None
    playback_url: str
    http_headers: dict[str, str]
    content_type: str | None
    extractor: str = EXTRACTOR_YT_DLP


_extract_cache: dict[str, tuple[float, ExtractedClip]] = {}
_extract_lock = threading.Lock()


def extract_clip(url: str, *, require_https: bool) -> ExtractedClip:
    validated = validate_direct_url(url, require_https=require_https)
    cached = _cached_clip(validated.normalized)
    if cached is not None:
        return cached

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 0,
        "format": PROGRESSIVE_FORMAT,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(validated.normalized, download=False)
    except DownloadError as exc:
        raise LinkExtractError(_public_extract_error(exc)) from exc
    except Exception as exc:
        raise LinkExtractError(
            "Could not extract a playable clip from that link"
        ) from exc

    clip = _clip_from_info(info, require_https=require_https)
    _store_clip(validated.normalized, clip)
    return clip


def open_media_stream(
    playback_url: str,
    *,
    headers: dict[str, str] | None = None,
    range_header: str | None = None,
    require_https: bool,
) -> requests.Response:
    validated = _validate_playback_url(playback_url, require_https=require_https)
    request_headers = dict(headers or {})
    if range_header:
        request_headers["Range"] = range_header
    try:
        response = requests.get(
            validated.normalized,
            headers=request_headers,
            stream=True,
            timeout=(15, 60),
            allow_redirects=True,
            hooks={"response": _reject_private_redirects(require_https)},
        )
    except DirectUrlError:
        raise
    except requests.RequestException as exc:
        raise LinkExtractError("The extracted clip could not be opened") from exc
    if response.status_code >= 400:
        response.close()
        raise LinkExtractError("The extracted clip could not be opened")
    return response


def forwarded_stream_headers(upstream: requests.Response) -> dict[str, str]:
    headers = {}
    for name, value in upstream.headers.items():
        if name.lower() in STREAM_HEADERS and value:
            headers[name] = value
    headers.setdefault("Accept-Ranges", "bytes")
    headers.setdefault("Content-Type", "video/mp4")
    headers["Cache-Control"] = "no-store"
    return headers


def _clip_from_info(info: dict | None, *, require_https: bool) -> ExtractedClip:
    selected = _selected_entry(info)
    playback_url = _playback_url(selected)
    protocol = str(selected.get("protocol") or "")
    if "m3u8" in protocol or "dash" in protocol:
        raise LinkExtractError(
            "That link does not provide a browser-playable video file"
        )
    validated = _validate_playback_url(playback_url, require_https=require_https)
    duration = selected.get("duration")
    try:
        duration_value = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_value = None
    if duration_value is not None and not (0 < duration_value <= 7 * 24 * 60 * 60):
        duration_value = None
    title = str(selected.get("title") or selected.get("fulltitle") or "Clip").strip()
    http_headers = {
        str(key): str(value)
        for key, value in dict(selected.get("http_headers") or {}).items()
        if str(key) and str(value)
    }
    ext = str(selected.get("ext") or "mp4").lower()
    content_type = {
        "mp4": "video/mp4",
        "m4v": "video/mp4",
        "webm": "video/webm",
        "mov": "video/quicktime",
        "ogg": "video/ogg",
        "ogv": "video/ogg",
    }.get(ext, "video/mp4")
    return ExtractedClip(
        title=title[:255] or "Clip",
        duration=duration_value,
        playback_url=validated.normalized,
        http_headers=http_headers,
        content_type=content_type,
    )


def _selected_entry(info: dict | None) -> dict:
    if not isinstance(info, dict):
        raise LinkExtractError("Could not extract a playable clip from that link")
    entries = info.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                return entry
        raise LinkExtractError("That playlist did not contain a playable clip")
    return info


def _playback_url(info: dict) -> str:
    url = info.get("url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    for collection_name in ("requested_formats", "formats"):
        formats = info.get(collection_name)
        if not isinstance(formats, list):
            continue
        for fmt in reversed(formats):
            if not isinstance(fmt, dict):
                continue
            candidate = fmt.get("url")
            protocol = str(fmt.get("protocol") or "")
            if (
                isinstance(candidate, str)
                and candidate.strip()
                and fmt.get("acodec") not in {None, "none"}
                and fmt.get("vcodec") not in {None, "none"}
                and "m3u8" not in protocol
                and "dash" not in protocol
            ):
                return candidate.strip()
    raise LinkExtractError("That link does not provide a browser-playable video file")


def _validate_playback_url(url: str, *, require_https: bool) -> ValidatedDirectUrl:
    return validate_direct_url(
        url,
        require_https=require_https,
        max_length=MAX_EXTRACTED_URL_LENGTH,
    )


def _reject_private_redirects(require_https: bool):
    def _hook(response: requests.Response, *_args, **_kwargs):
        if not response.is_redirect:
            return response
        location = response.headers.get("Location")
        if not location:
            raise LinkExtractError("The extracted clip could not be opened")
        _validate_playback_url(
            urljoin(response.url, location),
            require_https=require_https,
        )
        return response

    return _hook


def _cached_clip(url: str) -> ExtractedClip | None:
    now = time.monotonic()
    with _extract_lock:
        item = _extract_cache.get(url)
        if item is None:
            return None
        expires_at, clip = item
        if expires_at <= now:
            _extract_cache.pop(url, None)
            return None
        return clip


def _store_clip(url: str, clip: ExtractedClip) -> None:
    with _extract_lock:
        _extract_cache[url] = (time.monotonic() + EXTRACT_CACHE_TTL_SECONDS, clip)


def _public_extract_error(exc: Exception) -> str:
    detail = str(exc).split("\n", 1)[0]
    if "Unsupported URL" in detail:
        return "That site is not a supported video link"
    return "Could not extract a playable clip from that link"
