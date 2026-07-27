#!/bin/bash
set -e

# Apply defaults for any env vars not set via docker-compose / .env
: "${ICECAST_MODE:=internal}"   # 'internal' = run Icecast in this container
                                 # 'external' = use an existing Icecast on the network
: "${ICECAST_SOURCE:=changeme}"
: "${ICECAST_ADMIN_PASS:=changeme_admin}"
: "${ICECAST_MOUNT:=/dab}"
: "${ICECAST_PORT:=8000}"
# Bytes sent instantly on connect. Lower = audio starts sooner but the
# decoder has less to lock onto; higher = slower start. ~2s at DAB bitrates.
: "${ICECAST_BURST_SIZE:=65536}"
# Advertised in stream URLs on Icecast's status page. The container's own IP
# is useless to LAN listeners, so this must be set explicitly to be useful.
: "${ICECAST_HOSTNAME:=localhost}"
: "${DAEMON_PORT:=9980}"
: "${DISCOVERY_TIMEOUT:=30}"
: "${LISTEN_WAIT_TIMEOUT:=90}"
: "${LOG_DIR:=/var/log/dabservice}"
: "${LOG_MAX_BYTES:=10485760}"
: "${LOG_BACKUP_COUNT:=3}"
: "${SNR_WARNING_THRESHOLD:=10}"
: "${PREVENTIVE_RESTART_HOUR:=3}"

export ICECAST_SOURCE ICECAST_ADMIN_PASS ICECAST_MOUNT ICECAST_PORT ICECAST_HOSTNAME ICECAST_BURST_SIZE
export DAEMON_PORT DISCOVERY_TIMEOUT LISTEN_WAIT_TIMEOUT
export LOG_DIR LOG_MAX_BYTES LOG_BACKUP_COUNT SNR_WARNING_THRESHOLD PREVENTIVE_RESTART_HOUR

# Ensure log directory exists (handles empty host volume mounts)
mkdir -p "${LOG_DIR}"

# Warn loudly if the shipped example passwords are still in use — Icecast is
# reachable from the whole LAN, including its /admin interface.
case "${ICECAST_SOURCE}:${ICECAST_ADMIN_PASS}" in
    changeme:*|*:changeme_admin|hackme:*|*:hackme_admin)
        echo "[entrypoint] WARNING: default Icecast password(s) in use — set" >&2
        echo "[entrypoint]          ICECAST_SOURCE and ICECAST_ADMIN_PASS in docker/.env" >&2
        ;;
esac

if [ "$ICECAST_MODE" = "internal" ]; then
    # Always reach Icecast on localhost when running inside the container
    export ICECAST_HOST=localhost

    # Generate icecast.xml from template
    envsubst '${ICECAST_SOURCE} ${ICECAST_ADMIN_PASS} ${ICECAST_PORT} ${ICECAST_HOSTNAME} ${ICECAST_BURST_SIZE}' \
        < /etc/icecast.xml.template \
        > /etc/icecast2/icecast.xml

    # Start Icecast in the background
    echo "[entrypoint] Starting Icecast on port ${ICECAST_PORT}..."
    icecast2 -c /etc/icecast2/icecast.xml &
    ICECAST_PID=$!

    # Stop Icecast when this container is asked to shut down
    trap 'kill "${ICECAST_PID}" 2>/dev/null || true' TERM INT

    # Wait for Icecast to accept connections rather than guessing with sleep
    for _ in $(seq 1 30); do
        if python3 -c "import socket,sys; s=socket.create_connection(('127.0.0.1', ${ICECAST_PORT}), 1); s.close()" 2>/dev/null; then
            echo "[entrypoint] Icecast is accepting connections."
            break
        fi
        if ! kill -0 "${ICECAST_PID}" 2>/dev/null; then
            echo "[entrypoint] ERROR: Icecast exited during startup — check its config." >&2
            exit 1
        fi
        sleep 1
    done

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
