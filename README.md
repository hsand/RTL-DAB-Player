# Lyrion_DAB_Radio
Stream DAB+ radio to Lyrion Music Server via RTL-SDR

DAB+ radio reception via RTL-SDR for Lyrion Music Server (LMS).

Receives a full DAB multiplex using [welle-cli](https://github.com/AlbrechtL/welle.io), decodes all services simultaneously, streams each to a permanent Icecast mount with live metadata, and exposes an HTTP API for service discovery and MUX switching. An LMS plugin integrates it into the Radio menu as a flat station list.

![DAB Radio plugin icon](LMSPlugin/DABRadio/HTML/EN/plugins/DABRadio/html/images/LyrionDABRadio_svg.png)

---

## Architecture

```
RTL-SDR dongle
      ↓
  welle-cli -c 11C -Dw 9090   (decodes all services, serves MP3 over HTTP)
      ↓ http://localhost:9090/mp3/<SID>   (one stream per audio service)
  ffmpeg → Icecast /dab/dr-p1
  ffmpeg → Icecast /dab/dr-p2
  ffmpeg → Icecast /dab/dr-p3
  ...one ffmpeg per service...
      ↑
  dab-daemon  (HTTP API — service discovery, MUX switching, live metadata)
      ↑
  LMS Plugin  (Radio menu, flat station list)
```

**Service discovery:** welle-cli scans the MUX automatically. No manual SID or frequency configuration needed — all services are discovered at startup and cached.

**Live metadata:** the daemon polls welle-cli every 10 seconds for DLS (Dynamic Label Service) updates and pushes current song, genre, and description to each Icecast mount.

**Resilience:** a watchdog thread monitors ffmpeg processes and automatically restarts any that die.

---

## Web UI

The daemon includes a built-in web interface accessible at `http://<host>:9980` — no extra software needed.

Features:
- Multiplex selector — switch between configured MUXes with a single click
- Alphabetically sorted service list for the active MUX
- Built-in audio player — click a service to start listening immediately
- Live status indicator (receiving / switching / stopped)

---

## Option A — Docker (recommended for new users)

This is the easiest way to get started. Everything runs in a single container: welle-cli, ffmpeg, and Icecast are all included — no manual software installation needed.

Pre-built images for **amd64** and **arm64** are published automatically to GitHub Container Registry on every release.

### Prerequisites

- Docker and Docker Compose installed
- RTL-SDR dongle connected to the host
- DVB kernel drivers blacklisted (recommended — see below)

### Blacklisting the DVB kernel drivers

Linux automatically loads DVB drivers when you plug in an RTL-SDR dongle. Blacklist them to prevent conflicts:

```bash
echo -e "blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830" \
    | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf
sudo modprobe -r dvb_usb_rtl28xxu rtl2832 rtl2830
```

> **Docker users:** The container runs in privileged mode which lets librtlsdr claim the dongle automatically in most cases, but blacklisting avoids edge cases after re-plugging.

### Quickstart — pre-built image (easiest)

Create a `docker-compose.yml`, adjust the values, and run `docker compose up`.

#### With built-in Icecast

The container runs its own Icecast server. Each DAB service gets its own mount point (e.g. `/dab/dr-p1`, `/dab/dr-p3`).

```yaml
services:
  dab-radio:
    image: ghcr.io/macsatcom/lyrion-dab-radio:latest
    privileged: true
    devices:
      - /dev/bus/usb:/dev/bus/usb
    ports:
      - "9980:9980"   # DAB Daemon HTTP API
      - "8000:8000"   # Icecast streams
    environment:
      ICECAST_MODE:       internal
      ICECAST_SOURCE:     changeme
      ICECAST_ADMIN_PASS: changeme_admin
      ICECAST_PORT:       8000
      DAEMON_PORT:        9980
    restart: unless-stopped
```

#### With an existing Icecast server

The container streams to an Icecast server already running on your network.

```yaml
services:
  dab-radio:
    image: ghcr.io/macsatcom/lyrion-dab-radio:latest
    privileged: true
    devices:
      - /dev/bus/usb:/dev/bus/usb
    ports:
      - "9980:9980"   # DAB Daemon HTTP API
    environment:
      ICECAST_MODE:       external
      ICECAST_HOST:       192.168.1.50
      ICECAST_SOURCE:     your-source-password
      ICECAST_PORT:       8000
      DAEMON_PORT:        9980
    restart: unless-stopped
```

```bash
docker compose up
```

Once running:
- DAB Daemon API: `http://<host-ip>:9980`
- Icecast streams: `http://<host-ip>:8000/dab/<service-slug>`

### Quickstart — build from source

```bash
git clone https://github.com/macsatcom/Lyrion_DAB_Radio.git
cd Lyrion_DAB_Radio/docker
cp .env.example .env
# edit .env with your values
docker compose up --build
```

The first build takes several minutes (welle-cli is compiled from source).

### Configuring the MUX list

By default the container listens to **DR MUX (channel 11C)**. To change this, set the `MUX_LIST` environment variable as a JSON string:

```yaml
environment:
  MUX_LIST: '[{"key":"mux1","name":"My MUX (10B)","channel":"10B"}]'
```

Multiple MUXes are supported — the daemon can switch between them via the API.

---

## Option B — Manual installation

### Prerequisites

- Linux server (Debian/Ubuntu recommended)
- RTL-SDR USB dongle connected and working
- [welle-cli](https://github.com/AlbrechtL/welle.io) built and installed (see note below)
- `ffmpeg` installed (`apt install ffmpeg`)
- Icecast2 server running (`apt install icecast2` or Docker)
- Lyrion Music Server 8.x or 9.x

### RTL-SDR USB dongle

RTL-SDR is a type of software-defined radio (SDR) that uses a cheap DVB-T TV tuner dongle as a wideband radio receiver. Originally designed for receiving digital TV, these dongles can be repurposed to receive a wide range of radio signals — including DAB+ — when used with the right software. A basic dongle from China costing around 10 EUR works perfectly fine for DAB reception.

> **Note:** Getting your RTL-SDR dongle working and building welle-cli is outside the scope of this guide. See the [rtl-sdr quickstart](https://www.rtl-sdr.com/rtl-sdr-quick-start-guide/) and the [welle.io README](https://github.com/AlbrechtL/welle.io) for instructions. Verify your setup works by running `welle-cli -c 11C -s any` before proceeding.

### Building welle-cli

```bash
sudo apt install cmake librtlsdr-dev libfftw3-dev libfaad-dev libmpg123-dev libmp3lame-dev
git clone https://github.com/AlbrechtL/welle.io
cd welle.io && mkdir build && cd build
cmake -DBUILD_WELLE_CLI=ON -DBUILD_GUI_APP=OFF ..
make -j$(nproc)
sudo cp src/welle-cli/welle-cli /usr/local/bin/
```

---

## Daemon Setup

The daemon (`dab-daemon.py`) manages welle-cli and ffmpeg pipelines, and exposes an HTTP API for service discovery and MUX switching.

### 1. Configure

Edit `daemon/dab-daemon.py` and fill in the configuration section at the top:

```python
WELLE_CLI_BIN      = "/usr/local/bin/welle-cli"
WELLE_PORT         = 9090              # internal welle-cli HTTP port (not exposed)

ICECAST_HOST       = "your-icecast-host"
ICECAST_PORT       = 8000
ICECAST_SOURCE     = "your-source-password"
ICECAST_ADMIN_USER = "admin"
ICECAST_ADMIN_PASS = "your-admin-password"

DAEMON_PORT        = 9980
```

And define your MUX list:

```python
MUX_LIST = [
    {
        "key":     "mux1",
        "name":    "DR MUX (11C)",
        "channel": "11C",
    },
]
```

Optionally create the service cache directory:

```bash
sudo mkdir -p /var/lib/dab-daemon
sudo chown $USER /var/lib/dab-daemon
```

### 2. Install

```bash
sudo cp daemon/dab-daemon.py /usr/local/bin/dab-daemon.py
sudo chmod +x /usr/local/bin/dab-daemon.py
```

### 3. Install as a systemd service

```bash
sudo cp daemon/dab-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dab-daemon
sudo systemctl start dab-daemon
```

Check that it is running:

```bash
sudo systemctl status dab-daemon
journalctl -u dab-daemon -f
```

### 4. Test the API

```bash
# Current status and active streams
curl http://localhost:9980/status

# List discovered MUXes and services
curl http://localhost:9980/muxes

# Switch to a different MUX
curl http://localhost:9980/switch/mux2

# Force rescan of current MUX
curl http://localhost:9980/rescan

# Stop everything
curl -X POST http://localhost:9980/stop
```

### API reference

| Method | Path | Description |
|--------|------|-------------|
| GET | `/status` | Current MUX, welle-cli state, and all active stream URLs |
| GET | `/muxes` | List all MUXes with discovered services and Icecast stream URLs |
| GET | `/switch/<key>` | Switch to MUX by key (async, ~15–30 s for discovery) |
| POST | `/switch?mux=<key>` | Switch to MUX by key |
| GET | `/rescan` | Force rescan of current MUX (clears cache) |
| POST | `/stop` | Stop all ffmpeg pipelines and welle-cli |

---

## LMS Plugin Installation

### Manual install

1. Copy the `LMSPlugin/DABRadio` folder into your LMS plugin directory:
   - Docker: `/config/cache/Plugins/DABRadio`
   - Standard: `/usr/share/squeezeboxserver/Plugins/DABRadio`

2. Restart LMS.

### Via external repository

Add the following URL in LMS under **Settings → Plugins → Add repository**:

```
https://raw.githubusercontent.com/macsatcom/Lyrion_DAB_Radio/main/repo.xml
```

After adding the repository, DAB Radio will appear in the plugin list and can be installed from there.

### Plugin configuration

After installation, go to **Settings → Plugins → DAB Radio → Settings** and configure:

- **Daemon URL** — URL to your dab-daemon, e.g. `http://192.168.1.10:9980`
- **Icecast Host** — hostname or IP of your Icecast server (leave blank to use URLs from daemon)
- **Icecast Port** — typically `8000`

The plugin will appear under **Radio → DAB Radio** in LMS as a flat list of all services on the active MUX.

---

## License

MIT
