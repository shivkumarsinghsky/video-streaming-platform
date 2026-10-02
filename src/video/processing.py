"""Asynchronous transcoding pipeline and HLS packaging.

    upload complete → VideoUploaded event → transcode job per rendition (parallel, retried, idempotent)
                    → thumbnails → HLS playlists → status READY → VideoPublished event

Renditions follow an adaptive-bitrate ladder capped at the source resolution (never upscale). The `Transcoder`
interface hides the media tool: production uses FFmpeg (or a managed service) on GPU/CPU worker fleets; the
`SimulatedTranscoder` writes deterministic placeholder segments so the pipeline is testable without media tools.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from video.storage import ObjectStore

log = logging.getLogger("video.processing")

SEGMENT_SECONDS = 4


@dataclass(frozen=True)
class Rendition:
    name: str
    height: int
    bitrate_kbps: int
    width: int


LADDER = [
    Rendition("240p", 240, 400, 426),
    Rendition("480p", 480, 1000, 854),
    Rendition("720p", 720, 2800, 1280),
    Rendition("1080p", 1080, 5000, 1920),
]


def ladder_for(source_height: int) -> list[Rendition]:
    """Never upscale; always produce at least the lowest rendition for poor networks."""
    return [r for r in LADDER if r.height <= source_height] or [LADDER[0]]


class Status(str, Enum):
    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"


class TranscodeError(RuntimeError):
    pass


class Transcoder(Protocol):
    def transcode(self, source: bytes, rendition: Rendition, duration_s: int) -> list[bytes]:
        """Return the media segments for one rendition."""


class SimulatedTranscoder:
    def __init__(self, fail_times: dict[str, int] | None = None) -> None:
        self.fail_times = dict(fail_times or {})  # rendition → number of transient failures to simulate
        self.calls: list[str] = []

    def transcode(self, source: bytes, rendition: Rendition, duration_s: int) -> list[bytes]:
        self.calls.append(rendition.name)
        if self.fail_times.get(rendition.name, 0) > 0:
            self.fail_times[rendition.name] -= 1
            raise TranscodeError(f"worker lost while transcoding {rendition.name}")
        segments = -(-duration_s // SEGMENT_SECONDS)
        return [f"{rendition.name}-segment-{i}-of-{len(source)}".encode() for i in range(segments)]


def media_playlist(rendition: Rendition, segment_count: int, duration_s: int) -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{SEGMENT_SECONDS}", "#EXT-X-MEDIA-SEQUENCE:0"]
    for i in range(segment_count):
        length = min(SEGMENT_SECONDS, duration_s - i * SEGMENT_SECONDS)
        lines += [f"#EXTINF:{length:.3f},", f"segment_{i:05d}.ts"]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def master_playlist(renditions: list[Rendition]) -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for r in renditions:
        lines += [
            f"#EXT-X-STREAM-INF:BANDWIDTH={r.bitrate_kbps * 1000},RESOLUTION={r.width}x{r.height}",
            f"{r.name}/index.m3u8",
        ]
    return "\n".join(lines) + "\n"


@dataclass
class Video:
    id: str
    channel_id: str
    title: str
    description: str
    tags: list[str]
    source_height: int
    duration_s: int
    status: Status = Status.UPLOADED
    renditions: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class Pipeline:
    store: ObjectStore
    transcoder: Transcoder
    max_attempts: int = 3
    on_published: Callable[[Video], None] = lambda v: None

    def process(self, video: Video) -> Video:
        """Idempotent: renditions whose playlist already exists are skipped, so a re-delivered job resumes."""
        video.status = Status.PROCESSING
        ladder = ladder_for(video.source_height)
        source = self.store.get(f"uploads/{video.id}/source")
        for rendition in ladder:
            prefix = f"videos/{video.id}/{rendition.name}"
            if self.store.exists(f"{prefix}/index.m3u8"):
                continue
            for attempt in range(1, self.max_attempts + 1):
                try:
                    segments = self.transcoder.transcode(source, rendition, video.duration_s)
                    break
                except TranscodeError as e:
                    log.warning("transcode failed (attempt %s): %s", attempt, e)
                    if attempt == self.max_attempts:
                        video.status, video.error = Status.FAILED, str(e)
                        return video
            for i, seg in enumerate(segments):
                self.store.put(f"{prefix}/segment_{i:05d}.ts", seg)
            # The playlist is written last: its presence marks the rendition as complete.
            self.store.put(f"{prefix}/index.m3u8", media_playlist(rendition, len(segments), video.duration_s).encode())
        self.store.put(f"videos/{video.id}/thumbnail.jpg", b"thumbnail-placeholder")
        self.store.put(f"videos/{video.id}/master.m3u8", master_playlist(ladder).encode())
        video.renditions = [r.name for r in ladder]
        video.status = Status.READY
        self.on_published(video)
        return video
