"""HTTP API of the prototype. `X-User` stands in for the subject of a verified access token."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from video.engagement import Engagement, SearchIndex, recommend
from video.processing import Pipeline, SimulatedTranscoder, Status, Transcoder, Video
from video.storage import ObjectStore, UrlSigner
from video.uploads import UploadError, UploadService

User = Annotated[str, Header(alias="X-User", pattern=r"^[a-z0-9_-]{1,40}$")]


class CreateUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class CompleteUpload(BaseModel):
    channel_id: str
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    source_height: int = Field(ge=144, le=4320)
    duration_seconds: int = Field(gt=0, le=12 * 3600)


class CreateChannel(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def create_app(
    storage_root: Path, signing_secret: bytes = b"dev-only-secret", transcoder: Transcoder | None = None
) -> FastAPI:
    app = FastAPI(title="Video Sharing Platform (reference prototype)")
    store = ObjectStore(storage_root)
    signer = UrlSigner(signing_secret, "/cdn")
    uploads = UploadService()
    engagement = Engagement()
    search = SearchIndex()
    videos: dict[str, Video] = {}
    channels: dict[str, dict[str, str]] = {}

    def on_published(v: Video) -> None:  # in production: VideoPublished event → search indexer, notifications
        engagement.published_at[v.id] = time.time()
        search.index(v.id, v.title, v.description, v.tags)

    pipeline = Pipeline(store, transcoder or SimulatedTranscoder(), on_published=on_published)
    app.state.pipeline, app.state.engagement, app.state.videos = pipeline, engagement, videos

    def view(v: Video) -> dict[str, Any]:
        out = asdict(v) | {
            "status": v.status.value,
            "views": engagement.views.value(v.id),
            "likes": len(engagement.likes[v.id]),
        }
        if v.status is Status.READY:
            out["playbackUrl"] = signer.sign(f"videos/{v.id}/master.m3u8")
            out["thumbnailUrl"] = signer.sign(f"videos/{v.id}/thumbnail.jpg")
        return out

    def get_video(video_id: str) -> Video:
        if video_id not in videos:
            raise HTTPException(404, "video not found")
        return videos[video_id]

    @app.post("/v1/channels", status_code=201)
    def create_channel(body: CreateChannel, user: User) -> dict[str, str]:
        cid = "ch_" + uuid.uuid4().hex[:10]
        channels[cid] = {"id": cid, "name": body.name, "owner": user}
        return channels[cid]

    @app.post("/v1/uploads", status_code=201)
    def create_upload(body: CreateUpload, user: User) -> dict[str, Any]:
        try:
            s = uploads.create(user, body.filename, body.size, body.sha256)
        except UploadError as e:
            raise HTTPException(400, str(e)) from e
        return {"uploadId": s.id, "chunkSize": s.chunk_size, "totalChunks": s.total_chunks}

    @app.put("/v1/uploads/{upload_id}/chunks/{index}", status_code=204)
    async def put_chunk(
        upload_id: str, index: int, request: Request, user: User, x_chunk_sha256: Annotated[str, Header()]
    ) -> Response:
        try:
            uploads.put_chunk(upload_id, user, index, await request.body(), x_chunk_sha256)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except UploadError as e:
            raise HTTPException(400, str(e)) from e
        return Response(status_code=204)

    @app.get("/v1/uploads/{upload_id}")
    def upload_status(upload_id: str, user: User) -> dict[str, Any]:
        try:
            s = uploads.get(upload_id, user)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        return {"uploadId": s.id, "missingChunks": s.missing(), "completed": s.completed}

    @app.post("/v1/uploads/{upload_id}/complete", status_code=202)
    def complete(upload_id: str, body: CompleteUpload, user: User, tasks: BackgroundTasks) -> dict[str, Any]:
        channel = channels.get(body.channel_id)
        if channel is None or channel["owner"] != user:
            raise HTTPException(403, "you can only upload to your own channel")
        try:
            data = uploads.complete(upload_id, user)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except UploadError as e:
            raise HTTPException(409, str(e)) from e
        v = Video(
            upload_id,
            body.channel_id,
            body.title,
            body.description,
            body.tags,
            body.source_height,
            body.duration_seconds,
        )
        videos[v.id] = v
        store.put(f"uploads/{v.id}/source", data)
        tasks.add_task(pipeline.process, v)  # production: durable job queue + worker fleet
        return view(v)

    @app.get("/v1/videos/{video_id}")
    def get(video_id: str) -> dict[str, Any]:
        return view(get_video(video_id))

    @app.post("/v1/videos/{video_id}/views", status_code=202)
    def record_view(video_id: str, user: User) -> dict[str, bool]:
        get_video(video_id)
        return {"counted": engagement.record_view(user, video_id)}

    @app.put("/v1/videos/{video_id}/like")
    def like(video_id: str, user: User) -> dict[str, int]:
        get_video(video_id)
        return {"likes": engagement.like(user, video_id)}

    @app.delete("/v1/videos/{video_id}/like")
    def unlike(video_id: str, user: User) -> dict[str, int]:
        return {"likes": engagement.unlike(user, video_id)}

    @app.post("/v1/channels/{channel_id}/subscribe", status_code=204)
    def subscribe(channel_id: str, user: User) -> Response:
        if channel_id not in channels:
            raise HTTPException(404, "channel not found")
        engagement.subscriptions[user].add(channel_id)
        return Response(status_code=204)

    @app.get("/v1/feed/subscriptions")
    def feed(user: User, limit: int = 20) -> list[dict[str, Any]]:
        subs = engagement.subscriptions[user]
        ready = [v for v in videos.values() if v.channel_id in subs and v.status is Status.READY]
        ready.sort(key=lambda v: engagement.published_at.get(v.id, 0), reverse=True)
        return [view(v) for v in ready[:limit]]

    @app.get("/v1/search")
    def do_search(q: str, limit: int = 10) -> list[dict[str, Any]]:
        return [view(videos[vid]) | {"score": s} for vid, s in search.search(q, limit) if vid in videos]

    @app.get("/v1/recommendations")
    def recommendations(limit: int = 10) -> list[str]:
        ready = [v.id for v in videos.values() if v.status is Status.READY]
        return recommend(ready, engagement, time.time(), limit)

    @app.get("/cdn/t/{expires}/{sig}/{key:path}")
    def cdn(expires: int, sig: str, key: str) -> Response:
        """Simulates the CDN edge: validates the path token for the object's prefix, then serves the object."""
        if not key.startswith("videos/") or not signer.verify(key, expires, sig):
            raise HTTPException(403, "invalid or expired signature")
        if not store.exists(key):
            raise HTTPException(404, "not found")
        media = "application/vnd.apple.mpegurl" if key.endswith(".m3u8") else "application/octet-stream"
        return Response(store.get(key), media_type=media, headers={"Cache-Control": "public, max-age=31536000"})

    return app
