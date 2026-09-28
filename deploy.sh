#!/bin/sh

sudo cp pi-camera-gui.service /etc/systemd/system
sudo systemctl daemon-reload
sudo systemctl enable --now pi-camera-gui.service
echo "Check status of pi-camera-gui service via systemctl status pi-camera-gui.service"
