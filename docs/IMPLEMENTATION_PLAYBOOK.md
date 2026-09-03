# Implementation Playbook

This document defines how Vision Office changes are designed, implemented and accepted.

## Non-negotiable architecture rules

1. Preserve the working single-camera flow while adding Edge features. Existing local `Employee` and `Attendance` data must remain readable.
2. Never commit camera credentials, ERP URLs with secrets, device keys, Telegram tokens, employee photos or production databases. Keep them only in `config/settings.yaml` and ignored data folders.
3. Perform ERP and Telegram calls outside the camera inference path. Persist events first, then use retry/outbox delivery.
4. Treat each camera as isolated. A camera crash, RTSP timeout or reconnect attempt cannot stop another camera or the Health Checker.
5. Use UUIDs for Edge device IDs, remote people IDs and camera IDs. Do not use camera list indexes as persistent identifiers.
6. Make database changes additive and idempotent. Existing SQLite databases must upgrade without data loss.
7. Store and compare only normalized 512-dimensional finite float embeddings. Do not silently accept invalid vectors or ambiguous reference photos.

## Required change order

1. Update `ROADMAP.md`; move only the current stage to `IN PROGRESS`.
2. Define or extend models and an additive migration.
3. Add configuration fields to `settings.example.yaml`, validation and safe defaults.
4. Implement the service boundary; keep network, storage and inference responsibilities separate.
5. Add unit tests with local SQLite and mocked HTTP/RTSP dependencies.
6. Add logging and runtime status fields that make failures observable.
7. Update `docs/ERP_CONTRACT.md`, `docs/OPERATIONS_RUNBOOK.md` and README when public behavior changes.
8. Run the automated checks and the manual acceptance scenario; only then mark work `DONE` in `ROADMAP.md`.

## Definition of done

A feature is complete only when all applicable checks pass:

- `python -m unittest discover -s tests`
- relevant focused tests for the modified service
- `python verify_install.py --profile cpu` or the active GPU profile
- fresh-process restart with existing SQLite database
- offline/retry scenario for any feature that calls ERP or Telegram
- manual camera/video scenario for changes to recognition or stream handling
- `git diff --check` and a roadmap update containing evidence

## Data and privacy rules

- Reference photos are fetched only from configured ERP URLs and stored below `data/persons/`.
- Event and unknown-face photos are stored below `data/events/` and deleted after 30 days; database event metadata remains.
- Unknown faces stay local and are not sent to ERP under the current policy.
- Event delivery payloads use idempotency keys. Retries must not create a second attendance event.

## Failure handling

- Network errors are retryable with bounded exponential backoff.
- Validation, authentication and malformed ERP payload errors are terminal until configuration or server data changes.
- RTSP recovery must use a bounded reconnect loop and write a per-camera status transition.
- Never use broad `except` blocks to discard errors silently; log structured context and surface the failure to Health Checker.

## Review checklist

- Does the change preserve local/manual mode when `edge_integration.enabled` is false?
- Does it preserve current databases and migrations on restart?
- Does it avoid blocking frame processing on storage or HTTP?
- Is each new external field documented in the ERP contract and example config?
- Do automated tests cover success, retry and permanent-failure behavior?
