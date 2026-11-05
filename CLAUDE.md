# CLAUDE.md — working on Vision Office

Guide for AI assistants and engineers changing this repository. Read it before
touching recognition, delivery or data code. Architecture details live in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); the reasoning behind tuned numbers
lives in [docs/DECISIONS.md](docs/DECISIONS.md).

## What this system is

Vision Office is an on-premise face-recognition attendance system for a school
(learning center). RTSP cameras at the entrance/exit feed a YOLO face detector
and an InsightFace (ArcFace, 512-d) recognizer; recognised people become local
recognition events, and eligible staff/student events are delivered to the
EduSchool attendance API. Everything runs in Docker on one Windows PC with an
NVIDIA GPU. Operators use a Streamlit dashboard in Russian.

## Golden rules

1. **Production is a real school.** A wrong match marks a child present (parents
   may get a push/SMS). Prefer a missed recognition over a false one.
2. **Never write to the production database or send to EduSchool from tests,
   experiments or the replay bench.** Experiments run on the isolated replay bench
   (`docker-compose.replay.yml`), which has no Internet and no senders.
3. **Do not change recognition thresholds, guards or retry timing without
   evidence:** a replay-bench `Compare` run *and* a human check of every
   new/removed match (see "Changing recognition" below). Record the result in
   `docs/DECISIONS.md`.
4. **Reference photos are the main lever and the main risk.** A bad reference
   (top-down head, headscarf only, someone else) causes false matches for other
   people. New references must pass the consistency check in
   `core/face_labeling.py`.
5. **Never commit secrets or personal data:** `config/settings.yaml`, `.env`,
   `data/` (photos, dumps, exports, events), `video/`, model weights. They are
   git-ignored; keep it that way.
6. **Database changes are additive and idempotent.** New tables come from
   `Base.metadata.create_all`; new columns go into `database/migrations.py`.
   Never drop or rewrite production data in a migration; deactivate instead
   (`active = false`), which is reversible.
7. **Nothing slow in the camera loop.** HTTP, disk-heavy work and delivery run in
   separate services/outboxes; the frame loop must never wait on them.
8. **Each camera is isolated.** A crash or RTSP stall in one camera process must
   not affect the others, the health checker or delivery.

## Repository map

| Path | Purpose |
| --- | --- |
| `main.py` | Camera worker entrypoint; one spawned process per camera via `core/supervisor.py`. |
| `core/ai/engine.py` | Detection thread (YOLO + ByteTrack), FaceID worker process, matching, thresholds, retry timing. |
| `core/ai/recognizer.py` | InsightFace `buffalo_l` (detection + ArcFace recognition), CUDA with CPU fallback. |
| `core/ai/cache.py` | Gallery cache reloaded from the database every 5 s (new references apply without restart). |
| `core/ai/trace.py` | Per-attempt recognition trace; enabled only on the replay bench. |
| `core/events.py` | `RecognitionEventStore`: dedup (cooldown), event photos, 30-day photo retention. |
| `core/eduschool/` | EduSchool catalog sync, source-photo enrollment, local reference photos, turnstile attendance sender. |
| `core/edge/` | Legacy Learning-Center ERP edge sync and access-log outbox. |
| `core/face_labeling.py` | «Разметка» page logic: queue, quality gate, consistency check, audit log, undo. |
| `core/unknown_visitors.py` | Clusters unknown faces into visitor cards (low-priority service). |
| `core/health.py` | Health checker, incidents, Telegram outbox, external watchdog ping. |
| `core/overlay.py`, `core/preview.py`, `core/live_view.py` | Frame labels, bounded dashboard previews, bench-only MJPEG live view. |
| `database/` | SQLAlchemy models, engine factory, additive migration runner. |
| `ui/` | Streamlit dashboard (`ui/app.py`, pages in `ui/*.py`, theme in `ui/theme.css`). |
| `api/server.py` | Read-only FastAPI (status, employees, attendance, events, unknown visitors, incidents). |
| `tools/` | Operator/maintenance scripts (photo review pages, imports, audits, purges). |
| `tools/replay/`, `replay/`, `tools/replay_recordings.ps1` | Replay bench: recordings → RTSP → isolated copy of the system. |
| `tools/sql/` | Reviewed one-off SQL for production data fixes (with backup steps in the commit/docs). |
| `tests/` | `unittest` suite (SQLite, mocked HTTP/RTSP). |
| `docs/` | Architecture, decisions and runbooks — index in [docs/README.md](docs/README.md). |

## Environments

| | Development PC | Production (work) PC |
| --- | --- | --- |
| Path | `C:\projects\acs2` | `E:\vision-office` |
| GPU | RTX A1000 Laptop, 4 GB (bench: one camera at a time) | RTX 3070 |
| Cameras | none live; recordings in `video\` | `reception_01`, `camera_4e0397cf`, `exit_01`, `Chiqish_1` |
| Database | local Docker PostgreSQL + snapshots under `data\imports\review-*` | Docker PostgreSQL volume `vision-office-postgres` |
| Dashboard | bench on http://127.0.0.1:8502 | http://127.0.0.1:8501 |

Production data reaches the development PC only as exports
(`pg_dump -Fc` + `data/events` + `data/persons`) under `data/imports/review-YYYYMMDD/`.
Code moves through GitHub (`git pull` on the work PC, then `.\start_vision_office.ps1`).

## Commands

```powershell
# Tests (Windows venv)
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m unittest tests.test_face_labeling      # focused

