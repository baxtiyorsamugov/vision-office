# Operations Runbook

## Install and first launch

1. Install Python 3.10 x64 and Microsoft Visual C++ 2015-2022 x64 runtime.
2. Copy the project to the Edge Device and create `config/settings.yaml` from `config/settings.example.yaml`.
3. Run `Set-ExecutionPolicy -Scope Process Bypass` and `./install_vision_office.ps1 -Profile auto`.
4. Verify the selected runtime with `./.venv/Scripts/python.exe verify_install.py --profile cpu` or the selected GPU profile.
5. Start Vision Office with `./run_vision_office.ps1`. It automatically runs one active camera directly or two or more active cameras through the supervisor.

For a no-window video smoke check after an update:

```powershell
.\.venv\Scripts\python.exe test_video.py --headless --duration-seconds 35
```

## Camera configuration

Every active camera receives its own worker process. Keep RTSP credentials only in ignored `settings.yaml`.

```yaml
cameras:
  - id: "entry_01"
    name: "Main entrance"
    rtsp_url: "rtsp://USER:PASSWORD@NVR/Streaming/Channels/101"
    is_active: true
    event_type: "entry"
    location: "Main entrance"
  - id: "exit_01"
    name: "Main exit"
    rtsp_url: "rtsp://USER:PASSWORD@NVR/Streaming/Channels/201"
    is_active: true
    event_type: "exit"
    location: "Main exit"
```

For RTSP instability:

- Prefer wired Ethernet and RTSP over TCP.
- Check the NVR stream directly in a player before changing AI settings.
- Inspect per-camera capture FPS and frame age in `data/runtime_status_<camera_id>.json` and the dashboard.
- A decoder warning means packet loss or corrupted H.264 frames; it is separate from face recognition accuracy.

## GPU and CPU verification

```powershell
.\.venv\Scripts\python.exe test_gpu.py
.\.venv\Scripts\python.exe verify_install.py --profile cpu
nvidia-smi
```

For NVIDIA GTX 10-series use the Pascal profile; modern RTX cards use the modern profile. If no supported GPU or driver is available, use CPU mode and reduce camera FPS/resolution before adding cameras.

## ERP and outbox diagnostics

- Confirm `edge_integration.enabled`, `base_url`, `device_id` and device key in local config.
- Check the dashboard Edge status for cache count, pending outbox events and last sync error.
- Inspect `access_log_outbox` in SQLite for `pending`, `retry`, `sent` and `failed` rows.
- Do not delete pending rows to solve a network issue; restore ERP connectivity and let retry delivery run.

## Health and Telegram diagnostics

Health Checker starts with Vision Office and writes `data/health_status.json`. It checks SQLite, free disk, internet, ERP sync state and every camera. It records incidents in SQLite and sends Telegram through `notification_outbox` when `health.telegram_enabled`, bot token and chat IDs are configured locally. Expect one alert after the configured failure threshold and one recovery message after restoration.

## Safe update and backup

1. Stop the camera application and Streamlit/API processes.
2. Create a backup or transfer archive with `./create_transfer_archive.ps1`; it creates a consistent SQLite snapshot.
3. For code-only updates use `./create_update_archive.ps1`; it preserves local database, faces, models and settings.
4. Apply updates, restart the application, then verify runtime status, one camera frame and one local recognition event.
5. Keep the previous archive until the restart and verification complete successfully.

## Incident first response

| Symptom | First checks |
| --- | --- |
| No frames | RTSP URL, NVR reachability, camera power/network, frame age. |
| Recognition stalled | Runtime status timestamp, worker process, CPU/GPU use, model files. |
| ERP events pending | Internet, ERP health, device key, outbox error and retry time. |
| Database error | Free disk, SQLite file permissions, backup availability, `SELECT 1` health check. |
| Repeated alerts | Camera status transitions, ERP reachability and alert deduplication state. |
