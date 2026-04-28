#!/bin/bash
set -e

# Apply defaults for any env vars not set via docker-compose / .env
: "${ICECAST_MODE:=internal}"   # 'internal' = run Icecast in this container
                                 # 'external' = use an existing Icecast on the network
: "${ICECAST_SOURCE:=hackme}"
: "${ICECAST_ADMIN_PASS:=hackme_admin}"
: "${ICECAST_MOUNT:=/dab}"
: "${ICECAST_PORT:=8000}"
: "${DAEMON_PORT:=9980}"
: "${LOG_DIR:=/var/log/dabservice}"
: "${LOG_MAX_BYTES:=10485760}"
: "${LOG_BACKUP_COUNT:=3}"
: "${SNR_WARNING_THRESHOLD:=10}"
: "${PREVENTIVE_RESTART_HOUR:=3}"

export ICECAST_SOURCE ICECAST_ADMIN_PASS ICECAST_MOUNT ICECAST_PORT
export DAEMON_PORT
export LOG_DIR LOG_MAX_BYTES LOG_BACKUP_COUNT SNR_WARNING_THRESHOLD PREVENTIVE_RESTART_HOUR

# Ensure log directory exists (handles empty host volume mounts)
mkdir -p "${LOG_DIR}"
chmod 777 "${LOG_DIR}"

if [ "$ICECAST_MODE" = "internal" ]; then
    # Always reach Icecast on localhost when running inside the container
    export ICECAST_HOST=localhost

    # Generate icecast.xml from template (substitutes $ICECAST_SOURCE and $ICECAST_ADMIN_PASS)
    envsubst '${ICECAST_SOURCE} ${ICECAST_ADMIN_PASS}' \
        < /etc/icecast.xml.template \
        > /etc/icecast2/icecast.xml

    # Start Icecast in the background
    echo "[entrypoint] Starting Icecast on port ${ICECAST_PORT}..."
    icecast2 -c /etc/icecast2/icecast.xml &

    # Give Icecast a moment to bind its socket before ffmpeg connects
    sleep 2

elif [ "$ICECAST_MODE" = "external" ]; then
    if [ -z "${ICECAST_HOST:-}" ]; then
        echo "[entrypoint] ERROR: ICECAST_MODE=external but ICECAST_HOST is not set." >&2
        exit 1
    fi
    export ICECAST_HOST
    echo "[entrypoint] Using external Icecast at ${ICECAST_HOST}:${ICECAST_PORT}${ICECAST_MOUNT}"

else
    echo "[entrypoint] ERROR: Unknown ICECAST_MODE='${ICECAST_MODE}'. Use 'internal' or 'external'." >&2
    exit 1
fi

echo "[entrypoint] Starting DAB daemon..."
exec python3 /usr/local/bin/dab-daemon.py
