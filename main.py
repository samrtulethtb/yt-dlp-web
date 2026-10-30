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
import secrets
import shutil
import signal
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

from fastapi import FastAPI, HTTPException
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
MAX_SIZE_BYTES     = MAX_SIZE_MB * 1_000_000   # MB as the page shows them
COOKIES_FILE       = os.environ.get("COOKIES_FILE", "cookies.txt")
# How many downloads run at the same time; the rest wait their turn.
MAX_PARALLEL       = max(1, int(os.environ.get("MAX_PARALLEL_DOWNLOADS", 2)))
# A download that takes longer than this is stopped.
DOWNLOAD_TIMEOUT_MINUTES = int(os.environ.get("DOWNLOAD_TIMEOUT_MINUTES", 60))
# Keep yt-dlp up to date: sites change often and old versions stop working.
AUTO_UPDATE        = _env_flag("YTDLP_AUTO_UPDATE", True)
UPDATE_EVERY_HOURS = 24
# Links to local or private network addresses are refused unless this is on.
ALLOW_PRIVATE_URLS = _env_flag("ALLOW_PRIVATE_URLS", False)
FRONTEND_DIR       = Path(__file__).parent / "frontend"

INFO_TIMEOUT_SEC    = 120
KEEP_FILES_MINUTES  = 30      # finished files stay this long, then are deleted
MAX_URL_LENGTH      = 2048
MAX_FILENAME_BYTES  = 200
MP3_QUALITIES       = {"0": "MP3 · best quality", "128": "MP3 · 128 kbps, smaller"}
MP3_KBPS_ESTIMATE   = {"0": 245, "128": 128}   # "0" is LAME V0, about 245 kbps
OUTPUT_EXTENSIONS   = {"mp4", "mp3", "m4a"}
PARTIAL_SUFFIXES    = (".part", ".ytdl", ".temp", ".tmp")
TEMP_PREFIX         = "yt-dlp-web-"

log = logging.getLogger("yt-dlp-web")

# ── Models ──────────────────────────────────────────────────────────────
class InfoRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str
    ext: str
    audio_bitrate: Optional[str] = "0"
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

def _run(args: list, cwd: str = None, timeout_sec: int = INFO_TIMEOUT_SEC):
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
        return -1, "", "ERROR: The site took too long to answer"

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

# ── Format list ─────────────────────────────────────────────────────────
H264_CODECS = ("avc1", "avc3", "h264")
CODEC_NAMES = {"vp09": "VP9", "vp9": "VP9", "av01": "AV1", "hev1": "HEVC", "hvc1": "HEVC", "vp8": "VP8"}

def _has_video(f: dict) -> bool:
    return f.get("vcodec") != "none" and f.get("ext") not in ("mhtml",)

def _has_audio(f: dict) -> bool:
    return f.get("acodec") not in ("none",)

def _quality(f: dict) -> Optional[int]:
    """The 'p' number people know: the short side, so a vertical
    1080x1920 phone video counts as 1080p, not 1920p."""
    width, height = f.get("width"), f.get("height")
    if width and height:
        return min(width, height)
    return height

def _is_h264(f: dict) -> bool:
    return (f.get("vcodec") or "").lower().startswith(H264_CODECS)

def _codec_name(f: dict) -> Optional[str]:
    vcodec = (f.get("vcodec") or "").lower()
    if not vcodec or vcodec.startswith(H264_CODECS):
        return None
    return CODEC_NAMES.get(vcodec.split(".", 1)[0])

def _size(f: dict, duration) -> Optional[float]:
    size = f.get("filesize") or f.get("filesize_approx")
    if size:
        return float(size)
    if f.get("tbr") and duration:
        return f["tbr"] * 1000 / 8 * duration
    return None

# Audio added to a video-only format, and the M4A choice: stereo M4A
# first. It plays everywhere inside an MP4, while 5.1 and AC-3 tracks
# make the file bigger and don't play on many phones.
AUDIO_FOR_VIDEO = "bestaudio[ext=m4a][audio_channels<=?2]/bestaudio[ext=m4a]/bestaudio"

def _audio_rank(f: dict):
    channels = f.get("audio_channels")
    return (
        f.get("ext") == "m4a",
        channels is None or channels <= 2,
        (f.get("acodec") or "").startswith("mp4a"),
        "DRC" not in (f.get("format_note") or ""),
        f.get("language_preference") or 0,
        f.get("abr") or f.get("tbr") or 0,
    )

