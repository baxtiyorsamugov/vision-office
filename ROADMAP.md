# Vision Office Roadmap

**Source of truth for delivery status.** Update this file in the same change that implements or verifies a roadmap task. A task is `DONE` only after its automated checks and the stated manual acceptance scenario pass.

## Status legend

- `TODO`: planned, not started.
- `IN PROGRESS`: the single active engineering stage.
- `DONE`: implemented and verified.
- `BLOCKED`: cannot proceed without an external input; document the exact missing input below.

## Current snapshot

| Stage | Status | Goal | Acceptance criteria | Dependencies | Verification |
| --- | --- | --- | --- | --- | --- |
| Foundation | DONE | Establish delivery rules, configuration, logging and safe SQLite migrations. | Additive migrations, validation and rotating logs are implemented and tested. | Local SQLite, project config. | `unittest` migration/config/log tests; real SQLite migration smoke check. |
| ERP data sync | DONE | Synchronize people, reference photos and 512-value embeddings through an adapter and mock ERP. | Cache accepts normalized ERP vectors; photo fallback records invalid reasons or local photo/embedding. | Foundation; ERP mock contract. | Mock sync, invalid input and photo-fallback tests. |
| Recognition events | BLOCKED | Persist entry/exit, event images and deduplicated local events. | Local immutable events carry camera, direction, confidence and optional image path; unknown faces never leave the device. Code and automated checks are complete; recognition UAT needs a known-face fixture or live camera. | ERP data sync; camera config. | Event deduplication test; known-face video/RTSP acceptance pending. |
| Two cameras | BLOCKED | Run entry and exit cameras independently. | Supervisor and reconnect logic are implemented; physical two-stream acceptance remains. | Recognition events; supervisor; two approved streams. | Two RTSP/video fixture test and failure injection. |
| Containerized deployment | DONE | Run worker, API and dashboard as restartable isolated services. | Docker Desktop CPU build, migrations, API, dashboard, one RTSP worker and restart recovery passed. Runtime dependencies are baked into the image and RTSP credentials are redacted from application logs. | Docker Desktop Linux containers; approved model files; camera config. | Docker Compose build, startup, API health, worker restart and camera recovery. |
| Reliable delivery | DONE | Deliver outbox events to ERP with idempotency and retries. | Events and heartbeat use durable outbox; network retry survives restart. | Recognition events; ERP adapter. | Retry-then-send unit test and payload contract test. |
| Health and alerts | BLOCKED | Monitor Edge, DB, ERP, RTSP and application; notify Telegram. | Health state, incident history, local notification outbox and recovery tracking are implemented; production Telegram delivery awaits credentials. | Two cameras; logging; Telegram config. | Failure/recovery unit test; production Telegram UAT pending. |
| Validation and rollout | BLOCKED | Benchmark and prepare production rollout. | Code-level checks pass; live 1/2-camera benchmarks need approved streams. | All previous stages. | Benchmark report, restart and offline recovery checks. |

## Completed work

| Date | Status | Result | Evidence |
| --- | --- | --- | --- |
| 2026-09-03 | DONE | Documentation governance, ERP mock contract, runbook and engineering playbook created. | This file; `docs/` documents; README links. |
| 2026-09-03 | DONE | Foundation, mock ERP cache/photo fallback, local recognition events, durable delivery, camera supervisor and Health Checker implemented. | 16 unit tests, CPU install sanity check, API import, real SQLite migration smoke check and headless video pipeline run (22.6 capture FPS, 16 ms frame age). |
| 2026-09-03 | DONE | Health Checker hotfix: stale camera frames are now published during reader stalls; inactive test artifacts are ignored. | Regression test for stale configured camera plus full test suite. |
| 2026-09-04 | DONE | Windows status-file hotfix: a locked runtime JSON no longer terminates the camera process. | Regression test simulates `PermissionError`; 18 tests pass. |
| 2026-09-04 | DONE | Added Docker Compose topology, CPU Docker image, migration service, healthchecks, headless camera mode, shared-SQLite WAL/busy timeout and Docker runbook. | Docker asset tests and Python test suite; live Docker Desktop UAT is recorded below. |
| 2026-09-04 | DONE | Docker Desktop UAT completed: rebuilt CPU image, migrations exited successfully, API/UI/worker became healthy, one live RTSP camera ran at about 25 capture FPS, and a worker restart recovered the stream. Added `lap` to the immutable image and redacted RTSP credentials in logs. | Docker Compose build/start/restart, API health endpoint, live runtime status and full Python test suite (23 tests). |
| 2026-09-04 | DONE | Hardened the Docker runtime environment: service-specific environment blocks now retain shared settings, and the container entrypoint creates the transient Ultralytics settings directory before Python starts. | Rebuilt image; verified all services healthy, worker received `YOLO_CONFIG_DIR`, no Ultralytics permissions warning, live camera connected and API health returned `ok`. |

## Containerized deployment

| Task | Status | Modules | Done when |
| --- | --- | --- | --- |
| Documentation governance | DONE | `ROADMAP.md`, `docs/`, `README.md` | Documents are linked and describe current project truth. |
| Structured logging | DONE | `core/logging_setup.py`, runtime entrypoints | Rotating UTF-8 logs are created and carry process/module context. |
| Safe SQLite migrations | DONE | `database/migrations.py`, `database/` | Existing databases receive additive schema changes without losing attendance history. |
| Configuration validation | DONE | `core/config.py`, `core/edge/config.py` | Invalid/missing cameras and Edge credentials fail with actionable messages. |
| Physical two-camera acceptance | BLOCKED | `core/supervisor.py`, `core/video/streamer.py`, `main.py` | Entry and exit streams run for at least 30 minutes; disabling one stream does not stop the other. |
| Docker topology and runbook | DONE | `Dockerfile`, `docker-compose.yml`, `docker/healthcheck.py`, `docs/DOCKER_RUNBOOK.md` | Worker, API and dashboard are separate services; migrations precede startup; persistent directories and run commands are documented. |
| Docker Desktop build and one-camera UAT | DONE | `Dockerfile`, `docker-compose.yml`, target Edge Device | CPU image rebuilt; migrations, API, UI and one real RTSP stream ran successfully. A restarted worker reconnected and all long-running services returned healthy without runtime package installation. |

## External blockers

| Status | Missing input | Needed for | Temporary behavior |
| --- | --- | --- | --- |
| BLOCKED | Production ERP base URL, authentication method and final request/response examples. | Switching ERP adapter from mock to production. | Use the documented mock contract. |
| BLOCKED | Telegram bot token and responsible chat IDs. | Production alert delivery. | Log incidents locally; no Telegram messages are sent. |
| BLOCKED | Two stable RTSP URLs or approved video fixtures representing entry and exit cameras. | Two-camera acceptance and benchmark. | Use local video fixtures for automated tests. |
| BLOCKED | A short approved video or live stream where a registered/remote employee is visible. | Recognition-event acceptance including event photo and ERP payload. | Unit tests cover event storage and delivery contract. |

## Update protocol

1. Move at most one roadmap stage to `IN PROGRESS`.
2. Add the module paths, tests run, manual scenario and residual risk to the relevant row.
3. Record external dependencies as `BLOCKED`; do not hide them in code comments or silent fallbacks.
4. Mark `DONE` only after the acceptance criteria pass on a clean restart.
5. Add an entry to **Completed work** for every completed stage or meaningful milestone.

## Next step

Run the two-camera acceptance with the actual entry and exit RTSP streams. Then complete a live registered-face event test, production ERP/Telegram UAT, and the 30-minute stability benchmark.
