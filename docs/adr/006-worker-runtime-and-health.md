# ADR-006: отдельный worker runtime и generation-fenced health

## Статус

Принято для `CURSOR-10`, этап 5.

## Решение

API и worker запускаются разными процессами. Worker содержит пять обязательных
циклов: ingress, handoff expiry, ReplyPlan, outbound и локальный amoCRM mirror.
Реальные channel adapters по-прежнему не подключены. AMO-01B2 подключает
CRM REST entity convergence в существующий mirror-цикл (default-off).

При каждом старте создаётся новый случайный `generation_id`. Все пять строк
`worker_heartbeats` перерегистрируются одной транзакцией. Любое последующее
обновление heartbeat содержит `WHERE generation_id = ...`; предыдущий процесс
после restart теряет право подтверждать здоровье.

Перед регистрацией worker удерживает session-level PostgreSQL advisory lock.
Вторая копия завершается до изменения heartbeat, поэтому два контейнера не
устраивают бесконечную перерегистрацию поколений. Закрытие dedicated connection
освобождает lock даже при аварийном завершении процесса.

Успешным heartbeat считается завершённый tick, включая корректный idle tick.
Ошибка фиксируется немедленно без текста исключения. После настроенного числа
последовательных ошибок worker завершается с ненулевым кодом. Каждый tick
ограничен timeout, поэтому зависшая async-операция также приводит к ошибке и
последующему restart через Docker policy.

`/health/live` не обращается к БД. `/health/ready` при наличии `DATABASE_URL`
использует время PostgreSQL и требует:

- строку каждого обязательного цикла;
- хотя бы один успешный tick;
- свежий `last_succeeded_at`;
- отсутствие последовательной ошибки;
- отсутствие tick, который начался после последнего успеха и превысил timeout.

## Безопасность

- `BOT_MODE=OFF` и `EMERGENCY_LOCK=true` остаются defaults.
- Runtime обрабатывает только synthetic/no-op adapters; реальный outbound
  физически не зарегистрирован.
- Heartbeat не содержит пользовательских данных или credential material.
- Docker build context работает по default-deny и разрешает только runtime
  source, production lock-файл и Alembic assets.
- Контейнеры работают non-root, без Linux capabilities, с read-only rootfs.

## Восстановление

Durable очереди и deadline находятся в PostgreSQL. Падение процесса не требует
in-memory checkpoint: незавершённые lease возвращаются по существующим
recovery/fencing правилам, а handoff expiry повторно выбирает due Conversation.
После restart новое поколение heartbeat становится единственным допустимым.

## Не входит

- production/staging deploy;
- реальные VK/MAX/AI adapters;
- интеграция control-plane режимов `online-zapis-tv`;
- автоматическое разрешение клиентского outbound.

## Поправка AMO-01B1b

Chat projection worker (`AmocrmChatProjectionWorker`) дренируется внутри
цикла `amocrm_mirror`: после commit `DELIVERED` (+ mirror) бот-ответ с durable
`payload_json.text` может ставиться в очередь проекции **отдельной**
транзакцией. Сбой enqueue не откатывает `DELIVERED` и не вызывает sink снова.
Catch-up — только id-scoped repair по `outbound_id` (без bulk backfill и без
Chat HTTP в repair). Machine-only / non-DELIVERED → без projection row.

## Поправка AMO-01B2

Цикл `amocrm_mirror` остаётся одним из пяти обязательных. В него подключён
`CrmRestMirrorAdapter` (CRM REST entity convergence, default-off). Это не
меняет набор циклов, heartbeat и не подключает VK/MAX или AI.
