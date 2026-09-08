#!/usr/bin/env python3
"""
Web GUI to capture still photos from the Raspberry Pi camera, plus an
automatic motion-detection mode for unattended bird-feeder monitoring.

- Dropdown to pick a resolution
- Sliders for zoom/crop (ROI), contrast, saturation, and EV compensation
- "Save Picture" button that triggers a manual capture on the server side
- A background thread that watches the feeder area and automatically saves
  a full-resolution photo whenever something changes, no button press
  needed. It starts automatically as soon as this script runs (including
  on boot, via the systemd service), and can be paused/resumed from the
  web page without restarting anything.
- Uses the current `rpicam-still` CLI (Bookworm/Trixie), falling back to the
  older `libcamera-still` name if that's what's installed.

Run:
    python3 app.py
Then browse to:
    http://<pi-ip-address>:5000
"""

import datetime
import json
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from flask import Flask, request, jsonify, render_template_string, send_from_directory

try:
    import numpy as np
    from PIL import Image, ImageFilter
    _MOTION_DEPS_AVAILABLE = True
except ImportError:
    _MOTION_DEPS_AVAILABLE = False

app = Flask(__name__)

# Where captured photos get saved on the Pi.
PICTURES_DIR = Path.home() / "Pictures" / "webcam_captures"
PICTURES_DIR.mkdir(parents=True, exist_ok=True)

# Available resolutions shown in the dropdown -> (width, height)
RESOLUTIONS = {
    "640x480": (640, 480),
    "1640x1232": (1640, 1232),
    "1920x1080": (1920, 1080),
    "3280x2464": (3280, 2464),
}

# Slider bounds + defaults for the adjustable capture parameters.
# (rpicam-still technically accepts wider contrast/saturation ranges up to
# 32.0 and ev up to +/-10.0, but those extremes aren't useful in practice —
# these bounds keep the sliders in a sensible, well-behaved range.)
#
# left/right/top/bottom are independent crop margins, each as a percentage
# of the frame to trim from that edge (0 = no crop on that edge).
PARAM_SPECS = {
    "left":       {"min": 0,    "max": 90,   "default": 25,   "step": 5},
    "right":      {"min": 0,    "max": 90,   "default": 25,   "step": 5},
    "top":        {"min": 0,    "max": 90,   "default": 25,   "step": 5},
    "bottom":     {"min": 0,    "max": 90,   "default": 25,   "step": 5},
    "contrast":   {"min": 0.0,  "max": 3.0,  "default": 1.3,  "step": 0.1},
    "saturation": {"min": 0.0,  "max": 3.0,  "default": 1.3,  "step": 0.1},
    "ev":         {"min": -3.0, "max": 3.0,  "default": -0.3, "step": 0.1},
}

# Motion-detection tuning exposed in the UI. "Sensitivity" is a simplified
# 1-10 dial: higher = triggers on smaller amounts of change. "Cooldown" is
# the minimum time between two automatic captures, so one visiting bird
# doesn't fill the gallery with near-duplicate photos.
MOTION_SPECS = {
    "motion_sensitivity": {"min": 1, "max": 10, "default": 5, "step": 1},
    "motion_cooldown":    {"min": 5, "max": 120, "default": 20, "step": 5},
}

ALL_NUMERIC_SPECS = {**PARAM_SPECS, **MOTION_SPECS}

# Prefer the current command name; fall back to the legacy one.
CAMERA_CMD = shutil.which("rpicam-still") or shutil.which("libcamera-still")

# Serializes all camera CLI invocations (manual captures, motion-detection
# polling snapshots, and motion-triggered captures) so only one process
# ever touches the camera at a time.
CAMERA_LOCK = threading.Lock()

# Persisted "defaults" (resolution + slider values + motion-detection
# settings) so the last-saved settings are reloaded automatically the next
# time the app starts. Kept alongside app.py but NOT meant to be committed
# to git (see .gitignore) since it's per-device/user state, not code.
SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"

BUILTIN_DEFAULTS = {
    "resolution": next(iter(RESOLUTIONS)),
    **{key: spec["default"] for key, spec in ALL_NUMERIC_SPECS.items()},
    # Motion detection is on by default so a freshly set-up Pi starts
    # monitoring the feeder automatically without any extra steps.
    "motion_enabled": True,
}


