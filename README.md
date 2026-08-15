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

Control plane (`online-zapis-tv`) использует отдельный enum
`OFF|TEST|HINTS|DRAFT|AUTO`. Явный dual-enum contract — в
`app/core/mode_contract.py` и ADR-001: `TEST` не является `BotMode`;
control-plane `AUTO` не alias `AUTO_WRITE` (максимум `AUTO_READ` до
отдельного OWNER write gate). Live Booking Service S2S reads
(eligibility/availability) разрешены только при `AUTO_READ`/`AUTO_WRITE`
и `EMERGENCY_LOCK=false`.

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
событие в терминальный `SKIPPED`. `MIRRORED` значит: требуемое состояние
сущностей amoCRM для этого job успешно сошлось — не «текст сообщения скопирован
в CRM». При выключенном/невалидном CRM REST адаптер не делает CRM HTTP.
Очередь, lease и fencing остаются `amocrm_mirror_jobs` (см.
`docs/adr/004-amocrm-mirror.md`).

Attempt exhaustion (BOT-TV-10): `max_attempts` хранится в каждой durable-записи
и является единственным лимитом для claim/fail/recovery. Перед обычным claim
очередь терминализирует истёкший `PROCESSING`, уже использовавший последнюю
разрешённую попытку: запись атомарно переходит в `DEAD`, lease очищается, а
business handler, outbound sink и amoCRM adapter повторно не вызываются.
ReplyPlan recovery сохраняет dialog lock order и ставит терминальное
`REPLY_PLAN_STATE_CHANGED(DEAD)` в mirror outbox.

Resumable handoff (CURSOR-10): synthetic-сообщения менеджера хранятся
канонически в `manager_messages` и применяются только по обязательному
монотонному `provider_sequence`. Повторы, запоздалые события и сообщения без
надёжного порядка не меняют FSM. Клиентские и применённые менеджерские реплики
получают общий `conversation_event_seq`; контекст ограничен 40 сообщениями и
12 000 символами и не копируется в ReplyPlan/outbox. Первый ответ клиента во
время `HUMAN_ACTIVE` создаёт `HUMAN_PAUSE` и deferred ReplyPlan на
настраиваемые 10–15 минут,
следующие ответы заменяют план без продления deadline. Новый ответ менеджера
возвращает `HUMAN_ACTIVE` и отменяет только недопущенную bot-работу. Worker
истечения deadline атомарно возвращает `HUMAN_ACTIVE` без ответа, а
`HUMAN_PAUSE` — с единственным актуальным deferred ReplyPlan. Due rows
выбираются по часам PostgreSQL через `FOR UPDATE SKIP LOCKED`; падение до
commit оставляет их следующему процессу
(`docs/adr/005-resumable-manager-handoff.md`).

Централизованная защита PII на небезопасных границах (логи, repr, будущий
AI-контекст) описана в `docs/adr/007-pii-gateway.md`. Plaintext в PostgreSQL
для бизнес-функций сохраняется; gateway не маскирует durable storage.

Outbound admission фиксируется отдельным durable-состоянием `ADMITTED` под
блокировкой Conversation. До commit этой транзакции manager/client event может
отменить строку; после commit отмена запрещена. Synthetic sink вызывается только
после закрытия транзакции, с `outbound_id` как idempotency key, а успешный
результат фиксируется отдельным `ADMITTED → DELIVERED`. Падение между этими
точками оставляет восстанавливаемую `ADMITTED`-строку. Три fence
(`context_version`, `manager_epoch`, `event_seq_hwm`) проверяются и при dispatch,
и непосредственно при admission.

Настройки handoff:

- `HANDOFF_PAUSE_SECONDS=900` — от 600 до 900 секунд;
- `HANDOFF_EXPIRY_POLL_SECONDS=1` — от 1 до 60 секунд.

## Worker runtime и health

`python -m app.worker` запускает отдельный процесс с пятью независимыми
циклами: durable ingress, expiry handoff, `ReplyPlan`, outbound и локальный
amoCRM mirror. Каждый цикл:

- обрабатывает ограниченный batch;
- имеет timeout одного tick;
- записывает собственный heartbeat в PostgreSQL;
- после серии последовательных ошибок завершает весь worker с ненулевым кодом,
  чтобы supervisor действительно выполнил restart.

При старте worker атомарно регистрирует новый `generation_id` для всех циклов.
Обновления предыдущего поколения после restart отклоняются fencing-проверкой.
Heartbeat содержит только техническое состояние процесса — текста сообщений,
контактов, токенов и provider payload в таблице нет.
Отдельный PostgreSQL advisory lock допускает только один активный worker:
случайно запущенная вторая копия завершается до перерегистрации heartbeat.

Настройки worker:

- `WORKER_POLL_SECONDS=1` — poll основных очередей, 1–60 секунд;
- `WORKER_BATCH_SIZE=100` — максимум строк за tick, 1–1000;
- `WORKER_TICK_TIMEOUT_SECONDS=20` — timeout tick, 5–300 секунд;
- `WORKER_HEARTBEAT_INTERVAL_SECONDS=10` — период heartbeat, 1–60 секунд;
- `WORKER_HEARTBEAT_STALE_SECONDS=45` — окно свежести, 10–600 секунд;
- `WORKER_MAX_CONSECUTIVE_FAILURES=3` — порог завершения процесса, 1–20.

Перед запуском worker проверяет, что stale-окно больше timeout и как минимум
двух самых длинных poll/heartbeat-интервалов с запасом. `DATABASE_URL` для
worker обязателен.

Docker runtime состоит из одноразовой миграции, API и отдельного worker:

```bash
docker compose config --quiet
docker compose build
docker compose up -d
```

Обычный `docker compose up -d` **не** запускает attachment maintenance.
Сервис `attachment-maintenance` объявлен с profile `attachment-maintenance` и
остаётся выключенным, пока оператор явно не активирует profile **и** не
выставит `ATTACHMENT_MAINTENANCE_ENABLED=true`.

### Attachment maintenance (CURSOR-13 Stage 3A, default-off)

Compose wiring присутствует, но staging/production activation **не** входит в
Stage 3A и требует отдельного разрешения владельца.

Перед любым будущим включением (Stage 3B preflight):

- Docker Compose на хосте >= 2.24.0 (`env_file.required=false`);
- schema/migrations актуальны;
- external host-local keyring file существует по
  `ATTACHMENT_SPOOL_KEYS_ENV_FILE` (default
  `/etc/bot-tv/attachment-spool-keys.env`), вне Git, с ограниченными правами
  чтения; файл должен содержать active key id, active key material и все ещё
  нужные старые `ATTACHMENT_SPOOL_KEY_<ID>`;
- named volume `attachment-spool` смонтирован в container path
  `/var/lib/bot-tv/attachment-spool`; пользователь контейнера `bot-tv` должен
  иметь read/write/delete до включения (не исправлять права запуском от root);
- не печатать `docker compose config` без `--quiet`, rendered environment и
  содержимое keyring-файла;
- запускать одну replica; не использовать `--scale`.

Immediate rollback будущего включения: остановить только
`attachment-maintenance`; api/worker, БД и spool не трогать.

Любой будущий producer/consumer `AttachmentSpoolStore` обязан использовать тот
же named volume, тот же container path, ту же БД и совместимый keyring.

### Обязательный шлюз перед первым deploy

До production/staging deploy `CURSOR-10` необходимо выполнить в среде с
настоящим Docker/Podman и disposable PostgreSQL:

- `docker compose config --quiet` с безопасными `BOT_MODE=OFF` и
  `EMERGENCY_LOCK=true`;
- фактический `docker compose build` (статические тесты Dockerfile/YAML его не
  заменяют);
- `docker compose up -d` на disposable БД и успешное завершение миграции;
- состояние `healthy` у API и worker, включая все пять PostgreSQL-heartbeat;
- smoke-проверку `/health`, `/health/live` и `/health/ready`;
- полный PostgreSQL test suite без пропусков;
- проверку, что build context и итоговый образ не содержат `.env`, токены,
  credentials или Git-метаданные.

