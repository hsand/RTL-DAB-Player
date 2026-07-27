#!/usr/bin/env python3
"""
RTL DAB Player daemon - See README.md for setup and configuration.

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
import logging
import math
import traceback
from logging.handlers import RotatingFileHandler
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
import urllib.parse
import urllib.request


def linear_to_db(value):
    """Convert a linear audio level from welle-cli to dBFS.

    welle-cli reports 16-bit sample peaks (0-32768); older builds reported
    0.0-1.0 floats — normalize either to full scale = 1.0.
    """
    if value > 1:
        value = value / 32768.0
    if value <= 0:
        return -100.0  # essentially silence
    return 20 * math.log10(value)

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

# ─── Logging ──────────────────────────────────────────────────────────────────

LOG_DIR           = os.environ.get("LOG_DIR", "/var/log/dabservice")
LOG_MAX_BYTES     = int(os.environ.get("LOG_MAX_BYTES", "10485760"))
LOG_BACKUP_COUNT  = int(os.environ.get("LOG_BACKUP_COUNT", "3"))

# ─── Signal Quality ──────────────────────────────────────────────────────────

SNR_WARNING_THRESHOLD = float(os.environ.get("SNR_WARNING_THRESHOLD", "10"))

# ─── Preventive Restart ───────────────────────────────────────────────────────

PREVENTIVE_RESTART_HOUR = int(os.environ.get("PREVENTIVE_RESTART_HOUR", "3"))

# ─── Logging Setup ────────────────────────────────────────────────────────────

def setup_logging():
    """Configure rotating file loggers."""
    os.makedirs(LOG_DIR, exist_ok=True)

    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    # Daemon logger
    handler_daemon = RotatingFileHandler(
        os.path.join(LOG_DIR, 'dab-daemon.log'),
        maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
    )
    handler_daemon.setFormatter(fmt)

    logger_daemon = logging.getLogger('dab_daemon')
    logger_daemon.addHandler(handler_daemon)
    logger_daemon.setLevel(logging.INFO)
    logger_daemon.propagate = False

    # Signal quality logger
    handler_signal = RotatingFileHandler(
        os.path.join(LOG_DIR, 'signal-quality.log'),
        maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
    )
    handler_signal.setFormatter(fmt)

    logger_signal = logging.getLogger('signal_quality')
    logger_signal.addHandler(handler_signal)
    logger_signal.setLevel(logging.INFO)
    logger_signal.propagate = False

    # ffmpeg errors logger
    handler_ffmpeg = RotatingFileHandler(
        os.path.join(LOG_DIR, 'ffmpeg-errors.log'),
        maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
    )
    handler_ffmpeg.setFormatter(fmt)

    logger_ffmpeg = logging.getLogger('ffmpeg_errors')
    logger_ffmpeg.addHandler(handler_ffmpeg)
    logger_ffmpeg.setLevel(logging.WARNING)
    logger_ffmpeg.propagate = False

    return logger_daemon, logger_signal, logger_ffmpeg

logger_daemon, logger_signal, logger_ffmpeg = setup_logging()

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
<title>RTL DAB Player</title>
<style>
  :root {
    --bg: #0d1117; --card: #161b22; --card-hi: #1c2430; --border: #30363d;
    --text: #e6edf3; --dim: #8b949e; --faint: #484f58;
    --accent: #3fb950; --amber: #d29922; --red: #f85149;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--text);
    font-family: system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
    max-width: 1100px; margin: 0 auto;
    padding: 1.4rem 1.4rem 7.5rem;
  }
  header { display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; flex-wrap: wrap; }
  h1 { font-size: 1.35rem; letter-spacing: 0.04em; }
  #ensemble { color: var(--dim); font-size: 0.9rem; margin-top: 0.15rem; min-height: 1.1rem; }
  .pill {
    font-size: 0.85rem; font-weight: 600; padding: 0.35rem 0.8rem;
    border-radius: 999px; border: 1px solid var(--border); color: var(--dim);
    white-space: nowrap;
  }
  .pill.good { color: var(--accent); border-color: var(--accent); }
  .pill.mid  { color: var(--amber);  border-color: var(--amber); }
  .pill.bad  { color: var(--red);    border-color: var(--red); }
  .mux-row { display: flex; gap: 0.5rem; flex-wrap: wrap; margin: 1rem 0; }
  .mux-btn {
    background: var(--card); color: var(--dim); border: 1px solid var(--border);
    padding: 0.4rem 0.9rem; font-size: 0.85rem; cursor: pointer;
    border-radius: 999px; font-family: inherit; transition: all 0.15s;
  }
  .mux-btn.active { background: var(--accent); color: #04160a; border-color: var(--accent); font-weight: 700; }
  .mux-btn:hover:not(.active) { border-color: var(--dim); color: var(--text); }
  #msg { color: var(--dim); font-size: 0.9rem; min-height: 1.3rem; margin-bottom: 0.8rem; }
  .spin {
    width: 11px; height: 11px; border-radius: 50%; display: inline-block;
    border: 2px solid rgba(230, 237, 243, 0.25); border-top-color: currentColor;
    animation: spin 0.8s linear infinite; vertical-align: -1px; margin-right: 0.45rem;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  #grid.ghost .card { opacity: 0.45; }
  #grid.ghost .card:hover { opacity: 0.8; }
  .grid {
    display: grid; gap: 0.9rem;
    grid-template-columns: repeat(auto-fill, minmax(165px, 1fr));
  }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    overflow: hidden; cursor: pointer; transition: transform 0.12s, border-color 0.12s;
  }
  .card:hover { border-color: var(--dim); transform: translateY(-2px); background: var(--card-hi); }
  .card.playing { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
  .art {
    aspect-ratio: 4 / 3; width: 100%; position: relative;
    background: linear-gradient(135deg, #1c2430, #10151c);
    display: flex; align-items: center; justify-content: center;
  }
  .art .ph { font-size: 2.4rem; font-weight: 800; color: var(--faint); letter-spacing: 0.05em; }
  .art img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; }
  .badge {
    position: absolute; top: 0.45rem; right: 0.45rem; z-index: 2;
    font-size: 0.65rem; font-weight: 700; padding: 0.15rem 0.45rem; border-radius: 999px;
  }
  .badge.err { background: rgba(248, 81, 73, 0.9); color: #fff; }
  .meta { padding: 0.6rem 0.7rem 0.7rem; }
  .name { font-weight: 650; font-size: 0.95rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sub  { color: var(--dim); font-size: 0.72rem; margin-top: 0.1rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .dls  { color: var(--accent); font-size: 0.78rem; margin-top: 0.3rem; min-height: 1.05rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #player-bar {
    position: fixed; bottom: 0; left: 0; right: 0; display: none;
    background: rgba(13, 17, 23, 0.96); backdrop-filter: blur(8px);
    border-top: 1px solid var(--border);
  }
  #player-bar .inner {
    max-width: 1100px; margin: 0 auto; padding: 0.7rem 1.4rem;
    display: flex; align-items: center; gap: 1rem;
  }
  #bar-art {
    width: 72px; height: 54px; border-radius: 8px; overflow: hidden; flex-shrink: 0;
    background: var(--card-hi); display: flex; align-items: center; justify-content: center;
    font-weight: 800; color: var(--faint);
  }
  #bar-art img { width: 100%; height: 100%; object-fit: contain; }
  #bar-text { min-width: 0; flex: 1; }
  #bar-name { font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #bar-dls  { color: var(--accent); font-size: 0.8rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-height: 1.05rem; }
  audio { flex: 1 1 300px; max-width: 380px; height: 40px; }
  @media (max-width: 640px) {
    body { padding: 1rem 1rem 9rem; }
    #player-bar .inner { flex-wrap: wrap; gap: 0.5rem 1rem; padding: 0.6rem 1rem; }
    audio { flex-basis: 100%; max-width: none; }
  }
</style>
</head>
<body>
<header>
  <div>
    <h1>RTL DAB Player</h1>
    <div id="ensemble"></div>
  </div>
  <div id="snr-pill" class="pill">SNR &ndash;</div>
</header>
<div class="mux-row" id="mux-row"></div>
<div id="msg"></div>
<div class="grid" id="grid"></div>

<div id="player-bar">
  <div class="inner">
    <div id="bar-art"></div>
    <div id="bar-text">
      <div id="bar-name"></div>
      <div id="bar-dls"></div>
    </div>
    <audio id="player" controls></audio>
  </div>
</div>

<script>
'use strict';
let muxList = [], currentMux = null;
let switching = false, pendingMux = null, switchStarted = 0;
let lastNow = null;
let playing = null;          // {sid, name, mount}
let pendingPlay = null;      // station queued while tuning; auto-plays when live
let barArtTs = 0;            // slide version currently shown in the player bar
let cardRefs = {};           // sid -> {cardEl, artEl, dlsEl, genreEl, brEl, imgTs}
let gridSig = '';

const $ = (id) => document.getElementById(id);

function initials(name) {
  const words = name.trim().split(' ').filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  return name.trim().slice(0, 2).toUpperCase();
}

function muxName(key) {
  const m = muxList.find((x) => x.key === key);
  return m ? m.name : key;
}

function slideSrc(st) {
  return '/slide/' + st.sid + '?t=' + st.slide_ts;
}

async function init() {
  const p = $('player');
  // /listen holds the connection until the stream is live, so playback that
  // starts inside the click just buffers until audio flows. These handlers
  // only cover the rare failure cases (e.g. /listen gave up after 90 s).
  p.addEventListener('playing', () => { pendingPlay = null; });
  p.addEventListener('error', () => {
    setTimeout(() => {
      if (pendingPlay && p.src) {
        p.load();
        p.play().catch(() => {});
      }
    }, 3000);
  });
  await tick();
  setInterval(tick, 3000);
}

async function tick() {
  let now, muxes;
  try {
    [now, muxes] = await Promise.all([
      fetch('/now').then((r) => r.json()),
      fetch('/muxes').then((r) => r.json()),
    ]);
  } catch (e) {
    setMsg('Daemon unreachable&hellip;');
    return;
  }

  muxList = muxes;
  currentMux = now.current_mux;
  lastNow = now;

  if (now.switching_to) {
    switching = true;
    pendingMux = now.switching_to;
  } else if (switching && currentMux === pendingMux) {
    switching = false;
    pendingMux = null;
  } else if (switching && Date.now() - switchStarted > 8000) {
    switching = false;   // switch failed or was aborted server-side
    pendingMux = null;
  }

  renderMuxChips();
  renderHeader(now);

  if (switching) {
    showSwitchingState();
    return;
  }
  $('grid').classList.remove('ghost');

  const sig = now.stations.map((s) => s.sid).join(',');
  if (sig !== gridSig) {
    buildGrid(now.stations);
    gridSig = sig;
  }
  updateGrid(now.stations);

  if (pendingPlay) {
    // /listen is still holding the connection (element not paused) — leave
    // it alone. Only kick it if something actually failed.
    const p = $('player');
    if (p.paused) {
      p.load();
      p.play().catch(() => {});
    }
  }

  if (!now.welle_alive) setMsg('Receiver stopped.');
  else if (!now.stations.length) setMsg(spinnerMsg('Scanning for stations'));
  else setMsg('');
}

function spinnerMsg(text) {
  return '<span class="spin"></span>' + text + '&hellip;';
}

function showSwitchingState() {
  const n = renderSnapshot(pendingMux);
  $('grid').classList.add('ghost');
  setMsg(n ? '' : spinnerMsg('Tuning ' + muxName(pendingMux)));
}

function setMsg(html) { $('msg').innerHTML = html; }

function renderHeader(now) {
  $('ensemble').textContent = now.ensemble || '';

  const pill = $('snr-pill');
  const snr = now.signal.snr;
  if (snr == null || !now.welle_alive) {
    pill.textContent = 'SNR –';
    pill.className = 'pill';
  } else {
    pill.textContent = 'SNR ' + snr.toFixed(1) + ' dB';
    pill.className = 'pill ' + (snr >= 12 ? 'good' : snr >= 7 ? 'mid' : 'bad');
  }
}

function renderMuxChips() {
  const row = $('mux-row');
  row.innerHTML = '';
  for (const m of muxList) {
    const btn = document.createElement('button');
    const active = m.key === (pendingMux || currentMux);
    btn.className = 'mux-btn' + (active ? ' active' : '');
    btn.textContent = m.name;
    btn.onclick = () => switchMux(m.key);
    row.appendChild(btn);
  }
}

function makeArt(st, el) {
  el.innerHTML = '';
  const ph = document.createElement('span');
  ph.className = 'ph';
  ph.textContent = initials(st.name);
  el.appendChild(ph);
  if (st.slide_ts > 0) {
    const img = document.createElement('img');
    img.alt = '';
    img.onerror = () => img.remove();
    img.src = slideSrc(st);
    el.appendChild(img);
  }
}

function bitrateLine(st) {
  if (st.bitrate) return st.bitrate + ' kbit/s';
  // fall back to the part of welle's mode string after '@', e.g. "72 kbit/s"
  const i = (st.codec || '').indexOf('@');
  return i >= 0 ? st.codec.slice(i + 1).trim() : '';
}

function renderSnapshot(muxKey) {
  // Last known station list for a mux, shown while the switch runs.
  // Slides and now-playing are stripped: they'll be stale or 404 mid-switch.
  const snap = ((lastNow && lastNow.snapshots) || {})[muxKey] || [];
  const sig = 'snap:' + muxKey + ':' + snap.map((s) => s.sid).join(',');
  if (sig !== gridSig) {
    gridSig = sig;
    buildGrid(snap.map((s) => Object.assign({}, s, { slide_ts: 0, dls: '' })));
  }
  return snap.length;
}

function buildGrid(stations) {
  const grid = $('grid');
  grid.innerHTML = '';
  cardRefs = {};
  for (const st of stations) {
    const card = document.createElement('div');
    card.className = 'card';

    const art = document.createElement('div');
    art.className = 'art';
    makeArt(st, art);
    card.appendChild(art);

    const meta = document.createElement('div');
    meta.className = 'meta';
    const name = document.createElement('div');
    name.className = 'name';
    name.textContent = st.name;
    name.title = st.name;
    const genre = document.createElement('div');
    genre.className = 'sub';
    genre.textContent = st.genre || '';
    const br = document.createElement('div');
    br.className = 'sub';
    br.textContent = bitrateLine(st);
    const dls = document.createElement('div');
    dls.className = 'dls';
    meta.appendChild(name);
    meta.appendChild(genre);
    meta.appendChild(br);
    meta.appendChild(dls);
    card.appendChild(meta);

    card.onclick = () => play(st);
    grid.appendChild(card);
    cardRefs[st.sid] = { cardEl: card, artEl: art, dlsEl: dls, genreEl: genre, brEl: br, imgTs: st.slide_ts };
  }
}

function updateGrid(stations) {
  for (const st of stations) {
    const ref = cardRefs[st.sid];
    if (!ref) continue;

    ref.dlsEl.textContent = st.dls || '';
    ref.dlsEl.title = st.dls || '';
    ref.genreEl.textContent = st.genre || '';
    ref.brEl.textContent = bitrateLine(st);
    ref.cardEl.classList.toggle('playing', !!playing && playing.sid === st.sid);

    if (st.slide_ts > 0 && st.slide_ts !== ref.imgTs) {
      ref.imgTs = st.slide_ts;
      makeArt(st, ref.artEl);
    }
    // Keep the player-bar logo in sync with the playing card (e.g. the click
    // happened on a snapshot card before the slide was available).
    if (playing && playing.sid === st.sid && st.slide_ts !== barArtTs) {
      setBarArt(st);
    }

    if (playing && playing.sid === st.sid) {
      $('bar-dls').textContent = st.dls || '';
      updateMediaSession(st);
    }
  }
}

function play(st) {
  playing = { sid: st.sid, name: st.name, mount: st.mount };
  $('player-bar').style.display = 'block';
  $('bar-name').textContent = st.name;
  setBarArt(st);
  for (const sid in cardRefs) {
    cardRefs[sid].cardEl.classList.toggle('playing', sid === st.sid);
  }

  // Start inside the click gesture. /listen waits server-side for the
  // stream to come up, so this single play() covers mid-tuning clicks too.
  pendingPlay = (switching || !st.streaming) ? st : null;
  const p = $('player');
  p.src = '/listen/' + st.sid;
  p.play().catch(() => {});
  if (pendingPlay) {
    $('bar-dls').textContent = 'Starts when tuning finishes…';
  } else {
    $('bar-dls').textContent = st.dls || '';
    updateMediaSession(st);
  }
}

function setBarArt(st) {
  barArtTs = st.slide_ts || 0;
  const barArt = $('bar-art');
  barArt.innerHTML = '';
  if (st.slide_ts > 0) {
    const img = document.createElement('img');
    img.alt = '';
    img.onerror = () => { img.remove(); barArt.textContent = initials(st.name); };
    img.src = slideSrc(st);
    barArt.appendChild(img);
  } else {
    barArt.textContent = initials(st.name);
  }
}

function updateMediaSession(st) {
  if (!('mediaSession' in navigator)) return;
  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: st.dls || st.name,
      artist: st.name,
      album: 'DAB Radio',
      artwork: st.slide_ts > 0 ? [{ src: slideSrc(st) }] : [],
    });
  } catch (e) {}
}

function stopPlayback() {
  playing = null;
  pendingPlay = null;
  barArtTs = 0;
  const p = $('player');
  p.pause();
  p.removeAttribute('src');
  p.load();
  $('player-bar').style.display = 'none';
}

async function switchMux(key) {
  if (switching || key === currentMux) return;
  switching = true;
  pendingMux = key;
  switchStarted = Date.now();
  stopPlayback();
  gridSig = '';
  renderMuxChips();
  showSwitchingState();
  try { await fetch('/switch/' + key); } catch (e) {}
}

init();
</script>
</body>
</html>
"""

