# Retiring the Legacy ERP Catalog

This is an explicit device-local maintenance operation, never an automatic
migration. Git pull does not delete data or change ignored device configuration.
EduSchool synchronization and attendance delivery are separate and stay unchanged.

1. Back up PostgreSQL and device configuration. Schedule a camera maintenance window.
2. Stop `vision-worker` and `edge-sync`. Set only `edge_integration.enabled` to
   `false` in the target computer's `config/settings.yaml`. Do not disable the
   EduSchool sections. Keep `recognition_threshold` unchanged: recognition now
   respects this configured threshold even when the legacy integration is disabled.
3. Build the current UI image so the cleanup tool is present:

```powershell
docker compose build ui
docker compose run --rm --no-deps --entrypoint python ui tools/purge_legacy_erp.py --expected-people 17 --backup /app/data/backups/legacy-retirement.zip
```

4. Check the dry-run counts against the intended legacy catalog. Use the actual
   reviewed count, not an assumed count. To apply that exact operation:

```powershell
docker compose run --rm --no-deps --entrypoint python ui tools/purge_legacy_erp.py --expected-people 17 --backup /app/data/backups/legacy-retirement.zip --apply
```

The tool refuses an enabled legacy integration, an unexpected record count,
unsafe paths, files shared with other catalogs/history, and an existing backup.
Before deleting it creates and verifies a ZIP containing the legacy database
rows (including embeddings) and exact photo files. It deletes only legacy profile
and reference rows plus their validated photo files; no shared directory is
recursively deleted. Attendance history, EduSchool, local people and audit outboxes
remain unchanged. Stop all legacy writers before running it.

The backup contains sensitive biometric data: keep it local and access-restricted,
never commit or upload it. It is an administrator recovery archive, not a catalog
transfer ZIP. Restore selectively into a staging database before restoring any
records to production. Do not overwrite the live database with an old full dump.
If filesystem cleanup fails after the transaction, use its exact backup manifest
to inspect remaining files; do not delete `data/persons` wholesale.

5. Reprocess the affected EduSchool API photo with the existing automatic-photo
   service. Records previously marked `invalid` do not automatically retry just
   because a conflicting profile disappeared. Never bypass the identity-conflict
   check; another valid duplicate could still exist.
6. Rebuild `vision-worker`, restart the cameras and verify the target EduSchool
   profile has an active photo/embedding. Do not restart the retired `edge-sync`.

On the development PC on 2026-10-08, this operation removed 17 legacy profiles,
their embeddings and 16 existing photo files. EduSchool's affected profile was
successfully re-enrolled from its API image without bypassing conflict checks.
The catalog of 2,515 EduSchool profiles (including that profile), four local employees
and 3,192 historical events were preserved. This does not clean another computer;
that device needs its own reviewed maintenance operation.

The employee directory retains the EduSchool, local and unknown-visitor tabs
when legacy integration is disabled. An empty legacy tab cannot import a retired
catalog while the old integration is disabled.
