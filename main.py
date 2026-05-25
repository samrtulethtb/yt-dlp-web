"""
yt-dlp Web — Minimal self-hosted video downloader
Cross-platform: Windows, Linux, macOS
"""

import os
import re
import asyncio
import tempfile
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Config ──────────────────────────────────────────────────────────────
PORT          = int(os.environ.get("PORT", 8080))
MAX_SIZE_MB   = int(os.environ.get("MAX_SIZE_MB", 500))
COOKIES_FILE  = os.environ.get("COOKIES_FILE", "cookies.txt")
FRONTEND_DIR  = Path(__file__).parent / "frontend"

app = FastAPI(title="yt-dlp Web", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # ← browserul vede header-ul
)

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

# ── Models ──────────────────────────────────────────────────────────────
class InfoRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str
    ext: str
    audio_bitrate: Optional[str] = "320"
    title: Optional[str] = None

# ── Helpers ─────────────────────────────────────────────────────────────
def _ytdlp_base_args() -> list:
    args = ["yt-dlp", "--no-playlist", "--no-warnings"]
    if os.path.isfile(COOKIES_FILE):
        args += ["--cookies", COOKIES_FILE]
    return args

def _run(args: list, cwd: str = None, timeout_sec: int = 300):
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout_sec,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Download timed out"

# ── Routes ──────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    for p in [FRONTEND_DIR / "index.html", Path(__file__).parent / "index.html"]:
        if p.exists():
            return HTMLResponse(content=p.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>yt-dlp Web — place index.html in frontend/</h1>")

@app.post("/api/info")
async def get_info(req: InfoRequest):
    args = _ytdlp_base_args() + ["--dump-json", "--skip-download", req.url]
    code, stdout, stderr = await asyncio.to_thread(_run, args)

    if code != 0:
        raise HTTPException(400, detail=stderr.strip() or "Could not fetch video info")

    import json
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
    tmp_dir = tempfile.mkdtemp()
    try:
        if req.ext == "mp3":
            args = _ytdlp_base_args() + [
                "-f", "bestaudio/best",
                "--extract-audio",
                "--audio-format", "mp3",
                "--audio-quality", f"{req.audio_bitrate}k",
                "-o", os.path.join(tmp_dir, "%(title)s.%(ext)s"),
                req.url,
            ]
        else:
            args = _ytdlp_base_args() + [
                "-f", req.format_id,
                "--merge-output-format", "mp4",
                "-o", os.path.join(tmp_dir, "%(title)s.%(ext)s"),
                req.url,
            ]

        code, stdout, stderr = await asyncio.to_thread(_run, args, tmp_dir)

        if code != 0:
            raise HTTPException(400, detail=stderr.strip() or "Download failed")

        files = list(Path(tmp_dir).iterdir())
        if not files:
            raise HTTPException(500, detail="No output file generated")

        out_file = files[0]

        size_mb = out_file.stat().st_size / (1024 * 1024)
        if size_mb > MAX_SIZE_MB:
            raise HTTPException(
                400,
                detail=f"File too large ({size_mb:.0f} MB). Limit is {MAX_SIZE_MB} MB."
            )

        # Construim filename din titlul primit sau fallback la yt-dlp
        if req.title and req.title.strip():
            safe_title = re.sub(r'[\\/:*?"<>|]', '_', req.title).strip()
            ext = out_file.suffix.lstrip(".") or req.ext
            final_filename = f"{safe_title}.{ext}"
        else:
            final_filename = re.sub(r'[\\/:*?"<>|]', '_', out_file.name).strip()
            if not final_filename:
                final_filename = f"download.{req.ext}"

        background_tasks.add_task(shutil.rmtree, tmp_dir, True)

        # Content-Disposition cu UTF-8 encoding corect
        encoded = quote(final_filename)
        cd_header = f"attachment; filename=\"{final_filename.encode('ascii', 'replace').decode()}\"; filename*=UTF-8''{encoded}"

        return FileResponse(
            path=str(out_file),
            media_type="application/octet-stream",
            headers={"Content-Disposition": cd_header},
        )

    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(500, detail=str(e))

@app.get("/api/health")
async def health():
    code, out, _ = await asyncio.to_thread(_run, ["yt-dlp", "--version"], None, 5)
    return {
        "status": "ok",
        "ytdlp_version": out.strip() if code == 0 else "not found",
    }

if __name__ == "__main__":
    import uvicorn
    print(f"\n  yt-dlp Web running at http://localhost:{PORT}\n")
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
