# EduSchool Student Attendance (2026-10-09 Contract)

## What is implemented

Staff and students use the existing single-event POST endpoint:
`https://backend.eduschool.uz/external-api/turnstile/attendance`.
Headers are `apikey`, `branch` and JSON Content-Type; no Bearer token is sent.
Staff payloads contain `employeeNo`; students contain `studentNo`. Never both.
All camera payloads explicitly carry boolean `isCamera: true`, original UTC
`eventTime`, `method: face_recognition` and the camera's existing `deviceId`.

Students qualify automatically only when active, enrolled with a usable FaceID
embedding, not on operator hold, and assigned a unique studentNo in the camera's
branch. Student and staff number namespaces are separate. A student's branch is
stored from the verified catalog response and must match the sender branch.
No identifier is guessed from `_id`, UUID, employeeNo or local row position.

The catalog now stores `student_no` and `source_branch_id` in PostgreSQL. Existing
rows need a fresh catalog sync. Missing numbers remain missing and cannot send.
Source-number/branch changes revoke qualification; payloads are rechecked before
each request. The student's profile shows the number, delivery state, pause and
history. The API journal supports student filtering and number search.

## Notification and history boundary

Student check-ins/outs may notify parents and cause billable SMS according to
school settings. The updated backend documentation says events older than one
hour suppress these notifications; the client does not rely on this to send tests.
Do not invent visits or use a real student's number for automated testing.

Student delivery defaults to OFF separately from staff delivery:

```yaml
eduschool_turnstile:
  enabled: true
  students_enabled: false
```

These are fields in the existing section, not a replacement for its branch,
endpoint and device mappings. Setting students_enabled to true and restarting the
sender establishes a separate activation timestamp. Only new student events after
both activation and qualification are considered. Restarting an enabled sender
retains that boundary and pending queue. Disabling/re-enabling does not replay
old or previously skipped/blocked events. No per-event human approval is needed.

The existing staff queue, holds, activation time and endpoint remain unchanged.
Legacy pending staff payloads acquire isCamera at delivery without changing IDs.
Local people, unknown faces and retired ERP records remain ineligible.

## Deployment on another PC

1. Back up PostgreSQL and preserve `.env`, `config`, `data`, models and Compose GPU
   overrides. Update code in the existing working directory, not a second stack.
2. Build all images; keep students_enabled false during rollout:

```powershell
docker compose build
docker compose run --rm --no-deps migrate
docker compose up -d --no-deps ui api vision-worker eduschool-sync eduschool-photo-worker eduschool-turnstile
```

3. Confirm migrations and services succeeded. The catalog synchronizer fetches a
   full snapshot on startup. Check studentNo and FaceID in an actual student card.
4. Confirm the school's notification/SMS policy, branch and a consented live test.
   Set `students_enabled: true` in `config/settings.yaml`, keeping the rest intact:

```powershell
docker compose restart eduschool-turnstile
docker compose logs --tail 50 eduschool-turnstile
```

5. After automatic qualification, have the authorized student make a real pass.
   Verify the local event, attempted POST, HTTP status AND API code 0, and backend
   attendance record. Check both entry and exit on different camera deviceIds.

Do not restart retired `edge-sync` or delete volumes. A code update alone does not
opt the school into student notifications.

## Deliberate limitations

- Uses the single-event endpoint and existing bounded persistent queue, not bulk.
  The documented `/bulk` format is not implemented in this change.
- Staff retry behavior is preserved: explicit failures may retry, but timeouts
  with unknown outcome still require backend reconciliation. Although the new
  document recommends timeout retries, its dedup window is not a client-generated
  event ID; this change does not weaken the existing unknown-outcome safeguard.
- Historical student attendance is not backfilled. Any later replay is a separate
  explicit operation, with notification and duplicate checks.
- A read-only check on 2026-10-09 fetched 10 students from the real catalog. None
  exposed a `studentNo` key (reported total 2017). This is a sample, not a claim
  about every student. Backend must expose `Students.studentNo` in
  `/external-api/students/pagin` or provide its documented lookup endpoint.
  No real attendance POST was made during implementation.

## Validation

Tests use synthetic profiles and a mocked HTTP sender: separate number namespaces,
entry/exit payloads, camera flag, duplicate numbers, missing enrollment/number,
wrong branch, activation/history boundaries, pause/resume, payload revalidation,
legacy staff compatibility, additive migration and the student UI/journal.
PostgreSQL checks run against a disposable database, never production.

2026-10-09 validation: Docker suite ran 199 tests, 177 passed and 22 environment
skips, including student PostgreSQL and migration tests. CPU/CUDA images built;
the student delivery card was visually checked in an isolated browser preview.
Production services/configuration were not switched to the new version.
