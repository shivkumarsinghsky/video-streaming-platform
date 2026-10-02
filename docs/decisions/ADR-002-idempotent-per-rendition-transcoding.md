# ADR-002: Asynchronous, Idempotent Transcoding Jobs per Rendition

- **Status:** Accepted
- **Date:** 2026-10-01

## Context

Transcoding is CPU/GPU-intensive, takes minutes to hours and fails transiently (preempted workers). Users should be
able to watch as soon as possible.

## Decision

On `VideoUploaded`, the orchestrator schedules one job per rendition in the ladder (capped at the source
resolution). Each job writes segments, then its playlist last; a rendition is complete only when its playlist exists.
Jobs are retried with a bounded number of attempts; the video becomes READY when all renditions and the master
playlist exist.

## Alternatives Considered

- **One job per video** — simpler; a late failure repeats all work and blocks availability.
- **Per-segment jobs (chunked encoding)** — maximal parallelism for very long videos; more complex stitching.

## Trade-offs

More orchestration state; a video is only partially available while higher renditions are processing.

## Consequences

Workers can run on spot capacity; redelivered jobs resume without redoing finished renditions (tested).
