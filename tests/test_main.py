"""Tests for the safety checks and file naming. No network needed."""

import socket
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Resolve names without the network: 'internal.test' is private."""
    def getaddrinfo(host, *args, **kwargs):
        table = {
            "example.com": "93.184.215.14",
            "www.youtube.com": "142.250.180.14",
            "localhost": "127.0.0.1",
            "internal.test": "192.168.1.10",
        }
        if host not in table:
            raise socket.gaierror("unknown host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], 0))]
    monkeypatch.setattr(main.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(main, "ALLOW_PRIVATE_URLS", False)


BROWSER = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"


@pytest.fixture(autouse=True)
def default_access(monkeypatch):
    monkeypatch.setattr(main, "ACCESS_PASSWORD", "")
    monkeypatch.setattr(main, "PUBLIC_MODE", False)
    for store in (main.lookups, main.daily_downloads, main.failed_logins):
        store.clear()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "AUTO_UPDATE", False)
    monkeypatch.setattr(main, "_startup_checks", lambda: None)
    headers = {"X-Requested-With": "yt-dlp-web", "User-Agent": BROWSER}
    with TestClient(main.app, headers=headers) as c:
        yield c
    for job_id in list(main.jobs):
        main._forget_job(job_id)


@pytest.mark.parametrize("url", [
    "--version",
    "-a /etc/passwd",
    "ftp://example.com/video",
    "file:///etc/passwd",
    "example.com/video",
    "ytsearch:cats",
    "https://example.com/a b",
    "https://example.com/\nx",
    "https://" + "a" * 3000,
    "",
])
def test_rejects_anything_but_full_web_links(url):
    with pytest.raises(HTTPException) as err:
        main.check_url(url)
    assert err.value.status_code == 400


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/api/health",
    "http://localhost/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://192.168.1.1/",
    "http://10.0.0.5/",
    "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/",
    "http://internal.test/video",
])
def test_rejects_local_and_private_addresses(url):
    with pytest.raises(HTTPException) as err:
        main.check_url(url)
    assert "private" in err.value.detail


def test_private_addresses_allowed_when_switched_on(monkeypatch):
    monkeypatch.setattr(main, "ALLOW_PRIVATE_URLS", True)
    assert main.check_url("http://192.168.1.1/video.mp4") == "http://192.168.1.1/video.mp4"


def test_unknown_host_gets_a_clear_message():
    with pytest.raises(HTTPException) as err:
        main.check_url("https://no-such-site.test/video")
    assert "Could not find" in err.value.detail


def test_accepts_public_links():
    url = "  https://www.youtube.com/watch?v=abc&t=1  "
    assert main.check_url(url) == url.strip()
    assert main.check_url("https://93.184.215.14/v.mp4")


def test_link_is_always_passed_after_double_dash(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_run", lambda args, *a, **k: calls.append(args) or (1, "", "ERROR: nope"))
    res = client.post("/api/info", json={"url": "https://example.com/watch"})
    assert res.status_code == 400
    args = calls[0]
    assert args[-2:] == ["--", "https://example.com/watch"]


def test_option_like_url_never_reaches_ytdlp(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_run", lambda args, *a, **k: calls.append(args) or (0, "{}", ""))
    res = client.post("/api/info", json={"url": "--version"})
    assert res.status_code == 400
    assert calls == []


@pytest.mark.parametrize("payload", [
    {"ext": "exe"},
    {"format_id": ""},
    {"format_id": "x" * 500},
    {"url": "--version"},
])
def test_download_rejects_bad_options(client, monkeypatch, payload):
    monkeypatch.setattr(main, "_run_job", lambda *a, **k: pytest.fail("no job may start"))
    body = {"url": "https://example.com/v", "format_id": "18", "ext": "mp4"}
    body.update(payload)
    assert client.post("/api/download", json=body).status_code == 400


def test_errors_are_short_and_without_prefix():
    stderr = "WARNING: something\nERROR: [youtube] abc: Video unavailable\n"
    assert main._clean_error(stderr, "x") == "[youtube] abc: Video unavailable"
    assert main._clean_error("", "Fallback") == "Fallback"
    assert len(main._clean_error("ERROR: " + "a" * 1000, "x")) == 300


def test_no_cors_headers(client):
    res = client.get("/", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in res.headers


@pytest.mark.parametrize("title, expected", [
    ('Café: "déjà vu" / test?', "Café_ _déjà vu_ _ test_.mp4"),
    ("Видео 🎬", "Видео 🎬.mp4"),
    ("   ...   ", "download.mp4"),
    ("line\nbreak\ttab", "line break tab.mp4"),
    ("CON.", "CON.mp4"),
    ("family 👨\u200d👩\u200d👧", "family 👨\u200d👩\u200d👧.mp4"),
    ("evil\u202egpj.exe", "evilgpj.exe.mp4"),
])
def test_safe_filename(title, expected):
    assert main.safe_filename(title, "mp4") == expected


def test_long_titles_are_cut_to_a_safe_length():
    name = main.safe_filename("é" * 500, "mp4")
    assert len(name.encode("utf-8")) <= main.MAX_FILENAME_BYTES + len(".mp4")


def test_content_disposition_keeps_the_utf8_name():
    header = main.content_disposition("Café déjà vu 🎬.mp4")
    assert 'filename="Cafe deja vu .mp4"' in header
    utf8 = header.split("filename*=UTF-8''", 1)[1]
    assert unquote(utf8) == "Café déjà vu 🎬.mp4"


def test_content_disposition_without_any_ascii_letters():
    header = main.content_disposition("視頻.mp4")
    assert 'filename="download.mp4"' in header


def test_partial_files_are_never_picked(tmp_path):
    (tmp_path / "video.mp4.part").write_bytes(b"x" * 100)
    (tmp_path / "video.mp4").write_bytes(b"x" * 10)
    assert main._pick_output_file(str(tmp_path)).name == "video.mp4"


# ── Format list ─────────────────────────────────────────────────────────
def fmt(fid, **kw):
    base = {"format_id": fid, "vcodec": "avc1.64001F", "acodec": "none", "ext": "mp4",
            "protocol": "https"}
    base.update(kw)
    return base


def test_one_choice_per_quality_preferring_h264():
    data = {"duration": 100, "formats": [
        fmt("vp9-1080", vcodec="vp09.00.40.08", width=1920, height=1080, tbr=3000, ext="webm"),
        fmt("avc-1080", width=1920, height=1080, tbr=2500, filesize=30_000_000),
        fmt("av1-2160", vcodec="av01.0.12M.08", width=3840, height=2160, tbr=9000),
        fmt("aac", vcodec="none", acodec="mp4a.40.2", ext="m4a", abr=128, filesize=1_600_000),
        fmt("opus", vcodec="none", acodec="opus", ext="webm", abr=160),
    ]}
    video = [c for c in main.build_formats(data) if c["type"] == "video"]
    assert [c["label"] for c in video] == ["2160p 4K", "1080p"]
    assert video[1]["format_id"].startswith("avc-1080+bestaudio[ext=m4a][audio_channels<=?2]/")
    assert video[1]["format_id"].endswith("/avc-1080")
    assert video[1]["codec"] is None and video[0]["codec"] == "AV1"
    assert video[1]["filesize"] == 31_600_000


def test_vertical_videos_use_the_short_side():
    data = {"formats": [fmt("v", width=1080, height=1920, acodec="aac")]}
    video = [c for c in main.build_formats(data) if c["type"] == "video"]
    assert video[0]["label"] == "1080p" and video[0]["format_id"] == "v"


def test_direct_file_beats_hls_at_the_same_quality():
    data = {"formats": [
        fmt("hls-720", width=1280, height=720, protocol="m3u8_native", tbr=2000, acodec="aac"),
        fmt("http-720", width=1280, height=720, protocol="https", tbr=1900, acodec="aac"),
    ]}
    assert main.build_formats(data)[0]["format_id"] == "http-720"


def test_sixty_fps_is_shown():
    data = {"formats": [fmt("a", width=1920, height=1080, fps=60, acodec="aac")]}
    assert main.build_formats(data)[0]["label"] == "1080p60"


def test_too_big_choices_are_marked(monkeypatch):
    monkeypatch.setattr(main, "MAX_SIZE_BYTES", 10_000_000)
    data = {"duration": 60, "formats": [
        fmt("big", width=1920, height=1080, filesize=10_000_001, acodec="aac"),
        fmt("small", width=640, height=360, filesize=10_000_000, acodec="aac"),
    ]}
    flags = {c["label"]: c["too_big"] for c in main.build_formats(data)}
    assert flags["1080p"] is True and flags["360p"] is False


def test_site_without_resolutions_gets_best_quality():
    data = {"formats": [{"format_id": "0", "ext": "mp4", "url": "https://x/v.mp4"}]}
    video = [c for c in main.build_formats(data) if c["type"] == "video"]
    assert video == [dict(video[0], label="Best quality", format_id="bv*+ba/b")]


def test_audio_choices_are_honest():
    data = {"duration": 200, "formats": [
        fmt("aac", vcodec="none", acodec="mp4a.40.2", ext="m4a", abr=128, filesize=3_200_000),
    ]}
    audio = [c for c in main.build_formats(data) if c["type"] == "audio"]
    assert [c["label"] for c in audio] == [
        "MP3 · best quality", "MP3 · 128 kbps, smaller", "M4A · original, no conversion"]
    assert audio[1]["filesize"] == 128 * 1000 / 8 * 200
    assert audio[2]["filesize"] == 3_200_000


def test_no_m4a_choice_without_an_m4a_track():
    data = {"formats": [fmt("opus", vcodec="none", acodec="opus", ext="webm", abr=160)]}
    labels = [c["label"] for c in main.build_formats(data)]
    assert not any(label.startswith("M4A") for label in labels)


def test_old_mp3_bitrates_become_best_quality(client, monkeypatch):
    seen = []
    monkeypatch.setattr(main, "_run_job", lambda job, req, url: seen.append(req.audio_bitrate))
    body = {"url": "https://example.com/v", "format_id": "bestaudio/best", "ext": "mp3"}
    for bitrate in ("320", "256", "128"):
        assert client.post("/api/download", json=dict(body, audio_bitrate=bitrate)).status_code == 202
    assert seen == ["0", "0", "128"]


# ── Download jobs (with a fake yt-dlp) ─────────────────────────────────
FAKE_YTDLP = r"""
import os, sys, time
mode = sys.argv[1]
print("[youtube] abc: Downloading webpage", flush=True)
if mode == "fail":
    print("ERROR: [youtube] abc: Video unavailable", flush=True)
    sys.exit(1)
