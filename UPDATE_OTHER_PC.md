# Updating another computer

Use this process for code, UI, and logic changes. It intentionally preserves the target computer's attendance database, faces, camera settings, API credentials, models, and installed libraries.

## On the source computer

```powershell
cd C:\projects\acs2
.\create_update_archive.ps1
```

Copy `acs2-update.zip` to the flash drive.

## On the target computer

1. Stop `main.py` and Streamlit if they are running.
2. Copy `acs2-update.zip` from the flash drive.
3. Open PowerShell and run:

```powershell
Expand-Archive -LiteralPath E:\acs2-update.zip -DestinationPath C:\VisionOffice -Force
cd C:\VisionOffice
.\.venv\Scripts\python.exe -m py_compile main.py ui\app.py
```

Replace `E:\acs2-update.zip` with the drive letter of the flash drive. Then start the program as usual.

## When requirements changed

If you changed `requirements*.txt`, run this once after extracting the update:

```powershell
cd C:\VisionOffice
.\install_vision_office.ps1
```

The target computer needs an internet connection only when libraries are changed. The installer downloads the fixed versions directly from the official package indexes and automatically selects GPU or CPU.