def load_settings():
    """
    Load saved default settings from disk, falling back to the built-in
    defaults for anything missing, corrupted, or out of range.
    """
    settings = dict(BUILTIN_DEFAULTS)

    if not SETTINGS_FILE.exists():
        return settings

    try:
        saved = json.loads(SETTINGS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: could not read {SETTINGS_FILE}: {e}")
        return settings

    if not isinstance(saved, dict):
        return settings

    if saved.get("resolution") in RESOLUTIONS:
        settings["resolution"] = saved["resolution"]

    for key, spec in ALL_NUMERIC_SPECS.items():
        if key in saved:
            try:
                settings[key] = _validate_range(key, saved[key], spec)
            except ValueError:
                pass  # keep the built-in default for this one field

    if "motion_enabled" in saved:
        settings["motion_enabled"] = bool(saved["motion_enabled"])

    return settings


def save_settings(data):
    """
    Merge incoming fields with the currently saved settings, validate the
    result, and atomically persist it. Merging (rather than requiring a
    full payload) lets the motion-detection toggle/sliders save themselves
    immediately without needing to resend the capture resolution/crop/etc.
    """
    current = load_settings()
    merged = dict(current)

    if "resolution" in data:
        merged["resolution"] = data["resolution"]
    for key in ALL_NUMERIC_SPECS:
        if key in data:
            merged[key] = data[key]
    if "motion_enabled" in data:
        merged["motion_enabled"] = data["motion_enabled"]

    if merged.get("resolution") not in RESOLUTIONS:
        raise ValueError(f"Unknown resolution '{merged.get('resolution')}'.")

    validated = {"resolution": merged["resolution"]}
    for key, spec in ALL_NUMERIC_SPECS.items():
        validated[key] = _validate_range(key, merged.get(key, spec["default"]), spec)
    validated["motion_enabled"] = bool(merged.get("motion_enabled", True))

    # Reuses the same crop-margin validation as an actual capture, so an
    # invalid combination (e.g. left + right >= 100%) is rejected here too.
    _roi_from_margins(validated["top"], validated["bottom"], validated["left"], validated["right"])

    tmp_path = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(validated, indent=2))
    tmp_path.replace(SETTINGS_FILE)  # atomic on POSIX
    return validated

PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pi Camera Capture</title>
<style>
  :root {
    color-scheme: light dark;
  }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    max-width: 480px;
    margin: 40px auto;
    padding: 0 20px;
    text-align: center;
  }
  h1 {
    font-size: 1.4rem;
    margin-bottom: 1.5rem;
  }
  label {
    display: block;
    text-align: left;
    font-size: 0.9rem;
    font-weight: 600;
    margin-top: 14px;
  }
  .row {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  select, button {
    font-size: 1.05rem;
    padding: 10px 14px;
    border-radius: 8px;
    border: 1px solid #999;
    margin: 8px 0;
    width: 100%;
    box-sizing: border-box;
  }
  input[type="range"] {
    flex: 1;
  }
  .value {
    min-width: 3.5em;
    text-align: right;
    font-variant-numeric: tabular-nums;
    font-size: 0.9rem;
    opacity: 0.8;
  }
  button {
    background: #2563eb;
    color: white;
    border: none;
    cursor: pointer;
    font-weight: 600;
    margin-top: 20px;
  }
  button:disabled {
    background: #93b4f0;
    cursor: default;
  }
  button.secondary {
    background: transparent;
    color: #2563eb;
    border: 1px solid #2563eb;
    margin-top: 8px;
  }
  button.secondary:disabled {
    color: #93b4f0;
    border-color: #93b4f0;
    background: transparent;
  }
  #status {
    margin-top: 20px;
    min-height: 1.5em;
    font-size: 0.95rem;
  }
  .ok { color: #16a34a; }
  .err { color: #dc2626; }
  img#preview {
    max-width: 100%;
    margin-top: 16px;
    border-radius: 8px;
    display: none;
  }
  fieldset {
    border: 1px solid #ccc;
    border-radius: 8px;
    margin-top: 20px;
    padding: 4px 14px 14px;
  }
  legend {
    font-size: 0.85rem;
    opacity: 0.75;
  }
  h2 {
    font-size: 1.1rem;
    margin: 30px 0 10px;
    text-align: left;
  }
  #gallery {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 10px;
  }
  .thumb {
    cursor: pointer;
    border: 1px solid #ccc;
    border-radius: 8px;
    overflow: hidden;
    background: #0000000d;
  }
  .thumb img {
    width: 100%;
    height: 110px;
    object-fit: cover;
    display: block;
  }
  .thumb .cap {
    font-size: 0.7rem;
    padding: 4px 6px;
    opacity: 0.7;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .galleryNav {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    margin-top: 12px;
  }
  .galleryNav button {
    margin: 0;
    width: auto;
    flex: 1;
  }
  #galleryEmpty {
    font-size: 0.9rem;
    opacity: 0.7;
    margin-top: 10px;
  }
  #motionStatus {
    font-size: 0.85rem;
    opacity: 0.8;
    margin-top: 8px;
    text-align: left;
  }
  .motionWarning {
    color: #dc2626;
    font-size: 0.85rem;
    text-align: left;
  }
  .toggleRow {
    align-items: center;
    justify-content: space-between;
  }
  .toggleRow input[type="checkbox"] {
    width: auto;
    transform: scale(1.3);
  }
