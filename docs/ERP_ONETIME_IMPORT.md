# One-time ERP employee import

From the project folder in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\import_erp.ps1
```

Enter the Learning Center UUID from ERP (not the device UUID), ERP username,
and password. Password input is hidden. Credentials are used in memory only.
The PostgreSQL service and existing application schema must already be running.

The launcher mounts the current tools folder into a temporary container using
the existing edge-sync image. No rebuild is needed. It stops the scheduled sync
worker while importing and restarts it afterwards only if it was running before.
The import reads employees from the selected center and only imports IDs also
present in the device-token catalog. It updates local PostgreSQL and portraits,
preserves existing embeddings, and derives missing embeddings from source photos.
It does not create ERP employees or access events and does not delete local data.

For another PC, copy import_erp.ps1 to the project root and these two files into
its tools folder: import_erp_catalog.py and import_backend_people.py.
Refresh the UI after success. Exit 2 means partial completion: review missing
photos/FaceID and repeat after correcting the source or connectivity.
HTTP 401 indicates login failure or an expired token; 403 indicates insufficient
permissions; 404 may indicate an incorrect center UUID. Use an ERP account with
read access to the selected center. Model weights must be installed for FaceID.

This imports current employees once. Future name/photo changes require another
import until the device sync contract includes identity metadata.
