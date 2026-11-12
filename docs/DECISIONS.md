# Decision log

Tuned numbers and design choices, with the evidence behind them. Add an entry
whenever a threshold, guard, timing or data-policy changes, and link the bench
run or review that justified it. Newest first. Older platform decisions
(PostgreSQL, Docker topology, outboxes, EduSchool rollout) are recorded with
evidence in [../ROADMAP.md](../ROADMAP.md).

Format: **Context** · **Decision** · **Evidence** · **Consequences** · **Revisit when**.

---

## D-011 · 2026-11-12 · Reference drift: new references must resemble the main photo

**Context.** Oxunjonova Robiya (student) was marked present on 09, 10 and 12.11 while
absent on 10.11. 7 of her 9 marks matched only one reference: a camera frame imported on
2026-11-06 from the 2026-11-03 journal review (frames recognised at 0.50–0.55 and confirmed by
a person). That frame was 0.50-similar to one of her camera frames but only 0.27 to her
EduSchool photo; the 7 event crops were 0.16–0.32 to her EduSchool photo, i.e. another girl.
The 2 genuine marks (09.11 08:27, 12:00) were 0.50–0.57 to the EduSchool photo. All 9 were
delivered to EduSchool.

**Decision.** (1) Deactivate the 15 references imported on 2026-11-06 with similarity to the
owner's EduSchool photo < 0.30 (`tools/sql/2026-11-12_deactivate_15_drift_refs.sql`).
(2) The consistency check (labeling page and importer) also requires ≥ 0.30 similarity to the
person's EduSchool photo when one exists (`MAIN_PHOTO_FLOOR`). Failures go to the admin tab.

**Evidence.** The 15 share one signature: main-photo similarity 0.10–0.30 and ~0.50 to exactly
one other camera frame. They include Fayziev (confirmed false on 2026-11-04) and Niyatullayeva
("not sure" on 2026-11-06). 81 of 1098 local references overall have main-photo similarity
< 0.30; the remaining 66 older ones need a human review (some owners have old EduSchool photos).

**Consequences.** Journal-review rounds at 0.50–0.55 are the riskiest source of references:
confirming "the system says X" on a lookalike chains a wrong face into X's gallery. The new rule
blocks that chain. A runtime rule (require some main-photo support at match time) would have
blocked the 7 false marks and kept the 2 real ones here, but must pass the replay bench first.

**Follow-up (same day).** The admin reviewed the 66 older references on the drift page:
55 removed, 11 kept (genuine, old EduSchool photo). Deactivated by
`tools/sql/2026-11-12_deactivate_55_drift_refs.sql`; 42 people affected, none loses all camera
frames. 9 of the 55 (11.11 labeling round) had no crop on the page and were removed
conservatively (reversible).

**Revisit when.** The runtime rule is benchmarked.

---

## D-010 · 2026-11-05 · Staff label faces in the dashboard, one confirmation + auto-check

**Context.** Growth in recognition now depends on more camera-frame references
for people who have only an old EduSchool photo (792 of 1303 active people on
2026-11-03; 186 more have no usable photo at all). Operators cannot verify these
children; teachers and guards can.

**Decision.** Add the «Разметка» page (`ui/labeling.py`, `core/face_labeling.py`).
A confirmed frame becomes a reference immediately (single confirmation) only if:
crop ≥ 70 px and sharp enough; similarity to the person's own references ≥ 0.30
and ≥ 0.05 above the most similar other person; a person without references must
not resemble anyone else ≥ 0.45. Failed checks go to the admin tab. Every action is
logged in `face_label_decisions`; added references can be switched off.

**Evidence.** The consistency rule is the same test that found 28 bad references
(D-008). Unit tests: `tests/test_face_labeling.py`.

**Consequences.** Faster reference growth without waiting for review rounds. The same
check runs in `tools/import_reviewed_photos.py` for offline review bundles (default on). The
dashboard has no authentication: keep it on `127.0.0.1` until logins exist.

**Revisit when.** Authentication is added (teachers on phones), or the journal
shows reversed decisions.

---

## D-009 · 2026-11-04 · Track-level embedding averaging: not enabled

**Context.** Averaging the embeddings of a track's attempts raises the score of
consistent frames and was expected to add sensitivity.

**Decision.** Keep single-frame decisions in production. The averaging simulation
stays available on the bench trace.

**Evidence.** On the 4 recordings (prod profile, 2 rounds) averaging added 1–4
people per recording. With the old gallery the additions included Jalilova
(confirmed false) and Djabborova (unverified), both matched through bad references. With the cleaned gallery the
remaining additions (Hamdamova, Akmalova, Rustamov) could not be verified: each
has a single old EduSchool photo.

**Revisit when.** Those people have 2–3 confirmed camera references; then rerun
`Compare` and verify every addition.

---

## D-008 · 2026-11-04 · 28 local references deactivated

