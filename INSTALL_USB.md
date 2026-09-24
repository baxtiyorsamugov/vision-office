# Установка Vision Office с флешки

Актуальный способ: Docker Desktop + PostgreSQL + браузерный интерфейс.
На новом компьютере не нужно отдельно устанавливать Python, pip и PostgreSQL.
Первая сборка требует интернета. Новый запуск автоматически выбирает GPU/CPU,
проверяя вычисления YOLO и FaceID на самом компьютере. При неудачной проверке
CUDA режим `auto` проверяет и использует CPU.

Ниже рабочий проект: `C:\projects\acs2`, новый проект: `D:\acs`, флешка: `E:`.
Замените пути на свои. Инструкция предназначена для новой установки в пустую
папку; не заменяйте по ней настройки и базу уже работающего устройства.

## 1. Подготовить комплект на рабочем компьютере

В PowerShell:

```powershell
cd C:\projects\acs2
powershell -ExecutionPolicy Bypass -File .\create_update_archive.ps1
```

Скрипт использует Python рабочего компьютера. Он собирает текущие файлы кода,
включая новые файлы, ещё не отправленные в GitHub. Несмотря на название,
`acs2-update.zip` подходит и для первой Docker-установки.

Создайте на флешке новую папку `VisionOffice` и поместите в неё:

| Файл или папка | Откуда взять |
| --- | --- |
| `acs2-update.zip` | Корень рабочего проекта |
| `models` целиком | Папка рабочего проекта |
| `settings-device.yaml` | Копия `config\settings.yaml`, переименованная при переносе |
| `INSTALL_USB.md` | Эта инструкция |
| ZIP каталога сотрудников (необязательно) | «Сотрудники» → «ERP» → «Перенос каталога» → «Экспорт» |

Не переносите `.venv`, `.env`, `docker-compose.override.yml`, старые посещения или сырой Docker volume.
Локальный GPU-профиль новый компьютер должен выбрать самостоятельно.
Старый `create_transfer_archive.ps1` создаёт SQLite-комплект и для этой схемы не нужен.

Копия настроек содержит пароли камер и ключи интеграций. Не публикуйте её;
после установки удалите с флешки. Для самостоятельного нового Edge-устройства
получите собственные `device_id` и `device_api_key`. Два одновременно работающих
устройства не должны представляться одним устройством ERP.

## 2. Установить и запустить Docker Desktop

