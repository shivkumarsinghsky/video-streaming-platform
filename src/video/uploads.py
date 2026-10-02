"""Resumable, chunked uploads (tus-like).

Large files over mobile networks fail mid-way; restarting a 5 GB upload is unacceptable. The client creates an
upload session, sends fixed-size chunks (each with its SHA-256, idempotent per index, in any order), can ask which
chunks the server has, and completes when done. Completion verifies the whole-file checksum before anything is
processed. In production chunks go straight to object storage via multipart upload / pre-signed URLs, bypassing the
API servers.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from dataclasses import dataclass, field

CHUNK_SIZE = 256 * 1024  # small for the prototype; 8–64 MB in production
MAX_FILE_SIZE = 10 * 1024**3


class UploadError(ValueError):
    pass


@dataclass
class UploadSession:
    id: str
    owner: str
    filename: str
    size: int
    sha256: str
    chunk_size: int
    chunks: dict[int, bytes] = field(default_factory=dict)
    completed: bool = False

    @property
    def total_chunks(self) -> int:
        return max(1, -(-self.size // self.chunk_size))

    def missing(self) -> list[int]:
        return [i for i in range(self.total_chunks) if i not in self.chunks]


class UploadService:
    def __init__(self, chunk_size: int = CHUNK_SIZE) -> None:
        self.chunk_size = chunk_size
        self._sessions: dict[str, UploadSession] = {}
        self._lock = threading.Lock()

    def create(self, owner: str, filename: str, size: int, sha256: str) -> UploadSession:
        if not 0 < size <= MAX_FILE_SIZE:
            raise UploadError("size must be between 1 byte and 10 GB")
        if len(sha256) != 64:
            raise UploadError("sha256 must be a hex digest")
        session = UploadSession(uuid.uuid4().hex, owner, filename, size, sha256.lower(), self.chunk_size)
        with self._lock:
            self._sessions[session.id] = session
        return session

    def get(self, upload_id: str, owner: str) -> UploadSession:
        s = self._sessions.get(upload_id)
        if s is None or s.owner != owner:
            raise LookupError("upload not found")
        return s

    def put_chunk(self, upload_id: str, owner: str, index: int, data: bytes, chunk_sha256: str) -> bool:
        """Returns True if stored, False if this exact chunk was already present (idempotent retry)."""
        s = self.get(upload_id, owner)
        if s.completed:
            raise UploadError("upload already completed")
        if not 0 <= index < s.total_chunks:
            raise UploadError("chunk index out of range")
        expected_len = s.chunk_size if index < s.total_chunks - 1 else s.size - s.chunk_size * (s.total_chunks - 1)
        if len(data) != expected_len:
            raise UploadError(f"chunk {index} must be {expected_len} bytes")
        if hashlib.sha256(data).hexdigest() != chunk_sha256.lower():
            raise UploadError(f"checksum mismatch for chunk {index}; resend it")
        with self._lock:
            if index in s.chunks:
                return False
            s.chunks[index] = data
        return True

    def complete(self, upload_id: str, owner: str) -> bytes:
        s = self.get(upload_id, owner)
        if s.missing():
            raise UploadError(f"missing chunks: {s.missing()[:20]}")
        data = b"".join(s.chunks[i] for i in range(s.total_chunks))
        if hashlib.sha256(data).hexdigest() != s.sha256:
            raise UploadError("file checksum mismatch")
        s.completed = True
        s.chunks.clear()  # parts are released once assembled
        return data