</style>
</head>
<body>
  <h1>Raspberry Pi Camera Capture</h1>

  <label for="resolution">Resolution</label>
  <select id="resolution">
    {% for r in resolutions %}
    <option value="{{ r }}" {% if r == saved.resolution %}selected{% endif %}>{{ r }}</option>
    {% endfor %}
  </select>

  <fieldset>
    <legend>Crop margins (% trimmed from each edge)</legend>

    <label for="top">Top</label>
    <div class="row">
      <input type="range" id="top" min="{{ specs.top.min }}" max="{{ specs.top.max }}"
             step="{{ specs.top.step }}" value="{{ saved.top }}">
      <span class="value" id="topVal"></span>
    </div>

    <label for="bottom">Bottom</label>
    <div class="row">
      <input type="range" id="bottom" min="{{ specs.bottom.min }}" max="{{ specs.bottom.max }}"
             step="{{ specs.bottom.step }}" value="{{ saved.bottom }}">
      <span class="value" id="bottomVal"></span>
    </div>

    <label for="left">Left</label>
    <div class="row">
      <input type="range" id="left" min="{{ specs.left.min }}" max="{{ specs.left.max }}"
             step="{{ specs.left.step }}" value="{{ saved.left }}">
      <span class="value" id="leftVal"></span>
    </div>

    <label for="right">Right</label>
    <div class="row">
      <input type="range" id="right" min="{{ specs.right.min }}" max="{{ specs.right.max }}"
             step="{{ specs.right.step }}" value="{{ saved.right }}">
      <span class="value" id="rightVal"></span>
    </div>
  </fieldset>

  <fieldset>
    <legend>Adjustments</legend>

    <label for="contrast">Contrast</label>
    <div class="row">
      <input type="range" id="contrast" min="{{ specs.contrast.min }}" max="{{ specs.contrast.max }}"
             step="{{ specs.contrast.step }}" value="{{ saved.contrast }}">
      <span class="value" id="contrastVal"></span>
    </div>

    <label for="saturation">Saturation</label>
    <div class="row">
      <input type="range" id="saturation" min="{{ specs.saturation.min }}" max="{{ specs.saturation.max }}"
             step="{{ specs.saturation.step }}" value="{{ saved.saturation }}">
      <span class="value" id="saturationVal"></span>
    </div>

    <label for="ev">EV compensation</label>
    <div class="row">
      <input type="range" id="ev" min="{{ specs.ev.min }}" max="{{ specs.ev.max }}"
             step="{{ specs.ev.step }}" value="{{ saved.ev }}">
      <span class="value" id="evVal"></span>
    </div>
  </fieldset>

  <fieldset>
    <legend>Motion Detection (bird-feeder auto-capture)</legend>

    {% if not motion_deps_available %}
    <p class="motionWarning">
      Motion detection needs the Pillow and NumPy packages on the Pi.
      Install with:<br><code>sudo apt install -y python3-pil python3-numpy</code>,
      then restart the app.
    </p>
    {% endif %}

    <label for="motionEnabled" class="row toggleRow">
      <span>Enable automatic capture</span>
      <input type="checkbox" id="motionEnabled" {% if saved.motion_enabled %}checked{% endif %}
             {% if not motion_deps_available %}disabled{% endif %}>
    </label>

    <label for="motionSensitivity">Sensitivity</label>
    <div class="row">
      <input type="range" id="motionSensitivity" min="{{ motion_specs.motion_sensitivity.min }}"
             max="{{ motion_specs.motion_sensitivity.max }}" step="{{ motion_specs.motion_sensitivity.step }}"
             value="{{ saved.motion_sensitivity }}" {% if not motion_deps_available %}disabled{% endif %}>
      <span class="value" id="motionSensitivityVal"></span>
    </div>

    <label for="motionCooldown">Cooldown between auto-captures (seconds)</label>
    <div class="row">
      <input type="range" id="motionCooldown" min="{{ motion_specs.motion_cooldown.min }}"
             max="{{ motion_specs.motion_cooldown.max }}" step="{{ motion_specs.motion_cooldown.step }}"
             value="{{ saved.motion_cooldown }}" {% if not motion_deps_available %}disabled{% endif %}>
      <span class="value" id="motionCooldownVal"></span>
    </div>

    <div id="motionStatus">Checking motion detection status…</div>
  </fieldset>

  <button id="saveBtn">Save Picture</button>
  <button id="saveDefaultsBtn" class="secondary">Save Current Settings as Default</button>

  <div id="status"></div>
  <img id="preview" alt="preview of last capture">

  <h2>Past Pictures</h2>
  <div id="gallery"></div>
  <div id="galleryEmpty" style="display:none;">No pictures yet.</div>
  <div class="galleryNav">
    <button id="newerBtn" disabled>&laquo; Newer</button>
    <button id="olderBtn" disabled>Older &raquo;</button>
  </div>

  <script>
    const btn = document.getElementById('saveBtn');
    const status = document.getElementById('status');
    const preview = document.getElementById('preview');

    // Wire up each slider to live-update its displayed value.
    const marginIds = ['top', 'bottom', 'left', 'right'];
    const otherIds = ['contrast', 'saturation', 'ev'];
    for (const id of [...marginIds, ...otherIds]) {
      const slider = document.getElementById(id);
      const valSpan = document.getElementById(id + 'Val');
      const update = () => {
        valSpan.textContent = marginIds.includes(id) ? slider.value + '%' : slider.value;
      };
      slider.addEventListener('input', update);
      update();
    }

    btn.addEventListener('click', async () => {
      const resolution = document.getElementById('resolution').value;
      const top = document.getElementById('top').value;
      const bottom = document.getElementById('bottom').value;
      const left = document.getElementById('left').value;
      const right = document.getElementById('right').value;
      const contrast = document.getElementById('contrast').value;
      const saturation = document.getElementById('saturation').value;
      const ev = document.getElementById('ev').value;

      btn.disabled = true;
      status.textContent = 'Capturing...';
      status.className = '';
      preview.style.display = 'none';

      try {
        const resp = await fetch('/save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ resolution, top, bottom, left, right, contrast, saturation, ev })
        });
        const data = await resp.json();

        if (resp.ok && data.ok) {
          status.textContent = 'Saved: ' + data.filename;
          status.className = 'ok';
          preview.src = '/preview/' + data.filename + '?t=' + Date.now();
          preview.style.display = 'block';
          loadGallery(0);
        } else {
          status.textContent = 'Error: ' + (data.error || 'unknown error');
          status.className = 'err';
        }
      } catch (e) {
        status.textContent = 'Error: ' + e;
        status.className = 'err';
      } finally {
        btn.disabled = false;
      }
    });

    // ---- Past pictures gallery (10 at a time, most recent first) ----
    const GALLERY_PAGE_SIZE = 10;
    let galleryOffset = 0;

    const galleryEl = document.getElementById('gallery');
    const galleryEmptyEl = document.getElementById('galleryEmpty');
    const newerBtn = document.getElementById('newerBtn');
    const olderBtn = document.getElementById('olderBtn');

    async function loadGallery(offset) {
      const resp = await fetch('/photos?offset=' + offset + '&limit=' + GALLERY_PAGE_SIZE);
      const data = await resp.json();
      if (!resp.ok || !data.ok) return;

      galleryOffset = data.offset;
      galleryEl.innerHTML = '';

      galleryEmptyEl.style.display = (data.photos.length === 0 && galleryOffset === 0) ? 'block' : 'none';

      for (const photo of data.photos) {
        const div = document.createElement('div');
        div.className = 'thumb';
        const img = document.createElement('img');
        img.src = '/preview/' + photo.filename;
        img.loading = 'lazy';
        img.alt = photo.filename;
        const cap = document.createElement('div');
        cap.className = 'cap';
        cap.textContent = photo.filename + (photo.filename.startsWith('motion_') ? ' · auto' : '');
        div.appendChild(img);
        div.appendChild(cap);
        div.addEventListener('click', () => {
          preview.src = '/preview/' + photo.filename + '?t=' + Date.now();
          preview.style.display = 'block';
          preview.scrollIntoView({ behavior: 'smooth', block: 'center' });
        });
        galleryEl.appendChild(div);
      }

      newerBtn.disabled = galleryOffset <= 0;
      olderBtn.disabled = !data.has_more;
    }

    newerBtn.addEventListener('click', () => {
      loadGallery(Math.max(0, galleryOffset - GALLERY_PAGE_SIZE));
    });
    olderBtn.addEventListener('click', () => {
      loadGallery(galleryOffset + GALLERY_PAGE_SIZE);
    });

    loadGallery(0);

    // ---- Save current slider/resolution values as the new startup defaults ----
    const saveDefaultsBtn = document.getElementById('saveDefaultsBtn');
    saveDefaultsBtn.addEventListener('click', async () => {
      const payload = {
        resolution: document.getElementById('resolution').value,
        top: document.getElementById('top').value,
        bottom: document.getElementById('bottom').value,
        left: document.getElementById('left').value,
        right: document.getElementById('right').value,
        contrast: document.getElementById('contrast').value,
        saturation: document.getElementById('saturation').value,
        ev: document.getElementById('ev').value,
      };

      saveDefaultsBtn.disabled = true;
      status.textContent = 'Saving defaults...';
      status.className = '';

      try {
        const resp = await fetch('/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await resp.json();

        if (resp.ok && data.ok) {
          status.textContent = 'Saved as default — these settings will load automatically next time.';
          status.className = 'ok';
        } else {
          status.textContent = 'Error: ' + (data.error || 'unknown error');
          status.className = 'err';
        }
      } catch (e) {
        status.textContent = 'Error: ' + e;
        status.className = 'err';
      } finally {
        saveDefaultsBtn.disabled = false;
      }
    });

    // ---- Motion detection controls (apply immediately, not just on "save as default") ----
    const motionEnabled = document.getElementById('motionEnabled');
    const motionSensitivity = document.getElementById('motionSensitivity');
    const motionSensitivityVal = document.getElementById('motionSensitivityVal');
    const motionCooldown = document.getElementById('motionCooldown');
    const motionCooldownVal = document.getElementById('motionCooldownVal');
    const motionStatusEl = document.getElementById('motionStatus');

    function updateMotionValueLabels() {
      motionSensitivityVal.textContent = motionSensitivity.value;
      motionCooldownVal.textContent = motionCooldown.value + 's';
    }
    updateMotionValueLabels();

    async function postMotionSettings() {
      updateMotionValueLabels();
      try {
        await fetch('/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            motion_enabled: motionEnabled.checked,
            motion_sensitivity: motionSensitivity.value,
            motion_cooldown: motionCooldown.value,
          })
        });
      } catch (e) {
        // Best-effort; the status poll below will reflect the real state either way.
      }
      refreshMotionStatus();
    }

    motionEnabled.addEventListener('change', postMotionSettings);
    motionSensitivity.addEventListener('change', postMotionSettings);
    motionCooldown.addEventListener('change', postMotionSettings);
    motionSensitivity.addEventListener('input', updateMotionValueLabels);
    motionCooldown.addEventListener('input', updateMotionValueLabels);

    async function refreshMotionStatus() {
      try {
        const resp = await fetch('/motion/status');
        const data = await resp.json();
        if (!resp.ok || !data.ok) return;

        if (!data.deps_available) {
          motionStatusEl.textContent = 'Motion detection unavailable — missing Pillow/NumPy on the Pi.';
        } else if (!data.enabled) {
          motionStatusEl.textContent = 'Motion detection: paused.';
        } else if (data.last_capture_at) {
          const when = new Date(data.last_capture_at).toLocaleTimeString();
          motionStatusEl.textContent = 'Motion detection: running • last auto-capture ' + when + '.';
        } else {
          motionStatusEl.textContent = 'Motion detection: running • watching for activity…';
        }

        if (data.last_capture_filename) {
          loadGallery(0);
        }
      } catch (e) {
        motionStatusEl.textContent = 'Motion detection: status unavailable.';
      }
    }
    refreshMotionStatus();
    setInterval(refreshMotionStatus, 5000);
  </script>
