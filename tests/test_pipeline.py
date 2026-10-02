import hashlib
import random

import pytest

from video.engagement import Engagement, SearchIndex, ShardedCounter, ViewDeduper, recommend
from video.processing import Pipeline, SimulatedTranscoder, Status, Video, ladder_for, master_playlist
from video.storage import ObjectStore, UrlSigner
from video.uploads import UploadError, UploadService


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_resumable_upload_out_of_order_with_retries():
    svc = UploadService(chunk_size=4)
    data = b"0123456789"  # chunks: 4, 4, 2
    s = svc.create("alice", "v.mp4", len(data), sha(data))
    assert s.total_chunks == 3
    assert svc.put_chunk(s.id, "alice", 2, b"89", sha(b"89"))
    assert svc.put_chunk(s.id, "alice", 0, b"0123", sha(b"0123"))
    assert not svc.put_chunk(s.id, "alice", 0, b"0123", sha(b"0123"))  # retried chunk is idempotent
    assert s.missing() == [1]
    with pytest.raises(UploadError, match="missing"):
        svc.complete(s.id, "alice")
    with pytest.raises(UploadError, match="checksum"):
        svc.put_chunk(s.id, "alice", 1, b"4567", sha(b"corrupt"))
    with pytest.raises(UploadError, match="must be 4 bytes"):
        svc.put_chunk(s.id, "alice", 1, b"45", sha(b"45"))
    svc.put_chunk(s.id, "alice", 1, b"4567", sha(b"4567"))
    assert svc.complete(s.id, "alice") == data
    with pytest.raises(LookupError):
        svc.get(s.id, "mallory")


def test_whole_file_checksum_is_verified():
    svc = UploadService(chunk_size=4)
    s = svc.create("alice", "v.mp4", 4, sha(b"abcd"))
    svc.put_chunk(s.id, "alice", 0, b"abce", sha(b"abce"))
    with pytest.raises(UploadError, match="file checksum"):
        svc.complete(s.id, "alice")


def test_ladder_never_upscales():
    assert [r.name for r in ladder_for(720)] == ["240p", "480p", "720p"]
    assert [r.name for r in ladder_for(2160)] == ["240p", "480p", "720p", "1080p"]
    assert [r.name for r in ladder_for(144)] == ["240p"]
    assert "RESOLUTION=1280x720" in master_playlist(ladder_for(720))


def make(tmp_path, transcoder):
    store = ObjectStore(tmp_path)
    store.put("uploads/v1/source", b"source-bytes")
    published = []
    return store, Pipeline(store, transcoder, max_attempts=3, on_published=published.append), published


def test_pipeline_retries_transient_failures_and_packages_hls(tmp_path):
    t = SimulatedTranscoder(fail_times={"480p": 2})
    store, pipeline, published = make(tmp_path, t)
    v = pipeline.process(Video("v1", "ch", "Pump teardown", "", [], 720, 10))
    assert v.status is Status.READY and v.renditions == ["240p", "480p", "720p"]
    assert t.calls.count("480p") == 3
    playlist = store.get("videos/v1/720p/index.m3u8").decode()
    assert playlist.count("#EXTINF") == 3 and "#EXTINF:2.000," in playlist and playlist.endswith("#EXT-X-ENDLIST\n")
    assert store.exists("videos/v1/master.m3u8") and published == [v]


def test_pipeline_fails_after_max_attempts_and_resumes_idempotently(tmp_path):
    t = SimulatedTranscoder(fail_times={"720p": 5})
    store, pipeline, _ = make(tmp_path, t)
    v = pipeline.process(Video("v1", "ch", "t", "", [], 720, 8))
    assert v.status is Status.FAILED and "720p" in str(v.error)
    t.calls.clear()
    t.fail_times = {}
    assert pipeline.process(v).status is Status.READY
    assert t.calls == ["720p"]  # completed renditions were not transcoded again


def test_signed_urls_cover_one_video_prefix_and_expire():
    now = [1_000.0]
    signer = UrlSigner(b"secret", "/cdn", clock=lambda: now[0])
    url = signer.sign("videos/v1/master.m3u8", ttl_seconds=60)
    _, _, _, expires, sig, *_ = url.split("/")
    assert signer.verify("videos/v1/720p/segment_00000.ts", int(expires), sig)  # relative fetch under the prefix
    assert not signer.verify("videos/v2/master.m3u8", int(expires), sig)
    now[0] += 61
    assert not signer.verify("videos/v1/master.m3u8", int(expires), sig)


def test_engagement_counters_likes_search_and_ranking():
    c = ShardedCounter(shards=8, rng=random.Random(1))
    for _ in range(1000):
        c.incr("v1")
    assert c.value("v1") == 1000
    now = [0.0]
    d = ViewDeduper(window_s=60, clock=lambda: now[0])
    assert d.should_count("u", "v") and not d.should_count("u", "v")
    now[0] = 61
    assert d.should_count("u", "v")

    e = Engagement()
    assert e.like("a", "v1") == 1 and e.like("a", "v1") == 1 and e.unlike("a", "v1") == 0
    idx = SearchIndex()
    idx.index("v1", "Centrifugal pump seal replacement", "how to replace a mechanical seal", ["pump", "maintenance"])
    idx.index("v2", "Cooking pasta", "dinner", ["food"])
    assert [v for v, _ in idx.search("pump seal")] == ["v1"]
    e.published_at.update({"old": 0.0, "new": 3600 * 48.0})
    for _ in range(50):
        e.views.incr("old")
    for _ in range(40):
        e.views.incr("new")
    assert recommend(["old", "new"], e, now=3600 * 49.0) == ["new", "old"]  # freshness outweighs a small lead
