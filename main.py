"""
yt-dlp Web: a small self-hosted web UI for yt-dlp.
Runs on Windows, macOS and Linux.
"""

import asyncio
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# ── Config ──────────────────────────────────────────────────────────────
# HOST defaults to 127.0.0.1, so only this computer can use the app.
# Set HOST=0.0.0.0 to open it to other devices (read the README first).
HOST               = os.environ.get("HOST", "127.0.0.1")
PORT               = int(os.environ.get("PORT", 8080))
MAX_SIZE_MB        = int(os.environ.get("MAX_SIZE_MB", 500))
COOKIES_FILE       = os.environ.get("COOKIES_FILE", "cookies.txt")
# Keep yt-dlp up to date: sites change often and old versions stop working.
AUTO_UPDATE        = _env_flag("YTDLP_AUTO_UPDATE", True)
UPDATE_EVERY_HOURS = 24
# Links to local or private network addresses are refused unless this is on.
ALLOW_PRIVATE_URLS = _env_flag("ALLOW_PRIVATE_URLS", False)
FRONTEND_DIR       = Path(__file__).parent / "frontend"

MAX_URL_LENGTH      = 2048
MAX_FILENAME_BYTES  = 200
AUDIO_BITRATES      = {"128", "256", "320"}
OUTPUT_EXTENSIONS   = {"mp4", "mp3"}
PARTIAL_SUFFIXES    = (".part", ".ytdl", ".temp", ".tmp")

log = logging.getLogger("yt-dlp-web")

# ── Models ──────────────────────────────────────────────────────────────
class InfoRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str
    ext: str
    audio_bitrate: Optional[str] = "320"
    title: Optional[str] = None

# ── yt-dlp ──────────────────────────────────────────────────────────────
def _js_runtime_args() -> list:
    """YouTube needs a JavaScript runtime. yt-dlp uses Deno by default;
    if only Node.js is installed, tell yt-dlp to use that instead."""
    if shutil.which("deno"):
        return []
    if shutil.which("node"):
        return ["--js-runtimes", "node"]
    return []

def _ytdlp_base_args() -> list:
    # Run yt-dlp from this Python environment, the same one the
    # auto-updater upgrades.
    args = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings"]
    args += _js_runtime_args()
    if os.path.isfile(COOKIES_FILE):
        args += ["--cookies", COOKIES_FILE]
    return args

def _run(args: list, cwd: str = None, timeout_sec: int = 300):
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            timeout=timeout_sec,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "ERROR: Download timed out"

def _clean_error(stderr: str, fallback: str) -> str:
    """Return the last yt-dlp error line, short, without the 'ERROR:' prefix."""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("ERROR:")]
    message = (errors or lines or [fallback])[-1]
    message = re.sub(r"^ERROR:\s*", "", message)
    return message[:300]

def _ytdlp_version() -> str:
    code, out, _ = _run(_ytdlp_base_args()[:3] + ["--version"], None, 30)
    return out.strip() if code == 0 else "not found"

# ── Link checks ─────────────────────────────────────────────────────────
def _is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast

def _host_is_public(host: str) -> bool:
    try:
        return _is_public_ip(host)
    except ValueError:
        pass  # a name, not an IP address
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        raise HTTPException(400, detail="Could not find that website. Check the link.")
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(_is_public_ip(a) for a in addresses)

def check_url(url: str) -> str:
    """Accept only full http(s) links to public websites.

    The link is passed to yt-dlp after "--", so it can never be read as a
    yt-dlp option. The private address check covers the link itself; it
    does not follow redirects, so a public instance still belongs behind
    a password (see README).
    """
    url = (url or "").strip()
    invalid = HTTPException(400, detail="Paste a full link that starts with http:// or https://.")
    if not url or len(url) > MAX_URL_LENGTH:
        raise invalid
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise invalid
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise invalid
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise invalid
    if not ALLOW_PRIVATE_URLS and not _host_is_public(host):
        raise HTTPException(400, detail="Links to local or private network addresses are not allowed.")
    return url