</body>
</html>
"""


def _validate_range(name, value, spec):
    """Coerce to float and clamp/validate against the slider's min/max."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{name}' must be a number.")
    if not (spec["min"] <= value <= spec["max"]):
        raise ValueError(f"'{name}' must be between {spec['min']} and {spec['max']}.")
    return value


def _roi_from_margins(top, bottom, left, right):
    """
    Convert independent top/bottom/left/right crop margins (each a percent
    of the frame to trim from that edge) into an rpicam-still --roi string
    'x,y,w,h' (fractions 0-1).

    All-zero margins -> full frame (0,0,1,1).
    """
    top_f, bottom_f, left_f, right_f = (v / 100.0 for v in (top, bottom, left, right))

    if left_f + right_f >= 1.0:
        raise ValueError("'left' + 'right' must be less than 100%.")
    if top_f + bottom_f >= 1.0:
        raise ValueError("'top' + 'bottom' must be less than 100%.")

    x = left_f
    y = top_f
    w = 1.0 - left_f - right_f
    h = 1.0 - top_f - bottom_f
    return f"{x:.3f},{y:.3f},{w:.3f},{h:.3f}"


class CaptureError(Exception):
    """Raised when a camera capture fails for any reason."""


def _capture_photo(resolution, top, bottom, left, right, contrast, saturation, ev, prefix="capture"):
    """
    Run the camera capture CLI and save a full-resolution JPEG to
    PICTURES_DIR. Returns (filename, output_path, roi). Raises CaptureError
    or ValueError (invalid crop combination) on failure.

    Shared by the manual /save route and the background motion-detection
    thread; CAMERA_LOCK ensures only one of them ever touches the camera
    at a time.
    """
    if CAMERA_CMD is None:
        raise CaptureError("No camera CLI found (rpicam-still / libcamera-still).")

    if resolution not in RESOLUTIONS:
        raise CaptureError(f"Unknown resolution '{resolution}'.")
    width, height = RESOLUTIONS[resolution]

    roi = _roi_from_margins(top, bottom, left, right)  # may raise ValueError

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{prefix}_{resolution}_{timestamp}.jpg"
    output_path = PICTURES_DIR / filename

    cmd = [
        CAMERA_CMD,
        "--width", str(width),
        "--height", str(height),
        "--autofocus-mode", "auto",
        "--autofocus-range", "macro",
        "--roi", roi,
        "--contrast", f"{contrast:.2f}",
        "--saturation", f"{saturation:.2f}",
        "--ev", f"{ev:.2f}",
        "--metering", "spot",
        "--output", str(output_path),
        "--timeout", "1000",   # ms of preview before capture
        "--nopreview",
    ]

    with CAMERA_LOCK:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            raise CaptureError("Camera capture timed out.")
        except FileNotFoundError:
            raise CaptureError(f"Camera command not found: {CAMERA_CMD}")

    if result.returncode != 0 or not output_path.exists():
        raise CaptureError(result.stderr.strip() or "Capture failed.")

    return filename, output_path, roi


