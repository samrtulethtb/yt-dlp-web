"""Tests for the safety checks and file naming. No network needed."""

import socket
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


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "AUTO_UPDATE", False)
    monkeypatch.setattr(main, "_startup_checks", lambda: None)
    with TestClient(main.app) as c:
        yield c


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
    {"ext": "mp3", "audio_bitrate": "999"},
    {"format_id": ""},
    {"format_id": "x" * 500},
])
def test_download_rejects_bad_options(client, monkeypatch, payload):
    monkeypatch.setattr(main, "_run", lambda *a, **k: pytest.fail("yt-dlp must not run"))
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
