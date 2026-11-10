FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    libchromaprint-tools \
    mp3gain \
    python3-dev \
    build-essential \
    iproute2 \
    procps \
    ca-certificates \
    gnupg \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------
# Deno: the JavaScript runtime yt-dlp uses to solve YouTube's signature
# challenges. Without it yt-dlp can only use clients whose stream URLs YouTube
# often refuses. Together with the bgutil PO-token provider (the
# `bgutil-provider` service in docker-compose.example.yml) it keeps normal
# downloads working.
# ------------------------------------------------------------
RUN curl -fsSL https://deno.land/install.sh | sh -s -- \
    && ln -sf /root/.deno/bin/deno /usr/local/bin/deno

# ------------------------------------------------------------
# Mullvad VPN CLI + daemon: downloads go through Mullvad. Adds Mullvad's
# official apt repo and installs mullvad-vpn (`mullvad` CLI and
# `mullvad-daemon`). Ref: https://mullvad.net/en/download/vpn/linux
# ------------------------------------------------------------
RUN set -eux; \
    if curl -fsSLo /usr/share/keyrings/mullvad-keyring.asc https://repository.mullvad.net/deb/mullvad-keyring.asc; then \
        echo "deb [signed-by=/usr/share/keyrings/mullvad-keyring.asc arch=$(dpkg --print-architecture)] https://repository.mullvad.net/deb/stable $(. /etc/os-release && echo $VERSION_CODENAME) main" > /etc/apt/sources.list.d/mullvad.list; \
        if apt-get update && apt-get install -y --no-install-recommends mullvad-vpn; then \
            echo "Installed Mullvad VPN CLI/daemon"; \
        else \
            echo "Mullvad package install failed; continuing without VPN support"; \
        fi; \
    else \
        echo "Mullvad repository unavailable; skipping Mullvad installation"; \
    fi; \
    rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ------------------------------------------------------------
# These three track YouTube/Spotify site changes closely, so the version
# floors in requirements.txt go stale quickly. Always take the latest.
# ------------------------------------------------------------
# yt-dlp-ejs ships the JavaScript challenge-solver scripts yt-dlp otherwise
# refuses to fetch at runtime; without them most YouTube formats are dropped.
RUN pip install --no-cache-dir -U yt-dlp yt-dlp-ejs spotifyscraper ytmusicapi

# Application code: every module in src/ sits flat in /app (modules import
# each other by name), next to the container entrypoint.
COPY src/ ./
COPY docker/entrypoint.sh ./docker-entrypoint.sh

# Create volume mount points
RUN mkdir -p /music/albums /config /app/data

# Make entrypoints executable
RUN chmod +x /app/entrypoint.py /app/docker-entrypoint.sh

# docker-entrypoint.sh starts mullvad-daemon (there is no systemd in the
# container) and waits for it before handing off to entrypoint.py, which runs
# the per-user cycles and the bot, ARL page, one-star watcher and metadata daemon.
CMD ["/app/docker-entrypoint.sh"]
