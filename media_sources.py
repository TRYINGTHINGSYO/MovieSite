from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from models import MediaAsset, MediaSource, QueueEntry

from link_extract import EXTRACTOR_YT_DLP


MUX_UPLOAD = "mux_upload"
DIRECT_URL = "direct_url"
BROWSER_LOCAL = "browser_local"


class SourceAdapter(Protocol):
    source_type: str

    def public_payload(
        self, source: MediaSource, *, browser_client_id: str | None = None
    ) -> dict: ...


@dataclass(frozen=True)
class MuxSourceAdapter:
    source_type: str = MUX_UPLOAD

    def public_payload(
        self, source: MediaSource, *, browser_client_id: str | None = None
    ) -> dict:
        mux = source.mux
        return {
            "source_id": source.id,
            "source_type": self.source_type,
            "status": source.status,
            "playback_id": mux.playback_id if mux else None,
            "url": (
                f"https://stream.mux.com/{mux.playback_id}.m3u8"
                if mux and mux.playback_id
                else None
            ),
            "error": source.error,
            "availability": "remote" if source.status == "ready" else source.status,
        }


@dataclass(frozen=True)
class DirectUrlSourceAdapter:
    source_type: str = DIRECT_URL

    def public_payload(
        self, source: MediaSource, *, browser_client_id: str | None = None
    ) -> dict:
        direct = source.direct_url
        playback_url = direct.normalized_url if direct else None
        if direct and direct.extractor == EXTRACTOR_YT_DLP:
            playback_url = f"/api/media/sources/{source.id}/stream"
        return {
            "source_id": source.id,
            "source_type": self.source_type,
            "status": source.status,
            "url": playback_url,
            "error": source.error,
            "probe_result": direct.probe_result if direct else "not_probed",
            "extractor": direct.extractor if direct else "direct",
            "availability": (
                "remote"
                if source.status == "ready"
                else "browser_probe_required"
            ),
        }


@dataclass(frozen=True)
class BrowserLocalSourceAdapter:
    source_type: str = BROWSER_LOCAL

    def public_payload(
        self, source: MediaSource, *, browser_client_id: str | None = None
    ) -> dict:
        local = source.browser_local
        owns_source = bool(
            local
            and browser_client_id
            and local.browser_client_id == browser_client_id
        )
        if source.status == "missing":
            availability = "LOCAL_DATA_MISSING"
        elif source.status == "error":
            availability = "ERROR"
        elif owns_source and source.status == "ready":
            availability = "AVAILABLE_THIS_BROWSER"
        else:
            availability = "LOCAL_OWNER_OFFLINE"
        payload = {
            "source_id": source.id,
            "source_type": self.source_type,
            "status": source.status,
            "url": None,
            "error": source.error,
            "availability": availability,
        }
        if owns_source:
            payload["storage_key"] = local.storage_key
        return payload


ADAPTERS: dict[str, SourceAdapter] = {
    MUX_UPLOAD: MuxSourceAdapter(),
    DIRECT_URL: DirectUrlSourceAdapter(),
    BROWSER_LOCAL: BrowserLocalSourceAdapter(),
}


def preferred_source(asset: MediaAsset) -> MediaSource | None:
    active = [source for source in asset.sources if source.deleted_at is None]
    if not active:
        return None
    source_rank = {MUX_UPLOAD: 0, DIRECT_URL: 1, BROWSER_LOCAL: 2}
    status_rank = {"ready": 0, "processing": 1, "uploading": 2, "creating": 3}
    return min(
        active,
        key=lambda source: (
            status_rank.get(source.status, 9),
            source_rank.get(source.source_type, 9),
            source.priority,
            source.created_at,
        ),
    )


def public_source(
    source: MediaSource | None, *, browser_client_id: str | None = None
) -> dict:
    if source is None:
        return {
            "source_id": None,
            "source_type": None,
            "status": "unavailable",
            "url": None,
            "error": "No media source is available",
            "availability": "unavailable",
        }
    adapter = ADAPTERS.get(source.source_type)
    if adapter is None:
        return {
            "source_id": source.id,
            "source_type": source.source_type,
            "status": "unsupported",
            "url": None,
            "error": f"Unsupported source type: {source.source_type}",
            "availability": "unsupported",
        }
    return adapter.public_payload(source, browser_client_id=browser_client_id)


def queue_entry_to_public(
    entry: QueueEntry, *, browser_client_id: str | None = None
) -> dict:
    asset = entry.room_media.asset
    source_payload = public_source(
        preferred_source(asset), browser_client_id=browser_client_id
    )
    return {
        "id": entry.id,
        "queue_entry_id": entry.id,
        "room_media_id": entry.room_media_id,
        "media_asset_id": asset.id,
        "name": asset.title,
        "duration": asset.duration,
        **source_payload,
    }


def room_media_to_public(room_media, *, browser_client_id: str | None = None) -> dict:
    asset = room_media.asset
    return {
        "id": room_media.id,
        "media_asset_id": asset.id,
        "name": asset.title,
        "duration": asset.duration,
        "sources": [
            public_source(source, browser_client_id=browser_client_id)
            for source in asset.sources
            if source.deleted_at is None
        ],
    }
