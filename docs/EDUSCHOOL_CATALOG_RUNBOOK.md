# EduSchool External API directory

This optional directory reads `/external-api/students/pagin` and
`/external-api/employees/pagin` for one configured branch every hour. It stores
names, source status, external IDs and photo URLs in separate local PostgreSQL
tables. A separate low-priority `eduschool-photo-worker` downloads source images
from the configured EduSchool backend, checks for exactly one face and saves a
local FaceID embedding. It processes employees first, then students, one image
per cycle at a half-core CPU and 1 GB memory limit. A large directory can take
hours to backfill; `Фото API` in the directory shows progress per person. An
operator can still add up to ten extra
local photos per active person; the API sync does not overwrite them. Existing
ERP profiles and their delivery pipeline remain separate.

## Configure on each device

In the ignored `config/settings.yaml`, set:

```yaml
eduschool_catalog:
  enabled: true
  base_url: "https://backend.eduschool.uz"
  branch_id: "YOUR_BRANCH_ID"
  sync_interval_seconds: 3600
  page_size: 200
  request_timeout_seconds: 20
  local_reference_photo_limit: 10
  recognition_threshold: 0.55
```

In the ignored `.env`, set `EDUSCHOOL_EXTERNAL_BEARER_TOKEN` and
`EDUSCHOOL_EXTERNAL_API_KEY` to the credentials provided by EduSchool. Do not
commit either file or paste credentials into logs, screenshots or support tickets.

## Start and check

```powershell
docker compose build eduschool-sync eduschool-photo-worker ui
docker compose up -d eduschool-sync eduschool-photo-worker ui
docker compose ps eduschool-sync eduschool-photo-worker ui vision-worker postgres
docker compose logs --tail 30 eduschool-photo-worker
```

Open **Employees > EduSchool** in the dashboard. Choose staff or students,
search by name and open a profile. The table shows source-photo processing,
the count of FaceID photos and whether matching is ready. A new
reference enters the camera matching cache after its normal refresh (about
five seconds); no extra RTSP connection is opened. The directory also shows
the last successful sync, errors and local camera-event history.
EduSchool profiles use their own stricter similarity threshold (default 0.55);
the existing ERP threshold remains unchanged.

Photos are stored under `data/persons/eduschool/` on this device, not sent to
EduSchool. Source paths are confined to `/uploads/org-.../uploads/...` on the
configured HTTPS backend; redirects and third-party hosts are not followed.
Only JPEG/PNG/WebP images within size limits and with exactly one detectable
face are enrolled. Images conflicting with another active catalog identity are
flagged for operator review, not silently assigned. `Фото API: Проверить фото`
means the source image needs correction; `Повтор позже` means a transient error
will be retried after one hour. Manually added photos are kept separately and
are never removed by a source-photo change. A person absent or inactive in a
later complete ERP snapshot retains photos and history but is excluded from
live recognition. If the source photo changes, its old automatic embedding is
disabled until the replacement passes validation.
Automatic enrollment is a candidate match, not proof of identity; review the
profile and camera events before relying on it for attendance decisions.
For backup/transfer, protect both PostgreSQL and this local photo directory.

The sync first fetches *all* pages of both collections, then commits both to
PostgreSQL in one transaction. A partial response or HTTP failure leaves the
last good directory intact; the error is recorded and retried after one minute.
Some branch employee responses contain the same ID on more than one page. The
sync counts identical copies once; conflicting records for one ID fail the
whole refresh rather than overwrite a profile unpredictably. Compare the API's
reported row total with the unique employee count when investigating a warning.
Records missing from a later complete snapshot remain in the database with
status `absent` and inactive. A person marked inactive by EduSchool also stays
in the local catalog. Locally approved photos and embeddings are untouched.

Before changing branches, back up PostgreSQL and pause `eduschool-sync` and
`eduschool-turnstile`. Update the ignored catalog credentials and branch ID,
check one page of both API collections, then restart the sync service and verify
the new counts and `last_error`. The previous branch's cards and local photos
remain stored but become inactive. Set the turnstile branch ID and its dedicated
key for the new branch before restarting that sender; do not use the old branch
with the new catalog. No historical attendance is replayed. See
`docs/EDUSCHOOL_TURNSTILE_RUNBOOK.md` for delivery acceptance.

If the operator explicitly requests removal of the superseded branch, first
back up both PostgreSQL and `data/persons/eduschool/`. Stop the sync, photo and
turnstile services, then run the guarded tool in the rebuilt sync image:

```powershell
$oldCount = 2658       # Replace with the measured old-profile count.
$oldPhotoCount = 167   # Replace with the measured old-photo count.
docker compose stop eduschool-sync eduschool-photo-worker eduschool-turnstile
docker compose build eduschool-sync
docker compose run --rm --no-deps --entrypoint python eduschool-sync tools/purge_eduschool_absent.py --expected-people $oldCount --expected-photos $oldPhotoCount
docker compose run --rm --no-deps --entrypoint python eduschool-sync tools/purge_eduschool_absent.py --expected-people $oldCount --expected-photos $oldPhotoCount --apply
docker compose up -d --no-deps eduschool-sync eduschool-photo-worker eduschool-turnstile
```

The first command is a dry-run. The tool requires the remaining catalog to
match the latest successful snapshot and refuses to delete active people,
unexpected counts, shared/unsafe photo paths, or profiles with recognition or
attendance-outbox history. It removes old profile rows, their reference photos,
embeddings and per-person photo folders; it does not delete protected backups.
Check the UI and the next hourly sync before discarding any rollback backup.

## API boundary

The catalog Bearer token is used only for External API directory reads. The
separate turnstile sender has its own API-key setting and contract; directory
access alone does not prove that a key is authorized to submit attendance.
Recognized EduSchool people produce local camera events and never enter the
legacy ERP outbox or local-employee attendance rows. The existing 17-person
FaceID catalog retains matching priority when a
new EduSchool reference is only marginally closer to a detected face. Avoid
enrolling the same person twice in different catalogs when possible.
Confirm turnstile key scope with the backend owner and perform a supervised
known-person entry/exit test before treating attendance delivery as accepted.
