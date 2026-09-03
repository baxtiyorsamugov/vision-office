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
| Foundation | IN PROGRESS | Establish delivery rules, configuration, logging and safe SQLite migrations. | Documentation is present; migration and logging work is tested before this stage moves to DONE. | Local SQLite, project config. | `python -m unittest discover -s tests` plus manual startup. |
| ERP data sync | TODO | Synchronize people, reference photos and 512-value embeddings through an adapter and mock ERP. | Added/changed people reach local cache; invalid photos have a recorded reason. | Foundation; ERP mock contract. | Adapter and sync tests with mock HTTP responses. |
| Recognition events | TODO | Persist entry/exit, event images and deduplicated local events. | One passage creates one event with camera, direction, confidence and image path. | ERP data sync; camera config. | Database and cooldown tests; manual video fixture. |
| Two cameras | TODO | Run entry and exit cameras independently. | A broken stream reconnects without stopping the other camera. | Recognition events; supervisor. | Two video/RTSP fixture test and failure injection. |
| Reliable delivery | TODO | Deliver outbox events to ERP with idempotency and retries. | Offline events persist and are sent once after recovery. | Recognition events; ERP adapter. | Retry, restart and duplicate-delivery tests. |
| Health and alerts | TODO | Monitor Edge, DB, ERP, RTSP and application; notify Telegram. | Health state, incident history, deduplicated alert and recovery alert exist. | Two cameras; logging; Telegram config. | Mock failures and recovery scenarios. |
| Validation and rollout | TODO | Benchmark and prepare production rollout. | Recommended 1/2-camera settings and operational acceptance checklist are recorded. | All previous stages. | Benchmark report, restart and offline recovery checks. |

## Completed work

| Date | Status | Result | Evidence |
| --- | --- | --- | --- |
| 2026-09-03 | DONE | Documentation governance, ERP mock contract, runbook and engineering playbook created. | This file; `docs/` documents; README links. |

## Active stage: Foundation

| Task | Status | Modules | Done when |
| --- | --- | --- | --- |
| Documentation governance | DONE | `ROADMAP.md`, `docs/`, `README.md` | Documents are linked and describe current project truth. |
| Structured logging | TODO | `core/`, runtime entrypoints | Rotating logs include device, camera and event context. |
| Safe SQLite migrations | TODO | `database/` | Existing databases receive additive schema changes without losing attendance history. |
| Configuration validation | TODO | `config/`, edge config | Invalid/missing camera and integration config fail with actionable messages. |

## External blockers

| Status | Missing input | Needed for | Temporary behavior |
| --- | --- | --- | --- |
| BLOCKED | Production ERP base URL, authentication method and final request/response examples. | Switching ERP adapter from mock to production. | Use the documented mock contract. |
| BLOCKED | Telegram bot token and responsible chat IDs. | Production alert delivery. | Log incidents locally; no Telegram messages are sent. |
| BLOCKED | Two stable RTSP URLs or approved video fixtures representing entry and exit cameras. | Two-camera acceptance and benchmark. | Use local video fixtures for automated tests. |

## Update protocol

1. Move at most one roadmap stage to `IN PROGRESS`.
2. Add the module paths, tests run, manual scenario and residual risk to the relevant row.
3. Record external dependencies as `BLOCKED`; do not hide them in code comments or silent fallbacks.
4. Mark `DONE` only after the acceptance criteria pass on a clean restart.
5. Add an entry to **Completed work** for every completed stage or meaningful milestone.

## Next step

Implement the remaining Foundation tasks: structured logging, additive SQLite migration runner and configuration validation. Keep all other stages in `TODO` until Foundation is verified.
