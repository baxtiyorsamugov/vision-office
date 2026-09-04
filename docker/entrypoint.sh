#!/bin/sh
set -eu

# Ultralytics checks this directory during import but does not reliably create
# its parent directory in a fresh Linux container.
if [ -n "${YOLO_CONFIG_DIR:-}" ]; then
    mkdir -p "$YOLO_CONFIG_DIR/Ultralytics"
fi

exec "$@"