# ---------------------------------------------------------------------------
# Motion detection (automatic, unattended capture)
# ---------------------------------------------------------------------------
# Runs as a background thread that starts automatically as soon as app.py
# starts (see start_motion_thread() near the bottom). It periodically grabs
# a small low-resolution snapshot of the same feeder area the sliders are
# framing, compares it against a running background image, and — once
# enough of the frame has changed for long enough — takes a full-resolution
# photo the same way the "Save Picture" button does, tagged with a
# "motion_" filename prefix so it's easy to tell apart from manual shots in
# the gallery. It can be paused/resumed from the web page at any time.

MOTION_POLL_INTERVAL_SECONDS = 2.0   # how often to check for motion
MOTION_FRAMES_REQUIRED = 2           # consecutive polls needed before saving
MOTION_PIXEL_DIFF_THRESHOLD = 25     # per-pixel grayscale change, 0..255
MOTION_BACKGROUND_ALPHA = 0.05       # how fast the background adapts when calm

MOTION_POLL_TMP = Path(tempfile.gettempdir()) / "mypicam_motion_poll.jpg"

_motion_thread = None
_motion_stop_event = threading.Event()
_motion_state = {
    "last_capture_at": None,       # ISO timestamp string, for the status line
    "last_capture_filename": None,
    "last_error": None,
}


