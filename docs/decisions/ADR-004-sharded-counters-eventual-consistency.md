# ADR-004: Sharded View Counters With Eventual Consistency

- **Status:** Accepted
- **Date:** 2026-10-01

## Context

A viral video can receive thousands of views per second; a single counter row becomes a write hotspot. Exact,
immediately consistent counts are not a user requirement.

## Decision

Record view events to a stream; increment one of N counter shards per event (randomly chosen) and sum shards on read
(cached). De-duplicate views per viewer and time window. Analytics use the stream, not the counters.

## Alternatives Considered

- **Single counter with atomic increments** — exact, does not scale under contention.
- **Only batch analytics** — scalable, but counts would lag by hours.

## Trade-offs

Counts lag by seconds to minutes; reads cost N lookups (mitigated by caching).

## Consequences

Tests verify sharded sums and the de-duplication window.
