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
| Containerized deployment | BLOCKED | Run worker, API and dashboard as restartable isolated services. | Docker assets, headless worker, shared SQLite hardening and runbook are implemented; Docker Desktop build and RTSP UAT remain. | Docker Desktop Linux containers; approved model files; camera config. | Docker Compose build, four-service startup, restart and two-stream acceptance. |
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
| 2026-09-04 | DONE | Added Docker Compose topology, CPU Docker image, migration service, healthchecks, headless camera mode, shared-SQLite WAL/busy timeout and Docker runbook. | Docker asset tests and Python test suite; Docker Engine is not installed on the development computer, so target-device build/UAT remains blocked. |

## Containerized deployment

| Task | Status | Modules | Done when |
| --- | --- | --- | --- |
| Documentation governance | DONE | `ROADMAP.md`, `docs/`, `README.md` | Documents are linked and describe current project truth. |
| Structured logging | DONE | `core/logging_setup.py`, runtime entrypoints | Rotating UTF-8 logs are created and carry process/module context. |
| Safe SQLite migrations | DONE | `database/migrations.py`, `database/` | Existing databases receive additive schema changes without losing attendance history. |
| Configuration validation | DONE | `core/config.py`, `core/edge/config.py` | Invalid/missing cameras and Edge credentials fail with actionable messages. |
| Physical two-camera acceptance | BLOCKED | `core/supervisor.py`, `core/video/streamer.py`, `main.py` | Entry and exit streams run for at least 30 minutes; disabling one stream does not stop the other. |
| Docker topology and runbook | DONE | `Dockerfile`, `docker-compose.yml`, `docker/healthcheck.py`, `docs/DOCKER_RUNBOOK.md` | Worker, API and dashboard are separate services; migrations precede startup; persistent directories and run commands are documented. |
| Docker Desktop build and RTSP UAT | BLOCKED | Target Edge Device | Run CPU container image, verify healthchecks, restart recovery and at least one real RTSP stream. |

## External blockers

| Status | Missing input | Needed for | Temporary behavior |
| --- | --- | --- | --- |
| BLOCKED | Production ERP base URL, authentication method and final request/response examples. | Switching ERP adapter from mock to production. | Use the documented mock contract. |
| BLOCKED | Telegram bot token and responsible chat IDs. | Production alert delivery. | Log incidents locally; no Telegram messages are sent. |
| BLOCKED | Two stable RTSP URLs or approved video fixtures representing entry and exit cameras. | Two-camera acceptance and benchmark. | Use local video fixtures for automated tests. |
| BLOCKED | A short approved video or live stream where a registered/remote employee is visible. | Recognition-event acceptance including event photo and ERP payload. | Unit tests cover event storage and delivery contract. |
| BLOCKED | Docker Desktop with Linux containers and the approved `models/` directory on a target Edge Device. | Docker build, first startup and container restart acceptance. | Docker files are committed; native Windows launch remains available. |

## Update protocol

1. Move at most one roadmap stage to `IN PROGRESS`.
2. Add the module paths, tests run, manual scenario and residual risk to the relevant row.
3. Record external dependencies as `BLOCKED`; do not hide them in code comments or silent fallbacks.
4. Mark `DONE` only after the acceptance criteria pass on a clean restart.
5. Add an entry to **Completed work** for every completed stage or meaningful milestone.

## Next step

Install Docker Desktop on one target Edge Device and run the Docker acceptance checklist. Then run the two-camera acceptance with the actual entry and exit RTSP streams, followed by production ERP and Telegram UAT.
