"""Object storage abstraction (S3/GCS/Azure Blob in production; a local directory here) and signed CDN URLs."""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from urllib.parse import quote


class ObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValueError("invalid object key")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)  # atomic publish: readers never see partial objects

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete_prefix(self, prefix: str) -> None:
        base = self._path(prefix)
        if base.is_dir():
            for p in sorted(base.rglob("*"), reverse=True):
                p.unlink() if p.is_file() else p.rmdir()
            base.rmdir()


class UrlSigner:
    """HMAC-signed, expiring CDN URLs validated at the edge.

    The token is part of the *path* and covers a prefix (one video's directory), e.g.
    `/cdn/t/<expires>/<sig>/videos/<id>/master.m3u8`. HLS players resolve the relative URIs inside playlists
    (`720p/index.m3u8`, `segment_00001.ts`) against that path, so every segment request carries the same token —
    query-string signatures would be dropped by players on relative fetches."""

    def __init__(self, secret: bytes, base_url: str, clock: object = time.time) -> None:
        self.secret = secret
        self.base_url = base_url.rstrip("/")
        self.clock = clock

    def _sig(self, key: str, expires: int) -> str:
        return hmac.new(self.secret, f"{key}:{expires}".encode(), hashlib.sha256).hexdigest()[:32]

    @staticmethod
    def prefix_of(key: str) -> str:
        return "/".join(key.split("/")[:2]) + "/"  # e.g. "videos/<id>/"

    def sign(self, key: str, ttl_seconds: int = 3600) -> str:
        expires = int(self.clock()) + ttl_seconds  # type: ignore[operator]
        sig = self._sig(self.prefix_of(key), expires)
        return f"{self.base_url}/t/{expires}/{sig}/{quote(key)}"

    def verify(self, key: str, expires: int, sig: str) -> bool:
        if expires < int(self.clock()):  # type: ignore[operator]
            return False
        return hmac.compare_digest(sig, self._sig(self.prefix_of(key), expires))