# ─── State ────────────────────────────────────────────────────────────────────

current_mux_key = None
welle_proc      = None
welle_stderr_thread = None

# In-memory only: last known station list per mux, so the UI can show
# something immediately while a switch is in progress.
station_snapshots = {}
switching_to      = None  # target mux key while a switch is running, else None
stream_procs    = {}   # mount → {"ffmpeg": proc, "service": svc_dict, "stderr_thread": thread, "reconnect_count": int}
dls_state       = {}   # mount → last known DLS string
state_lock      = threading.Lock()

# Consolidated mux.json cache
latest_mux_json = None
mux_json_lock   = threading.Lock()
mux_json_event  = threading.Event()

# Signal quality tracking
prev_error_counters = {}  # mount → {frameerrors, rserrors, aacerrors}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def slugify(name):
    s = name.lower().strip()
    s = re.sub(r'[æÆ]', 'ae', s)
    s = re.sub(r'[øØ]', 'oe', s)
    s = re.sub(r'[åÅ]', 'aa', s)
    s = re.sub(r'[^a-z0-9]+', '-', s)
    return s.strip('-') or 'service'

def fetch_json_with_close(url, timeout=3):
    """Fetch JSON from welle-cli and always close the response."""
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        data = json.loads(resp.read())
        resp.close()
        return data
    except Exception as e:
        logger_daemon.debug(f"fetch_json_with_close failed for {url}: {e}")
        return None

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
        logger_daemon.warning(f"Could not save service cache: {e}")

