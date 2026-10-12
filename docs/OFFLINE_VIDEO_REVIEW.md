# Offline camera recording review

This tool analyses local recordings without attendance events, outbox entries,
photo enrollment, migrations, or API POSTs. The live camera workers are not used.
No models or reference photos are downloaded. Existing approved weights are required.

## Windows / Docker

Put recordings in `video` and run from the repository directory:

```powershell
.\tools\test_recordings.ps1
```

Docker Desktop and the existing PostgreSQL container must be running. The script
does not start or rebuild production services. It mounts the current code and
recordings read-only, exports a read-only gallery snapshot, and runs CPU analysis
in a separate container using the existing UI image. CPU work shares host resources;
run outside busy periods. This is intentionally not an automatic GPU stress test on
the 4 GB production card. `-VideoDirectory`, `-SampleFps`, and `-ImageSize` are optional.

Open the resulting `data/exports/video-review-<timestamp>/index.html` in a browser.
Keep its `media` folder beside it. Everything is local: scripts, videos, frames and
report data. No web server or CDN is needed. H.264 previews are silent; originals
are not modified. The report provides exact analysed frames with overlays,
unannotated video playback, a filmstrip, latency statistics and manual review.
Review labels are stored per report in the browser when storage is available;
export JSON to preserve them independently of the browser.

## Native analysis

With the project Python dependencies and models installed:

```powershell
.\.venv\Scripts\python.exe tools/analyze_videos.py --export-gallery data/exports/gallery.json
.\.venv\Scripts\python.exe tools/analyze_videos.py --gallery data/exports/gallery.json --input video --output data/exports/review --device cpu
```

The first command needs access to the correct existing database. Use the Docker
launcher when PostgreSQL is only reachable inside Docker. `--device cuda` explicitly
requests CUDA for YOLO and attempts CUDA for FaceID; the report records actual FaceID
device per video. Concurrent workloads affect latency. `--ffmpeg <executable>` creates
browser-compatible previews; without it, original videos are copied and codec support
depends on the browser. Exact analysed JPEG frames remain available either way.

`--render-only --output <existing-report>` regenerates HTML without inference.
`--prepare-media --output <existing-report>` creates silent H.264 copies using ffmpeg
and updates report links without rerunning inference. These modes never open the DB.

## Interpretation and privacy

- Sampling defaults to 5 frames per second with YOLO image size 960. Frame positions
  use frame index / declared source FPS; variable-rate or discontinuous footage needs
  timestamp-aware preprocessing before timing conclusions. Times are relative to the
  clip, not attendance timestamps. The filename date is not treated as capture date.
- Detection uses the existing YOLO weights and prediction defaults. Recognition uses
  `FaceRecognizer`, the production 48 px minimum, crop padding, target-center selection,
  identity matching helper and configured thresholds. No threshold is relaxed.
- This evaluates every eligible face independently. It does NOT reproduce live
  ByteTrack, two-observation stabilization, three-face scheduling, cooldowns, cache
  refresh, inference queues or API delivery. Performance and decisions may therefore
  differ from the live worker. Latencies exclude warm-up; total analysis time includes
  decoding, matching and saving JPEG frames, but excludes video transcoding.
- Gallery scope is active EduSchool references plus local employees. Retired/legacy
  ERP references are intentionally excluded. The snapshot records active catalog
  counts separately from enrolled identities. Missing local student references mean
  student names cannot be assessed on that machine.
- Counts are face observations, not distinct visitors, attendance or accuracy. Repeated
  appearances count repeatedly. Unique matched IDs are predictions, not verified people.
  Manual labels review displayed detections; false-negative detection metrics require
  separate annotation of missed faces. No accuracy percentage is fabricated.
- Partial runs preserve completed clips. A decode-count mismatch is reported, but a
  complete count does not prove absence of timestamp discontinuities or corrupt frames.
- Gallery JSON contains sensitive embeddings. HTML/JSON/JPEG/video outputs contain
  sensitive personal data. Keep them local, do not commit or upload them, and apply
  your retention/access policy. `video/` and `data/exports/` are Git-ignored.
