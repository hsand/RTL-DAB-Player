#!/usr/bin/env python3
"""
DAB Radio Daemon - See README.md for setup and configuration.

Architecture:
  welle-cli -c <channel> -Dw <welle_port>   (decodes all services, serves MP3)
      ↓ http://localhost:<welle_port>/mp3/<SID>   (one per audio service)
  ffmpeg -i <welle_url> -c copy -f mp3  →  Icecast/<mount>
"""

import os
import sys
import re
import signal
import subprocess
import threading
import time
import json
import base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
import urllib.parse
import urllib.request

# ─── Configuration ────────────────────────────────────────────────────────────

WELLE_CLI_BIN      = os.environ.get("WELLE_CLI_BIN",    "/usr/local/bin/welle-cli")
WELLE_PORT         = int(os.environ.get("WELLE_PORT",   "9090"))
SERVICES_CACHE     = os.environ.get("SERVICES_CACHE",   "/var/lib/dab-daemon/services.json")
ICECAST_HOST       = os.environ.get("ICECAST_HOST",     "your-icecast-host")
ICECAST_PORT       = int(os.environ.get("ICECAST_PORT", "8000"))
ICECAST_SOURCE     = os.environ.get("ICECAST_SOURCE",   "your-source-password")
ICECAST_ADMIN_USER = os.environ.get("ICECAST_ADMIN_USER", "admin")
ICECAST_ADMIN_PASS = os.environ.get("ICECAST_ADMIN_PASS", "your-admin-password")
DAEMON_PORT        = int(os.environ.get("DAEMON_PORT",  "9980"))
ICECAST_MODE       = os.environ.get("ICECAST_MODE",    "internal")
ICECAST_MOUNT_BASE = os.environ.get("ICECAST_MOUNT",   "/dab")

# How long to wait for welle-cli to discover services
DISCOVERY_TIMEOUT  = int(os.environ.get("DISCOVERY_TIMEOUT", "30"))

# ─── MUX configuration ────────────────────────────────────────────────────────

# MUX_LIST can be overridden via a JSON env var, e.g.:
# MUX_LIST=[{"key":"mux1","name":"DR MUX (11C)","channel":"11C"}]
_mux_list_env = os.environ.get("MUX_LIST")
if _mux_list_env:
    MUX_LIST = json.loads(_mux_list_env)
else:
    MUX_LIST = [
        {
            "key":     "mux1",
            "name":    "DR MUX (11C)",
            "channel": "11C",
        },
        # Add more MUXes here:
        # { "key": "mux2", "name": "MUX 2", "channel": "10B" },
    ]

# ─── Web UI ───────────────────────────────────────────────────────────────────

