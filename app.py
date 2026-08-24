#!/usr/bin/env python3
"""
Simple web GUI to capture a still photo from the Raspberry Pi camera.

- Dropdown to pick a resolution
- "Save Picture" button that triggers a capture on the server side
- Uses the current `rpicam-still` CLI (Bookworm/Trixie), falling back to the
  older `libcamera-still` name if that's what's installed.

Run:
    python3 app.py
Then browse to:
    http://<pi-ip-address>:5000
"""

import shutil
import subprocess
import datetime
from pathlib import Path

from flask import Flask, request, jsonify, render_template_string

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

# Prefer the current command name; fall back to the legacy one.
CAMERA_CMD = shutil.which("rpicam-still") or shutil.which("libcamera-still")

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
    margin: 60px auto;
    padding: 0 20px;
    text-align: center;
  }
  h1 {
    font-size: 1.4rem;
    margin-bottom: 1.5rem;
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
  button {
    background: #2563eb;
    color: white;
    border: none;
    cursor: pointer;
    font-weight: 600;
  }
  button:disabled {
    background: #93b4f0;
    cursor: default;
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
</style>
</head>
<body>
  <h1>Raspberry Pi Camera Capture</h1>

  <label for="resolution">Resolution</label>
  <select id="resolution">
    {% for r in resolutions %}
    <option value="{{ r }}">{{ r }}</option>
    {% endfor %}
  </select>

  <button id="saveBtn">Save Picture</button>

  <div id="status"></div>
  <img id="preview" alt="preview of last capture">

  <script>
    const btn = document.getElementById('saveBtn');
    const status = document.getElementById('status');
    const preview = document.getElementById('preview');

    btn.addEventListener('click', async () => {
      const resolution = document.getElementById('resolution').value;
      btn.disabled = true;
      status.textContent = 'Capturing...';
      status.className = '';
      preview.style.display = 'none';

      try {
        const resp = await fetch('/save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ resolution })
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
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE, resolutions=list(RESOLUTIONS.keys()))


@app.route("/save", methods=["POST"])
def save_picture():
    if CAMERA_CMD is None:
        return jsonify(ok=False, error="No camera CLI found (rpicam-still / libcamera-still)."), 500

    data = request.get_json(silent=True) or {}
    resolution = data.get("resolution")
    if resolution not in RESOLUTIONS:
        return jsonify(ok=False, error=f"Unknown resolution '{resolution}'."), 400

    width, height = RESOLUTIONS[resolution]
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"capture_{resolution}_{timestamp}.jpg"
    output_path = PICTURES_DIR / filename

    cmd = [
        CAMERA_CMD,
        "--width", str(width),
        "--height", str(height),
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

    return jsonify(ok=True, filename=filename, path=str(output_path))


@app.route("/preview/<filename>")
def preview(filename):
    from flask import send_from_directory
    return send_from_directory(PICTURES_DIR, filename)


if __name__ == "__main__":
    if CAMERA_CMD is None:
        print("WARNING: neither 'rpicam-still' nor 'libcamera-still' was found on PATH.")
        print("Install with: sudo apt install rpicam-apps")
    else:
        print(f"Using camera command: {CAMERA_CMD}")
    print(f"Pictures will be saved to: {PICTURES_DIR}")
    app.run(host="0.0.0.0", port=5000, debug=False)
