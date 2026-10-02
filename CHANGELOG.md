# Changelog

## [1.0.0] - 2026-10-02

### Added

- Video sharing platform reference system design (requirements, capacity, uploads, processing, CDN, storage,
  data model, scaling, trade-offs).
- Python prototype: resumable chunked uploads with checksums, transcoding pipeline with retries and idempotent
  resume, HLS packaging (simulated encoder), signed CDN path tokens, sharded view counters, likes, subscriptions,
  search and recommendations.
- Tests, Dockerfile, docker compose, CI, ADR-001 to ADR-004.
