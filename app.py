#!/usr/bin/env python3
"""
Simple web GUI to capture a still photo from the Raspberry Pi camera.

- Dropdown to pick a resolution
- Sliders for zoom/crop (ROI), contrast, saturation, and EV compensation
- "Save Picture" button that triggers a capture on the server side
- Uses the current `rpicam-still` CLI (Bookworm/Trixie), falling back to the
  older `libcamera-still` name if that's what's installed.

Run:
    python3 app.py
Then browse to:
    http://<pi-ip-address>:5000
"""

import json
import shutil
import subprocess
import datetime
from pathlib import Path

from flask import Flask, request, jsonify, render_template_string, send_from_directory

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

# Prefer the current command name; fall back to the legacy one.
CAMERA_CMD = shutil.which("rpicam-still") or shutil.which("libcamera-still")

# Persisted "defaults" (resolution + slider values) so the last-saved settings
# are reloaded automatically the next time the app starts. Kept alongside
# app.py but NOT meant to be committed to git (add it to .gitignore) since
# it's per-device/user state, not code.
SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"

BUILTIN_DEFAULTS = {
    "resolution": next(iter(RESOLUTIONS)),
    **{key: spec["default"] for key, spec in PARAM_SPECS.items()},
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

    for key in PARAM_SPECS:
        if key in saved:
            try:
                settings[key] = _validate_range(key, saved[key], PARAM_SPECS[key])
            except ValueError:
                pass  # keep the built-in default for this one field

    return settings


def save_settings(data):
    """Validate incoming settings and atomically persist them to disk."""
    resolution = data.get("resolution")
    if resolution not in RESOLUTIONS:
        raise ValueError(f"Unknown resolution '{resolution}'.")

    validated = {"resolution": resolution}
    for key in PARAM_SPECS:
        validated[key] = _validate_range(key, data.get(key, PARAM_SPECS[key]["default"]), PARAM_SPECS[key])

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
        cap.textContent = photo.filename;
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


@app.route("/")
def index():
    saved = load_settings()
    return render_template_string(PAGE, resolutions=list(RESOLUTIONS.keys()), specs=PARAM_SPECS, saved=saved)


@app.route("/save", methods=["POST"])
def save_picture():
    if CAMERA_CMD is None:
        return jsonify(ok=False, error="No camera CLI found (rpicam-still / libcamera-still)."), 500

    data = request.get_json(silent=True) or {}
    defaults = load_settings()

    resolution = data.get("resolution", defaults["resolution"])
    if resolution not in RESOLUTIONS:
        return jsonify(ok=False, error=f"Unknown resolution '{resolution}'."), 400
    width, height = RESOLUTIONS[resolution]

    try:
        top = _validate_range("top", data.get("top", defaults["top"]), PARAM_SPECS["top"])
        bottom = _validate_range("bottom", data.get("bottom", defaults["bottom"]), PARAM_SPECS["bottom"])
        left = _validate_range("left", data.get("left", defaults["left"]), PARAM_SPECS["left"])
        right = _validate_range("right", data.get("right", defaults["right"]), PARAM_SPECS["right"])
        contrast = _validate_range("contrast", data.get("contrast", defaults["contrast"]), PARAM_SPECS["contrast"])
        saturation = _validate_range("saturation", data.get("saturation", defaults["saturation"]), PARAM_SPECS["saturation"])
        ev = _validate_range("ev", data.get("ev", defaults["ev"]), PARAM_SPECS["ev"])
        roi = _roi_from_margins(top, bottom, left, right)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"capture_{resolution}_{timestamp}.jpg"
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

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15
        )
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="Camera capture timed out."), 500
    except FileNotFoundError:
        return jsonify(ok=False, error=f"Camera command not found: {CAMERA_CMD}"), 500

    if result.returncode != 0 or not output_path.exists():
        return jsonify(ok=False, error=result.stderr.strip() or "Capture failed."), 500

    return jsonify(ok=True, filename=filename, path=str(output_path), roi=roi)


GALLERY_PAGE_SIZE_DEFAULT = 10
GALLERY_PAGE_SIZE_MAX = 50


@app.route("/photos")
def list_photos():
    """
    List past captures, most recent first, paginated via ?offset=&limit=.
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


@app.route("/preview/<filename>")
def preview(filename):
    return send_from_directory(PICTURES_DIR, filename)


if __name__ == "__main__":
    if CAMERA_CMD is None:
        print("WARNING: neither 'rpicam-still' nor 'libcamera-still' was found on PATH.")
        print("Install with: sudo apt install rpicam-apps")
    else:
        print(f"Using camera command: {CAMERA_CMD}")
    print(f"Pictures will be saved to: {PICTURES_DIR}")
    app.run(host="0.0.0.0", port=5000, debug=False)
