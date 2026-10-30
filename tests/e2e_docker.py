"""End-to-end check against a running app (used by CI on the Docker image).

Usage: python3 tests/e2e_docker.py http://127.0.0.1:8080 <video link>
Standard library only, so it runs anywhere.
"""

import json
import sys
import time
import urllib.request

BASE, LINK = sys.argv[1].rstrip("/"), sys.argv[2]
HEADERS = {"X-Requested-With": "yt-dlp-web", "Content-Type": "application/json"}


def call(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=180) as res:
        return json.load(res)


def download(choice, title):
    started = call("/api/download", {
        "url": LINK, "format_id": choice["format_id"], "ext": choice["ext"],
        "audio_bitrate": choice.get("audio_bitrate", "0"), "title": title,
    })
    job_id = started["job_id"]
    for _ in range(300):
        job = call(f"/api/jobs/{job_id}")
        if job["status"] == "error":
            sys.exit(f"{choice['label']}: {job['error']}")
        if job["status"] == "done":
            break
        time.sleep(1)
    else:
        sys.exit(f"{choice['label']}: still not done")
    with urllib.request.urlopen(f"{BASE}/api/jobs/{job_id}/file", timeout=180) as res:
        content = res.read()
        disposition = res.headers["Content-Disposition"]
    print(f"{choice['label']}: {len(content)} bytes, {disposition}")
    assert len(content) == job["size"] > 1000, "file is empty or cut short"
    return content


info = call("/api/info", {"url": LINK})
print("title:", info["title"], "| choices:", [c["label"] for c in info["formats"]])
video = next(c for c in info["formats"] if c["type"] == "video")
mp3 = next(c for c in info["formats"] if c["ext"] == "mp3")

mp4_bytes = download(video, "Test video é")
assert mp4_bytes[4:8] == b"ftyp", "not an MP4 file"

mp3_bytes = download(mp3, "Test audio")
assert mp3_bytes[:3] == b"ID3" or mp3_bytes[0] == 0xFF, "not an MP3 file"
print("OK")
