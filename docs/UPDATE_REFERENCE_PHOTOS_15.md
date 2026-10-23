# Добавление проверенных эталонов (до 15 на человека) на рабочем ПК

Что меняется: лимит локальных эталонных фото на человека поднят с 10 до 15, и в базу
добавляются 127 вручную подтверждённых фото (72 человека) из пакета
`data/exports/approved-reference-candidates-20261016`. Миграция схемы не нужна:
каждое фото и так хранится отдельной строкой в `eduschool_reference_photos`, а
распознавание сравнивает лицо со всеми эталонами человека и берёт лучшее совпадение.
Эмбеддинги создаются на рабочем ПК той же моделью `buffalo_l`, через штатную проверку
(одно лицо на фото, нет похожего лица у другого человека). Существующие эталоны не
меняются и не удаляются.

Проверено на копии каталога из дампа рабочей БД: импортированы 127 из 127, отказов нет.

## На этом ПК (источник)

```powershell
cd C:\projects\acs2
.\create_update_archive.ps1
```

На флешку скопировать:

1. `acs2-update.zip`
2. папку `data\exports\approved-reference-candidates-20261016` целиком
   (в архив обновления папка `data` не попадает).

## На рабочем ПК

Все команды выполнять в PowerShell из папки программы (ниже `C:\VisionOffice`,
замените на свою). `E:` замените на букву флешки.

### 1. Резервная копия

```powershell
cd C:\VisionOffice
New-Item -ItemType Directory -Force backups | Out-Null
docker compose exec -T postgres pg_dump -U vision_office -Fc -f /tmp/before-refs15.dump vision_office
docker compose cp postgres:/tmp/before-refs15.dump backups\before-refs15.dump
Compress-Archive -Path data\persons -DestinationPath backups\persons-before-refs15.zip
```

Проверьте, что `backups\before-refs15.dump` не пустой (несколько МБ). Дамп в бинарном
формате `-Fc`, поэтому кириллица в именах не портится при перенаправлении PowerShell.

### 2. Обновить код и положить пакет фото

```powershell
Expand-Archive -LiteralPath E:\acs2-update.zip -DestinationPath C:\VisionOffice -Force
New-Item -ItemType Directory -Force data\exports | Out-Null
Copy-Item -Recurse E:\approved-reference-candidates-20261016 data\exports\
```

### 3. Проверить лимит в настройках

```powershell
Select-String -Path config\settings.yaml -Pattern local_reference_photo_limit
```

Если строк нет, ничего делать не нужно: новый код по умолчанию использует 15.
Если там стоит `10`, замените на `15` в обоих разделах (`edge_integration` и
`eduschool_catalog`) через `notepad config\settings.yaml`.

### 4. Пересобрать и запустить

```powershell
.\start_vision_office.ps1
```

### 5. Пробный запуск (ничего не пишет в БД)

```powershell
docker compose run --rm --no-deps migrate python tools/import_reviewed_photos.py --bundle data/exports/approved-reference-candidates-20261016 --output data/exports/refs15-dry-run
```

Ожидается: `"mode": "dry_run"` и `"counts": {"ready": 127}`.
Если `blocked` только с причиной `profile_missing_or_inactive` (человек архивирован
в EduSchool), добавьте к обеим командам `--skip-inactive`: такие фото будут пропущены.
При других `blocked` или `already_present` остановитесь и пришлите файл
`data\exports\refs15-dry-run\report.json`.

### 6. Запись в БД

```powershell
docker compose run --rm --no-deps migrate python tools/import_reviewed_photos.py --bundle data/exports/approved-reference-candidates-20261016 --output data/exports/refs15-apply --apply
```

Занимает несколько минут (CPU). Ожидается `"counts": {"imported": 127}` и код
возврата 0. Перед записью инструмент сам кладёт копию затрагиваемых карточек в
`data\exports\refs15-apply\before-import.zip`. Повторный запуск безопасен: уже
добавленные фото пропускаются (папка `--output` каждый раз должна быть новой).

### 7. Проверка

```powershell
docker compose exec -T postgres psql -U vision_office -d vision_office -Atc "SELECT count(*) FROM eduschool_reference_photos WHERE active AND source = 'local';"
docker compose logs --tail 50 vision-worker
```

Число локальных эталонов должно вырасти на 127. Перезапуск не нужен: камера
перечитывает эталоны из БД каждые несколько секунд.

## Откат

Если что-то пошло не так, восстановите резервную копию из шага 1 (и при необходимости
распакуйте `backups\persons-before-refs15.zip` обратно в `data`):

```powershell
docker compose stop vision-worker edge-sync eduschool-sync eduschool-photo-worker eduschool-turnstile unknown-clusterer api ui
docker compose exec -T postgres psql -U vision_office -d postgres -c "DROP DATABASE vision_office WITH (FORCE);" -c "CREATE DATABASE vision_office OWNER vision_office;"
docker compose cp backups\before-refs15.dump postgres:/tmp/restore.dump
docker compose exec -T postgres pg_restore -U vision_office -d vision_office /tmp/restore.dump
docker compose up -d
```

Не используйте `docker compose down -v`: эта команда удаляет том с базой.

Если после шага 6 остался файл `data\.reviewed-photo-import.lock`, сначала убедитесь,
что импорт не запущен в другом окне, и только потом удаляйте файл.