# ─── welle-cli management ─────────────────────────────────────────────────────

def read_stderr_thread(proc, name):
    """Read stderr from a process and log any output."""
    try:
        for line in proc.stderr:
            line = line.decode('utf-8', errors='replace').strip()
            if line:
                logger_daemon.info(f"[{name}] {line}")
    except Exception:
        pass

def stop_welle():
    global welle_proc, welle_stderr_thread
    if welle_proc and welle_proc.poll() is None:
        welle_proc.terminate()
        try:
            welle_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger_daemon.warning("welle-cli did not respond to SIGTERM — sending SIGKILL")
            welle_proc.kill()
            welle_proc.wait(timeout=5)
        welle_proc = None
        welle_stderr_thread = None
        mux_json_event.clear()
        logger_daemon.info("welle-cli stopped")
    elif welle_proc:
        welle_proc = None
        welle_stderr_thread = None
        logger_daemon.info("welle-cli already exited, cleared reference")

def start_welle(channel):
    global welle_proc, welle_stderr_thread
    stop_welle()
    welle_proc = subprocess.Popen(
        [WELLE_CLI_BIN, "-c", channel, "-Dw", str(WELLE_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    # Start stderr reader thread
    welle_stderr_thread = threading.Thread(
        target=read_stderr_thread, args=(welle_proc, "welle-cli"), daemon=True
    )
    welle_stderr_thread.start()
    logger_daemon.info(f"welle-cli started on channel {channel} (internal port {WELLE_PORT})")

# ─── Consolidated mux.json fetcher ────────────────────────────────────────────

def mux_json_fetcher():
    """Single background thread that fetches mux.json periodically and caches it."""
    while True:
        time.sleep(5)
        if not welle_proc or welle_proc.poll() is not None:
            continue
        try:
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{WELLE_PORT}/mux.json", timeout=5
            )
            data = json.loads(resp.read())
            resp.close()
            with mux_json_lock:
                global latest_mux_json
                latest_mux_json = data
                mux_json_event.set()
        except Exception as e:
            logger_daemon.debug(f"mux_json_fetcher failed: {e}")

# ─── Signal Quality Monitor ────────────────────────────────────────────────────

def signal_quality_monitor():
    """Polls cached mux.json and logs signal quality metrics."""
    while True:
        time.sleep(10)
        with mux_json_lock:
            data = latest_mux_json

        if not data:
            continue

        # Global metrics (under "demodulator" in welle-cli's mux.json)
        demod = data.get("demodulator") or {}
        snr = demod.get("snr")
        freq_corr = demod.get("frequencycorrection")

        if snr is not None:
            msg = f"SNR={snr:.1f} dB"
            if freq_corr is not None:
                msg += f", freq_corr={freq_corr} Hz"

            if snr < SNR_WARNING_THRESHOLD:
                logger_signal.warning(f"{msg} — BELOW THRESHOLD ({SNR_WARNING_THRESHOLD} dB)")
            else:
                logger_signal.info(msg)

        # Per-service metrics
        services = data.get("services", [])
        for svc in services:
            if not svc.get("url_mp3"):
                continue
            name = svc.get("label", {}).get("label", "unknown")
            sid = svc.get("sid")
            mount = f"/dab/{slugify(name)}"

            audio = svc.get("audiolevel", {})
            left_linear = audio.get("left", 0)
            right_linear = audio.get("right", 0)
            left_db = linear_to_db(left_linear)
            right_db = linear_to_db(right_linear)

            errs = svc.get("errorcounters", {})
            fe = errs.get("frameerrors", 0)
            rs = errs.get("rserrors", 0)
            aac = errs.get("aacerrors", 0)

            log_msg = f"{name} (SID={sid}) audio_left={left_db:.1f} dBFS audio_right={right_db:.1f} dBFS frameerrors={fe} rserrors={rs} aacerrors={aac}"
            logger_signal.info(log_msg)

            # Check for warnings
            if left_db < -60 or right_db < -60:
                logger_signal.warning(f"{name} audio level very low (silence?): left={left_db:.1f}, right={right_db:.1f}")

            prev = prev_error_counters.get(mount, {})
            if prev:
                df = fe - prev.get("frameerrors", 0)
                dr = rs - prev.get("rserrors", 0)
                da = aac - prev.get("aacerrors", 0)
                if df > 5 or dr > 5 or da > 5:
                    logger_signal.warning(f"{name} error counters increased significantly: frame+{df}, rs+{dr}, aac+{da}")

            # High RS errors directly cause audio glitches/dropouts
            if rs > 100:
                logger_signal.warning(f"{name} HIGH RS errors ({rs}) — expect audio dropouts/glitches")

            prev_error_counters[mount] = {"frameerrors": fe, "rserrors": rs, "aacerrors": aac}

# ─── Service discovery ───────────────────────────────────────────────────────

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
            resp.close()
            # Audio services have a url_mp3 set
            services = [s for s in data.get("services", []) if s.get("url_mp3")]
            count = len(services)
            if count != prev_count:
                prev_count = count
                stable_since = time.time()
                if count:
                    logger_daemon.info(f"Discovered {count} audio services...")
            elif count > 0 and (time.time() - stable_since) >= 5:
                logger_daemon.info(f"Service list stable at {count} services")
                return services
        except Exception:
            pass
        time.sleep(1)

    # Timeout — return whatever we have
    try:
        resp = urllib.request.urlopen(url, timeout=3)
        data = json.loads(resp.read())
        resp.close()
        return [s for s in data.get("services", []) if s.get("url_mp3")]
    except Exception as e:
        logger_daemon.warning(f"Failed to fetch mux.json: {e}")
        return []

# ─── Per-service ffmpeg → Icecast ─────────────────────────────────────────────

FFMPEG_ERROR_KEYWORDS = ['error', 'failed', 'reconnect', 'buffer', 'underflow', 'overflow', 'dropped', 'discontinuity']

def read_ffmpeg_stderr(proc, mount, name):
    """Read ffmpeg stderr and log relevant lines."""
    try:
        for line in proc.stderr:
            line = line.decode('utf-8', errors='replace').strip()
            if not line:
                continue
            lower = line.lower()
            for kw in FFMPEG_ERROR_KEYWORDS:
                if kw in lower:
                    logger_ffmpeg.warning(f"[{name}] {line}")
                    break
    except Exception:
        pass

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
        "-c:a", "copy",
        "-f", "mp3",
        "-ice_name",        name,
        "-ice_description", desc,
        "-ice_genre",       genre,
        "-ice_public",      "1",
        ice_url,
    ]

    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
    svc_info = {
        "sid":    sid,
        "name":   name,
        "mount":  mount,
        "stream": f"http://{ICECAST_HOST}:{ICECAST_PORT}{mount}",
    }
    stderr_thread = threading.Thread(
        target=read_ffmpeg_stderr, args=(proc, mount, name), daemon=True
    )
    stderr_thread.start()
    stream_procs[mount] = {"ffmpeg": proc, "service": svc_info, "stderr_thread": stderr_thread, "reconnect_count": 0}
    logger_daemon.info(f"Stream started: {name} ({sid}) → {mount}")

def stop_stream(mount):
    entry = stream_procs.pop(mount, None)
    dls_state.pop(mount, None)
    prev_error_counters.pop(mount, None)
    if not entry:
        return
    p = entry.get("ffmpeg")
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    logger_daemon.info(f"Stream stopped: {mount}")

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

        with mux_json_lock:
            data = latest_mux_json
        if not data:
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

# ─── welle-cli watchdog ───────────────────────────────────────────────────────

_welle_restart_in_progress = threading.Event()

def _welle_health_check():
    """
    Returns True if welle-cli process is alive AND its HTTP endpoint responds.
    A hanging/zombie welle-cli that no longer serves data is considered dead.
    """
    with state_lock:
        proc = welle_proc
    if proc is None or proc.poll() is not None:
        return False
    # Process is alive — verify it actually responds
    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{WELLE_PORT}/mux.json", timeout=5
        )
        resp.read()
        resp.close()
        return True
    except Exception:
        return False

