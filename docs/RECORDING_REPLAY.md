# Replay recordings as live RTSP cameras

Use this to watch recognition on recorded camera footage in real time, with the
same code path as production: RTSP reader, YOLO detector, tracker, FaceID
scheduling, thresholds and event recording. Unlike `tools/test_recordings.ps1`
(offline frame sampling), this runs the real `main.py` camera worker.

## Isolation

- Separate Compose project `vision-replay` (`docker-compose.replay.yml`). Production
  containers are never stopped, rebuilt or reconfigured.
- Separate PostgreSQL volume, restored from a `pg_dump` snapshot of production
  (the gallery and thresholds are identical). Replay events never reach the
  production database.
- No edge-sync, EduSchool catalog/photo/turnstile or unknown-clusterer services.
  The worker and database are on an `internal` Docker network without Internet.
  The generated settings additionally disable the turnstile sender.
- Generated files live in `replay/runtime/` (Git- and Docker-ignored).
- Camera code is mounted read-only from the working tree, so a recognition change
  can be tried here first and production keeps running its built image.

## Use

1. Put recordings in `video\` (or pass `-VideoDirectory`). File names: Latin letters,
   digits, `-`, `_` (for example `kirish.mp4`, `chiqish1.mp4`). Names containing
   `chiqish`, `exit` or `out` become exit cameras. Other names are skipped with a warning.
2. Start: `.\tools\replay_recordings.ps1` (CPU) or `-Device cuda` (only if at least
   1500 MiB VRAM is free; the production worker shares the card).
   `-RefreshDb` takes a new snapshot of this PC's database; `-Snapshot <file>` uses a
   `pg_dump -Fc` file copied from another PC (for example the work PC gallery on a
   developer PC); `-Transcode` re-encodes files whose stream copy fails. Thresholds
   are pinned to the final production values (EduSchool 0.50, edge 0.40) even if
   this PC's settings.yaml is older.
3. Watch `http://127.0.0.1:8502` (production stays on `:8501`). Any stream can also
   be opened in VLC: `rtsp://127.0.0.1:8554/<file-name>`.
4. Report: `.\tools\replay_recordings.ps1 -Action Report [-Minutes 30]` opens an
   HTML page of recognised faces and misses. For each unknown face the three closest
   gallery people are listed: `near-miss` (within 0.10 below threshold), `weak`
   (0.30..near-miss) or `far` (below 0.30, outsider or unusable face).
5. `-Action Logs`, `-Action Stop` (keeps the replay DB) and `-Action Purge`.

Recordings loop forever, in real time. A reconnect restarts the file from the
beginning. Counts are observations, not people; the same person can appear in
several loops.

## Live window (smooth real-time view)

The dashboard preview on :8502 is a file snapshot capped at a few FPS, so it looks
choppy. The bench also serves every annotated frame (boxes + names, up to 25 FPS,
1920 px, JPEG 90) as MJPEG: camera 1 on http://127.0.0.1:8090, camera 2 on :8091, ...
`Start` opens them automatically as separate Edge app windows; reopen with
`.\tools\replay_recordings.ps1 -Action View`. Only the bench sets
`VISION_OFFICE_LIVE_VIEW_PORT`; production never starts this server. The worker
stays on the internal network; `replay-view` relays only these ports to localhost.

## Honest statistics and near-miss review (2026-11-04)

* Each recording now plays **once** by default (`-Loop` to loop for watching). A
  looped one-minute clip counted the same people ten times.
* `-Action Report` counts only the latest run and starts with a **per-person** table:
  a person is the recognised identity or the best candidate of a near-miss/weak miss;
  `far` faces are not attributed to anyone. "Min. margin" is the smallest gap between
  the best and second-best person on near-miss frames (large gap = no confusion).
  `-AllRuns` includes earlier runs.
* `-Action Review` exports the bench results like a work-PC export (replay dump +
  events + persons) and runs `tools/build_similarity_review.py` with
  `--min-similarity 0.40`, giving the usual review page. Decisions go through
  `build_reference_bundle.py` and `import_reviewed_photos.py` on the work PC as before.
  Reference photos come from the newest `data/imports/review-*/persons` (or `-PersonsDir`).

## Experiments: trace, knobs and Compare

Every recognition attempt on the bench is appended to
`replay/runtime/data/recognition_trace/<camera>.jsonl` (`VISION_OFFICE_RECOGNITION_TRACE`):
time, track, crop size, best and second-best *different* person with scores, the
decision (`known`, `unknown`, `low-margin`, `no-embedding`), the unit embedding and,
for scores ≥ 0.35, the crop image. `-Action Report` adds a track-voting simulation
from this trace. Production never sets the variable.

Experiment knobs (production defaults in brackets), passed to `Start`/`Compare`:

| Parameter | Env var | Meaning |
| --- | --- | --- |
| `-UnknownRetrySec` (1.5) | `VISION_OFFICE_UNKNOWN_RETRY_SEC` | Retry pace for unknown tracks |
| `-FaceDetThresh` (0.5) | `VISION_OFFICE_FACE_DET_THRESH` | InsightFace detector threshold on the crop |
| `-MinMargin` (0; prod compose 0.05) | `VISION_OFFICE_MIN_MATCH_MARGIN` | Required gap to the best other person |

`-Action Compare` plays every recording alone, first with the production profile
(retry 1.5 s, margin 0.05), then with the candidate (`-CandidateRetrySec`,
`-CandidateMinMargin`), waits until each stream ends, and writes
`replay/runtime/data/exports/replay-compare-*.html`: recognised people per run,
people recognised only in one profile, matches blocked by the guard, attempts per
track and the share of attempts without an embedding. `-ProdOnly` runs only the
production profile; `-Repeat N` repeats each pair to expose run-to-run noise.

A change is accepted only after every person recognised only by the candidate and
every match it blocks has been checked by a human (see CLAUDE.md, "Changing
recognition", and DECISIONS.md).