def _best_audio(formats: list) -> Optional[dict]:
    """The track yt-dlp picks for AUDIO_FOR_VIDEO, used for size estimates."""
    audio = [f for f in formats if f.get("vcodec") == "none" and _has_audio(f)]
    return max(audio, key=_audio_rank) if audio else None

def _video_rank(f: dict):
    # Plays everywhere > smoother > direct file (faster than HLS) > bitrate
    direct = (f.get("protocol") or "").startswith("http")
    return (_is_h264(f), f.get("fps") or 0, direct, f.get("tbr") or 0)

def build_formats(data: dict) -> list:
    """One video choice per quality plus the audio choices. Each choice
    names the exact yt-dlp selector that is downloaded, so the size shown
    is the size of the file the user gets."""
    formats = [f for f in data.get("formats") or [] if not f.get("has_drm")]
    duration = data.get("duration")
    limit = MAX_SIZE_BYTES
    audio = _best_audio(formats)
    audio_size = _size(audio, duration) if audio else 0

    best_by_quality = {}
    for f in formats:
        quality = _quality(f)
        if not _has_video(f) or not quality or not f.get("format_id"):
            continue
        current = best_by_quality.get(quality)
        if current is None or _video_rank(f) > _video_rank(current):
            best_by_quality[quality] = f

    choices = []
    for quality in sorted(best_by_quality, reverse=True):
        f = best_by_quality[quality]
        fid = f["format_id"]
        if _has_audio(f) and f.get("acodec"):
            selector, size = fid, _size(f, duration)
        else:
            # Video-only: add the best audio; fall back to video alone if
            # the site has no separate audio.
            selector = "/".join(f"{fid}+{audio}" for audio in AUDIO_FOR_VIDEO.split("/")) + f"/{fid}"
            video_size = _size(f, duration)
            size = video_size + (audio_size or 0) if video_size else None
        fps = f.get("fps") or 0
        label = f"{quality}p" + (f"{round(fps)}" if fps > 30 else "")
        if quality >= 2160:
            label += " 4K"
        choices.append({
            "format_id": selector,
            "label":     label,
            "height":    quality,
            "ext":       "mp4",
            "type":      "video",
            "codec":     _codec_name(f),
            "filesize":  size,
            "too_big":   bool(size and size > limit),
        })

    if not choices and (not formats or any(_has_video(f) for f in formats)):
        # Some sites don't say which resolution they serve.
        choices.append({
            "format_id": "bv*+ba/b", "label": "Best quality", "height": None,
            "ext": "mp4", "type": "video", "codec": None,
            "filesize": None, "too_big": False,
        })

    for quality, label in MP3_QUALITIES.items():
        size = MP3_KBPS_ESTIMATE[quality] * 1000 / 8 * duration if duration else None
        choices.append({
            "format_id":     "bestaudio/best",
            "label":         label,
            "ext":           "mp3",
            "type":          "audio",
            "audio_bitrate": quality,
            "filesize":      size,
            "too_big":       bool(size and size > limit),
        })

    if audio and audio.get("ext") == "m4a":
        choices.append({
            "format_id": AUDIO_FOR_VIDEO,
            "label":     "M4A · original, no conversion",
            "ext":       "m4a",
            "type":      "audio",
            "filesize":  audio_size,
            "too_big":   bool(audio_size and audio_size > limit),
        })

    return choices

# ── File names ──────────────────────────────────────────────────────────
def _filename_char(ch: str) -> str:
    category = unicodedata.category(ch)
    if category == "Cc":       # tabs, line breaks and other control codes
        return " "
    if category[0] == "C" and ch != "‍":  # keep the joiner inside emoji
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

# ── Download jobs ───────────────────────────────────────────────────────
# A download runs in the background. The page asks for its progress
# every second and, when it is done, opens a normal file link, so the
# browser saves the file itself (this works on phones and for big files).
PROGRESS_PREFIX = "PROG "
PROGRESS_TEMPLATE = (
    "download:" + PROGRESS_PREFIX + "%(progress.downloaded_bytes)s %(progress.total_bytes)s "
    "%(progress.total_bytes_estimate)s %(progress.speed)s %(progress.eta)s"
)
POSTPROCESS_STEPS = ("[Merger]", "[ExtractAudio]", "[VideoRemuxer]", "[VideoConvertor]", "[FixupM3u8]")