def _sensitivity_to_min_area_fraction(sensitivity):
    """
    Map the 1-10 "Sensitivity" slider to a fraction of the analysis frame
    that must change before it counts as motion. 1 = least sensitive
    (15% of the frame), 10 = most sensitive (1% of the frame).
    """
    sensitivity = max(1.0, min(10.0, float(sensitivity)))
    high_frac, low_frac = 0.15, 0.01
    return high_frac - (sensitivity - 1) * (high_frac - low_frac) / 9.0


def _capture_motion_poll_frame(roi):
    """
    Grab a small grayscale snapshot of the feeder area for motion analysis.
    Returns a PIL Image, or None if the capture failed for any reason.
    """
    if CAMERA_CMD is None:
        return None

    cmd = [
        CAMERA_CMD,
        "--width", "320",
        "--height", "240",
        "--roi", roi,
        "--nopreview",
        "--timeout", "300",
        "--output", str(MOTION_POLL_TMP),
    ]

    with CAMERA_LOCK:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        except Exception:
            return None

    if result.returncode != 0 or not MOTION_POLL_TMP.exists():
        return None

    try:
        return Image.open(MOTION_POLL_TMP).convert("L")
    except Exception:
        return None


def motion_detection_loop():
    """Background loop: watch the feeder area and auto-save on motion."""
    background = None
    motion_frames = 0
    last_capture_monotonic = 0.0

    print("[motion] Background motion-detection thread started.")

    while not _motion_stop_event.is_set():
        settings = load_settings()

        if not settings.get("motion_enabled", False):
            background = None
            motion_frames = 0
            _motion_stop_event.wait(MOTION_POLL_INTERVAL_SECONDS)
            continue

        try:
            roi = _roi_from_margins(settings["top"], settings["bottom"], settings["left"], settings["right"])
        except ValueError:
            roi = "0.000,0.000,1.000,1.000"

        try:
            frame_img = _capture_motion_poll_frame(roi)
            if frame_img is None:
                _motion_stop_event.wait(MOTION_POLL_INTERVAL_SECONDS)
                continue

            frame_img = frame_img.filter(ImageFilter.GaussianBlur(radius=2))
            frame = np.asarray(frame_img, dtype=np.float32)

            if background is None:
                background = frame.copy()
                _motion_stop_event.wait(MOTION_POLL_INTERVAL_SECONDS)
                continue

            delta = np.abs(frame - background)
            changed_count = int(np.count_nonzero(delta > MOTION_PIXEL_DIFF_THRESHOLD))
            min_area_fraction = _sensitivity_to_min_area_fraction(settings.get("motion_sensitivity", 5))
            min_area_pixels = min_area_fraction * frame.size

            detected = changed_count >= min_area_pixels
            motion_frames = motion_frames + 1 if detected else 0

            now = time.monotonic()
            cooldown = settings.get("motion_cooldown", 20)

            if motion_frames >= MOTION_FRAMES_REQUIRED and (now - last_capture_monotonic) >= cooldown:
                try:
                    filename, _, _ = _capture_photo(
                        settings["resolution"],
                        settings["top"], settings["bottom"], settings["left"], settings["right"],
                        settings["contrast"], settings["saturation"], settings["ev"],
                        prefix="motion",
                    )
                    last_capture_monotonic = now
                    _motion_state["last_capture_at"] = datetime.datetime.now().isoformat()
                    _motion_state["last_capture_filename"] = filename
                    _motion_state["last_error"] = None
                    print(f"[motion] Saved {filename} (changed pixels={changed_count})")
                except (CaptureError, ValueError) as e:
                    _motion_state["last_error"] = str(e)
                    print(f"[motion] Capture failed: {e}")

                motion_frames = 0
                background = None  # force a fresh background after a capture
                _motion_stop_event.wait(1.0)
            else:
                if not detected:
                    background = background * (1 - MOTION_BACKGROUND_ALPHA) + frame * MOTION_BACKGROUND_ALPHA
                _motion_stop_event.wait(MOTION_POLL_INTERVAL_SECONDS)

        except Exception as e:
            # Never let an unexpected error kill the background thread.
            _motion_state["last_error"] = str(e)
            print(f"[motion] Unexpected error: {e}")
            _motion_stop_event.wait(MOTION_POLL_INTERVAL_SECONDS)

    print("[motion] Background motion-detection thread stopped.")