def welle_watchdog():
    """
    Checks every 15 s if welle-cli is alive and responsive.
    If the HTTP health-check fails for 3 consecutive attempts (45 s),
    restarts the full mux (welle-cli + all ffmpeg streams).
    """
    consecutive_failures = 0
    FAILURE_THRESHOLD = 3

    while True:
        time.sleep(15)
        with state_lock:
            mux_key = current_mux_key

        if not mux_key:
            consecutive_failures = 0
            continue

        if _welle_restart_in_progress.is_set():
            # A restart is already underway — don't pile on
            consecutive_failures = 0
            continue

        if _welle_health_check():
            consecutive_failures = 0
            continue

        consecutive_failures += 1
        logger_daemon.warning(f"welle-watchdog health-check failed ({consecutive_failures}/{FAILURE_THRESHOLD})")

        if consecutive_failures >= FAILURE_THRESHOLD:
            consecutive_failures = 0
            logger_daemon.warning(f"welle-watchdog: welle-cli unresponsive — restarting mux '{mux_key}'")
            _welle_restart_in_progress.set()
            try:
                switch_mux(mux_key)
            finally:
                # Clear after a delay so the new welle-cli has time to start
                threading.Timer(DISCOVERY_TIMEOUT + 10, _welle_restart_in_progress.clear).start()

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
            logger_daemon.warning(f"Restarting dead stream: {mount}")
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

