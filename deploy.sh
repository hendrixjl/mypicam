#!/bin/sh

echo sudo apt-get install -y python3-flask
sudo apt-get install -y python3-flask
echo sudo apt install -y python3-pil python3-numpy
sudo apt install -y python3-pil python3-numpy
echo sudo cp pi-camera-gui.service /etc/systemd/system
sudo cp pi-camera-gui.service /etc/systemd/system
echo sudo systemctl daemon-reload
sudo systemctl daemon-reload
echo sudo systemctl enable --now pi-camera-gui.service
sudo systemctl enable --now pi-camera-gui.service
systemctl status pi-camera-gui.service