# ── File names ──────────────────────────────────────────────────────────
def _filename_char(ch: str) -> str:
    category = unicodedata.category(ch)
    if category == "Cc":       # tabs, line breaks and other control codes
        return " "
    if category[0] == "C" and ch != "\u200d":  # keep the joiner inside emoji
        return ""              # e.g. right-to-left override, unassigned
    return ch

def safe_filename(name: str, ext: str) -> str:
    """A file name that works on Windows, macOS and Linux."""
    name = unicodedata.normalize("NFC", name or "")
    name = "".join(_filename_char(ch) for ch in name)
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()
    while len(name.encode("utf-8")) > MAX_FILENAME_BYTES:
        name = name[:-1]
    name = name.rstrip(" .") or "download"
    return f"{name}.{ext}"

def content_disposition(filename: str) -> str:
    """Attachment header with a plain ASCII name for old clients and the
    full UTF-8 name (RFC 5987) for everything else."""
    ascii_name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode()
    ascii_name = re.sub(r'["\\]', "_", ascii_name).strip()
    ext = filename.rpartition(".")[2]
    if not ascii_name or ascii_name.startswith("."):
        ascii_name = f"download.{ext or 'bin'}"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"

def _pick_output_file(folder: str) -> Optional[Path]:
    files = [
        p for p in Path(folder).iterdir()
        if p.is_file() and not p.name.endswith(PARTIAL_SUFFIXES)
    ]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_size)

@asynccontextmanager
async def lifespan(_app):
    threading.Thread(target=_startup_checks, daemon=True).start()
    if AUTO_UPDATE:
        threading.Thread(target=_auto_update_loop, name="yt-dlp-update", daemon=True).start()
    yield

app = FastAPI(title="yt-dlp Web", version="1.1.0", lifespan=lifespan)