# ─── Preventive Restart ───────────────────────────────────────────────────────

def preventive_restart_scheduler():
    """
    Performs a graceful restart of the current MUX at a configured hour
    (default 03:00) to clear accumulated welle-cli file descriptor leaks.
    """
    while True:
        now = time.localtime()
        next_run = time.mktime((
            now.tm_year, now.tm_mon, now.tm_mday,
            PREVENTIVE_RESTART_HOUR, 0, 0,
            now.tm_wday, now.tm_yday, now.tm_isdst
        ))
        if next_run <= time.time():
            # Schedule for tomorrow
            next_run += 86400

        sleep_secs = next_run - time.time()
        logger_daemon.info(f"Preventive restart scheduled in {sleep_secs/3600:.1f} hours (at {PREVENTIVE_RESTART_HOUR}:00)")
        time.sleep(sleep_secs)

        with state_lock:
            mux_key = current_mux_key

        if not mux_key:
            logger_daemon.info("No active MUX — skipping preventive restart")
            continue

        logger_daemon.info(f"Performing preventive restart of MUX '{mux_key}'")
        switch_mux(mux_key)

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
            logger_daemon.info(f"Metadata updated: {mount} → {title}")
            return
        except urllib.error.HTTPError as e:
            if e.code != 401:
                logger_daemon.warning(f"Metadata update failed ({mount}): {e}")
                return
        except Exception as e:
            logger_daemon.warning(f"Metadata update failed ({mount}): {e}")
            return
    logger_daemon.warning(f"Metadata update failed ({mount}): authentication failed")

