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

ReplyPlan / Outbound Arbiter (01C): каждое новое inbox-сообщение атомарно
увеличивает `context_version`, supersede'ит незавершённые планы и создаёт
`ReplyPlan` с `bot_response_delay_ms=5000` и сохранённым `not_before`.
Единственный источник времени для `not_before`, `lease_until` и допуска —
часы PostgreSQL (`app/db/clock.py`): часы хоста приложения не участвуют в
планировании, поэтому рассинхронизация машины и сервера не сдвигает задержку.
Единственная точка допуска исходящего synthetic sink — `OutboundArbiter`.
Реальные channel adapters, AI и боевая отправка сообщений в этот этап не
входят (см. `docs/adr/002-durable-ingress.md` и
`docs/adr/003-reply-outbound-arbiter.md`).

amoCRM mirror (CURSOR-09): односторонний исходящий transactional outbox
`amocrm_mirror_jobs`. Четыре доменных события bot-TV (новое клиентское
сообщение, терминальное состояние `ReplyPlan` `DISPATCHED`/`DEAD`, manager
takeover, допуск synthetic outbound) ставятся в очередь внутри той же
транзакции, что и сам домен, поэтому job существует ровно тогда, когда
изменение закоммичено. `amocrm_mirror_jobs` — последняя таблица в lock order
`conversations → inbox_messages → reply_plans → outbox_messages →
amocrm_mirror_jobs`. Worker захватывает job через lease с fencing token,
перепроверяет живое состояние диалога под блокировкой и переводит устаревшее
событие в терминальный `SKIPPED`. Adapter — локальный no-op sink: реального
amoCRM API, OAuth, внешних идентификаторов, entity-семантики (lead/contact/
note/task) и клиентского текста в этапе нет; payload собирается только по
whitelist технических полей (см. `docs/adr/004-amocrm-mirror.md`).

Attempt exhaustion (BOT-TV-10): `max_attempts` хранится в каждой durable-записи
и является единственным лимитом для claim/fail/recovery. Перед обычным claim
очередь терминализирует истёкший `PROCESSING`, уже использовавший последнюю
разрешённую попытку: запись атомарно переходит в `DEAD`, lease очищается, а
business handler, outbound sink и amoCRM adapter повторно не вызываются.
ReplyPlan recovery сохраняет dialog lock order и ставит терминальное
`REPLY_PLAN_STATE_CHANGED(DEAD)` в mirror outbox.

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
