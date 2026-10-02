# ADR-001: Resumable, Chunked Uploads Directly to Object Storage

- **Status:** Accepted
- **Date:** 2026-10-01

## Context

Uploads of several gigabytes from mobile networks frequently fail. Streaming that volume through API servers wastes
bandwidth and capacity on the most expensive tier.

## Decision

Upload sessions with fixed-size chunks, per-chunk checksums, any-order idempotent chunk writes, a status endpoint
returning missing chunks, and an explicit completion that verifies the whole-file checksum. In production, chunks are
written to object storage via pre-signed multipart upload URLs.

## Alternatives Considered

- **Single PUT** — simplest; any failure restarts the whole upload.
- **tus protocol server** — standard resumable protocol; a good option when not using cloud multipart directly.

## Trade-offs

Clients implement chunking and resumption; abandoned sessions need garbage collection.

## Consequences

The prototype tests out-of-order chunks, idempotent retries, checksum mismatches and missing chunks.