# ── Routes ──────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    page = FRONTEND_DIR / "index.html"
    if page.exists():
        return HTMLResponse(content=page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>yt-dlp Web: frontend/index.html is missing</h1>")

@app.post("/api/info")
async def get_info(req: InfoRequest):
    url = await asyncio.to_thread(check_url, req.url)
    args = _ytdlp_base_args() + ["--dump-json", "--skip-download", "--", url]
    code, stdout, stderr = await asyncio.to_thread(_run, args)

    if code != 0:
        raise HTTPException(400, detail=_clean_error(stderr, "Could not fetch video info"))

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        raise HTTPException(400, detail="Invalid response from yt-dlp")

    formats = []
    seen = set()

    for f in data.get("formats", []):
        fid    = f.get("format_id", "")
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        height = f.get("height")
        ext    = f.get("ext", "")

        if vcodec == "none" or not height:
            continue

        label = f"{height}p {ext.upper()}"
        if label in seen:
            continue
        seen.add(label)

        format_id = fid if acodec != "none" else f"{fid}+bestaudio"

        formats.append({
            "format_id": format_id,
            "label":     label,
            "height":    height,
            "ext":       "mp4",
            "type":      "video",
            "filesize":  f.get("filesize") or f.get("filesize_approx"),
        })

    formats.sort(key=lambda x: x.get("height", 0), reverse=True)

    for bitrate, label in [("320", "MP3 320kbps"), ("256", "MP3 256kbps"), ("128", "MP3 128kbps")]:
        formats.append({
            "format_id":     "bestaudio/best",
            "label":         label,
            "ext":           "mp3",
            "type":          "audio",
            "audio_bitrate": bitrate,
            "filesize":      None,
        })

    return {
        "title":     data.get("title", "Unknown"),
        "uploader":  data.get("uploader", ""),
        "duration":  data.get("duration"),
        "thumbnail": data.get("thumbnail", ""),
        "formats":   formats,
    }

@app.post("/api/download")
async def download(req: DownloadRequest, background_tasks: BackgroundTasks):
    url = await asyncio.to_thread(check_url, req.url)
    if req.ext not in OUTPUT_EXTENSIONS:
        raise HTTPException(400, detail="Unknown output format.")
    audio_bitrate = req.audio_bitrate or "320"
    if req.ext == "mp3" and audio_bitrate not in AUDIO_BITRATES:
        raise HTTPException(400, detail="Unknown audio quality.")
    format_id = (req.format_id or "").strip()
    if not format_id or len(format_id) > 200 or any(ord(ch) < 32 for ch in format_id):
        raise HTTPException(400, detail="Unknown video format.")

    tmp_dir = tempfile.mkdtemp(prefix="yt-dlp-web-")
    # %(title).150B keeps yt-dlp's own file name short enough for every OS.
    output = os.path.join(tmp_dir, "%(title).150B.%(ext)s")
    try:
        if req.ext == "mp3":
            args = _ytdlp_base_args() + [
                "-f", "bestaudio/best",
                "--extract-audio",
                "--audio-format", "mp3",
                "--audio-quality", f"{audio_bitrate}k",
                "-o", output,
                "--", url,
            ]
        else:
            args = _ytdlp_base_args() + [
                "-f", format_id,
                "--merge-output-format", "mp4",
                "-o", output,
                "--", url,
            ]

        code, stdout, stderr = await asyncio.to_thread(_run, args, tmp_dir)

        if code != 0:
            raise HTTPException(400, detail=_clean_error(stderr, "Download failed"))

        out_file = _pick_output_file(tmp_dir)
        if out_file is None:
            raise HTTPException(500, detail="No output file generated")

        size_mb = out_file.stat().st_size / (1024 * 1024)
        if size_mb > MAX_SIZE_MB:
            raise HTTPException(
                400,
                detail=f"File too large ({size_mb:.0f} MB). Limit is {MAX_SIZE_MB} MB."
            )

        # Prefer the title the page showed; fall back to yt-dlp's file name.
        ext = out_file.suffix.lstrip(".") or req.ext
        title = req.title if req.title and req.title.strip() else out_file.stem
        final_filename = safe_filename(title, ext)

        background_tasks.add_task(shutil.rmtree, tmp_dir, True)

        return FileResponse(
            path=str(out_file),
            media_type="application/octet-stream",
            headers={"Content-Disposition": content_disposition(final_filename)},
        )

    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        log.exception("Download failed")
        raise HTTPException(500, detail="Download failed. See the server log for details.")

@app.get("/api/health")
async def health():
    version = await asyncio.to_thread(_ytdlp_version)
    runtime = "deno" if shutil.which("deno") else ("node" if shutil.which("node") else None)
    return {
        "status": "ok",
        "ytdlp_version": version,
        "js_runtime": runtime,
    }

# ── yt-dlp auto-update ─────────────────────────────────────────────────
def update_ytdlp() -> None:
    """Upgrade yt-dlp (with its YouTube helper package) in this Python
    environment. New downloads use the new version right away."""
    cmd = [
        sys.executable, "-m", "pip", "install", "--upgrade", "--quiet",
        "--disable-pip-version-check", "yt-dlp[default]",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("yt-dlp update failed: %s", exc)
        return
    if result.returncode != 0:
        reason = _clean_error(result.stderr, "pip error").replace("ERROR: ", "")
        log.warning("yt-dlp update failed: %s", reason)
        if "externally-managed" in result.stderr:
            log.warning("Run this app inside a virtual environment (see README) so it can update yt-dlp.")
        return
    log.info("yt-dlp is up to date (%s)", _ytdlp_version())

def _auto_update_loop() -> None:
    while True:
        update_ytdlp()
        time.sleep(UPDATE_EVERY_HOURS * 3600)

def _startup_checks() -> None:
    log.info("yt-dlp %s", _ytdlp_version())
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg was not found: merging video+audio and MP3 conversion will fail. See README.")
    if not (shutil.which("deno") or shutil.which("node")):
        log.warning("No JavaScript runtime found: YouTube may show few or no formats. Install Deno (see README).")
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "Listening on %s: anyone who can reach port %s can download through this server. "
            "Put it behind a password before exposing it to the internet (see README).",
            HOST, PORT,
        )


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    shown = "localhost" if HOST in ("127.0.0.1", "0.0.0.0", "::", "::1") else HOST
    print(f"\n  yt-dlp Web running at http://{shown}:{PORT}\n")
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
