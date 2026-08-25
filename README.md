# Vision Office

Vision Office is a local face-recognition and attendance platform for RTSP cameras, video tests, employee registration, analytics, and read-only integration API.

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

This creates a portable `acs2-transfer.zip` with a consistent SQLite snapshot.
