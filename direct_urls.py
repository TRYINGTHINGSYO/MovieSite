from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit


MAX_DIRECT_URL_LENGTH = 4096
BLOCKED_SCHEMES = frozenset({"javascript", "data", "file", "ftp", "gopher"})
ALLOWED_PROBE_RESULTS = frozenset(
    {
        "playable",
        "playable_no_seek",
        "unsupported_format",
        "network_or_cors_failure",
        "unavailable",
        "not_probed",
    }
)
PLAYABLE_PROBE_RESULTS = frozenset({"playable", "playable_no_seek"})
LOCAL_HOST_SUFFIXES = (".internal", ".lan", ".home", ".localdomain", ".home.arpa")


class DirectUrlError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedDirectUrl:
    original: str
    normalized: str


def validate_direct_url(value: object, *, require_https: bool) -> ValidatedDirectUrl:
    if not isinstance(value, str):
        raise DirectUrlError("Media URL must be a string")
    if len(value) > MAX_DIRECT_URL_LENGTH:
        raise DirectUrlError("Media URL is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DirectUrlError("Media URL contains control characters")
    if "\\" in value:
        raise DirectUrlError("Media URL contains an ambiguous backslash")
    original = value.strip()
    if not original:
        raise DirectUrlError("An absolute media URL is required")
    if any(character.isspace() for character in original):
        raise DirectUrlError("Media URL contains whitespace")

    try:
        parsed = urlsplit(original)
    except ValueError as exc:
        raise DirectUrlError("Malformed media URL") from exc

    scheme = parsed.scheme.lower()
    if not scheme or not parsed.netloc:
        raise DirectUrlError("An absolute media URL is required")
    if scheme in BLOCKED_SCHEMES or scheme not in {"http", "https"}:
        raise DirectUrlError("Only HTTP and HTTPS media URLs are supported")
    if require_https and scheme != "https":
        raise DirectUrlError("HTTPS is required for direct media URLs")
    if parsed.username is not None or parsed.password is not None:
        raise DirectUrlError("Embedded URL credentials are not allowed")

    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise DirectUrlError("Malformed URL port") from exc
    if not hostname:
        raise DirectUrlError("Media URL must include a hostname")
    if re.search(r"\s", hostname):
        raise DirectUrlError("Malformed media URL hostname")

    normalized_host = _validate_hostname(hostname)
    if port is not None and not (1 <= port <= 65535):
        raise DirectUrlError("Malformed URL port")

    netloc_host = (
        f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    )
    netloc = f"{netloc_host}:{port}" if port is not None else netloc_host
    normalized = urlunsplit(
        SplitResult(scheme, netloc, parsed.path or "/", parsed.query, "")
    )
    return ValidatedDirectUrl(original=original, normalized=normalized)


def validate_probe_result(value: object) -> str:
    result = "not_probed" if value in (None, "") else str(value)
    if result not in ALLOWED_PROBE_RESULTS:
        raise DirectUrlError("Invalid browser probe result")
    return result


def require_playable_probe(value: object) -> str:
    result = validate_probe_result(value)
    if result not in PLAYABLE_PROBE_RESULTS:
        raise DirectUrlError("The media URL must pass the browser playback probe")
    return result


def _validate_hostname(hostname: str) -> str:
    candidate = hostname.rstrip(".").lower()
    if candidate == "localhost" or candidate.endswith(".localhost"):
        raise DirectUrlError("Localhost media URLs are not allowed")
    if candidate == "local" or candidate.endswith(".local"):
        raise DirectUrlError("Local network media URLs are not allowed")
    if candidate.endswith(LOCAL_HOST_SUFFIXES):
        raise DirectUrlError("Local network media URLs are not allowed")

    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        if re.fullmatch(
            r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*",
            candidate,
        ):
            raise DirectUrlError("Ambiguous numeric IP media URLs are not allowed")
        try:
            ascii_host = candidate.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise DirectUrlError("Malformed media URL hostname") from exc
        if len(ascii_host) > 253:
            raise DirectUrlError("Malformed media URL hostname")
        labels = ascii_host.split(".")
        if any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not re.fullmatch(r"[a-z0-9-]+", label)
            for label in labels
        ):
            raise DirectUrlError("Malformed media URL hostname")
        if "." not in ascii_host:
            raise DirectUrlError("Single-label media hostnames are not allowed")
        return ascii_host

    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise DirectUrlError("Private or local IP media URLs are not allowed")
    return address.compressed
