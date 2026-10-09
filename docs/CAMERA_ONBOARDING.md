# Additional Entrance and Exit Cameras

## Update an existing installation

Install the new code using the site's normal update procedure. Preserve `.env`,
`config`, `data`, models and the PostgreSQL volume. A ZIP installation with an
empty `git init` is not a working clone: do not use reset/clean to force an update.
From the existing deployment directory, retaining its GPU Compose override:

```powershell
docker compose build ui api vision-worker eduschool-turnstile
docker compose up -d --no-deps ui api vision-worker eduschool-turnstile
docker compose ps
```

This one-time update briefly restarts camera processing. PostgreSQL must already
be running. Never run `down -v`. The sender continues to honor its existing
enabled flag, credentials, activation boundary, eligibility and delivery rules.
Starting the container does not override disabled delivery configuration.
Update all four services together: the API's camera monitor also reads the shared
camera file and must understand the new deviceId field before saving new cameras.

## Add a camera

1. Open Cameras and select Add camera.
2. Enter a descriptive name, optional location and the RTSP address.
3. Keep the suggested camera ID or choose a unique ID such as `entrance_02`.
4. Select Entry or Exit and an appropriate processing profile (Balanced initially).
5. Leave deviceId API empty to use the camera ID (maximum 64 characters), or enter
   the unique deviceId agreed with the backend. Existing YAML mappings are retained.
6. Save with the camera disabled first, then enable it and save when ready to test.
7. The supervisor applies the change without restarting other cameras. Model
   loading takes time; verify Connected and AI Ready rather than just Saved.

Camera IDs and saved API deviceIds cannot be reassigned in the form. Duplicate
deviceIds are rejected. Blank RTSP on an existing camera preserves its secret.
Save refreshes the form immediately; subsequent edits and per-camera restarts use
the new revision. Disabling a camera retains its route for queued events.

## API boundary

The existing POST `/external-api/turnstile/attendance` contract is unchanged:
entry becomes `check_in`, exit becomes `check_out`, and `deviceId` identifies the
camera. Only routes are reloaded by the running sender on its normal poll cycle.
No key, endpoint, branch, automatic qualification, retry rule or activation time
is changed. A configured route does not prove the sender is running or delivery
succeeded. Qualified EduSchool staff remain eligible. Student delivery requires
the separate [student rollout](EDUSCHOOL_STUDENT_ATTENDANCE.md) and opt-in flag;
adding a camera does not enable it. Local people and unknown faces remain excluded.

Do not label a corridor or outdoor observation as entry/exit merely to send it.
Those locations need an agreed backend contract before enabling delivery.

## Acceptance on the target PC

- Add one camera at a time. Check Connected, AI Ready, FPS and processing delay.
- Verify an authorized staff member's real pass creates the correct camera and
  direction in the local events, then inspect the delivery log and backend result.
- Test both directions; confirm camera IDs differ and old cameras continue working.
- Pause/resume the new camera and verify the original cameras are unaffected.
- Observe peak activity for 30 minutes before adding more cameras. No camera
  capacity or recognition accuracy is guaranteed without that machine's test.

Controls and RTSP credentials remain in `data/camera_controls.json` on this PC.
Back up that file privately; never upload it to GitHub or attach it to support logs.
Removing camera overrides manually can remove API routes; prefer disabling cameras
instead, especially with pending deliveries. Restrict dashboard access to operators.

## Verification

Automated tests cover entry/exit payloads using a mocked HTTP sender, unchanged
existing mappings and credentials, duplicate/reassigned IDs, invalid configuration,
retained disabled-camera routes, and repeated Streamlit edits without exposing
the saved RTSP secret. Additional physical streams are not available yet; live
multi-camera capacity and real API acceptance remain target-PC checks.

On 2026-10-09, Docker ran all 179 discovered tests: 157 passed, 22 were skipped
by environment guards. The eight onboarding tests also passed on Windows and
inside Docker. CPU service and CUDA worker images built successfully. The form
was visually checked in a temporary Docker UI at desktop and 390px width without
saving any production camera changes or sending real API events.

The separate Windows-wide test run had three pre-existing environment/portability
errors: two missing-openpyxl errors and a ZIP test expecting Windows backslashes
instead of archive slashes. Those tests pass in the deployed Linux container.