Если хотя бы один пункт не выполнен, deploy заблокирован. После проверки
контейнеры также остаются в `BOT_MODE=OFF`; включение реальных адаптеров и
автоматической отправки не входит в `CURSOR-10`.

### Controlled amoCRM enablement (after disabled deploy)

Порядок только такой. На каждом шаге `BOT_MODE=OFF` и `EMERGENCY_LOCK=true`
сохраняются; Chat/CRM HTTP включаются **только** явными `AMOCRM_*_ENABLED=true`.

1. **Disabled deploy** — `docker compose up -d` с defaults: все
   `AMOCRM_*_ENABLED=false` (compose прокидывает флаги; секреты/OAuth key
   bytes не в образе). Smoke `/health*`.
2. **OAuth bootstrap** (host/venv, offline CLI; образ ops не содержит):
   `python -B -m app.amocrm_crm_ops bootstrap` при настроенных
   `AMOCRM_CRM_OAUTH_*` keys / `DATABASE_URL`. CRM REST может оставаться
   `false`.
3. **Chat binding seed** (explicit ids only, no Chat/CRM HTTP):
   `python -B -m app.amocrm_chat_binding_ops seed-binding \
     --conversation-id UUID \
     --amocrm-chat-id CHAT_ID \
     --integration-conversation-id INTEG_CID`
   Outcomes / exit codes (safe under `set -e` for success paths):
   - `SEEDED` (new ACTIVE row) → exit `0`
   - `UPDATED` (same conversation+chat, filled NULL `integration_conversation_id`
     once) → exit `0`
   - `ALREADY_PRESENT` (identical ACTIVE binding) → exit `0`
   - `REFUSED` / errors (conflict, repoint, invalid input) → exit `2`
   Conflict/repoint of non-null integ or conversation/chat mismatch → fail
   closed, zero mutation.
4. **Staged enablement** (по одному, с проверкой):
   - `AMOCRM_CHAT_WEBHOOK_ENABLED=true` + channel secret + scope → manager
     ingress (`POST /webhooks/amocrm/chat/{scope_id}`);
   - `AMOCRM_CHAT_EGRESS_ENABLED=true` + scope → CLIENT_INBOUND / BOT_OUTBOUND
     projection;
   - `AMOCRM_CRM_REST_ENABLED=true` + client id/secret + API base +
     `AMOCRM_CRM_REDIRECT_URI` (exact OAuth redirect_uri);
   - `AMOCRM_CRM_DEAL_CREATE_ENABLED=true` + pipeline/status → TECHNICAL_DEAL.
   Rollback шага: соответствующий enable-флаг → `false` (zero HTTP).

`docker-compose.yml` сохраняет безопасные defaults `BOT_MODE=OFF` и
`EMERGENCY_LOCK=true`, не принимает `DATABASE_URL` по умолчанию, запускает
контейнеры без root/capabilities с read-only filesystem и применяет
`restart: unless-stopped`. `.dockerignore` исключает `.env`, Git-метаданные,
локальные окружения, тесты и документацию из build context и по default-deny
правилу разрешает только runtime source, lock-файл и Alembic assets.

Docker health worker читает все пять heartbeat из PostgreSQL. Сам worker
дополнительно контролирует зависший tick через timeout: один только статус
`unhealthy` не выдаётся за supervisor.

Интеграция режимов с control plane `online-zapis-tv` запрещена до
`CONTRACT-MODE-01` (см. `docs/adr/001-mode-contract-deferred.md`).

Health endpoints:

- `GET /health` — совместимый базовый ответ;
- `GET /health/live` — процесс жив;
- `GET /health/ready` — при настроенной БД проверяет все пять heartbeat и
  возвращает HTTP 503 для отсутствующего, stale, failed или stuck цикла.
  Без `DATABASE_URL` сохраняется прежний безопасный standalone health-контракт
  для `BOT_MODE=OFF`; Docker runtime всегда требует БД.

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