if mode == "slow":
    time.sleep(30)
if mode == "too-big":
    print("[download] File is larger than max-filesize (9 bytes > 1 bytes). Aborting.", flush=True)
    sys.exit(0)
print("[download] Destination: Clip.f1.mp4", flush=True)
for done in (100, 500, 1000):
    print(f"PROG {done} 1000 NA 2048.5 3", flush=True)
    time.sleep(0.05)
print("[download] Destination: Clip.f2.m4a", flush=True)
print("PROG 10 NA 40 NA NA", flush=True)
print('[Merger] Merging formats into "Clip.mp4"', flush=True)
open("Clip.mp4", "wb").write(b"x" * 1234)
open("Clip.f1.mp4.part", "wb").write(b"x")
"""


@pytest.fixture
def fake_ytdlp(monkeypatch, tmp_path):
    script = tmp_path / "fake_ytdlp.py"
    script.write_text(FAKE_YTDLP)
    mode = {"value": "ok"}
    monkeypatch.setattr(main, "_download_args",
                        lambda req, url, folder: [main.sys.executable, str(script), mode["value"]])
    return mode


def wait_for(client, job_id, timeout=10):
    import time
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.05)
    pytest.fail("job did not finish")


def start(client, **extra):
    body = {"url": "https://example.com/v", "format_id": "1+bestaudio", "ext": "mp4",
            "title": "My clip"}
    body.update(extra)
    res = client.post("/api/download", json=body)
    assert res.status_code == 202
    return res.json()["job_id"]


def test_job_reports_progress_and_serves_the_file(client, fake_ytdlp):
    job_id = start(client)
    job = wait_for(client, job_id)
    assert job["status"] == "done" and job["percent"] == 100
    assert job["filename"] == "My clip.mp4" and job["size"] == 1234
    assert "folder" not in job and "file" not in job
    res = client.get(f"/api/jobs/{job_id}/file")
    assert res.status_code == 200 and len(res.content) == 1234
    assert "My%20clip.mp4" in res.headers["content-disposition"]


def test_job_error_message_comes_from_ytdlp(client, fake_ytdlp):
    fake_ytdlp["value"] = "fail"
    job = wait_for(client, start(client))
    assert job["status"] == "error"
    assert job["error"] == "[youtube] abc: Video unavailable"


def test_job_too_big_is_explained(client, fake_ytdlp):
    fake_ytdlp["value"] = "too-big"
    job = wait_for(client, start(client))
    assert job["status"] == "error" and "MB limit" in job["error"]


def test_unknown_or_unfinished_jobs_have_no_file(client, fake_ytdlp):
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.get("/api/jobs/nope/file").status_code == 404


def test_expired_jobs_are_deleted_with_their_files(client, fake_ytdlp):
    job_id = start(client)
    wait_for(client, job_id)
    folder = main.jobs[job_id]["folder"]
    main._forget_job(job_id)
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert not Path(folder).exists()


def test_progress_line_parsing():
    assert main._number("NA") is None
    assert main._number("12.5") == 12.5


def test_download_args_put_the_link_last_after_double_dash():
    req = main.DownloadRequest(url="https://example.com/v", format_id="18", ext="mp4")
    args = main._download_args(req, "https://example.com/v", "/tmp/x")
    assert args[-2:] == ["--", "https://example.com/v"]
    assert args[args.index("--max-filesize") + 1] == str(main.MAX_SIZE_MB * 1_000_000)


def test_stereo_aac_is_preferred_over_surround_tracks():
    data = {"duration": 100, "formats": [
        fmt("v", width=426, height=240, filesize=1_000_000),
        fmt("258", vcodec="none", acodec="mp4a.40.2", ext="m4a", audio_channels=6, abr=388, filesize=5_000_000),
        fmt("380", vcodec="none", acodec="ac-3", ext="m4a", audio_channels=6, abr=384, filesize=5_000_000),
        fmt("140-drc", vcodec="none", acodec="mp4a.40.2", ext="m4a", audio_channels=2, abr=129,
            filesize=1_600_000, format_note="medium, DRC"),
        fmt("140", vcodec="none", acodec="mp4a.40.2", ext="m4a", audio_channels=2, abr=129, filesize=1_600_000),
    ]}
    assert main._best_audio(data["formats"])["format_id"] == "140"
    assert main.build_formats(data)[0]["filesize"] == 2_600_000


def test_slow_downloads_are_stopped(client, fake_ytdlp, monkeypatch):
    import time
    monkeypatch.setattr(main, "DOWNLOAD_TIMEOUT_MINUTES", 0.01)  # 0.6 seconds
    fake_ytdlp["value"] = "slow"
    started = time.time()
    job = wait_for(client, start(client), timeout=10)
    assert job["status"] == "error" and "longer than" in job["error"]
    assert time.time() - started < 10



# ── Access and public mode ─────────────────────────────────────────────
import base64
from types import SimpleNamespace


def basic(password, user="me"):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_api_needs_the_page_header(client):
    res = client.post("/api/info", json={"url": "https://example.com/v"},
                      headers={"X-Requested-With": ""})
    assert res.status_code == 403


def test_page_and_health_need_no_header(client, monkeypatch):
    monkeypatch.setattr(main, "_ytdlp_version", lambda: "test")
    assert client.get("/", headers={"X-Requested-With": ""}).status_code == 200
    assert client.get("/api/health", headers={"X-Requested-With": ""}).status_code == 200


def test_password_protects_everything_but_health(client, monkeypatch):
    monkeypatch.setattr(main, "ACCESS_PASSWORD", "s3cret é")
    monkeypatch.setattr(main, "_ytdlp_version", lambda: "test")
    res = client.get("/")
    assert res.status_code == 401 and res.headers["www-authenticate"].startswith("Basic")
    assert client.get("/", headers={"Authorization": basic("wrong")}).status_code == 401
    assert client.get("/", headers={"Authorization": basic("s3cret é", user="anyone")}).status_code == 200
    assert client.get("/api/jobs/x/file").status_code == 401
    assert client.get("/api/health").status_code == 200


def test_too_many_wrong_passwords_are_slowed_down(client, monkeypatch):
    monkeypatch.setattr(main, "ACCESS_PASSWORD", "right")
    for _ in range(main.FAILED_LOGINS_ALLOWED):
        assert client.get("/", headers={"Authorization": basic("wrong")}).status_code == 401
    assert client.get("/", headers={"Authorization": basic("right")}).status_code == 429


@pytest.mark.parametrize("agent", ["curl/8.5.0", "python-requests/2.32", "Wget/1.21", "", "Go-http-client/2.0",
                                   "Mozilla/5.0 HeadlessChrome/130.0"])
def test_public_mode_refuses_scripts(client, monkeypatch, agent):
    monkeypatch.setattr(main, "PUBLIC_MODE", True)
    res = client.post("/api/info", json={"url": "https://example.com/v"}, headers={"User-Agent": agent})
    assert res.status_code == 403 and "browser" in res.json()["detail"]


def test_scripts_are_fine_without_public_mode(client, monkeypatch):
    monkeypatch.setattr(main, "_run", lambda *a, **k: (1, "", "ERROR: nope"))
    res = client.post("/api/info", json={"url": "https://example.com/v"}, headers={"User-Agent": "curl/8"})
    assert res.status_code == 400


def test_public_mode_limits_lookups(client, monkeypatch):
    monkeypatch.setattr(main, "PUBLIC_MODE", True)
    monkeypatch.setattr(main, "_run", lambda *a, **k: (1, "", "ERROR: nope"))
    for _ in range(main.PUBLIC_LOOKUPS):
        assert client.post("/api/info", json={"url": "https://example.com/v"}).status_code == 400
    assert client.post("/api/info", json={"url": "https://example.com/v"}).status_code == 429


def test_public_mode_daily_limit_and_refund(client, fake_ytdlp, monkeypatch):
    monkeypatch.setattr(main, "PUBLIC_MODE", True)
    monkeypatch.setattr(main, "DOWNLOADS_PER_DAY", 2)
    fake_ytdlp["value"] = "fail"
    wait_for(client, start(client))            # failed: not counted
    fake_ytdlp["value"] = "ok"
    wait_for(client, start(client))
    wait_for(client, start(client))
    body = {"url": "https://example.com/v", "format_id": "18", "ext": "mp4"}
    res = client.post("/api/download", json=body)
    assert res.status_code == 429 and "today's limit of 2" in res.json()["detail"]
    assert main.downloads_left("testclient") == 0


def test_public_mode_limits_downloads_at_once(client, fake_ytdlp, monkeypatch):
    monkeypatch.setattr(main, "PUBLIC_MODE", True)
    monkeypatch.setattr(main, "DOWNLOAD_TIMEOUT_MINUTES", 0.05)  # stops the slow fakes after 3 s
    fake_ytdlp["value"] = "slow"
    job_ids = [start(client) for _ in range(main.PUBLIC_ACTIVE_PER_VISITOR)]
    body = {"url": "https://example.com/v", "format_id": "18", "ext": "mp4"}
    res = client.post("/api/download", json=body)
    assert res.status_code == 429 and "already have" in res.json()["detail"]
    for job_id in job_ids:
        assert wait_for(client, job_id)["status"] == "error"


def test_low_disk_space_refuses_downloads(client, monkeypatch):
    monkeypatch.setattr(main.shutil, "disk_usage", lambda path: SimpleNamespace(free=10))
    body = {"url": "https://example.com/v", "format_id": "18", "ext": "mp4"}
    res = client.post("/api/download", json=body)
    assert res.status_code == 503 and "disk space" in res.json()["detail"]


def test_info_tells_how_many_downloads_are_left(client, monkeypatch):
    monkeypatch.setattr(main, "_run", lambda *a, **k: (0, '{"title": "t", "formats": []}', ""))
    assert client.post("/api/info", json={"url": "https://example.com/v"}).json()["downloads_left"] is None
    monkeypatch.setattr(main, "PUBLIC_MODE", True)
    monkeypatch.setattr(main, "DOWNLOADS_PER_DAY", 5)
    assert client.post("/api/info", json={"url": "https://example.com/v"}).json()["downloads_left"] == 5


@pytest.mark.parametrize("host, key", [
    ("203.0.113.7", "203.0.113.7"),
    ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
    ("2001:db8:1:2:bbbb::9", "2001:db8:1:2::/64"),
    ("::ffff:203.0.113.7", "203.0.113.7"),
])
def test_visitor_key(host, key):
    request = SimpleNamespace(client=SimpleNamespace(host=host))
    assert main.visitor_key(request) == key