jobs = {}
jobs_lock = threading.Lock()
download_slots = threading.Semaphore(MAX_PARALLEL)

def _number(value: str) -> Optional[float]:
    try:
        number = float(value)
    except ValueError:
        return None  # yt-dlp prints "NA" when it doesn't know
    return number if number >= 0 else None

def _step_names(req: DownloadRequest) -> list:
    if req.ext in ("mp3", "m4a"):
        return ["Downloading audio"]
    if "+" in req.format_id:
        return ["Downloading video", "Downloading audio"]
    return ["Downloading"]

def _download_args(req: DownloadRequest, url: str, folder: str) -> list:
    # %(title).150B keeps yt-dlp's own file name short enough for every OS.
    output = os.path.join(folder, "%(title).150B.%(ext)s")
    args = _ytdlp_base_args() + [
        "--newline", "--progress-template", PROGRESS_TEMPLATE,
        "--max-filesize", str(MAX_SIZE_BYTES),
        "-o", output,
    ]
    if req.ext == "mp3":
        quality = "0" if req.audio_bitrate == "0" else f"{req.audio_bitrate}K"
        args += ["-f", "bestaudio/best", "--extract-audio",
                 "--audio-format", "mp3", "--audio-quality", quality]
    elif req.ext == "m4a":
        # Keeps the original AAC track as it is; converts only if the
        # site has no AAC audio.
        args += ["-f", AUDIO_FOR_VIDEO, "--extract-audio", "--audio-format", "m4a"]
    else:
        args += ["-f", req.format_id, "--merge-output-format", "mp4"]
    return args + ["--", url]

def _update(job: dict, **changes) -> None:
    with jobs_lock:
        job.update(changes)