def start_motion_thread():
    """Start the motion-detection background thread if it isn't running."""
    global _motion_thread
    if _motion_thread is not None and _motion_thread.is_alive():
        return
    _motion_thread = threading.Thread(target=motion_detection_loop, daemon=True, name="motion-detect")
    _motion_thread.start()


@app.route("/")
def index():
    saved = load_settings()
    return render_template_string(
        PAGE,
        resolutions=list(RESOLUTIONS.keys()),
        specs=PARAM_SPECS,
        motion_specs=MOTION_SPECS,
        motion_deps_available=_MOTION_DEPS_AVAILABLE,
        saved=saved,
    )


@app.route("/save", methods=["POST"])
def save_picture():
    data = request.get_json(silent=True) or {}
    defaults = load_settings()

    resolution = data.get("resolution", defaults["resolution"])

    try:
        top = _validate_range("top", data.get("top", defaults["top"]), PARAM_SPECS["top"])
        bottom = _validate_range("bottom", data.get("bottom", defaults["bottom"]), PARAM_SPECS["bottom"])
        left = _validate_range("left", data.get("left", defaults["left"]), PARAM_SPECS["left"])
        right = _validate_range("right", data.get("right", defaults["right"]), PARAM_SPECS["right"])
        contrast = _validate_range("contrast", data.get("contrast", defaults["contrast"]), PARAM_SPECS["contrast"])
        saturation = _validate_range("saturation", data.get("saturation", defaults["saturation"]), PARAM_SPECS["saturation"])
        ev = _validate_range("ev", data.get("ev", defaults["ev"]), PARAM_SPECS["ev"])
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400

    try:
        filename, output_path, roi = _capture_photo(
            resolution, top, bottom, left, right, contrast, saturation, ev, prefix="capture"
        )
    except (CaptureError, ValueError) as e:
        status_code = 400 if isinstance(e, ValueError) else 500
        return jsonify(ok=False, error=str(e)), status_code

    return jsonify(ok=True, filename=filename, path=str(output_path), roi=roi)


