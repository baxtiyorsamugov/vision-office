# Camera Operations and Recognition Audit

## Scope and boundaries

This update changes camera scheduling, local diagnostics, dashboard queries and
camera controls. It does not change EduSchool/ERP endpoints, credentials,
qualification, payloads, outbox delivery, retries or student-delivery eligibility.
No historical events are replayed. Recognition similarity thresholds and the
existing catalog precedence are unchanged.

## Findings and fixes

- Analytics previously queried the device-only attendance table, omitting
  EduSchool. It now aggregates recognition events in SQL by person type and ID,
  uses Asia/Tashkent day boundaries and paginates rows. Identical names remain
  separate people. Unsupported assumptions about 09:00 lateness were removed.
- The dashboard now counts EduSchool and local staff and recognized people by
  identity. Unknown detections are not counted as identified attendees.
- Streamlit shares one initialized database engine instead of running migrations
  and creating an engine on each interaction.
- Waiting faces are scheduled fairly instead of always choosing the largest
  three. All tracked faces can be displayed; the FaceID queue stays bounded.
- Crops retain their aspect ratio; FaceID chooses the face containing the crop
  center rather than accidentally selecting a neighboring face. An ambiguous
  crop is retried, not force-matched.
- Tasks older than two seconds are dropped. Missing tracks are evicted after ten
  seconds; known tracks are checked again after five seconds without generating
  another event solely for that recheck. Failed jobs can be retried.
- Reference vectors refresh off the inference queue every five seconds. A
  refresh failure retains the last good snapshot temporarily; after 30 seconds
  without refresh, new recognition jobs wait instead of using stale identities.
- RTSP open/read operations have three-second timeouts, backoff is interruptible,
  and only the reader releases its OpenCV handle. Frames older than two seconds
  are not passed to AI. USB source `0` is handled as a device index.
- Camera processes are supervised even for a single-camera installation. An
  individual configuration change restarts only that camera. Dead workers retry
  with bounded backoff. Native-window preview no longer overwrites its publisher.
- Runtime heartbeat older than 15 seconds cannot claim the camera is running.
  Dashboard previews show their saved timestamp, not an implied live timestamp.

## Operator workflow

Open **Cameras** from the top navigation. Select a camera or **Add camera**.
Set a unique ID, name, location, stream address, entry/exit direction and profile.
New cameras default to disabled. An empty password-style stream field preserves
an existing address. Saving does not expose the existing RTSP password.

Profiles:

| Profile | Detector size | Target detection FPS |
| --- | --- | --- |
| From settings.yaml | Existing configuration | Existing configuration |
| Balanced | 640 | 12 |
| Detail | 960 | 15 |
| Economy | 640 | 6 |

These are targets, not promised throughput. Larger images may help small/distant
faces but cost compute. Lower FPS does not repair missing or unsuitable portraits.

The supervisor reads changes approximately every two seconds; stopping a worker
and warming up models take longer. Controls do not start Docker itself. If
`vision-worker` is stopped, the UI reports this and saves changes for its next run.
Camera IDs are immutable in the edit form. A new ID requires separate verification
of the existing EduSchool `deviceId` mapping; adding a camera does not authorize
new API mappings or change sending policy.

Overrides contain RTSP credentials. Keep `data/camera_controls.json` local, with
restricted filesystem access; never commit it or send it in a support archive.
Back it up securely with the device configuration. Saved overrides take precedence
over the matching camera in YAML. To revert an override, stop the worker, back up
the file and remove only that camera's entry using a JSON editor, then restart.
Removing a UI-added camera entry removes that camera; removing an override for a
YAML camera restores its YAML definition. Do not remove API configuration.
The supported writer is one UI process per device; session revision checks reject
stale edits. Invalid JSON is rejected instead of silently replacing configuration.

## Reading diagnostics

The dashboard diagnostics refresh every five seconds without opening another RTSP
stream. They show capture/detection FPS, actual CPU/CUDA providers, recent p95
latencies, queue delay, frame age, FaceID errors and available reference vectors.
CPU, RAM, disk free space and NVIDIA utilization/VRAM are sampled by the supervisor.
Docker CPU/RAM figures describe Linux/WSL, not total Windows utilization. A missing
GPU metric is unavailable, not proof that GPU load is zero.

- High frame age: investigate camera/network/decoder before changing FaceID.
- Fresh frames but high detector p95 or queue wait: inspect CPU/GPU and try a
  lower-throughput profile. Do not lower similarity thresholds to hide latency.
