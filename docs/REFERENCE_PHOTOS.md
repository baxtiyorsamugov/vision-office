# Reference photos

Reference photos decide who the system can recognise and who it confuses. This
runbook covers adding, auditing and fixing them. Background and numbers:
[DECISIONS.md](DECISIONS.md) D-007…D-010.

## What makes a good reference

| Good | Bad (do not add) |
| --- | --- |
| Face visible from the front or slightly turned | Top of the head, face hidden by a headscarf or hand |
| Crop ≥ 70 px, in focus | Blurry, motion smear, tiny |
| Clearly this person | "Probably", lookalike siblings, someone in the background |
| Taken by the same cameras (`local`) | Several near-identical frames of one pass |

A person needs **2–3 good camera frames** in addition to the EduSchool photo. One
bad frame can make the system recognise *other* people as this person.

## Adding references: the «Разметка» page

Dashboard → **Разметка**. Enter your name first; every decision is logged with it.

1. **Очередь** shows one card per pass of an unknown face. Mode «Почти узнанные»
   (similarity 0.40–0.50) gives the most gain; «Все неизвестные с подсказкой»
   shows everything with a usable frame.
2. Each card: camera frames · the system's guess with the person's **main
   EduSchool photo** and existing camera frames · second guess.
3. Actions: **Да, это …** · **Другой человек** (search by surname) → «Сохранить
   выбранного» · **Не знаю** · **Плохой кадр**.
4. On «Да» the frame passes an automatic check and becomes active within 5 s.
   If it resembles someone else too much, it goes to **Нужна проверка админа**.
5. **Журнал** lists all decisions; a wrongly added reference is switched off there.

Frames smaller than 70 px or blurry never appear. People without any reference
cannot be suggested; add them through «Другой человек» or a better EduSchool photo.

## Batch review (offline pages)

For larger rounds, or when the work PC is not at hand:

```powershell
# On the work PC: export a snapshot
docker compose exec -T postgres pg_dump -U vision_office -Fc -f /tmp/vo.dump vision_office
docker compose cp postgres:/tmp/vo.dump F:\vo-MMDD.dump
robocopy data\events\YYYY-MM-DD F:\events\YYYY-MM-DD /E

# On the development PC: build the review page (near-misses ≥ 0.40)
.\.venv\Scripts\python.exe tools\build_similarity_review.py --input-root data\imports\review-YYYYMMDD `
    --output data\exports\review-near-YYYYMMDD --since YYYY-MM-DD --min-similarity 0.40
```

Decisions exported from the page → `tools/build_reference_bundle.py` → bundle →
`tools/import_reviewed_photos.py` on the work PC (dry run first, then `--apply`).
Since 2026-11-05 the importer applies the same consistency check as the labeling
page to every photo (own photos vs the most similar other person); failed photos
are reported as `rejected` with reason `consistency_check: …` and are not imported.
`--no-consistency-check` disables it (not recommended). Note: a dry run does not
compute embeddings, so check results appear only in the `--apply` report.
If EduSchool renamed a person after the review (same ID and type, e.g. word order
fixed), the importer blocks the whole bundle with `profile_metadata_changed`;
`--accept-renamed` imports such photos and records `renamed_from`/`renamed_to`.
See [UPDATE_REFERENCE_PHOTOS_15.md](UPDATE_REFERENCE_PHOTOS_15.md).

## Auditing the gallery

Run after every large import. A `local` reference is suspicious when it is at
least as similar to another person as to its own photos, or less than 0.30
similar to its own photos. The 2026-11-04 audit found 28 of 808 such frames.

Procedure used then (scripts are ad hoc; port them into `tools/` before reuse):

1. Load active references from a snapshot; compute the similarity matrix.
2. Flag frames by the rule above; build a page: frame · owner's photos · lookalike.
3. A human marks keep/remove.
4. Write a reviewed SQL file in `tools/sql/` that sets `active = false`
   (never `DELETE`), with the list of ids.

## Applying a SQL fix in production

```powershell
cd E:\vision-office
git pull
docker compose exec -T postgres pg_dump -U vision_office -Fc -f /tmp/before-fix.dump vision_office
docker compose cp postgres:/tmp/before-fix.dump backups\before-fix-YYYYMMDD.dump
Get-Content tools\sql\<file>.sql | docker compose exec -T postgres psql -U vision_office -d vision_office
```

The script prints the number of rows changed; compare with the expected count.
To undo a deactivation: `UPDATE eduschool_reference_photos SET active = true WHERE id IN (...);`.

## Who still needs references

A report of every active person by need (no photo · only a poor EduSchool photo ·
only an EduSchool photo · photo unlike camera frames · one camera frame) was built
on 2026-11-05 as `data/exports/Эталоны_лиц_нет_или_плохие_2026-11-05.xlsx`
(not in git: personal data). Status on the 2026-11-03 snapshot:

| Priority | Need | People |
| --- | --- | --- |
| Критично | No reference at all (144 without photo in EduSchool, 42 unusable) | 186 |
| Высокий | Only an EduSchool photo, and it is small/blurred/dark | 122 |
| Средний | Only an EduSchool photo | 670 |
| Низкий | One camera frame | 162 |
| Норма | Enough references | 161 |

EduSchool photo quality thresholds used: short side < 200 px, Laplacian variance
< 70 (on a 256 px center crop), mean brightness < 90 or > 215.

## Verifying the effect

After a round of references, run the bench with a fresh snapshot:

```powershell
.\tools\replay_recordings.ps1 -Action Compare -Device cuda -ProdOnly -Repeat 2 `
    -Snapshot data\imports\review-YYYYMMDD\vo-MMDD.dump
```

Compare recognised people with the previous run and check every newly
recognised person on a verification page before trusting the gain.
