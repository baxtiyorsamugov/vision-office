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
are not modified. Video playback is the default, with boxes synchronized to the last
analysed frame using requestVideoFrameCallback (animation-frame fallback). Video
plays at its native cadence; boxes still update at the analysis sampling rate.
There is no invented tracking, interpolated identity or new inference between samples.
Boxes disappear while seeking or if the observation is more than one sampling interval
(plus 20 ms rounding tolerance) old. Exact analysed frames, a filmstrip, latency
statistics and manual review remain available. Pause video to enter a review label;
labels refer to the displayed analysis timestamp, not an unanalysed video frame.
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

### Paired ROI experiment

`tools/compare_video_roi.py` compares full-frame detection against detection on an
unscaled source ROI, using the SAME sampled frames, immutable gallery snapshot,
YOLO input size and recognition thresholds. It never connects to the database.
The 48px gate and FaceID crop padding use original pixels for both variants.
Only boxes completely inside the specified ROI (2px border margin) are counted in
BOTH variants, to avoid comparing a crowded full background to an empty crop.
Boundary detections are excluded; counts are not recall and detections are not
matched to ground-truth identities. A higher count can include false positives.

Example for `kirish.mp4`, ROI x1=800, y1=320, x2=1920, y2=1440:

```powershell
.\.venv\Scripts\python.exe tools/compare_video_roi.py --input video/kirish.mp4 --gallery data/exports/gallery.json --roi 800 320 1920 1440 --preview data/exports/kirish-full-timed.mp4 --roi-preview data/exports/kirish-roi-timed.mp4 --output data/exports/roi-comparison-new --device cuda
```

Use an existing **full-frame H.264 preview from the same source clip** for `--preview`.
Both previews must preserve the original frame count/FPS; the tool checks timing
metadata and aspect ratio, not pixel provenance. They are used ONLY for playback;
inference always reads `--input`. CFR conversion or trimmed previews may fail this
check. For variable-timestamp recordings, create new previews with ffmpeg
`setpts=N/(FPS*TB)` and `-r FPS -fps_mode cfr`, using the original OpenCV FPS;
apply `scale=1280:-2` for the full preview and `crop=1120:1120:800:320` for the ROI.
This deliberately conforms playback to the analysis frame-index/FPS timeline,
not the source's wall-clock timestamps. Recheck frame counts after transcoding.
The same frame-index/FPS timestamp limitations as the normal report apply.

Each pair alternates A/B execution order after warming both detector shapes. The
reported pipeline timing includes detection, FaceID and matching but excludes
decoding, saved JPEG frames and transcoding; it is not a live-camera throughput
benchmark. CPU/GPU contention still affects timing. The report includes input,
gallery and detector hashes. Switching variants keeps the review time; the full
view marks the comparison zone. A crop may improve detector detail but cannot
recover information absent from the original or repair missing references.

Focused tests: `python -m unittest discover -s tests -p test_video_roi.py` and
`node tests/video_report.cjs` with jsdom available.

### Read-only reference photo audit

```powershell
docker compose run --rm --no-deps --entrypoint python -v "${PWD}:/offline:ro" ui /offline/tools/audit_reference_photos.py --output /app/data/exports/reference-quality-new
```

Run from the project root with its existing database and detection model. The audit
uses one CPU thread, reads active EduSchool references of active people with a
database-enforced read-only transaction, and never runs recognition matching,
enrollment, migrations, downloads, attendance or outbox services. It excludes local
employees and legacy ERP. Photos must resolve within the project's data directory.
Unreadable/oversize images are flagged; a detector runtime failure stops the audit.

The new directory contains HTML, JSON and local thumbnails. Name/category/status
filters and pagination allow inspection. Heuristics flag small faces, low-detail
images, exposure extremes, face-edge contact and non-single-face detections. Metrics
are calculated from source images, not thumbnails. Sharpness is Laplacian variance
on a 112x112 grayscale face crop. Thresholds are triage heuristics, not calibrated
quality scores or evidence that a particular video miss was caused by its reference.
No enrollment photo, embedding, recognition threshold or API setting is changed.

To link an audit, a video report's optional `reference_quality` field accepts a local
path shaped `../reference-quality-<name>/index.html`; regenerate its HTML afterwards.
Preserve `created_at` and sample order to retain existing manual-label keys.

Focused checks: `python -m unittest discover -s tests -p test_reference_quality.py`
and `python -m unittest discover -s tests -p test_offline_video.py`.
Browser logic tests: `node tests/video_report.cjs` with jsdom available in NODE_PATH.

### Missing student references

The catalog alone is not a FaceID gallery. If students have source-photo URLs but
no references, take a database backup and use the bounded enrollment command:

```powershell
docker compose run --rm --no-deps --entrypoint python -v "${PWD}:/offline:ro" ui /offline/tools/enroll_eduschool_photos.py --person-type student
docker compose run --rm --no-deps --entrypoint python -v "${PWD}:/offline:ro" ui /offline/tools/enroll_eduschool_photos.py --person-type student --apply --output /app/data/exports/student-enrollment.json
```

The first command is a dry run. The second uses CPU by default and requires a new
output path. It enrolls up to 1,000 active, pending or due-for-retry students with
source URLs and no active reference. It excludes missing photos, inactive records,
invalid-photo cases and already enrolled people. Existing single-face validation,
disabled-photo checks and cross-person conflict rejection remain in force. Photos
are downloaded from the configured source backend; no attendance is sent and no
delivery settings are changed. Enrollment DOES add photos/embeddings to the local
gallery used by live recognition. Run without a concurrent photo-enrollment worker.

Use `--limit` to bound a run. For an explicitly provisioned GPU container, the same
command can use service `vision-worker` with `--device cuda`; this launches only the
enrollment script, not the live worker. Ensure GPU memory headroom first. A failed
CUDA initialization can fall back to CPU, recorded in the result. Refresh the gallery
snapshot and generate a new video report after enrollment; old reports remain unchanged.

### Measurement limits

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
