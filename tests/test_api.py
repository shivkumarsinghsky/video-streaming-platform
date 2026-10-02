import hashlib

from fastapi.testclient import TestClient

from video.api import create_app


def test_upload_to_playback_end_to_end(tmp_path):
    client = TestClient(create_app(tmp_path))
    alice, bob = {"X-User": "alice"}, {"X-User": "bob"}
    channel = client.post("/v1/channels", json={"name": "Maintenance How-To"}, headers=alice).json()

    data = bytes(range(256)) * 2048  # 512 KiB → 2 chunks of 256 KiB
    up = client.post(
        "/v1/uploads",
        json={"filename": "seal.mp4", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        headers=alice,
    ).json()
    size = up["chunkSize"]
    for i in range(up["totalChunks"]):
        chunk = data[i * size : (i + 1) * size]
        r = client.put(
            f"/v1/uploads/{up['uploadId']}/chunks/{i}",
            content=chunk,
            headers=alice | {"X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()},
        )
        assert r.status_code == 204
    assert client.get(f"/v1/uploads/{up['uploadId']}", headers=alice).json()["missingChunks"] == []

    meta = {
        "channel_id": channel["id"],
        "title": "Replacing a pump seal",
        "tags": ["pump", "seal"],
        "source_height": 1080,
        "duration_seconds": 9,
    }
    assert client.post(f"/v1/uploads/{up['uploadId']}/complete", json=meta, headers=bob).status_code == 403
    accepted = client.post(f"/v1/uploads/{up['uploadId']}/complete", json=meta, headers=alice)
    assert accepted.status_code == 202 and accepted.json()["status"] == "UPLOADED"

    video = client.get(f"/v1/videos/{up['uploadId']}").json()  # background job ran after the response
    assert video["status"] == "READY" and video["renditions"] == ["240p", "480p", "720p", "1080p"]
    master_url = video["playbackUrl"]
    master = client.get(master_url)
    assert master.status_code == 200 and "1080p/index.m3u8" in master.text
    base = master_url.rsplit("/", 1)[0]
    media = client.get(f"{base}/720p/index.m3u8").text
    assert client.get(f"{base}/720p/{media.splitlines()[5]}").status_code == 200  # segment via relative URI
    tampered = master_url.replace("/videos/", "/videos/x").replace(video["id"], "other")
    assert client.get(tampered).status_code == 403

    vid = video["id"]
    assert client.post(f"/v1/videos/{vid}/views", headers=bob).json() == {"counted": True}
    assert client.post(f"/v1/videos/{vid}/views", headers=bob).json() == {"counted": False}
    client.put(f"/v1/videos/{vid}/like", headers=bob)
    assert client.put(f"/v1/videos/{vid}/like", headers=bob).json() == {"likes": 1}
    client.post(f"/v1/channels/{channel['id']}/subscribe", headers=bob)
    assert [v["id"] for v in client.get("/v1/feed/subscriptions", headers=bob).json()] == [vid]
    assert client.get("/v1/search", params={"q": "pump seal"}).json()[0]["id"] == vid
    assert client.get("/v1/recommendations").json() == [vid]
    assert client.get(f"/v1/videos/{vid}").json()["views"] == 1


def test_validation(tmp_path):
    client = TestClient(create_app(tmp_path))
    h = {"X-User": "alice"}
    assert (
        client.post("/v1/uploads", json={"filename": "x", "size": 0, "sha256": "0" * 64}, headers=h).status_code == 422
    )
    assert (
        client.post(
            "/v1/uploads", json={"filename": "x", "size": 1, "sha256": "0" * 64}, headers={"X-User": "Bad User"}
        ).status_code
        == 422
    )
    assert client.get("/v1/videos/nope").status_code == 404
