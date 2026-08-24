# Pi Camera Capture Web GUI

A minimal Flask app with:
- A resolution dropdown: 640x480, 1640x1232, 1920x1080, 3280x2464
- A "Save Picture" button that triggers a capture

## How it works

The button click sends a fetch/POST request to `/save`, which shells out to
`rpicam-still` (the current Raspberry Pi OS camera CLI on Bookworm/Trixie) —
or falls back to the older `libcamera-still` name if that's what's on your
system — with `--width`/`--height` set from your dropdown choice. Captured
JPEGs are saved to `~/Pictures/webcam_captures/` with a resolution +
timestamp filename, and the page shows a preview of the last shot.

## Setup on the Raspberry Pi

1. Copy this folder to the Pi (e.g. via `scp` or a USB drive).
2. Make sure the camera CLI tools are installed (they're included by default
   on current Raspberry Pi OS):
   ```bash
   sudo apt update
   sudo apt install -y rpicam-apps python3-flask
   ```
   (If `python3-flask` isn't available via apt on your OS version, use
   `pip3 install flask --break-system-packages` or a virtualenv instead.)
3. Confirm the camera itself works first:
   ```bash
   rpicam-hello --list-cameras
   ```
4. Run the app:
   ```bash
   cd pi_camera_gui
   python3 app.py
   ```
5. From any device on the same network, browse to:
   ```
   http://<pi-ip-address>:5000
   ```
   (Find the Pi's IP with `hostname -I` on the Pi itself.)

## Running as a systemd service (survives reboots, auto git-pull)

This repo includes `run.sh` and `pi-camera-gui.service` to run the app as a
background service on boot. On each start, `run.sh` runs `git pull` first
(best-effort — if the Pi isn't online yet, or the pull fails for any reason,
it logs a warning and starts the app with whatever code is already on disk
instead of failing to boot).

1. Clone your GitHub repo onto the Pi, e.g.:
   ```bash
   cd ~
   git clone https://github.com/<you>/<your-repo>.git pi_camera_gui
   cd pi_camera_gui
   chmod +x run.sh
   ```

2. Edit `pi-camera-gui.service` if your username or clone path differs from
   `pi` / `/home/pi/pi_camera_gui` (check with `whoami` and `pwd`).

3. Install and enable the service:
   ```bash
   sudo cp pi-camera-gui.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable pi-camera-gui.service
   sudo systemctl start pi-camera-gui.service
   ```

4. Check status and logs:
   ```bash
   sudo systemctl status pi-camera-gui.service
   journalctl -u pi-camera-gui.service -f
   ```

5. To pick up new commits without a full reboot, just restart the service —
   it re-runs `git pull` on every start:
   ```bash
   sudo systemctl restart pi-camera-gui.service
   ```

**Note on the 1 A+ and boot timing:** the unit waits on
`network-online.target` before starting so the `git pull` has a chance to
succeed, but on Raspberry Pi OS this target only actually waits for the
network if the wait-online helper is enabled. If you notice `git pull` never
succeeding at boot, enable it once with:
```bash
sudo systemctl enable NetworkManager-wait-online.service   # Bookworm/Trixie (NetworkManager)
# or, on older releases using dhcpcd:
sudo systemctl enable dhcpcd.service
```
This isn't required for the app to work — it just affects whether the
auto-update-on-boot happens before or after the service starts.

**Private repos:** the commands above assume a public GitHub repo pulled
over HTTPS with no credentials. If your repo is private, set up a
[deploy key](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)
or a GitHub personal access token stored via `git credential-store`, so
`git pull` can run unattended without a password prompt.

## Notes

- This runs Flask's built-in dev server, which is fine for a personal LAN
  tool like this.
- If you're on an older Raspberry Pi OS (Bullseye or earlier) that only has
  `libcamera-still`, the app auto-detects and uses that instead — no changes
  needed.
- The 1640x1232 and 3280x2464 options match the Camera Module V2 (imx219)
  native sensor modes. If you're using a different camera module (HQ camera,
  Camera Module 3, etc.), those exact resolutions may not be native modes —
  they'll still work but may get scaled/cropped by the ISP. Let me know your
  camera model if you want the dropdown tuned to its native modes.
- The Pi 1 A+'s single-core ARM11 CPU means a still capture at the highest
  resolution (3280x2464, ~8MP) may take several seconds. The app already
  waits up to 15 seconds for the capture process before reporting a timeout
  — if you still see timeouts at the highest resolution, increase the
  `timeout=15` value in `app.py`'s `subprocess.run(...)` call.
