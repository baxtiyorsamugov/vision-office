#!/bin/sh
# Publish one recording as a live RTSP camera. mediamtx sets MTX_PATH/RTSP_PORT.
set -eu
name="${MTX_PATH:?}"
port="${RTSP_PORT:-8554}"
dir="${REPLAY_DIR:-/recordings}"
file=""
for candidate in "$dir/$name".*; do
    [ -f "$candidate" ] && { file="$candidate"; break; }
done
if [ -z "$file" ]; then
    echo "replay: no recording named $name in $dir" >&2
    exit 1
fi
loop="-stream_loop -1"
marker="/tmp/replay-played-$name"
if [ "${REPLAY_LOOP:-0}" != "1" ] && [ -f "$marker" ]; then
    # Play once: a reconnect must not replay the file (the report would count people twice).
    echo "replay: /$name already played once (restart the bench to play again)" >&2
    exec sleep 2147483647
fi
# Start only when THIS camera's worker can recognise: with several cameras the
# models need 40-80 s to load and the first part of the recording would be lost.
camera_id="replay_$(echo "$name" | tr 'A-Z' 'a-z')"
status="/status/runtime_status_${camera_id}.json"
started=$(date +%s)
waited=0
while [ "$waited" -lt "${REPLAY_READY_TIMEOUT:-600}" ]; do
    if [ -f "$status" ] && [ "$(stat -c %Y "$status")" -ge "$started" ] && grep -Eq '"ai_ready": *true' "$status"; then
        break
    fi
    sleep 1
    waited=$((waited + 1))
done
echo "replay: /$name worker ready after ${waited}s" >&2
sleep "${REPLAY_START_DELAY:-2}"
if [ "${REPLAY_LOOP:-0}" != "1" ]; then
    touch "$marker"
    loop=""
fi
echo "replay: streaming $file as /$name (loop=${REPLAY_LOOP:-0})" >&2
if [ "${REPLAY_TRANSCODE:-0}" = "1" ]; then
    # Fallback for files whose codec/timestamps do not survive stream copy.
    exec ffmpeg -hide_banner -loglevel warning -re $loop -fflags +genpts -i "$file" \
        -an -c:v libx264 -preset veryfast -tune zerolatency -g 50 -pix_fmt yuv420p \
        -f rtsp -rtsp_transport tcp "rtsp://127.0.0.1:$port/$name"
fi
# Default: send the original H.264/H.265 bitstream unchanged, like the camera did.
exec ffmpeg -hide_banner -loglevel warning -re $loop -fflags +genpts -i "$file" \
    -an -c:v copy -f rtsp -rtsp_transport tcp "rtsp://127.0.0.1:$port/$name"