**Context.** A confirmed false match (Jalilova) and a suspicious one (Djabborova) traced back
to camera-frame references that look more like other people than like their owner (mostly top-down frames where only
a headscarf is visible; one such frame caused 7 of 8 false Jalilova matches).

**Decision.** Audit every `local` reference: flag it when its best similarity to
another person ≥ its best similarity to its own photos, or own similarity < 0.30.
All 28 flagged frames (17 from 2026-10-24, 11 from 2026-10-28) were reviewed and
deactivated with `tools/sql/2026-11-04_deactivate_28_refs.sql` (reversible).

**Evidence.** 808 local references audited. Re-scoring the bench trace with the
cleaned gallery removed both known false averaged matches. Cost: Islamova (staff)
is no longer matched, because all her working references were among the 28;
identity unconfirmed.

**Consequences.** The same rule now guards every new reference (D-010).

**Revisit when.** A staff member confirms Islamova; restore her best frames.

---

## D-007 · 2026-11-04 · Margin guard 0.05 in production

**Context.** Some accepted matches were near ties between two similar children
(e.g. 0.518 vs 0.503).

**Decision.** `VISION_OFFICE_MIN_MATCH_MARGIN=0.05` for `vision-worker`: the matched
person must beat the best different person by 0.05.

**Evidence.** Across bench runs the guard blocked 3 matches confirmed false by a
human (Fayziev 0.518/0.503, Vaxabov 0.507/0.487, Jalilova 0.535/0.488) and 1
confirmed correct (Saidolimxonov 0.508/0.497). All other verified correct matches
had a gap ≥ 0.058.

**Consequences.** Slightly fewer matches for people with one weak reference; they
should get more references rather than a lower guard.

**Revisit when.** The gallery is richer (gaps of correct matches grow) or the
journal shows frequent low-margin rejections of real people.

---

## D-006 · 2026-11-04 · Unknown-face retry stays at 1.5 s

**Context.** Tracks got only ~2 FaceID attempts while a person crosses the view;
the GPU used ~10 % of its FaceID capacity.

**Decision.** Keep `VISION_OFFICE_UNKNOWN_RETRY_SEC=1.5` (production default).
Faster retry is available on the bench only.

**Evidence.** Retry 0.4 s raised attempts per track 2 → 5 and recognised people
18 → 21 on the 4 recordings, but all 5 additional matches were confirmed false by
a human: more attempts mean more low-quality frames that cross 0.50 for a lookalike.
A softer InsightFace detector threshold (0.3) cut empty attempts 44 % → 14 % with
no recognition gain.

**Revisit when.** References are cleaned and enriched; with threshold 0.55 the
same test produced 0 false matches.

---

## D-005 · 2026-11-04 · EduSchool threshold remains 0.50

**Context.** The threshold was lowered from 0.55 to 0.50 (decided 2026-10-29, in production by 2026-11-03)
with the rule "return to 0.55 on the first confirmed error in 0.50–0.55".

**Decision.** Keep 0.50.

**Evidence.** All 9 production-profile matches in 0.50–0.55 on the recordings were
confirmed correct by a human. The false matches seen on 2026-11-04 came only from
the faster retry experiment (D-006) and from bad references (D-008).

**Revisit when.** Any confirmed false production match in 0.50–0.55.

---

## D-004 · 2026-11-04 · Replay bench for every recognition change

**Context.** Recognition changes were judged from live use, without a baseline.

**Decision.** Recordings are replayed as RTSP into an isolated copy of the system
(`docker-compose.replay.yml`, `tools/replay_recordings.ps1`). Rules: one recording
at a time on the 4 GB development GPU; play once; start only when the camera's AI
is ready; log every attempt (`VISION_OFFICE_RECOGNITION_TRACE`); verify every
changed match by eye. See CLAUDE.md "Changing recognition".

**Evidence.** Two early comparisons were invalid because four cameras loaded the
models for 40–80 s while the video already played; the readiness gate fixed it.

---

## D-003 · 2026-11-04 · Frame labels drawn on small patches

**Context.** Labels used a Windows font path (Cyrillic rendered as boxes in
Docker) and converted the whole frame to PIL per face: ~119 ms per frame with 8
faces on 2560×1440, which also slowed the detection hand-off.

**Decision.** Bundle DejaVu Sans Bold (`core/assets/fonts`), render only the label
patch, cache rendered labels; colour by state (green known, red unknown, yellow
pending) with the score.

**Evidence.** < 1 ms per frame with 8 faces.

---

## Earlier baseline (see ROADMAP.md)

- Detector YOLOv8n-face at imgsz 960 and ≤ 20 detections/s; ByteTrack; ≤ 3 FaceID
  crops per cycle; min box 48 px; 2 observations before FaceID.
- InsightFace `buffalo_l`, crop detector size 320; gallery reload every 5 s.
- Event cooldown 60 s; event photos 30 days; unknown visitors 30 days.
- One process per camera (spawn) so CUDA and failures stay isolated.
- PostgreSQL in Docker; all outbound calls through durable outboxes.
