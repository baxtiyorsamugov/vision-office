# Vision Office

Vision Office is a local face-recognition and attendance platform for RTSP cameras, video tests, employee registration, analytics, and read-only integration API.

## Engineering roadmap

- [Roadmap and live status](ROADMAP.md)
- [Implementation playbook](docs/IMPLEMENTATION_PLAYBOOK.md)
- [ERP adapter and mock contract](docs/ERP_CONTRACT.md)
- [Operations runbook](docs/OPERATIONS_RUNBOOK.md)
- [Docker deployment runbook](docs/DOCKER_RUNBOOK.md)
- [Automatic GPU/CPU selection and recovery](docs/GPU_RUNTIME.md)
- [Local PostgreSQL, hourly ERP sync and reference-photo runbook](docs/POSTGRESQL_RUNBOOK.md)
- [Transfer ERP employees, photos and FaceID to another device](docs/CATALOG_TRANSFER_RUNBOOK.md)
- [Unknown visitors catalog runbook](docs/UNKNOWN_VISITORS_RUNBOOK.md)
- [UI design system](docs/UI_DESIGN_SYSTEM.md)

The roadmap is updated with each verified stage; do not treat a task as complete until its documented checks pass.

## Two-camera setup

Add two active camera entries to the local `config/settings.yaml`, assigning `event_type: entry` to the entrance stream and `event_type: exit` to the exit stream. `main.py` starts an isolated worker for each camera; an RTSP failure reconnects independently. See [the operations runbook](docs/OPERATIONS_RUNBOOK.md) for the full configuration and acceptance procedure.

## Install on another Windows computer

For a new deployment, follow the [USB installation guide](INSTALL_USB.md).
Prepare current code, model weights and device-local settings as described there,
then start Docker and optionally import the full ERP employee catalog:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_vision_office.ps1
powershell -ExecutionPolicy Bypass -File .\import_erp.ps1
```

The launcher probes real YOLO/FaceID models on the target GPU and falls back to
CPU if CUDA is unavailable or fails preflight. The native `install_vision_office.ps1`
remains a separate option, not a prerequisite for Docker.

## Local launch

For offline catalog bootstrap on another device of the same learning center,
use **Employees > ERP > Catalog transfer**: export a ZIP on the source computer,
then preview and confirm its import on the new one. It preserves ERP IDs and
does not replace attendance, settings or the existing ERP synchronization.
The archive contains personal photos and biometrics; keep it private.
See the [catalog transfer runbook](docs/CATALOG_TRANSFER_RUNBOOK.md).

```powershell
cd C:\projects\acs2
.\run_vision_office.ps1
```

For the administration panel:

```powershell
.\.venv\Scripts\python.exe -m streamlit run ui\app.py --server.port 8501
```

Open http://127.0.0.1:8501 in a browser.

## Docker launch

Docker runs PostgreSQL, the shared ERP synchronizer, low-priority unknown-face clusterer, camera/AI worker, API and dashboard as separate services. It preserves local settings, event photos and models on the host computer; PostgreSQL uses its own durable Docker volume:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_vision_office.ps1
docker compose ps
```

Open http://127.0.0.1:8501. GPU acceleration is reserved for `vision-worker`;
support services stay CPU-only. Use `-Profile cpu` to force the portable CPU
runtime. The launcher saves the selection locally for subsequent `docker compose up -d`.
See the [Docker runbook](docs/DOCKER_RUNBOOK.md) before first launch.

For a separate low-impact local camera window, open http://127.0.0.1:8000/monitor. It displays bounded worker-generated preview frames and never opens a second RTSP connection.

## Before first launch

Create `config/settings.yaml` from `config/settings.example.yaml` and enter the RTSP camera address and credentials. Do not commit the local settings file, database, face photos, test videos, model weights, or generated archives.

## API

```powershell
python -m uvicorn api.server:app --host 127.0.0.1 --port 8000
```

API documentation: http://127.0.0.1:8000/docs

## Edge-device integration

Vision Office can operate as an edge device for a Learning Center backend. Copy the
`edge_integration` section from `config/settings.example.yaml` to the untracked
`config/settings.yaml`, set `enabled: true`, and enter the backend URL, device UUID,
and newly issued device API key. Do not use an administrator JWT on the device.

When enabled, the backend is the only source of ERP people: the app synchronizes its
local matching cache from `/persons/sync` and sends only recognized ERP access events through
an on-disk outbox. Unknown faces and local employees remain on the device. The first synchronization happens
after the recognition worker starts. Review its status on the **Registration** page.

The **Registration** page also has an optional **Local employee** form. These profiles
and their attendance remain only in the device's PostgreSQL database; their events are
never sent to ERP. They are matched alongside the synchronized ERP catalog.

Use **Update from ERP** on the **Registration** page when an immediate full catalog refresh is needed. People no longer returned by ERP remain in local PostgreSQL as inactive, with their audit history retained, and are excluded from FaceID matching.

Operator-facing times use `edge_integration.timezone` (default: `Asia/Tashkent`); UTC remains the internal storage format for reliable ERP synchronization and historical data.

If the current ERP `persons/sync` endpoint does not provide names and official photo
URLs, use the controlled one-time catalog bootstrap documented in the
[interactive ERP import guide](docs/ERP_ONETIME_IMPORT.md).
It does not persist administrator credentials and is only needed until the ERP device
sync contract is extended.

## Transfer archive

For a new Docker installation, create the code archive below, then copy model
weights and device settings separately as described in the [USB guide](INSTALL_USB.md).

```powershell
.\create_update_archive.ps1
```

## Updating installed computers

For normal code and UI updates, create the safe code-only archive with `./create_update_archive.ps1`. It does not include or overwrite each computer's database, camera settings, face photos, models, or installed environment. See [UPDATE_OTHER_PC.md](UPDATE_OTHER_PC.md).

The legacy `create_transfer_archive.ps1` creates a SQLite-based archive and is
not the PostgreSQL/Docker installation workflow.
