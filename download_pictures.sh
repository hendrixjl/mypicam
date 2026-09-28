#!/bin/sh

mkdir -p ~/Pictures/webcam_captures
rsync -rtvP \
  hendrixj@rp1aplus:/home/hendrixj/Pictures/webcam_captures/ \
  ~/Downloads/webcam_captures/
