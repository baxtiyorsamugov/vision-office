# Vision Office

Vision Office is a local face-recognition and attendance platform for RTSP cameras, video tests, employee registration, analytics, and read-only integration API.

## Install on another Windows computer

For a normal installation, run the automatic installer from the project folder:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install_vision_office.ps1
```

It uses a fixed dependency set and selects a modern NVIDIA GPU, GTX 10/Pascal GPU, or CPU profile automatically. Detailed flash-drive instructions are in `INSTALL_USB.md`.

## Local launch

```powershell
cd C:\projects\acs2
.\.venv\Scripts\Activate.ps1
python -m streamlit run ui\app.py --server.port 8501
```

Open http://127.0.0.1:8501 in a browser.

## Before first launch

Create `config/settings.yaml` from `config/settings.example.yaml` and enter the RTSP camera address and credentials. Do not commit the local settings file, database, face photos, test videos, model weights, or generated archives.

## API

```powershell
python -m uvicorn api.server:app --host 127.0.0.1 --port 8000
```

API documentation: http://127.0.0.1:8000/docs

## Transfer archive

```powershell
.\create_transfer_archive.ps1
```

## Updating installed computers

For normal code and UI updates, create the safe code-only archive with `./create_update_archive.ps1`. It does not include or overwrite each computer's database, camera settings, face photos, models, or installed environment. See [UPDATE_OTHER_PC.md](UPDATE_OTHER_PC.md).

This creates a portable `acs2-transfer.zip` with a consistent SQLite snapshot.
