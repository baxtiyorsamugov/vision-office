# Local PostgreSQL and ERP Sync

## Architecture

Docker Compose now runs PostgreSQL as the local durable database. `vision-worker` only captures and recognizes faces; `edge-sync` is the one shared process that synchronizes people from ERP and delivers the outbox. This avoids duplicate ERP calls when two cameras are active.

- `postgres`: local PostgreSQL 16 data service, persisted in the `vision-office-postgres` Docker volume.
- `migrate`: applies schema changes after PostgreSQL becomes ready, then exits.
- `edge-sync`: performs the initial sync at startup and subsequent employee syncs every hour.
- `vision-worker`: refreshes its in-memory matching cache from PostgreSQL every few seconds without contacting ERP.

The worker and ERP synchronizer can restart independently. The main ERP photo and embedding remain server-owned. Operator-added photos exist only in local PostgreSQL and `data/persons/local/`; ERP synchronization never deletes them.

## First Start

From the project root, make local database credentials first:

```powershell
Copy-Item .env.example .env
notepad .env
```

Replace `VISION_OFFICE_POSTGRES_PASSWORD` with a long local password that does not contain `@`, `:`, `/`, or spaces. Keep `.env` only on the Edge device; it is ignored by Git.

Start all services:

```powershell
docker compose up -d --build
docker compose ps
docker compose logs --tail 100 edge-sync
```

Expected state: `postgres`, `vision-worker`, `edge-sync`, `api`, and `ui` are running; `migrate` is `Exited (0)`. PostgreSQL is deliberately not exposed to the LAN. Use `docker compose exec postgres psql -U vision_office -d vision_office` only from the local device when administration is required.

### Existing Database and a Changed `.env` Password

PostgreSQL reads `POSTGRES_PASSWORD` only when its data volume is created for the first time. If `.env` was changed after a previous start, do not delete the volume or the data. Align the existing database role with the password from the current `.env`, then restart the stack:

```powershell
docker compose exec -T postgres sh -lc '{ printf "\\password %s\n" "$POSTGRES_USER"; printf "%s\n%s\n" "$POSTGRES_PASSWORD" "$POSTGRES_PASSWORD"; } | psql -U "$POSTGRES_USER" -d postgres'
docker compose up -d
```

The command reads the password only inside the PostgreSQL container; it does not print it or store it in the terminal history.

## ERP Schedule

`edge_integration.sync_interval_seconds` is `3600` by default and in the device configuration. The first sync runs after `edge-sync` starts; each following people sync runs once per hour. Access-event delivery remains independent and checks the durable outbox every five seconds.

To verify the schedule and last result:

```powershell
docker compose logs --tail 200 edge-sync
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
```

The dashboard's **Synchronization** page displays the cached people, sync error, queue state, and local reference-photo count.

## Adding Extra Photos

1. Open `http://127.0.0.1:8501`.
2. Open **Synchronization**.
3. Choose an active person synchronized from ERP.
4. Upload a clear JPG or PNG photo with one visible face.

The device validates the photo, creates a 512-value embedding, saves the original locally, and adds the vector to matching. It never uploads this extra photo to ERP and never changes the ERP main photo. A camera process reloads the local cache within a few seconds. The default limit is ten active extra photos per person; change `edge_integration.local_reference_photo_limit` only when a larger set is genuinely needed.

## Moving Existing SQLite Data Once

Run this only after PostgreSQL starts and before normal Docker services begin writing attendance data:

```powershell
docker compose up -d postgres
docker compose run --rm --no-deps migrate python tools/migrate_sqlite_to_postgres.py --source data/office.db
```

The migration is explicit and copies current tables into PostgreSQL. Keep `data/office.db` as a backup until the dashboard, people cache and API health have been checked.

## Backup and Restore

```powershell
New-Item -ItemType Directory -Force backups | Out-Null
docker compose exec -T postgres pg_dump -U vision_office vision_office > backups\vision-office-postgres.sql

# Restore only after stopping application writers.
docker compose stop vision-worker edge-sync api ui
Get-Content backups\vision-office-postgres.sql | docker compose exec -T postgres psql -U vision_office -d vision_office
docker compose up -d
```

Do not use `docker compose down -v`: it removes the PostgreSQL volume and permanently deletes the local recognition database.