def _stop_process(proc: subprocess.Popen) -> None:
    """Stop yt-dlp and the ffmpeg it may have started."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass

def _fail(job: dict, error: str) -> None:
    shutil.rmtree(job["folder"], ignore_errors=True)
    _update(job, status="error", error=error, finished=time.time())

def _run_job(job: dict, req: DownloadRequest, url: str) -> None:
    try:
        _download(job, req, url)
    except Exception:
        log.exception("Download failed")
        _fail(job, "Download failed. See the server log for details.")

def _download(job: dict, req: DownloadRequest, url: str) -> None:
    folder = job["folder"]
    steps = _step_names(req)
    with download_slots:
        _update(job, status="downloading", step=steps[0], started=time.time())
        popen_extra = {"start_new_session": True} if os.name == "posix" else {}
        try:
            proc = subprocess.Popen(
                _download_args(req, url, folder),
                cwd=folder,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **popen_extra,
            )
        except OSError as exc:
            _fail(job, f"Could not start yt-dlp: {exc}")
            return

        timer = threading.Timer(DOWNLOAD_TIMEOUT_MINUTES * 60, _stop_process, [proc])
        timer.daemon = True
        timer.start()
        last_lines, part, too_big = [], -1, False
        try:
            for line in proc.stdout:
                line = line.strip()
                if line.startswith(PROGRESS_PREFIX):
                    values = (line[len(PROGRESS_PREFIX):].split() + ["NA"] * 5)[:5]
                    done, total, estimate, speed, eta = (_number(v) for v in values)
                    total = total or estimate
                    percent = min(100.0, done / total * 100) if done and total else None
                    _update(job, downloaded=done, total=total, percent=percent, speed=speed, eta=eta)
                    continue
                last_lines = (last_lines + [line])[-30:]
                if line.startswith("[download] Destination:"):
                    part += 1
                    _update(job, step=steps[min(part, len(steps) - 1)], percent=0,
                            downloaded=None, total=None, speed=None, eta=None)
                elif "larger than max-filesize" in line:
                    too_big = True
                elif line.startswith(POSTPROCESS_STEPS):
                    _update(job, status="processing", step="Finishing the file",
                            percent=None, downloaded=None, speed=None, eta=None)
            code = proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                _stop_process(proc)

    elapsed = time.time() - job["started"]
    out_file = _pick_output_file(folder)
    error = None
    if too_big:
        error = f"This file is bigger than the {MAX_SIZE_MB} MB limit. Pick a lower quality."
    elif code != 0 and elapsed >= DOWNLOAD_TIMEOUT_MINUTES * 60 - 1:
        error = f"The download took longer than {DOWNLOAD_TIMEOUT_MINUTES} minutes and was stopped."
    elif code != 0:
        error = _clean_error("\n".join(last_lines), "Download failed")
    elif out_file is None:
        error = "yt-dlp finished but no file was made."
    elif out_file.stat().st_size > MAX_SIZE_BYTES:
        size_mb = out_file.stat().st_size / 1_000_000
        error = f"File too large ({size_mb:.0f} MB). Limit is {MAX_SIZE_MB} MB."

    if error:
        _fail(job, error)
        return

    # Prefer the title the page showed; fall back to yt-dlp's file name.
    ext = out_file.suffix.lstrip(".") or req.ext
    title = req.title if req.title and req.title.strip() else out_file.stem
    _update(job, status="done", step="Done", percent=100.0, speed=None, eta=None,
            file=str(out_file), filename=safe_filename(title, ext),
            size=out_file.stat().st_size, finished=time.time())

def _public_job(job: dict) -> dict:
    keys = ("id", "status", "step", "percent", "downloaded", "total", "speed", "eta",
            "error", "filename", "size")
    with jobs_lock:
        return {key: job.get(key) for key in keys}

def _get_job(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, detail="This download has expired. Please start it again.")
    return job

def _forget_job(job_id: str) -> None:
    with jobs_lock:
        job = jobs.pop(job_id, None)
    if job:
        shutil.rmtree(job["folder"], ignore_errors=True)

def _cleanup_loop() -> None:
    while True:
        time.sleep(60)
        cutoff = time.time() - KEEP_FILES_MINUTES * 60
        with jobs_lock:
            old = [jid for jid, job in jobs.items() if (job.get("finished") or time.time()) < cutoff]
        for jid in old:
            _forget_job(jid)

def _remove_leftover_folders() -> None:
    """Delete download folders left behind if the app was stopped mid-download."""
    cutoff = time.time() - 600
    for folder in Path(tempfile.gettempdir()).glob(TEMP_PREFIX + "*"):
        try:
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass

# ── Routes ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app):
    threading.Thread(target=_startup_checks, daemon=True).start()
    threading.Thread(target=_cleanup_loop, name="cleanup", daemon=True).start()
    if AUTO_UPDATE:
        threading.Thread(target=_auto_update_loop, name="yt-dlp-update", daemon=True).start()
    yield

app = FastAPI(title="yt-dlp Web", version="1.2.0", lifespan=lifespan)

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

    return {
        "title":       data.get("title", "Unknown"),
        "uploader":    data.get("uploader", ""),
        "duration":    data.get("duration"),
        "thumbnail":   data.get("thumbnail", ""),
        "max_size_mb": MAX_SIZE_MB,
        "formats":     build_formats(data),
    }

@app.post("/api/download", status_code=202)
async def download(req: DownloadRequest):
    url = await asyncio.to_thread(check_url, req.url)
    if req.ext not in OUTPUT_EXTENSIONS:
        raise HTTPException(400, detail="Unknown output format.")
    if req.ext == "mp3":
        # Older pages sent 256/320; those now get the best-quality MP3.
        req.audio_bitrate = "128" if req.audio_bitrate == "128" else "0"
    format_id = (req.format_id or "").strip()
    if not format_id or len(format_id) > 200 or any(ord(ch) < 32 for ch in format_id):
        raise HTTPException(400, detail="Unknown video format.")
    req.format_id = format_id

    job_id = secrets.token_urlsafe(16)
    job = {
        "id": job_id, "status": "queued", "step": "Waiting for another download to finish",
        "percent": None, "created": time.time(),
        "folder": tempfile.mkdtemp(prefix=TEMP_PREFIX),
    }
    with jobs_lock:
        jobs[job_id] = job
    threading.Thread(target=_run_job, args=(job, req, url), name=f"job-{job_id[:6]}", daemon=True).start()
    return {"job_id": job_id}

@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    return _public_job(_get_job(job_id))

@app.get("/api/jobs/{job_id}/file")
async def job_file(job_id: str):
    job = _get_job(job_id)
    if job.get("status") != "done" or not job.get("file") or not os.path.isfile(job["file"]):
        raise HTTPException(404, detail="This file is not ready or has expired.")
    return FileResponse(
        path=job["file"],
        media_type="application/octet-stream",
        headers={"Content-Disposition": content_disposition(job["filename"])},
    )

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
    _remove_leftover_folders()
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
