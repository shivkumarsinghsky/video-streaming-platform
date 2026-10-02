# ADR-003: HLS Adaptive Streaming via CDN With Signed Path Tokens

- **Status:** Accepted
- **Date:** 2026-10-01

## Context

Egress dominates cost and latency; private and unlisted videos must not be fetchable by anyone who guesses a URL.

## Decision

Package renditions as HLS (segments + playlists), serve through a CDN with long cache lifetimes for immutable
segments, and authorise access with an HMAC token embedded in the URL path and covering the video's prefix
(`/cdn/t/<expires>/<sig>/videos/<id>/...`), so relative URIs inside playlists inherit the token.

## Alternatives Considered

- **Query-string signatures per URL** — standard for single files; players drop query strings on relative playlist
  URIs, requiring playlist rewriting per viewer.
- **Signed cookies** — works for browsers; less suitable for native/TV clients.
- **DRM** — required for premium licensed content; additional licence servers and cost.

## Trade-offs

A leaked token grants access to that video until expiry; short TTLs and per-session tokens limit exposure.

## Consequences

Tests verify that segments under the signed prefix are served, other prefixes are rejected, and tokens expire.
