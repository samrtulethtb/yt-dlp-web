FROM python:3.12-slim

# Deno: the JavaScript runtime yt-dlp uses for YouTube
COPY --from=denoland/deno:bin-2.9.8 /deno /usr/local/bin/deno

# ffmpeg: merges video+audio and converts to MP3
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Python packages live in a virtual environment owned by the app user,
# so the app can keep yt-dlp up to date while it runs.
RUN useradd --create-home --uid 1000 app \
    && python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && chown -R app:app /opt/venv

COPY main.py .
COPY frontend ./frontend

USER app
EXPOSE 8080

HEALTHCHECK --interval=5m --timeout=30s --start-period=30s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '8080'), timeout=25)"

CMD ["python", "main.py"]
