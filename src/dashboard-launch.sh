#!/bin/sh
# Limit private dependency paths to Dashboard and its Python process.
LIB=/usr/lib/mudi7-customizer
HERE=$(dirname "$0")
export PYTHONPATH="$LIB/python" LD_LIBRARY_PATH="$LIB/lib"
export PYTHONDONTWRITEBYTECODE=1
if [ "$#" -eq 1 ] && [ "$1" = --check ]; then
    exec /usr/bin/python3 -c 'import numpy; from PIL import Image, ImageDraw, ImageFont'
fi
exec /usr/bin/python3 "$HERE/dashboard.py" "$@"
