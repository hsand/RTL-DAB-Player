# DAB Radio Daemon — Docker image
# Includes: welle-cli (built from source), ffmpeg, Icecast2, Python3
#
# Build context must be the repository root:
#   docker build -f docker/Dockerfile -t dab-radio .
#
# Normally you use docker-compose instead — see docker-compose.yml.

FROM debian:bookworm-slim

# ── System dependencies ───────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        ffmpeg \
        icecast2 \
        libfftw3-dev \
        libfaad-dev \
        libmpg123-dev \
        libmp3lame-dev \
        librtlsdr-dev \
        libusb-1.0-0 \
        libusb-1.0-0-dev \
        cmake \
        build-essential \
        git \
        ca-certificates \
        pkg-config \
        xxd \
        gettext-base \
    && rm -rf /var/lib/apt/lists/*

# ── Build welle-cli from source ───────────────────────────────────────────────
# Using AlbrechtL/welle.io — produces the 'welle-cli' binary used by the daemon.
# We only build the CLI tool, not the GUI application.
RUN git clone --depth 1 https://github.com/AlbrechtL/welle.io.git /tmp/welle.io \
    && cmake -S /tmp/welle.io -B /tmp/welle.io/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_WELLE_IO=OFF \
        -DBUILD_WELLE_CLI=ON \
        -DRTLSDR=ON \
    && cmake --build /tmp/welle.io/build --target welle-cli -j$(nproc) \
    && install -m 755 /tmp/welle.io/build/welle-cli /usr/local/bin/welle-cli \
    && rm -rf /tmp/welle.io

# ── Application files ─────────────────────────────────────────────────────────
COPY daemon/dab-daemon.py         /usr/local/bin/dab-daemon.py
COPY docker/icecast.xml.template  /etc/icecast.xml.template
COPY docker/entrypoint.sh         /entrypoint.sh

RUN chmod +x /usr/local/bin/dab-daemon.py /entrypoint.sh \
    && mkdir -p /var/lib/dab-daemon

# 9980 = DAB Daemon HTTP API, 8000 = Icecast stream
EXPOSE 9980 8000

ENTRYPOINT ["/entrypoint.sh"]
