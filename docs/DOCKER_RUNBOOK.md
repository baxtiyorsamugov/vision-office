# Docker Runbook

## Scope

Docker Compose runs Vision Office as independent services:

| Service | Responsibility | Persistent paths |
| --- | --- | --- |
| `migrate` | Applies additive SQLite migrations, then exits successfully. | `data/` |
| `vision-worker` | RTSP capture, AI recognition, ERP outbox and Health Checker. | `config/`, `data/`, `models/` |
| `api` | Read-only FastAPI integration API. | `config/`, `data/` |
| `ui` | Streamlit operations dashboard. It does not start duplicate workers in Docker. | `config/`, `data/`, `models/` |

The default image is CPU-only and headless. It does not open an OpenCV window; use the browser dashboard and logs instead. The native Windows installation remains the current GPU option. A GPU Docker profile is deliberately not enabled until it is validated against the exact NVIDIA driver and GPU on the target device.

All Python dependencies, including the ByteTrack `lap` package, are baked into the image. A production container must not install Python packages while it is processing camera frames.

## Prerequisites

1. Install Docker Desktop with the **Linux containers** and WSL 2 backend enabled.
2. In PowerShell, verify:

```powershell
docker version
docker compose version
```

3. Copy the project without `.venv`, then create the device-local configuration:

```powershell
Copy-Item .\config\settings.example.yaml .\config\settings.yaml
```

4. Put real RTSP/ERP/Telegram values only in `config/settings.yaml`. This file is excluded from Git and from the Docker image.
5. Copy the already approved model files from the working device into these exact locations:

```text
models/yolov8n-face.pt
models/insightface/models/buffalo_l/
```

`models/` is mounted read-only. The container never silently downloads or replaces production model weights.

## First Start

Run all commands from the project root:

```powershell
docker compose build --pull
docker compose up -d
docker compose ps
```

Expected result:

- `migrate` has status `Exited (0)`; it is a one-time schema service.
- `vision-worker`, `api` and `ui` are `Up` and become `healthy` after startup.
- Dashboard: http://127.0.0.1:8501
- API documentation: http://127.0.0.1:8000/docs

The ports bind only to `127.0.0.1` by default. Do not expose the API to the LAN until an API key and allowed CORS origins are configured.

## Runtime Operations

```powershell
# Follow all logs, or one service only.
docker compose logs --tail 200 -f
docker compose logs --tail 200 -f vision-worker

# Service status and built-in healthchecks.
docker compose ps

# Restart only the camera/AI service after an RTSP or model correction.
docker compose restart vision-worker

# Stop services without deleting database, events, settings or models.
docker compose down

# Start them again.
docker compose up -d
```

`vision-worker` remains responsible for restarting an individual camera process. An unavailable RTSP camera does not stop its sibling camera, API or UI.
RTSP passwords are read only from `config/settings.yaml` and are redacted from application logs.

## Health and Diagnostics

1. `docker compose ps` shows container state and `healthy`/`unhealthy` status.
2. `docker compose logs -f vision-worker` shows RTSP reconnects, recognition startup, ERP retries and Health Checker errors.
3. The dashboard displays per-camera FPS, frame age and reconnect attempts.
4. API health:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
```

5. A camera with no frames becomes unhealthy after the configured Health Checker threshold. Telegram notification still depends on the local Telegram token and chat IDs in `settings.yaml`.

For an RTSP issue, test the NVR stream before changing AI parameters. H.264 decoding warnings indicate a camera/network stream problem, not a Docker or recognition-model failure.

## Backup and Safe Update

`config/`, `data/` and `models/` are host-mounted and survive rebuilds. Never run `docker compose down -v` for this project: it is unnecessary and can remove future named volumes.

```powershell
# Stop writes before copying SQLite and event images.
docker compose stop

# Create a dated local backup.
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
Copy-Item .\data ".\backups\data-$stamp" -Recurse
Copy-Item .\config\settings.yaml ".\backups\settings-$stamp.yaml"

# Update only tracked project code, rebuild, then start services.
git pull --ff-only
docker compose build --pull
docker compose up -d
docker compose ps
```

Keep one validated backup until the dashboard, one camera and API health are verified after the update. Do not copy another device's `settings.yaml`, `office.db`, model weights or Telegram/ERP credentials unless that migration is intentional.

## External API Settings

The API is private to the same computer by default. To use a browser client on the same device, no additional variables are needed. For deliberate LAN exposure, create an ignored `.env` file:

```dotenv
VISION_OFFICE_API_KEY=replace-with-a-long-random-value
VISION_OFFICE_CORS_ORIGINS=https://approved-dashboard.example
VISION_OFFICE_API_PUBLIC_URL=https://approved-api.example
```

Then change the required port binding in `docker-compose.yml` only after firewall, TLS reverse proxy and API-key validation are in place.

## Acceptance Checklist

1. `docker compose build --pull` finishes without errors on the target Docker Desktop device.
2. `docker compose up -d` produces healthy `vision-worker`, `api` and `ui` services.
3. A configured camera publishes fresh frame status and recognition events.
4. Disconnecting one of two cameras does not stop the other camera, UI or API.
5. `docker compose restart vision-worker` preserves SQLite data and resumes the outbox.
6. Stop/start recovery and an offline ERP retry have been checked before production rollout.
