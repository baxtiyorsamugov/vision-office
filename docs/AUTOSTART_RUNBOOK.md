# Автозапуск Vision Office

## Что делает помощник

`VisionOfficeAutostart.exe` запускается при **входе выбранного пользователя в Windows**. Он не открывает консоль, ждёт готовности Docker Desktop до пяти минут и выполняет безопасную команду:

```powershell
docker compose up -d --no-build
```

Поэтому он использует уже проверенный локальный CPU/GPU-профиль из `docker-compose.override.yml`, не перестраивает образы, не меняет настройки камеры, не делает ERP-импорт и не открывает второй RTSP-поток. Журнал: `data/logs/docker-autostart.log` (старый журнал автоматически ротируется после 2 МБ).

Docker Desktop под Windows запускается после входа пользователя в систему; до первого входа в Windows контейнеры через Desktop не могут быть гарантированно запущены. Это ограничение Docker Desktop/WSL2, а не Vision Office.

## Подготовка один раз

1. Установите Docker Desktop с WSL2 и Linux containers.
2. Разверните проект, `config/settings.yaml`, `.env` и модели согласно [USB-инструкции](../INSTALL_USB.md).
3. Выполните обычный первый запуск, чтобы проверить модели и выбрать CPU/GPU:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_vision_office.ps1
```

4. Соберите лёгкий EXE. Команда нужна только на компьютере разработчика или на устройстве, где отсутствует готовый EXE:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\tools\requirements-build.txt
powershell -ExecutionPolicy Bypass -File .\tools\build_autostart_launcher.ps1
```

5. Установите ярлык в автозагрузку текущего пользователя:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\install_docker_autostart.ps1
```

После следующего входа в Windows Vision Office поднимется сам. Проверить только файлы, не запуская Docker:

```powershell
.\launcher\VisionOfficeAutostart.exe --check
```

Для первого немедленного запуска после установки ярлыка используйте:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\install_docker_autostart.ps1 -RunNow
```

## Контроль и восстановление

```powershell
# Состояние после входа в Windows.
docker compose ps

# Причина неудачного автоматического запуска.
Get-Content .\data\logs\docker-autostart.log -Tail 100

# Логи отдельного сервиса.
docker compose logs --tail 200 vision-worker
```

Если в журнале истекло ожидание Docker Desktop, откройте Docker Desktop один раз и убедитесь, что выбран Linux containers/WSL2. Если отсутствуют контейнерные образы либо изменилась версия кода, выполните вручную `start_vision_office.ps1`, дождитесь успешной проверки, затем автозапуск снова будет использовать сохранённую конфигурацию.

Чтобы приостановить систему до следующего входа, выполните `docker compose stop`. Чтобы полностью отменить дальнейший автозапуск:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\install_docker_autostart.ps1 -Uninstall
```

## Обновление на другом компьютере

EXE не содержит RTSP, ERP, Telegram, пароль PostgreSQL или настройки конкретного устройства. Готовый `launcher/VisionOfficeAutostart.exe` входит в архив обновления, когда архив создан после сборки. После обновления кода достаточно заново выполнить обычный проверочный запуск `start_vision_office.ps1`; ярлык автозапуска сохраняется. При замене папки проекта повторите установку ярлыка, чтобы он ссылался на новый путь.