# ─── MUX switching ────────────────────────────────────────────────────────────

def get_mux(key):
    for m in MUX_LIST:
        if m["key"] == key:
            return m
    return None

def switch_mux(mux_key):
    global switching_to
    mux = get_mux(mux_key)
    if not mux:
        logger_daemon.warning(f"Unknown MUX: {mux_key}")
        return False

    def _do_switch():
        global current_mux_key, switching_to
        logger_daemon.info(f"Switching to: {mux['name']}")

        with state_lock:
            stop_all_streams()
            start_welle(mux["channel"])

        logger_daemon.info(f"Waiting for service discovery (up to {DISCOVERY_TIMEOUT}s)...")
        raw_services = fetch_services_from_welle()

        if not raw_services:
            logger_daemon.warning("ERROR: No services found — check channel and reception")
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

        logger_daemon.info(f"Switch complete: {mux['name']} — {len(services)} active streams")

    def _run_switch():
        global switching_to
        try:
            _do_switch()
        finally:
            switching_to = None

    switching_to = mux_key  # set before returning so /now reflects it immediately
    threading.Thread(target=_run_switch, daemon=True).start()
    return True

# ─── Consolidated UI payload ──────────────────────────────────────────────────

def build_now_payload():
    """Everything the web UI needs in one call, built from the cached mux.json."""
    with mux_json_lock:
        data = latest_mux_json

    with state_lock:
        mux_key     = current_mux_key
        welle_alive = welle_proc is not None and welle_proc.poll() is None
        streams = {
            info["service"]["sid"]: {
                "mount": mount,
                "alive": info["ffmpeg"].poll() is None,
            }
            for mount, info in stream_procs.items()
        }

    stations = []
    if data and welle_alive:
        for svc in data.get("services", []):
            if not svc.get("url_mp3"):
                continue
            sid  = svc.get("sid")
            name = (svc.get("label") or {}).get("label", "").strip()
            if not name:
                continue
            entry = streams.get(sid)
            audio = svc.get("audiolevel") or {}
            errs  = svc.get("errorcounters") or {}
            mot   = svc.get("mot") or {}

            bitrate = None
            for comp in svc.get("components") or []:
                sub = comp.get("subchannel") or {}
                if sub.get("bitrate"):
                    bitrate = sub["bitrate"]
                    break

            stations.append({
                "sid":        sid,
                "name":       name,
                "mount":      entry["mount"] if entry else f"{ICECAST_MOUNT_BASE}/{slugify(name)}",
                "streaming":  bool(entry and entry["alive"]),
                "genre":      svc.get("ptystring") or "",
                "codec":      svc.get("mode") or "",
                "samplerate": svc.get("samplerate") or 0,
                "bitrate":    bitrate,
                "dls":        (svc.get("dls") or {}).get("label", "").strip(),
                "slide_ts":   mot.get("lastchange") or 0,
                "audio_db": {
                    "left":  round(linear_to_db(audio.get("left", 0)), 1),
                    "right": round(linear_to_db(audio.get("right", 0)), 1),
                },
                "errors": {
                    "frame": errs.get("frameerrors", 0),
                    "rs":    errs.get("rserrors", 0),
                    "aac":   errs.get("aacerrors", 0),
                },
            })

    stations.sort(key=lambda s: s["name"].lower())

    # Remember the station list per mux (in-memory) so the UI can show the
    # previous line-up instantly during a switch. Don't record mid-switch:
    # mux.json may already belong to the new mux while current_mux_key is old.
    if mux_key and stations and switching_to is None:
        station_snapshots[mux_key] = stations

    data     = data or {}
    demod    = data.get("demodulator") or {}
    receiver = data.get("receiver") or {}
    hardware = receiver.get("hardware") or {}
    # ensemble.label is a label object, same shape as service labels
    ens_label = ((data.get("ensemble") or {}).get("label") or {}).get("label", "")

    return {
        "current_mux": mux_key,
        "switching_to": switching_to,
        "snapshots":   station_snapshots,
        "welle_alive": welle_alive,
        "ensemble":    ens_label.strip(),
        "signal": {
            "snr":            demod.get("snr"),
            "freq_corr":      demod.get("frequencycorrection"),
            "fic_crc_errors": (demod.get("fic") or {}).get("numcrcerrors"),
            "hardware":       hardware.get("name", ""),
            "gain":           hardware.get("gain"),
        },
        "stations": stations,
    }

