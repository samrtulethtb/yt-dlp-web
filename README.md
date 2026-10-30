# yt-dlp/web

A clean, self-hosted web frontend for [yt-dlp](https://github.com/yt-dlp/yt-dlp).
Download videos and audio from YouTube, TikTok, Instagram, Twitter, Reddit, and 1000+ platforms — on your own computer, with no ads and no limits.

> **Don't want to self-host?** Use the free hosted version at **[noadsdl.com](https://noadsdl.com)** — zero ads, zero signup, same yt-dlp engine.

<img width="744" height="564" alt="frontend-index" src="https://github.com/user-attachments/assets/64eca607-6634-4a9c-ac57-84e418943311" />
<img width="758" height="1035" alt="frontend-download" src="https://github.com/user-attachments/assets/23228abd-6388-4fa4-a5ea-5f27593e1f47" />

---

## Features

- 🎬 **Video** — MP4 up to 4K, all available resolutions
- 🎵 **Audio** — MP3 at 128 / 256 / 320 kbps
- 🔄 **Stays working** — updates yt-dlp by itself once a day, because sites change often
- 🍪 **Cookie support** — age-restricted & member-only content
- 🔒 **Private by default** — only your computer can open it until you choose otherwise
- 🖥 **Cross-platform** — Windows, macOS, Linux
- ⚡ **Minimal** — single file backend, zero database
- 🐳 **Docker** — one command, everything included

---

## Quick Start

### Option A: Docker (easiest)

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows, macOS) or Docker Engine (Linux). Everything else (ffmpeg, Deno, yt-dlp) is inside the image.

```bash
git clone https://github.com/samrtulethtb/yt-dlp-web
cd yt-dlp-web
docker compose up -d
```

Open **http://localhost:8080** in your browser.

### Option B: Python

You need three things installed first:

- **Python 3.11 or newer** — [python.org](https://www.python.org/downloads/)
- **ffmpeg** — merges video with audio and makes MP3 files
- **Deno** — YouTube needs a JavaScript runtime. If you already have **Node.js 20 or newer**, you can skip Deno.

| System | ffmpeg | Deno |
|---|---|---|
| Windows | `winget install Gyan.FFmpeg` | `winget install DenoLand.Deno` |
| macOS | `brew install ffmpeg` | `brew install deno` |
| Linux (Debian/Ubuntu) | `sudo apt install ffmpeg` | `curl -fsSL https://deno.land/install.sh \| sh` |

Close and reopen your terminal after installing, so it finds the new programs.

**Linux / macOS:**

```bash
git clone https://github.com/samrtulethtb/yt-dlp-web
cd yt-dlp-web
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

**Windows:**

```bat
git clone https://github.com/samrtulethtb/yt-dlp-web
cd yt-dlp-web
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Open **http://localhost:8080** in your browser.

---

## Updating

- **yt-dlp updates itself** every 24 hours while the app runs. Turn this off with `YTDLP_AUTO_UPDATE=false`.
- **The app itself:** run `git pull`, then restart it. With Docker: `git pull && docker compose up -d --build`.

---

## Cookie Support (optional)

Needed for: age-restricted videos, member-only content, private playlists.

1. Install the **[Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)** browser extension
2. Log in to YouTube (or any platform)
3. Click the extension → Export cookies
4. Save the file as `cookies.txt` next to `main.py`
5. **Docker only:** uncomment the two `volumes` lines in `docker-compose.yml`
6. Restart the app

> Cookies are your logged-in session. Never share `cookies.txt`, and use a spare account if you can.

> **TikTok note**: Works on home connections. Server/VPS IPs may be blocked by TikTok — add `cookies.txt` to fix.

---

## Using it from other devices

By default only the computer running the app can open it. To use it from your phone or another computer on your home network:

- **Python:** start it with `HOST=0.0.0.0 python3 main.py` (Windows: `set HOST=0.0.0.0` then `python main.py`), then open `http://<your-computer's-IP>:8080`.
- **Docker:** in `docker-compose.yml`, change `"127.0.0.1:8080:8080"` to `"8080:8080"`, then run `docker compose up -d`.

**Putting it on the internet?** The app has no password. Anyone who finds it can download through your server and use up your bandwidth. Put it behind a password, for example with nginx (`sudo apt install nginx apache2-utils`, then `sudo htpasswd -c /etc/nginx/.htpasswd yourname`):

```nginx
server {
    listen 80;
    server_name yourdomain.com;

    auth_basic           "yt-dlp web";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

Add HTTPS with [Certbot](https://certbot.eff.org/) so the password is not sent in plain text.

---

## Configuration

Set these as environment variables before starting the app (Docker: under `environment` in `docker-compose.yml`).

| Variable             | Default       | Description |
|----------------------|---------------|-------------|
| `HOST`               | `127.0.0.1`   | Who can connect. `0.0.0.0` = every device that can reach this computer |
| `PORT`               | `8080`        | HTTP port |
| `MAX_SIZE_MB`        | `500`         | Largest file the app will send, in MB |
| `COOKIES_FILE`       | `cookies.txt` | Path to the cookies file |
| `YTDLP_AUTO_UPDATE`  | `true`        | Update yt-dlp at start and every 24 hours |
| `ALLOW_PRIVATE_URLS` | `false`       | Allow links to local network addresses (`192.168.x.x`, `localhost`…). Leave off on a public server |

**Example:**
```bash
PORT=9000 MAX_SIZE_MB=1000 python3 main.py
```

---

## Troubleshooting

- **YouTube shows only a few qualities, or none.** YouTube needs Deno (or Node.js 20+). Open `http://localhost:8080/api/health`: `js_runtime` should say `deno` or `node`. Install Deno (see Quick Start) and restart the app.
- **"Sign in to confirm you're not a bot".** YouTube is blocking your IP address for a while. Add `cookies.txt` (see above) or try again later.
- **MP3 or high qualities fail with an ffmpeg error.** ffmpeg is missing or your terminal doesn't see it yet. Install it and reopen the terminal.
- **A site stopped working.** Wait for the daily yt-dlp update or restart the app to update now. If it still fails, the site may not be supported by yt-dlp yet.

---

## Stack

- **Backend**: [FastAPI](https://fastapi.tiangolo.com/) + [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- **Frontend**: Vanilla HTML/CSS/JS — no framework, no build step
- **Engine**: yt-dlp supports 1000+ sites out of the box

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

---

## Support
If this saved you time, consider giving it a ⭐ — it helps others find the project.

## License

[MIT](LICENSE) — do whatever you want with it.