# Production stack
.\start_vision_office.ps1                 # build/start, probes GPU, falls back to CPU
docker compose ps
docker compose logs --tail 100 vision-worker

# Replay bench (isolated; see docs/RECORDING_REPLAY.md)
.\tools\replay_recordings.ps1 -Snapshot data\imports\review-YYYYMMDD\vo-MMDD.dump -Device cuda -Camera kirish
.\tools\replay_recordings.ps1 -Action Report      # per-person table, voting simulation
.\tools\replay_recordings.ps1 -Action Compare -Device cuda -ProdOnly -Repeat 2
.\tools\replay_recordings.ps1 -Action Review      # near-miss photo-review page
.\tools\replay_recordings.ps1 -Action Stop
```

Many tests import the real models or PostgreSQL and skip when they are missing;
"N passed, M skipped" is normal on a machine without weights.

## Recognition invariants (current production values)

| Setting | Value | Where |
| --- | --- | --- |
| EduSchool match threshold | 0.50 | `config/settings.yaml` → `eduschool_catalog.recognition_threshold` |
| Edge/legacy threshold | 0.40 | `edge_integration.recognition_threshold` |
| Min gap to best *other* person | 0.05 | `VISION_OFFICE_MIN_MATCH_MARGIN` in `docker-compose.yml` |
| Unknown-face retry | 1.5 s | `VISION_OFFICE_UNKNOWN_RETRY_SEC` (default) |
| Detector | YOLOv8n-face, imgsz 960, 20 det/s, ByteTrack | `ai` section, `core/ai/engine.py` |
| Faces sent to FaceID per detection | max 3, min box 48 px, 2 track observations | `core/ai/engine.py` constants |
| Event cooldown | 60 s per person/track per camera | `edge_integration.event_cooldown_seconds` |
| Reference limit | 15 local photos per person | `local_reference_photo_limit` |

Why each value is what it is: [docs/DECISIONS.md](docs/DECISIONS.md).

## Changing recognition (required procedure)

1. Put recordings in `video\` and a fresh production snapshot under `data\imports\`.
2. Make the change configurable (env var with the production value as default),
   wire it into `docker-compose.replay.yml` and `tools/replay_recordings.ps1`.
3. Run `-Action Compare` (one recording at a time; a 4 GB GPU cannot load four
   cameras fairly) for the production profile and the candidate.
4. Read the per-attempt trace (`replay/runtime/data/recognition_trace/*.jsonl`):
   recognised people per run, attempts per track, share without embedding,
   low-margin rejections.
5. Build a verification page (camera crop ↔ references ↔ second candidate) for
   **every** person recognised only by the candidate and every match it blocks,
   and get a human verdict. One confirmed false match blocks the change.
6. Only then change the production default, add a `docs/DECISIONS.md` entry with
   the numbers, and deploy.

## Code conventions

- Python 3.10, standard library + the pinned `requirements*.txt`. No new heavy
  dependencies in the worker image without a reason.
- Embeddings are always normalized 512-d finite float vectors; reject anything else.
- Identities: `edu:s:<externalId>` (student), `edu:e:<externalId>` (staff),
  `local:<id>` (device-only employee), plain ERP UUIDs for the legacy edge catalog.
- Store times as UTC; display in `Asia/Tashkent` via `core/local_time.py`.
- Operator-facing text (UI, error messages shown to staff) is Russian; code,
  comments, logs and docs are English.
- New features ship behind config/env flags that default to current behaviour.
- Tests: `unittest`, temporary SQLite, synthetic vectors (`axis(i)` mixes), no network.

## Git and line endings

- Branch `main`, remote `origin` = GitHub `vision-office`. Commit messages in
  English, imperative, explaining *why*.
- Windows checkouts use `core.autocrlf=true`; the repository stores LF. Tools
  that run git outside Windows (Linux shells on the mounted folder) must not
  commit CRLF: check `git show --stat HEAD` for whole-file rewrites before pushing.
- `.gitattributes` forces LF for `*.sh`, `*.yml`, `*.yaml`, `Dockerfile`.
- Update `ROADMAP.md` with evidence when a stage changes status.

## Where to look first

- Something is not recognised → `docs/RECOGNITION_AUDIT_RUNBOOK.md`, then the bench.
- Attendance not delivered → `docs/EDUSCHOOL_TURNSTILE_RUNBOOK.md`, `eduschool_turnstile_outbox`.
- Camera offline → `docs/OPERATIONS_RUNBOOK.md`, `data/runtime_status_<camera>.json`.
- Reference photos → `docs/REFERENCE_PHOTOS.md` («Разметка» page, audits, SQL fixes).