# ─── HTTP API ─────────────────────────────────────────────────────────────────

def json_response(handler, code, data):
    body = json.dumps(data, indent=2).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", len(body))
    handler.end_headers()
    handler.wfile.write(body)

class DABHandler(BaseHTTPRequestHandler):

    QUIET_PATHS = ("/now", "/muxes", "/slide/", "/status", "/health", "/config", "/favicon")

    def log_message(self, fmt, *args):
        msg = fmt % args
        # The web UI polls constantly — don't flood the log with those requests
        if any(p in msg for p in self.QUIET_PATHS):
            return
        logger_daemon.info(f"[http] {self.address_string()} {msg}")

    def do_GET(self):
        try:
            self._do_GET()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logger_daemon.error(f"Unhandled error serving GET {self.path}:\n{traceback.format_exc()}")
            try:
                json_response(self, 500, {"error": "internal error"})
            except Exception:
                pass

    def _do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            body = WEB_UI_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path == "/now":
            json_response(self, 200, build_now_payload())

        elif parsed.path.startswith("/slide/"):
            sid = parsed.path[len("/slide/"):]
            if not re.fullmatch(r"(0x)?[0-9a-fA-F]{1,8}", sid):
                json_response(self, 404, {"error": "bad sid"})
                return
            try:
                resp = urllib.request.urlopen(
                    f"http://127.0.0.1:{WELLE_PORT}/slide/{sid}", timeout=3
                )
                body  = resp.read()
                ctype = resp.headers.get("Content-Type", "image/jpeg")
                resp.close()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", len(body))
                # UI cache-busts with ?t=<mot lastchange>, so cache aggressively
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                json_response(self, 404, {"error": "no slide"})

        elif parsed.path.startswith("/listen/"):
            # Play a station by SID. If a mux switch is in progress the
            # connection is held open until the stream is live, then audio is
            # piped through. This lets the browser start playback inside the
            # user's click and just wait out the tuning — no autoplay issues.
            sid = parsed.path[len("/listen/"):]
            if not re.fullmatch(r"(0x)?[0-9a-fA-F]{1,8}", sid):
                json_response(self, 404, {"error": "bad sid"})
                return

            deadline = time.time() + 90
            upstream = None
            while time.time() < deadline and upstream is None:
                mount = None
                with state_lock:
                    if switching_to is None:
                        for m, info in stream_procs.items():
                            if info["service"]["sid"] == sid and info["ffmpeg"].poll() is None:
                                mount = m
                                break
                if mount:
                    try:
                        upstream = urllib.request.urlopen(
                            f"http://{ICECAST_HOST}:{ICECAST_PORT}{mount}", timeout=5
                        )
                    except Exception:
                        upstream = None  # ffmpeg not connected to Icecast yet
                if upstream is None:
                    time.sleep(1)

            if upstream is None:
                json_response(self, 404, {"error": "stream not available"})
                return

            try:
                self.send_response(200)
                self.send_header("Content-Type", upstream.headers.get("Content-Type", "audio/mpeg"))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while True:
                    chunk = upstream.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # listener went away
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass

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

        elif parsed.path == "/health":
            with state_lock:
                welle_alive = welle_proc is not None and welle_proc.poll() is None
                streams = []
                overall = "OK"
                for mount, info in stream_procs.items():
                    alive = info["ffmpeg"].poll() is None
                    streams.append({
                        "mount":           mount,
                        "name":            info["service"]["name"],
                        "ffmpeg_alive":    alive,
                        "reconnect_count": info.get("reconnect_count", 0),
                    })
                    if not alive:
                        overall = "CRITICAL"

            with mux_json_lock:
                data = latest_mux_json

            snr = None
            if data:
                snr = (data.get("demodulator") or {}).get("snr")
                if snr is not None and snr < SNR_WARNING_THRESHOLD:
                    if overall == "OK":
                        overall = "DEGRADED"

            json_response(self, 200, {
                "welle_status": welle_alive and "alive" or "unresponsive",
                "signal_snr":  snr,
                "streams":     streams,
                "overall":     overall,
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
    logger_daemon.info("shutting down...")
    stop_all_streams()
    stop_welle()
    os._exit(0)

if __name__ == "__main__":
    if not MUX_LIST:
        logger_daemon.error("No MUXes configured")
        sys.exit(1)

    signal.signal(signal.SIGINT,  shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    server = ThreadingHTTPServer(("0.0.0.0", DAEMON_PORT), DABHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=metadata_updater, daemon=True).start()
    threading.Thread(target=welle_watchdog,  daemon=True).start()
    threading.Thread(target=stream_watchdog, daemon=True).start()
    threading.Thread(target=mux_json_fetcher, daemon=True).start()
    threading.Thread(target=signal_quality_monitor, daemon=True).start()
    threading.Thread(target=preventive_restart_scheduler, daemon=True).start()

    logger_daemon.info(f"HTTP API on port {DAEMON_PORT}")
    logger_daemon.info(f"Endpoints: /now  /status  /health  /muxes  /slide/<sid>  /switch/<key>  /rescan  POST /stop")

    switch_mux(MUX_LIST[0]["key"])

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        shutdown_handler(None, None)
