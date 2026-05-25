# yt-dlp/web

A clean, self-hosted web frontend for [yt-dlp](https://github.com/yt-dlp/yt-dlp).  
Download videos and audio from YouTube, TikTok, Instagram, Twitter, Reddit, and 1000+ platforms — locally, with no ads and no limits.

> **Don't want to self-host?** Use the free hosted version at **[noadsdl.com](https://noadsdl.com)** — zero ads, zero signup, same yt-dlp engine.

---

## Features

- 🎬 **Video** — MP4 up to 4K, all available resolutions
- 🎵 **Audio** — MP3 at 128 / 256 / 320 kbps
- 🍪 **Cookie support** — age-restricted & member-only content
- 🖥 **Cross-platform** — Windows, macOS, Linux
- ⚡ **Minimal** — single file backend, zero database
- 🐳 **Docker** — one command deploy

---

## Requirements

- **Python 3.9+**
- **ffmpeg** — required for merging video+audio and MP3 conversion

### Install ffmpeg

**Windows:**
```
winget install ffmpeg
```
Or download from https://ffmpeg.org/download.html → extract → add `bin/` to PATH.

**macOS:**
```bash
brew install ffmpeg
```

**Linux (Debian/Ubuntu):**
```bash
sudo apt install ffmpeg
```

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/samrtulethtb/yt-dlp-web
cd ytdlp-web

# 2. Create virtual environment
python3 -m venv venv

# 3. Activate it
source venv/bin/activate        # Linux / macOS
# venv\Scripts\activate         # Windows

# 4. Install dependencies
pip install -r requirements.txt

# 5. Run
python3 main.py
```

Open **http://localhost:8080** in your browser.

---

## Windows Step-by-Step

```bat
git clone https://github.com/samrtulethtb/yt-dlp-web
cd ytdlp-web
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Then open http://localhost:8080

---

## Cookie Support (optional)

Needed for: age-restricted videos, member-only content, private playlists.

1. Install **[Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)** browser extension
2. Log in to YouTube (or any platform)
3. Click the extension → Export cookies
4. Save the file as `cookies.txt` next to `main.py`
5. Restart the server

> **TikTok note**: Works on home connections. Server/VPS IPs may be blocked by TikTok — add `cookies.txt` to fix.

---

## Configuration

Set via environment variables before running:

| Variable       | Default       | Description              |
|---------------|--------------|--------------------------|
| `PORT`         | `8080`        | HTTP port                |
| `MAX_SIZE_MB`  | `500`         | Max file size in MB      |
| `COOKIES_FILE` | `cookies.txt` | Path to cookies file     |

**Example:**
```bash
PORT=9000 MAX_SIZE_MB=1000 python3 main.py
```

---

## Docker

```bash
# One command
docker compose up

# Or manually
docker build -t ytdlp-web .
docker run -p 8080:8080 ytdlp-web

# With cookies
docker run -p 8080:8080 -v ./cookies.txt:/app/cookies.txt:ro ytdlp-web
```

---

## Deploy on a Linux Server

```bash
# Clone & install
git clone https://github.com/samrtulethtb/yt-dlp-web
cd ytdlp-web
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Run with uvicorn directly
uvicorn main:app --host 0.0.0.0 --port 8080

# Or keep alive with screen
screen -S ytdlp python3 main.py
```

**Nginx reverse proxy:**
```nginx
server {
    listen 80;
    server_name yourdomain.com;

    location / {
        proxy_pass http://localhost:8080;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## Stack

- **Backend**: [FastAPI](https://fastapi.tiangolo.com/) + [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- **Frontend**: Vanilla HTML/CSS/JS — no framework, no build step
- **Engine**: yt-dlp supports 1000+ sites out of the box

---

## License

MIT — do whatever you want with it.