- Many unknowns with low latency: check active FaceID photos, portrait quality,
  face size/angle and conflicts between legacy and EduSchool profiles.
- Pending photos: inspect `eduschool-photo-worker`; a synced person or visible
  portrait alone is not proof that an active embedding exists.
- Cache errors: restore PostgreSQL connectivity; new references cannot load.

Do not derive a promised camera capacity from one GPU-utilization reading. Add
one camera at a time, observe peak crowding for at least 30 minutes, and retain
headroom for photo enrollment, sync and reporting. There is no automatic capacity
guarantee in this dashboard.

## Verification on 2026-10-08

The development PC, not the remote deployment, was inspected. Its PostgreSQL
snapshot contained 438 active EduSchool staff with 152 active FaceID profiles,
and 1,104 active students with no active FaceID profiles. Available photo states
included pending and invalid records. These are local snapshot counts, not a
statement about the remote computer or backend photo availability.

A read-only 60-frame sample from the entrance camera at detector size 640 used
CUDA for YOLO and FaceID: detector p50/p95 15.9/25.6 ms, FaceID p50/p95
15.9/59.0 ms, 57 usable embeddings from 60 crops, capture about 24.8 FPS.
This measures processing and extraction, not identity accuracy or end-to-end
attendance latency. No labeled identities were tested. A separate 960 sample
was taken at a different moment, so it is not a controlled before/after benchmark.

An isolated GPU container with temporary SQLite/config/data successfully ran
one real RTSP camera, added the second live, paused only the second, resumed it
and shut the supervisor down cleanly. Both detector and FaceID reported CUDA.
The first run observed about 25 capture FPS per camera. Concurrent builds/tests
changed throughput in the repeat run; these short checks are not a capacity test.
No working PostgreSQL events or API requests were created by this UAT.

Desktop and 390px-wide UI were inspected. Historical PostgreSQL events, name
filtering, empty dates, counters and camera diagnostics rendered successfully.
The full suite passed 163 tests without skips, followed by 14 focused checks
including an additional PostgreSQL test with a non-UTC session timezone.
Automated checks cover SQLite, isolated PostgreSQL, controls, scheduling,
expired jobs, cache failure, duplicate-event suppression, analytics identity/time
boundaries and existing API behavior.

## Repeatable diagnostics

From the project directory, with the correct device GPU/CPU Compose override:

```powershell
docker compose run --rm --no-deps --entrypoint python vision-worker tools/diagnose_recognition.py --camera reception_01 --samples 60 --imgsz 640 --output data/exports/recognition-check.json
```

This opens an additional temporary camera reader and loads models, so run it in
a maintenance window. It does not write attendance, send API requests or save
camera images. Replace the camera ID with a configured ID.

`tools/uat_camera_runtime.py` is for isolated engineering testing only. It refuses
to run unless `/app/data` and `/app/config` are separate Linux tmpfs mounts. Supply
the original config read-only at `/source-config` and models read-only under
`/app/models`; never mount the working data directory as its writable data store.

## Updating another computer

Keep a database backup and secure copies of `.env`, `config/settings.yaml`,
`data/camera_controls.json` if present, model weights and local photos. Confirm
`git status` has no unreviewed conflicts. Update during a maintenance window:

```powershell
git pull --ff-only
powershell -ExecutionPolicy Bypass -File .\start_vision_office.ps1
docker compose ps
```

The existing launcher rebuilds images, probes the target GPU/CPU runtime and
starts the configured services. It preserves device-local credentials and GPU
selection behavior. Starting the installation resumes its already-configured
senders; this update does not change their policy. Do not use `down -v`, reset the
database or overwrite the target computer's configuration with another device's.

## Remaining deployment acceptance

Before calling the system production-accepted, test on the actual target PC:

1. Verify catalog/reference readiness and resolve conflicting identities.
2. Make labeled entry/exit passes with consenting known people, including students
   with valid FaceID, at expected distances and lighting. Count misses and wrong
   identities; record time from visibility to local recognition.
3. Run 30 minutes under peak camera/crowd load, including network reconnection and
   Docker restart. Observe frame age, queue delay, CPU, RAM and VRAM.
4. Check timestamps, direction and paired visits against the observed passes.
5. Separately confirm existing API delivery and backend readback using the
   documented attendance runbook. Student delivery restrictions remain unchanged.

Missing portraits, unsuitable camera placement, unknown backend contracts and
unmeasured target-device capacity cannot be fixed or certified by a local code
audit alone.
