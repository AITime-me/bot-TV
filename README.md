# Bot TV — Backend

Минимальный fail-closed фундамент Bot Core на FastAPI. Требуется Python 3.10+.

## Безопасные значения по умолчанию

- `BOT_MODE=OFF`
- `EMERGENCY_LOCK=true`
- токены каналов, AI, PostgreSQL и Redis для запуска в `OFF` не требуются;
- автоматические исходящие действия не разрешены ни в одном режиме.

Допустимые значения `BOT_MODE`: `OFF`, `HINTS`, `DRAFT`, `AUTO_READ`,
`AUTO_WRITE`. Неизвестное значение отклоняется при запуске.
`EMERGENCY_LOCK` имеет безусловный приоритет над режимом.
Для `EMERGENCY_LOCK` допустимы только точные значения `true` и `false`;
регистр, пробелы и псевдобулевы значения не нормализуются.

Даже `BOT_MODE=AUTO_WRITE` и `EMERGENCY_LOCK=false` пока не подключают внешние
интеграции и сами по себе не разрешают отправку сообщений.

## Локальный запуск

```bash
python -m venv venv
```

Активируйте окружение:

```powershell
# Windows PowerShell
.\venv\Scripts\Activate.ps1
```

```bash
# Linux / macOS
source venv/bin/activate
```

Установите полный закреплённый набор runtime-зависимостей и запустите
приложение:

```bash
python -m pip install -r requirements-lock.txt
uvicorn app.main:app --reload
```

`requirements.txt` фиксирует прямые production-зависимости,
`requirements-lock.txt` — проверенный полный набор прямых и транзитивных
production-зависимостей для Python 3.12. Platform-specific зависимости
закреплены в lock-файлах с environment markers и применяются только на
поддерживаемых платформах.

Приложение безопасно запускается без `.env`. При необходимости настройки
передаются через environment; `.env.example` служит только перечнем имён и
безопасных defaults.

PostgreSQL (BOT-CORE-FOUNDATION-01A / BOT-CORE-INGRESS-01B) опционален для
`BOT_MODE=OFF` health-запуска. Для миграций и durable ingress задайте
`DATABASE_URL` в форме `postgresql+asyncpg://...`. Миграции:
`alembic upgrade head`.

Durable ingress (01B): событие сначала фиксируется в `ingress_events`, и только
после успешного commit синтетический адаптер подтверждает приём. Worker
захватывает строку через lease (`FOR UPDATE SKIP LOCKED` + fencing token) и
затем переиспользует foundation `InboundService` для conversation/inbox/outbox.
Реальные channel adapters, AI, ReplyPlan и Outbound Arbiter в этот этап не
входят (см. `docs/adr/002-durable-ingress.md`).

Интеграция режимов с control plane `online-zapis-tv` запрещена до
`CONTRACT-MODE-01` (см. `docs/adr/001-mode-contract-deferred.md`).

Health endpoints:

- `GET /health` — совместимый базовый ответ;
- `GET /health/live` — процесс жив;
- `GET /health/ready` — конфигурация успешно загружена.

## Тесты

```bash
python -m pip install -r requirements-dev-lock.txt
python -m pytest -p no:cacheprovider
```

`requirements-dev.txt` фиксирует прямые dev-зависимости и подключает
`requirements.txt`; `requirements-dev-lock.txt` содержит полный
воспроизводимый набор для тестов.

Без `BOT_TV_TEST_DATABASE_URL` PostgreSQL-интеграционные тесты пропускаются.
Для полного прогона на изолированной test-БД задайте переменную только в
текущей shell-сессии (не в файлах репозитория):

```powershell
# PowerShell example — use a disposable test database name with a discrete
# "test" segment. Never point this at production/staging.
$env:BOT_TV_TEST_DATABASE_URL = "postgresql+asyncpg://USER:PASSWORD@127.0.0.1:5432/bot_tv_foundation_test"
python -m pytest -p no:cacheprovider
Remove-Item Env:BOT_TV_TEST_DATABASE_URL
```

`DATABASE_URL` для destructive fixtures никогда не используется. Имя БД должно
содержать отдельный сегмент `test` (`bot_tv_test`, `test_bot`, …). Пароль и
полный URL не должны попадать в логи или коммиты.
