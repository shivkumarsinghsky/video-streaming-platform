"""Engagement at scale: sharded view counters, idempotent likes, subscriptions feed, simple search and ranking.

View counts are the hottest write path on a video platform: a viral video can receive many thousands of views per
second, far beyond what one database row can absorb. Views are accumulated in N shards (and, in production, batched
in a stream processor), so the displayed count is eventually consistent.
"""

from __future__ import annotations

import math
import random
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

WORD = re.compile(r"[a-z0-9]+")


class ShardedCounter:
    def __init__(self, shards: int = 16, rng: random.Random | None = None) -> None:
        self.shards = shards
        self._values: dict[str, list[int]] = defaultdict(lambda: [0] * shards)
        self._lock = threading.Lock()
        self._rng = rng or random.Random()

    def incr(self, key: str, n: int = 1) -> None:
        shard = self._rng.randrange(self.shards)  # spread contention across shards
        with self._lock:
            self._values[key][shard] += n

    def value(self, key: str) -> int:
        with self._lock:
            return sum(self._values.get(key, [0]))


@dataclass
class ViewDeduper:
    """Count at most one view per (viewer, video) per window — basic protection against refresh inflation."""

    window_s: float = 30 * 60
    clock: object = time.time
    _seen: dict[tuple[str, str], float] = field(default_factory=dict)

    def should_count(self, viewer: str, video_id: str) -> bool:
        now = float(self.clock())  # type: ignore[operator]
        last = self._seen.get((viewer, video_id))
        if last is not None and now - last < self.window_s:
            return False
        self._seen[(viewer, video_id)] = now
        return True


@dataclass
class Engagement:
    views: ShardedCounter = field(default_factory=ShardedCounter)
    deduper: ViewDeduper = field(default_factory=ViewDeduper)
    likes: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    subscriptions: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # user → channels
    published_at: dict[str, float] = field(default_factory=dict)

    def record_view(self, viewer: str, video_id: str) -> bool:
        if self.deduper.should_count(viewer, video_id):
            self.views.incr(video_id)
            return True
        return False

    def like(self, user: str, video_id: str) -> int:
        self.likes[video_id].add(user)  # set semantics: liking twice is idempotent
        return len(self.likes[video_id])

    def unlike(self, user: str, video_id: str) -> int:
        self.likes[video_id].discard(user)
        return len(self.likes[video_id])


class SearchIndex:
    """Inverted index over title, description and tags with TF-IDF-style scoring (Elasticsearch/OpenSearch in
    production, fed asynchronously by VideoPublished events)."""

    def __init__(self) -> None:
        self._postings: dict[str, Counter[str]] = defaultdict(Counter)
        self._docs: set[str] = set()

    def index(self, video_id: str, title: str, description: str, tags: list[str]) -> None:
        self._docs.add(video_id)
        for weight, text in ((3, title), (1, description), (2, " ".join(tags))):
            for word in WORD.findall(text.lower()):
                self._postings[word][video_id] += weight

    def search(self, query: str, limit: int = 10) -> list[tuple[str, float]]:
        scores: dict[str, float] = defaultdict(float)
        for word in set(WORD.findall(query.lower())):
            postings = self._postings.get(word)
            if not postings:
                continue
            idf = math.log(1 + len(self._docs) / len(postings))
            for video_id, tf in postings.items():
                scores[video_id] += tf * idf
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
        return [(v, round(s, 4)) for v, s in ranked]


def recommend(candidates: list[str], engagement: Engagement, now: float, limit: int = 10) -> list[str]:
    """Simple, explainable ranking: popularity (log views + likes) decayed by age. Real systems use two stages —
    candidate generation (collaborative filtering, embeddings) then a learned ranking model."""

    def score(video_id: str) -> float:
        popularity = math.log1p(engagement.views.value(video_id)) + 0.5 * math.log1p(len(engagement.likes[video_id]))
        age_hours = max(0.0, (now - engagement.published_at.get(video_id, now)) / 3600)
        return float(popularity / (1 + age_hours / 24) ** 1.5)

    return sorted(candidates, key=score, reverse=True)[:limit]