Установите [Docker Desktop для Windows](https://docs.docker.com/desktop/setup/install/windows-install/)
на поддерживаемую 64-битную Windows. Используйте Linux containers и WSL 2.
Выполните перезагрузку, если её запросит установка. Запустите Docker Desktop
и дождитесь состояния Engine running.

```powershell
docker version
docker compose version
```

В `docker version` должны присутствовать **Client и Server**.
Если Server недоступен, сначала восстановите запуск Docker.

## 3. Скопировать проект на новый компьютер

Используйте пустую папку. Если `D:\acs` уже содержит рабочую установку,
не выполняйте эти шаги поверх неё.

```powershell
New-Item -ItemType Directory -Path D:\acs
Expand-Archive -LiteralPath E:\VisionOffice\acs2-update.zip -DestinationPath D:\acs
Copy-Item -LiteralPath E:\VisionOffice\models -Destination D:\acs\models -Recurse
Copy-Item -LiteralPath E:\VisionOffice\settings-device.yaml -Destination D:\acs\config\settings.yaml
cd D:\acs
```

Проверьте файлы:

```powershell
Get-Item .\Dockerfile, .\docker-compose.yml, .\docker\entrypoint.sh
Get-Item .\start_vision_office.ps1, .\docker-compose.gpu.yml, .\docker\check_runtime.py
Get-Item .\import_erp.ps1, .\tools\import_erp_catalog.py, .\tools\import_backend_people.py
Get-Item .\models\yolov8n-face.pt
Get-ChildItem .\models\insightface\models\buffalo_l\*.onnx
```

В `buffalo_l` должны быть перенесённые ONNX-модели, включая `det_10g.onnx`
и `w600k_r50.onnx`. Не допускайте вложенности `models\models`.

## 4. Настроить новый компьютер

```powershell
notepad .\config\settings.yaml
Copy-Item .\.env.example .\.env
notepad .\.env
```

До первого запуска PostgreSQL замените `VISION_OFFICE_POSTGRES_PASSWORD`
в `.env` случайным паролем. Для совместимости со строкой подключения используйте
не менее 32 латинских букв и цифр. На существующей базе смена значения в `.env`
сама по себе не меняет пароль PostgreSQL.

Проверьте в `settings.yaml`:

- RTSP-адреса, доступные из сети нового компьютера; активны только подключённые камеры.
- Для входа `event_type: entry`, для выхода `event_type: exit`; ID камер разные.
- В `edge_integration`: `enabled: true`, `base_url: https://tatibaev.uz`, ключ и UUID устройства.
- Часовой пояс `timezone: Asia/Tashkent` в `edge_integration`.
- Telegram-настройки, если нужны уведомления.

Подключение PostgreSQL задаёт Docker Compose. Отдельно менять адрес БД в YAML
для этой установки не требуется. Административный пароль ERP и JWT в `.env` не добавляйте.

## 5. Собрать и запустить

Выполняйте по очереди. При ошибке разберите её перед следующим шагом.

```powershell
cd D:\acs
docker compose config --quiet
powershell -ExecutionPolicy Bypass -File .\start_vision_office.ps1
docker compose ps -a
```

Первая сборка скачивает зависимости и может занять значительное время;
CUDA-образ дополнительно скачивает несколько гигабайт. Для NVIDIA нужен
совместимый Windows-драйвер и GPU-доступ из WSL2, отдельный CUDA Toolkit не нужен.
Скрипт проверяет обе модели для всех активных камер и ожидает healthchecks.
Принудительный CPU: `powershell -ExecutionPolicy Bypass -File .\start_vision_office.ps1 -Profile cpu`.
Проверка временно останавливает запущенные камеры; выполняйте её в окно обслуживания.
Подробности и ограничения: [GPU/CPU runbook](docs/GPU_RUNTIME.md).

| Сервис | Ожидаемое состояние |
| --- | --- |
| postgres | healthy |
| migrate | Exited (0), нормальное завершение миграции |
| vision-worker, api, ui, unknown-clusterer | running, затем healthy |
| edge-sync | running; успех синхронизации проверяется по журналу |

```powershell
docker compose logs --tail 60 edge-sync
docker compose logs --tail 60 vision-worker
```

Откройте на новом компьютере:

- [Панель сотрудников и событий](http://127.0.0.1:8501).
- [Отдельный монитор камер](http://127.0.0.1:8000/monitor).
- [Локальный Swagger](http://127.0.0.1:8000/docs).

Docker не открывает отдельное окно OpenCV.

## 6. Один раз импортировать полные карточки ERP

**Простой вариант без входа администратором ERP:** если на исходном компьютере
уже есть готовые сотрудники, экспортируйте их ZIP через «Сотрудники» → «ERP» →
«Перенос каталога». На новом компьютере в этом же разделе выберите «Импорт»,
загрузите ZIP, проверьте изменения и подтвердите тот же учебный центр.
Фотографии и FaceID попадут в локальную PostgreSQL с исходными ERP ID;
повторный импорт не создаст дубли. У обоих компьютеров должны быть собственные
настроенные device-ключи того же центра. Архив содержит биометрию, храните его
на защищённом носителе. [Подробная инструкция](docs/CATALOG_TRANSFER_RUNBOOK.md).

**Альтернатива: загрузить полные карточки напрямую из ERP.**

Если список появился, но вместо имён видно `employee + ID` и «Требуется фото»,
сокращённый device API не передал ФИО и фотографии. Запустите:

```powershell
cd D:\acs
powershell -ExecutionPolicy Bypass -File .\import_erp.ps1
```

Введите по запросу:

1. Learning Center UUID. Для ранее использованного центра:
   `78e17fab-e021-5553-bb51-7840fe68355f`.
   Для другого центра используйте его UUID из ERP. Это **не device_id**.
2. Логин ERP с правом чтения сотрудников центра.
3. Пароль ERP. Символы при вводе не отображаются.

Мастер временно остановит работающий `edge-sync`, импортирует сотрудников
и возобновит синхронизацию. Камеры продолжат работу; создание embeddings
потребует ресурсов CPU. Пароль и токен не сохраняются. Пересборка Docker не нужна.

В итоговой строке смотрите `Updated`, `photos`, `FaceID ready`, `issues`.
При `issues > 0` импорт частичный: проверьте фото, модели и доступ к ERP.
Обновите страницу «Сотрудники». Повторный импорт обновляет записи по ID без дублей.

Пока backend не расширил `/persons/sync`, для новых ФИО и фотографий может
потребоваться повторный запуск мастера. Кнопка полного обновления ERP получает
весь список, но использует тот же сокращённый endpoint.

Подробнее: [одноразовый импорт ERP](docs/ERP_ONETIME_IMPORT.md).

## 7. Приостановить и запустить позднее

```powershell
cd D:\acs
docker compose stop
```

Для повторного запуска откройте Docker Desktop, затем:

```powershell
cd D:\acs
docker compose start
docker compose ps
```

После изменения камер в настройках:

```powershell
docker compose restart vision-worker edge-sync
```

Не выполняйте `docker compose down -v`: параметр `-v` удаляет том PostgreSQL.

## 8. Диагностика

**Не найден docker_engine.** Запустите Docker Desktop. Дождитесь появления
Server в `docker version`.

**Virtualization support not detected.** Проверьте виртуализацию в BIOS/UEFI
и WSL 2. Переустановка проекта не исправляет эту ошибку.

**Миграция завершилась с ошибкой.** Получите точную причину:

```powershell
docker compose logs --tail 100 migrate
```

Код 127 сам по себе не доказывает проблему окончаний строк. Если журнал
указывает на `entrypoint.sh`, `/bin/sh^M` или неверные окончания строк,
перенесите актуальные Dockerfile и `.gitattributes` из комплекта и выполните:

```powershell
docker compose build
docker compose up -d
```

Актуальный Dockerfile нормализует Linux-скрипт во время сборки.

**Нет import_erp_catalog.py.** Перенесите актуальные `import_erp.ps1`,
`tools\import_erp_catalog.py` и `tools\import_backend_people.py` в соответствующие
папки проекта. Запускайте мастер через `import_erp.ps1`.

**Импорт ERP: 401 / 403 / 404.** Проверьте логин, права аккаунта и UUID центра
соответственно. Не публикуйте пароли или токены в журналах и снимках экрана.

**Нет видео.** Проверьте `docker compose logs --tail 100 vision-worker`,
RTSP, сеть до регистратора и наличие моделей. Running у контейнера ещё не
подтверждает наличие свежих кадров.

Перед вводом в работу проверьте фото и FaceID, реальное событие входа/выхода,
свежесть кадров, повторный запуск и синхронизацию ERP. Эта установка создаёт
новую PostgreSQL. Старые посещения и локальные сотрудники не переносятся;
их миграция выполняется отдельно по [PostgreSQL runbook](docs/POSTGRESQL_RUNBOOK.md).
