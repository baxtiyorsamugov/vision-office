# EduSchool staff turnstile attendance

The example configuration is **off by default**. This device was enabled on
2026-09-24 after a validation-only `55103 UserNotFound` probe. The probe did
not create attendance. Staff now qualify automatically only when the directory
record is active, `employeeNo` is unique, and an active FaceID photo exists.
The integration is separate from the hourly EduSchool
student/staff catalog, the existing learning-center ERP outbox, and camera workers.
No student, unknown visitor, local employee or legacy ERP event is eligible.

## Prerequisites

1. Ask the school administrator for the camera branch ID and the `apiKey` of an
   **Other-type** EduSchool integration as documented by EduSchool. On this
   device, the supplied existing key reached `55103 UserNotFound` for a random
   nonexistent employee and a future timestamp: the authorization path accepted
   the key/branch without creating attendance. Confirm its intended scope with
   the school before broader rollout. The catalog Bearer token is not used for
   attendance. Never put either credential in Git or screenshots.
2. The EduSchool staff record must have a unique, nonempty `employeeNo` (1-64
   characters) and a valid source/local FaceID photo. These conditions trigger
   automatic delivery eligibility. Automatic matching is not human identity
   verification: review suspicious photos in **Employees > EduSchool** and use
   the per-person pause switch when necessary.
3. Give each active camera a distinct stable `deviceId` and verify the entry/
   exit direction in `config/settings.yaml`. Keep the Edge clock synchronized.
4. Create a backup of the PostgreSQL volume before a production rollout.

Put the dedicated key in the ignored `.env` file:

```text
EDUSCHOOL_TURNSTILE_API_KEY=<OTHER_INTEGRATION_KEY>
```

Copy the `eduschool_turnstile` example block into the ignored
`config/settings.yaml`, set its `branch_id`, map every intended camera ID to a
unique `deviceId`, then set `enabled: true`. Restart only the new sender:

```powershell
docker compose up -d --build eduschool-turnstile
docker compose logs --tail 100 eduschool-turnstile
```

`POST https://backend.eduschool.uz/external-api/turnstile/attendance` sends
`employeeNo`, `check_in`/`check_out`, original UTC `eventTime`,
`method=face_recognition`, and stable `deviceId`. The `apikey` and `branch`
headers are applied by the isolated sender. It has a 0.25 CPU / 256 MB budget
and never opens RTSP. The existing camera and ERP services are not restarted by
the command above.

## Operator workflow

- The sender activates from the current time. It does **not** replay old
  recognition events. Automatic qualification is refreshed by the sender;
  only events after qualification can be sent. Previously skipped events stay
  skipped and do not replay when a photo becomes ready.
- The outbox records one decision per local recognition event: `skipped`
  (ineligible), `pending`, `sending`, `retry`, `sent`, `blocked`, or `ambiguous`.
  The profile shows recent decisions and response codes.
- `code: 0` and `duplicate: true` both mean success. HTTP 5xx and definite
  connection failures retry with backoff. Codes 10004, 10500, 10600, 51804,
  55103, 55101 and 422 stop for correction. Check the payload, key, branch, academic year,
  employee number, clock or payload respectively.
- A timeout or interrupted in-flight request has **unknown outcome**. It is
  never retried automatically. Find the matching `employeeNo`, direction,
  timestamp and camera in EduSchool first. In the profile, mark **record exists**
  or **no record**. Only the latter authorizes a retry. EduSchool's documented
  duplicate window is not a general exactly-once guarantee.
- An inactive profile, missing/duplicate `employeeNo`, or loss of an active
  FaceID photo removes qualification automatically. A changed source photo or
  number resets the prior qualification until the updated profile has an
  eligible photo and number again. The pause switch stays in force through
  catalog refreshes until the operator releases it. Queued requests are
  rechecked immediately before sending. `skipped` and `blocked` rows remain
  for audit and do not auto-requeue.
- To stop outbound traffic, set `enabled: false` and restart only the sender.
  Pending/retry rows become blocked; in-flight rows become ambiguous for
  reconciliation. Keep the PostgreSQL volume for audit.

## Acceptance before production

Use one consented test employee with a verified `employeeNo` and photo. Confirm
the branch and key with the school. Check a single entry then exit in EduSchool,
the local outbox status, timestamp in Asia/Tashkent, and that the legacy ERP
outbox, unknown/local people and both camera FPS remain unchanged. Simulate an
offline interval and restart; inspect `retry`/`ambiguous` before any manual
replay. This acceptance has **not** been performed by automated tests.