WEB_UI_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DAB Radio</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #111827; color: #e5e7eb;
    font-family: ui-monospace, monospace;
    min-height: 100vh; padding: 2rem;
    display: flex; flex-direction: column; align-items: center; gap: 1.5rem;
  }
  h1 { font-size: 1.1rem; letter-spacing: 0.25em; color: #6b7280; text-transform: uppercase; }
  #now-playing { font-size: 1.6rem; font-weight: bold; color: #34d399; min-height: 2rem; }
  #dls { font-size: 0.9rem; color: #9ca3af; min-height: 1.2rem; }
  audio { width: 100%; max-width: 480px; accent-color: #34d399; }
  .section-label {
    font-size: 0.75rem; letter-spacing: 0.15em; text-transform: uppercase;
    color: #4b5563; width: 100%; max-width: 480px;
  }
  .mux-row { display: flex; gap: 0.5rem; flex-wrap: wrap; width: 100%; max-width: 480px; }
  .mux-btn {
    background: #1f2937; color: #9ca3af; border: 1px solid #374151;
    padding: 0.4rem 0.9rem; font-size: 0.85rem; cursor: pointer;
    border-radius: 5px; font-family: inherit;
  }
  .mux-btn.active { background: #34d399; color: #111827; border-color: #34d399; font-weight: bold; }
  .mux-btn:hover:not(.active) { border-color: #6b7280; color: #e5e7eb; }
  .service-list { width: 100%; max-width: 480px; display: flex; flex-direction: column; gap: 0.4rem; }
  .svc-btn {
    background: #1f2937; color: #e5e7eb; border: 1px solid #374151;
    padding: 0.55rem 1rem; font-size: 0.95rem; cursor: pointer;
    border-radius: 5px; text-align: left; font-family: inherit;
  }
  .svc-btn.active { border-color: #34d399; color: #34d399; }
  .svc-btn:hover:not(.active) { border-color: #6b7280; }
  #status-line { font-size: 0.8rem; color: #4b5563; }
  #status-dot { width: 8px; height: 8px; border-radius: 50%; background: #374151; display: inline-block; margin-right: 0.4rem; }
  #status-dot.alive { background: #34d399; }
  .switching { color: #fbbf24 !important; }
</style>
</head>
<body>
<h1>DAB Radio</h1>
<div id="now-playing">--</div>
<div id="dls"></div>
<audio id="player" controls></audio>

<div class="section-label">Multiplex</div>
<div class="mux-row" id="mux-row"></div>

<div class="section-label">Services</div>
<div class="service-list" id="service-list"></div>

<div id="status-line"><span id="status-dot"></span><span id="status-text">loading...</span></div>

<script>
let cfg = {};
let muxData = [];
let currentMux = null;
let currentStream = null;
let switching = false;

async function init() {
  cfg = await fetch('/config').then(r => r.json());
  await refresh();
  setInterval(refresh, 3000);
}

function streamUrl(svc) {
  const path = svc.stream.replace(/^https?:\/\/[^/]+/, '');
  const host = cfg.icecast_mode === 'internal' ? window.location.hostname : cfg.icecast_host;
  return 'http://' + host + ':' + cfg.icecast_port + path;
}

async function refresh() {
  try {
    const [muxes, status] = await Promise.all([
      fetch('/muxes').then(r => r.json()),
      fetch('/status').then(r => r.json()),
    ]);

    const alive = status.welle_alive;
    document.getElementById('status-dot').className = alive ? 'alive' : '';

    if (switching) {
      document.getElementById('status-text').textContent = 'switching mux...';
      document.getElementById('status-text').className = 'switching';
    } else {
      document.getElementById('status-text').textContent = alive ? 'receiving' : 'stopped';
      document.getElementById('status-text').className = '';
    }

    if (status.current_mux !== currentMux || muxData.length === 0) {
      currentMux = status.current_mux;
      switching = false;
      muxData = muxes;
      renderMuxButtons(muxes, currentMux);
      const activeMux = muxes.find(m => m.key === currentMux);
      if (activeMux) renderServices(activeMux.services);
    }
  } catch(e) {}
}

function renderMuxButtons(muxes, activeMux) {
  const row = document.getElementById('mux-row');
  row.innerHTML = '';
  for (const m of muxes) {
    const btn = document.createElement('button');
    btn.className = 'mux-btn' + (m.key === activeMux ? ' active' : '');
    btn.textContent = m.name;
    btn.onclick = () => switchMux(m.key);
    row.appendChild(btn);
  }
}

function renderServices(services) {
  const list = document.getElementById('service-list');
  list.innerHTML = '';
  const sorted = [...services].sort((a, b) => a.name.localeCompare(b.name));
  for (const svc of sorted) {
    const btn = document.createElement('button');
    const url = streamUrl(svc);
    btn.className = 'svc-btn' + (url === currentStream ? ' active' : '');
    btn.textContent = svc.name;
    btn.onclick = () => playSvc(svc, btn);
    list.appendChild(btn);
  }
}

function playSvc(svc, btn) {
  const url = streamUrl(svc);
  currentStream = url;
  document.getElementById('player').src = url;
  document.getElementById('now-playing').textContent = svc.name;
  document.querySelectorAll('.svc-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

async function switchMux(key) {
  if (key === currentMux || switching) return;
  switching = true;
  currentStream = null;
  document.getElementById('player').src = '';
  document.getElementById('now-playing').textContent = '--';
  document.getElementById('service-list').innerHTML = '';
  document.getElementById('status-text').textContent = 'switching mux...';
  document.getElementById('status-text').className = 'switching';
  renderMuxButtons(muxData, key);
  await fetch('/switch/' + key);
}

init();
</script>
</body>
</html>
"""

# ─── State ────────────────────────────────────────────────────────────────────

current_mux_key = None
welle_proc      = None
stream_procs    = {}   # mount → {"ffmpeg": proc, "service": svc_dict}
dls_state       = {}   # mount → last known DLS string
state_lock      = threading.Lock()

# ─── Helpers ──────────────────────────────────────────────────────────────────

def slugify(name):
    s = name.lower().strip()
    s = re.sub(r'[æÆ]', 'ae', s)
    s = re.sub(r'[øØ]', 'oe', s)
    s = re.sub(r'[åÅ]', 'aa', s)
    s = re.sub(r'[^a-z0-9]+', '-', s)
    return s.strip('-') or 'service'

# ─── Service cache ────────────────────────────────────────────────────────────

def load_service_cache():
    try:
        with open(SERVICES_CACHE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_service_cache(cache):
    try:
        os.makedirs(os.path.dirname(SERVICES_CACHE), exist_ok=True)
        with open(SERVICES_CACHE, 'w') as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"[daemon] Could not save service cache: {e}")

# ─── welle-cli management ─────────────────────────────────────────────────────

def stop_welle():
    global welle_proc
    if welle_proc and welle_proc.poll() is None:
        welle_proc.terminate()
        try:
            welle_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            welle_proc.kill()
        welle_proc = None
        print("[daemon] welle-cli stopped")

def start_welle(channel):
    global welle_proc
    stop_welle()
    welle_proc = subprocess.Popen(
        [WELLE_CLI_BIN, "-c", channel, "-Dw", str(WELLE_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"[daemon] welle-cli started on channel {channel} (internal port {WELLE_PORT})")

def fetch_services_from_welle():
    """
    Poll welle-cli's /mux.json until the audio service list is stable.
    Returns a list of raw service dicts from welle-cli.
    """
    url      = f"http://127.0.0.1:{WELLE_PORT}/mux.json"
    deadline = time.time() + DISCOVERY_TIMEOUT
    prev_count = -1
    stable_since = None

    while time.time() < deadline:
        try:
            resp = urllib.request.urlopen(url, timeout=2)
            data = json.loads(resp.read())
            # Audio services have a url_mp3 set
            services = [s for s in data.get("services", []) if s.get("url_mp3")]
            count = len(services)
            if count != prev_count:
                prev_count = count
                stable_since = time.time()
                if count:
                    print(f"[daemon] Discovered {count} audio services...")
            elif count > 0 and (time.time() - stable_since) >= 5:
                print(f"[daemon] Service list stable at {count} services")
                return services
        except Exception:
            pass
        time.sleep(1)

    # Timeout — return whatever we have
    try:
        resp = urllib.request.urlopen(url, timeout=3)
        data = json.loads(resp.read())
        return [s for s in data.get("services", []) if s.get("url_mp3")]
    except Exception as e:
        print(f"[daemon] Failed to fetch mux.json: {e}")
        return []

# ─── Per-service ffmpeg → Icecast ─────────────────────────────────────────────

def start_stream(raw_svc):
    """Pull MP3 from welle-cli and push to Icecast."""
    name  = raw_svc["label"]["label"].strip()
    sid   = raw_svc["sid"]
    mount = f"/dab/{slugify(name)}"
    src   = f"http://127.0.0.1:{WELLE_PORT}{raw_svc['url_mp3']}"

    genre = raw_svc.get("ptystring") or "DAB"
    desc  = raw_svc.get("mode") or "DAB+ via RTL-SDR"

    ice_url = f"icecast://source:{ICECAST_SOURCE}@{ICECAST_HOST}:{ICECAST_PORT}{mount}"

    cmd = [
        "ffmpeg", "-loglevel", "error",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", src,
        "-c:a", "libmp3lame", "-b:a", "128k",
        "-f", "mp3",
        "-ice_name",        name,
        "-ice_description", desc,
        "-ice_genre",       genre,
        "-ice_public",      "1",
        ice_url,
    ]

    proc = subprocess.Popen(cmd, stderr=subprocess.DEVNULL)
    svc_info = {
        "sid":    sid,
        "name":   name,
        "mount":  mount,
        "stream": f"http://{ICECAST_HOST}:{ICECAST_PORT}{mount}",
    }
    stream_procs[mount] = {"ffmpeg": proc, "service": svc_info}
    print(f"[daemon] Stream started: {name} ({sid}) → {mount}")

def stop_stream(mount):
    entry = stream_procs.pop(mount, None)
    dls_state.pop(mount, None)
    if not entry:
        return
    p = entry.get("ffmpeg")
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    print(f"[daemon] Stream stopped: {mount}")

def stop_all_streams():
    for mount in list(stream_procs.keys()):
        stop_stream(mount)

# ─── DLS metadata polling ─────────────────────────────────────────────────────

def metadata_updater():
    """
    Polls welle-cli /mux.json every 10 s and pushes DLS updates to Icecast.
    Runs as a daemon thread for the lifetime of the process.
    """
    while True:
        time.sleep(10)
        if not stream_procs or not welle_proc:
            continue
        try:
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{WELLE_PORT}/mux.json", timeout=3
            )
            data = json.loads(resp.read())
        except Exception:
            continue

        svc_by_sid = {s["sid"]: s for s in data.get("services", [])}

        with state_lock:
            items = list(stream_procs.items())

        for mount, info in items:
            sid = info["service"]["sid"]
            raw = svc_by_sid.get(sid)
            if not raw:
                continue
            dls = raw.get("dls", {}).get("label", "").strip()
            if dls and dls != dls_state.get(mount):
                dls_state[mount] = dls
                update_icecast_metadata(mount, dls)

# ─── Stream watchdog ─────────────────────────────────────────────────────────

def stream_watchdog():
    """
    Checks every 30 s if ffmpeg processes are still alive.
    Restarts any that have died, as long as welle-cli is running.
    """
    while True:
        time.sleep(30)
        if not welle_proc or welle_proc.poll() is not None:
            continue

        with state_lock:
            dead = [
                (mount, info)
                for mount, info in stream_procs.items()
                if info["ffmpeg"].poll() is not None
            ]

        for mount, info in dead:
            print(f"[watchdog] Restarting dead stream: {mount}")
            with state_lock:
                stream_procs.pop(mount, None)
                dls_state.pop(mount, None)
            start_stream_from_info(info["service"])

def start_stream_from_info(svc_info):
    """Restart a stream given the cached service info dict."""
    # Reconstruct the minimal raw_svc needed by start_stream
    raw_svc = {
        "label": {"label": svc_info["name"]},
        "sid":   svc_info["sid"],
        "url_mp3": f"/mp3/{svc_info['sid']}",
        "ptystring": "",
        "mode": "DAB+ via RTL-SDR",
    }
    start_stream(raw_svc)

# ─── Icecast metadata ─────────────────────────────────────────────────────────

def update_icecast_metadata(mount, title):
    """Update stream title on Icecast. Tries source password first, then admin.

    Icecast 2.4 expects the song parameter URL-encoded as Latin-1,
    not UTF-8 — hence the explicit encoding.
    """
    song = urllib.parse.quote(title, encoding="latin-1", errors="replace")
    params = f"mode=updinfo&mount={urllib.parse.quote(mount)}&song={song}"
    url = f"http://{ICECAST_HOST}:{ICECAST_PORT}/admin/metadata?{params}"
    for user, pw in [("source", ICECAST_SOURCE), (ICECAST_ADMIN_USER, ICECAST_ADMIN_PASS)]:
        try:
            req = urllib.request.Request(url)
            creds = base64.b64encode(f"{user}:{pw}".encode()).decode()
            req.add_header("Authorization", f"Basic {creds}")
            urllib.request.urlopen(req, timeout=3)
            print(f"[daemon] Metadata updated: {mount} → {title}")
            return
        except urllib.error.HTTPError as e:
            if e.code != 401:
                print(f"[daemon] Metadata update failed ({mount}): {e}")
                return
        except Exception as e:
            print(f"[daemon] Metadata update failed ({mount}): {e}")
            return
    print(f"[daemon] Metadata update failed ({mount}): authentication failed")

# ─── MUX switching ────────────────────────────────────────────────────────────

def get_mux(key):
    for m in MUX_LIST:
        if m["key"] == key:
            return m
    return None

def switch_mux(mux_key):
    mux = get_mux(mux_key)
    if not mux:
        print(f"[daemon] Unknown MUX: {mux_key}")
        return False

    def _do_switch():
        global current_mux_key
        print(f"[daemon] Switching to: {mux['name']}")

        with state_lock:
            stop_all_streams()
            start_welle(mux["channel"])

        print(f"[daemon] Waiting for service discovery (up to {DISCOVERY_TIMEOUT}s)...")
        raw_services = fetch_services_from_welle()

        if not raw_services:
            print("[daemon] ERROR: No services found — check channel and reception")
            return

        # Build and cache a clean service list
        services = [
            {
                "sid":    s["sid"],
                "name":   s["label"]["label"].strip(),
                "mount":  f"/dab/{slugify(s['label']['label'].strip())}",
                "stream": f"http://{ICECAST_HOST}:{ICECAST_PORT}/dab/{slugify(s['label']['label'].strip())}",
            }
            for s in raw_services
        ]
        cache = load_service_cache()
        cache[mux_key] = services
        save_service_cache(cache)

        with state_lock:
            for s in raw_services:
                start_stream(s)
            current_mux_key = mux_key

        print(f"[daemon] Switch complete: {mux['name']} — {len(services)} active streams")

    threading.Thread(target=_do_switch, daemon=True).start()
    return True

# ─── HTTP API ─────────────────────────────────────────────────────────────────

def json_response(handler, code, data):
    body = json.dumps(data, indent=2).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", len(body))
    handler.end_headers()
    handler.wfile.write(body)

class DABHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            body = WEB_UI_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path == "/config":
            json_response(self, 200, {
                "icecast_mode":  ICECAST_MODE,
                "icecast_host":  ICECAST_HOST,
                "icecast_port":  ICECAST_PORT,
            })

        elif parsed.path == "/status":
            with state_lock:
                active = [
                    {
                        "mount":        mount,
                        "name":         info["service"]["name"],
                        "sid":          info["service"]["sid"],
                        "stream":       info["service"]["stream"],
                        "ffmpeg_alive": info["ffmpeg"].poll() is None,
                    }
                    for mount, info in stream_procs.items()
                ]
            json_response(self, 200, {
                "current_mux":    current_mux_key,
                "welle_alive":    welle_proc is not None and welle_proc.poll() is None,
                "active_streams": active,
            })

        elif parsed.path == "/muxes":
            cache = load_service_cache()
            out = []
            for m in MUX_LIST:
                services = cache.get(m["key"], [])
                out.append({
                    "key":      m["key"],
                    "name":     m["name"],
                    "channel":  m["channel"],
                    "scanned":  len(services) > 0,
                    "services": [
                        {
                            "sid":    s["sid"],
                            "name":   s["name"],
                            "stream": s["stream"],
                        }
                        for s in services
                    ],
                })
            json_response(self, 200, out)

        elif parsed.path == "/rescan":
            if current_mux_key:
                cache = load_service_cache()
                cache.pop(current_mux_key, None)
                save_service_cache(cache)
                switch_mux(current_mux_key)
                json_response(self, 202, {"ok": True, "note": "rescanning..."})
            else:
                json_response(self, 400, {"error": "no active MUX"})

        elif parsed.path.startswith("/switch/"):
            mux_key = parsed.path[len("/switch/"):]
            if switch_mux(mux_key):
                json_response(self, 202, {"ok": True, "switching_to": mux_key})
            else:
                json_response(self, 404, {"error": f"unknown mux: {mux_key}"})

        else:
            json_response(self, 404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/switch":
            mux_key = qs.get("mux", [None])[0]
            if not mux_key:
                json_response(self, 400, {"error": "missing mux parameter"})
                return
            if switch_mux(mux_key):
                json_response(self, 202, {"ok": True, "switching_to": mux_key})
            else:
                json_response(self, 404, {"error": f"unknown mux: {mux_key}"})

        elif parsed.path == "/stop":
            with state_lock:
                stop_all_streams()
                stop_welle()
            json_response(self, 200, {"ok": True, "status": "stopped"})

        else:
            json_response(self, 404, {"error": "not found"})

# ─── Entrypoint ───────────────────────────────────────────────────────────────

def shutdown_handler(sig, frame):
    print("\n[daemon] shutting down...")
    stop_all_streams()
    stop_welle()
    os._exit(0)

if __name__ == "__main__":
    if not MUX_LIST:
        print("[daemon] ERROR: No MUXes configured")
        sys.exit(1)

    signal.signal(signal.SIGINT,  shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    server = HTTPServer(("0.0.0.0", DAEMON_PORT), DABHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=metadata_updater, daemon=True).start()
    threading.Thread(target=stream_watchdog, daemon=True).start()
    print(f"[daemon] HTTP API on port {DAEMON_PORT}")
    print(f"[daemon] Endpoints: /status  /muxes  /switch/<key>  /rescan  POST /stop")

    switch_mux(MUX_LIST[0]["key"])

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        shutdown_handler(None, None)