GALLERY_PAGE_SIZE_DEFAULT = 10
GALLERY_PAGE_SIZE_MAX = 50


@app.route("/photos")
def list_photos():
    """
    List past captures (manual and motion-triggered), most recent first,
    paginated via ?offset=&limit=.
    """
    try:
        offset = int(request.args.get("offset", 0))
        limit = int(request.args.get("limit", GALLERY_PAGE_SIZE_DEFAULT))
    except ValueError:
        return jsonify(ok=False, error="'offset' and 'limit' must be integers."), 400

    if offset < 0:
        return jsonify(ok=False, error="'offset' must be >= 0."), 400
    if not (1 <= limit <= GALLERY_PAGE_SIZE_MAX):
        return jsonify(ok=False, error=f"'limit' must be between 1 and {GALLERY_PAGE_SIZE_MAX}."), 400

    files = [f for f in PICTURES_DIR.iterdir() if f.is_file()]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)  # most recent first

    total = len(files)
    page = files[offset:offset + limit]

    photos = [
        {"filename": f.name, "modified": datetime.datetime.fromtimestamp(f.stat().st_mtime).isoformat()}
        for f in page
    ]

    return jsonify(
        ok=True,
        photos=photos,
        offset=offset,
        limit=limit,
        total=total,
        has_more=(offset + limit) < total,
    )


@app.route("/settings", methods=["GET"])
def get_settings():
    return jsonify(ok=True, settings=load_settings())


@app.route("/settings", methods=["POST"])
def update_settings():
    data = request.get_json(silent=True) or {}
    try:
        validated = save_settings(data)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, settings=validated)


@app.route("/motion/status")
def motion_status():
    settings = load_settings()
    return jsonify(
        ok=True,
        deps_available=_MOTION_DEPS_AVAILABLE,
        enabled=settings.get("motion_enabled", False),
        thread_alive=bool(_motion_thread and _motion_thread.is_alive()),
        last_capture_at=_motion_state.get("last_capture_at"),
        last_capture_filename=_motion_state.get("last_capture_filename"),
        last_error=_motion_state.get("last_error"),
    )


@app.route("/preview/<filename>")
def preview(filename):
    return send_from_directory(PICTURES_DIR, filename)


if __name__ == "__main__":
    if CAMERA_CMD is None:
        print("WARNING: neither 'rpicam-still' nor 'libcamera-still' was found on PATH.")
        print("Install with: sudo apt install rpicam-apps")
    else:
        print(f"Using camera command: {CAMERA_CMD}")

    if not _MOTION_DEPS_AVAILABLE:
        print("WARNING: Pillow/NumPy not found — motion detection is disabled.")
        print("Install with: sudo apt install -y python3-pil python3-numpy")
    else:
        start_motion_thread()

    print(f"Pictures will be saved to: {PICTURES_DIR}")
    app.run(host="0.0.0.0", port=5000, debug=False)
