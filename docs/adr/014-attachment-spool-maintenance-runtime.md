# ADR 014: Attachment Spool Maintenance Runtime (CURSOR-13 Stage 1)

## Status

Accepted — Stage 1 library-only, Stage 2A process wiring, Stage 2B PostgreSQL
integration tests, Stage 3A default-off Compose service wiring, Stage 3B
staging runtime rollout, and CURSOR-14 Stage 2 supervision hardening
(SUCCESS-only tmpfs heartbeat, Docker healthcheck, `unless-stopped` with
idle-wait for disabled/known startup failures).

## Context

Stages 1A1–1A2B3 delivered public `AttachmentSpoolStore.reconcile` and
`purge_expired` APIs with row-level `FOR UPDATE SKIP LOCKED` concurrency and a
shared filesystem-first `DELETE_PENDING` finalizer. ADR 013 left operators to
schedule those APIs externally. CURSOR-13 Stage 1 adds a reusable in-process
maintenance runner without activating production scheduling.

## Decision

### Runtime form

- Library class `AttachmentMaintenanceRunner` in
  `app/services/attachment_maintenance.py`.
- Public API: `run_once()`, `run_forever(*, stop_event)`, `status`.
- Constructor-injected `AttachmentSpoolStore` + `AttachmentMaintenanceConfig`.
- **Not** wired into FastAPI lifespan, existing `WorkerRuntime`, CLI, compose,
  cron, or env/`Settings.from_env` in this stage.
- Runner does not start automatically.

### One-cycle orchestration

1. Call `store.reconcile(limit=config.reconcile_limit)`.
2. Call `store.purge_expired(limit=config.purge_limit)`.
3. Aggregate a typed `AttachmentMaintenanceCycleResult` (`SUCCESS` / `PARTIAL` /
   `FAILED` only).

Rules:

- Purge runs after operational `AttachmentError` from reconcile.
- Unexpected non-`AttachmentError` `Exception` from reconcile skips purge,
  records status `INTERNAL_ERROR`, and is re-raised (fatal to `run_forever`).
- One cycle = one bounded reconcile batch + one bounded purge batch (no drain
  loop).
- Runner calls only public store APIs; no repository SQL and no direct
  filesystem lifecycle.

### Per-instance serialization

- One private `asyncio.Lock` per runner instance.
- **Every** immutable status snapshot replacement happens only while holding
  that lock (cycle body, `run_forever` loop start, and `run_forever` finally).
- Lock covers reconcile + purge + status transition for a single cycle.
- Concurrent `run_once` calls wait (serialize); no overlapping cycles.
- Interval / initial-delay waits do **not** hold the lock.
- `run_forever` does not hold the lock around `run_once` (non-reentrant lock).
- If stop ends an interval wait while an external `run_once` holds the lock,
  finally waits for the lock and only clears `loop_running` — it must not wipe
  a fresher in-flight or completed cycle snapshot.
- No global lock, PostgreSQL advisory lock, Redis, or leader table.

### Duplicate `run_forever` guard

- At most one active `run_forever` per instance.
- Second call fails fast with `RuntimeError("ATTACHMENT_MAINTENANCE_ALREADY_RUNNING")`
  before any await.
- After setting the guard, enter `try` immediately (no status/log calls between
  guard set and `try`).
- Guard cleared in `finally` (under the cycle lock) on normal stop, cancellation,
  waiter/`now_fn`/store failure, and logging-adjacent paths so the same instance
  may restart.

### Long-running loop

- Fixed delay **after** each completed cycle (`interval_seconds`).
- `initial_delay_seconds` default `0` → immediate first run.
- Interruptible wait abstraction (production: `stop_event` + timeout; tests:
  injectable event-driven waiter). No `asyncio.sleep` in runner.

### `stop_event` vs cancellation

- `stop_event` interrupts only initial-delay and interval waits.
- `stop_event` does **not** cancel an in-flight reconcile/purge.
- If stop is set during an active cycle, the cycle completes fully; then the
  loop exits without another interval/cycle.
- Graceful shutdown may therefore wait for the current cycle (accepted).
- External `Task.cancel()` propagates `CancelledError`; cycle status becomes
  `CANCELLED` when cancel hits inside a cycle; wait-time cancel does not
  overwrite the last completed cycle status.
- Forced kill remains crash-safe via durable `WRITING` / `DELETE_PENDING`
  recovery (ADR 012/013).

### Error taxonomy

| Class | Behavior |
|-------|----------|
| `AttachmentError` | Operational; safe `exc.code` only; cycle result PARTIAL/FAILED; loop continues |
| `asyncio.CancelledError` | Propagate; status CANCELLED if inside cycle; not an operational failure counter bump |
| Other `Exception` | Status INTERNAL_ERROR; log `type(exc).__name__` only; re-raise; no result DTO |

### Status and logging

- Immutable `AttachmentMaintenanceStatus` snapshots replaced under the cycle
  lock (not GIL-based atomicity claims).
- Exact idle / active / completed invariants:
  - active: `cycle_running=True`, `started_at` set, `finished_at`/`status`/codes
    cleared;
  - completed SUCCESS/PARTIAL/FAILED/CANCELLED/INTERNAL_ERROR require timestamps
    and matching operational code rules; CANCELLED does not bump the
    unsuccessful counter; INTERNAL_ERROR clears operational codes and bumps it.
- Timezone-aware UTC timestamps via injectable `now_fn`.
- `now_fn` failure before publishing active status does not create a zombie
  active cycle. Finish-time `now_fn` failure uses started_at as fallback,
  publishes INTERNAL_ERROR (or CANCELLED when cancelling), and re-raises with
  CancelledError taking priority over a finish-clock failure.
- Wall-clock timestamps may move backwards. A finish timestamp earlier than the
  cycle `started_at` is **clamped** to `started_at` so completed-status
  invariants hold; clamping is not INTERNAL_ERROR and does not change waiter
  interval scheduling (scheduling remains the interruptible waiter contract).
- Logging is **best-effort and non-fatal** via a private `_log_safely` wrapper:
  logger adapter failures cannot stick the guard, leave `cycle_running=True`,
  skip purge after operational `AttachmentError`, mask `CancelledError`, alter
  cycle results, or stop `run_forever`.
- Structured stdlib logs with fixed event names and numeric counters only.
- No `logger.exception`; no exception text/args/traceback in normal events;
  no UUID/path/token/digest/SQL/credentials.
- Concurrency unit tests use a deterministic observed lock/barrier (no
  sleep/`timeout=0` scheduling primitives for proofs).

### Config (Stage 1)

- `AttachmentMaintenanceConfig`: required `interval_seconds`, `reconcile_limit`,
  `purge_limit`; optional `initial_delay_seconds=0`.
- Exact `int` validation; `bool` rejected; ranges 1..86400 / 0..86400 / 1..1000.
- Invalid → `ValueError` with fixed messages.
- No env parsing; no `enabled` flag; no `BOT_MODE` / `EMERGENCY_LOCK` coupling.

### Multi-instance

- Correctness across processes relies on existing row-level SKIP LOCKED contracts.
- Parallel maintenance processes are correctness-safe but may waste IO.
- Advisory lock is **not** required for correctness and is not added here.

### Explicit Stage 1 exclusions

- CLI / process entrypoint
- Compose / Docker / deploy / cron / systemd
- HTTP health endpoints
- `app/config.py`, `app/main.py`, `app/worker.py` changes
- Models / migrations / dependency updates
- Channel adapters and client outbound

### Stage 2A — process wiring (accepted)

- Separate process entrypoint: `python -B -m app.attachment_maintenance`
  (`app/attachment_maintenance.py`). **Not** a sixth `WorkerRuntime` loop and
  **not** FastAPI lifespan / `app/main.py`.
- Fail-closed `ATTACHMENT_MAINTENANCE_ENABLED=false` by default (Settings).
- Maintenance pacing/limits live in `Settings.from_env`; enablement is **not**
  coupled to `BOT_MODE` or `EMERGENCY_LOCK`.
- Spool root and TTL are **process-local** env only (`ATTACHMENT_SPOOL_ROOT`,
  `ATTACHMENT_SPOOL_TTL_SECONDS`, default TTL `900`) — not Settings fields.
- When enabled, spool root must already exist as a non-symlink absolute
  directory; the entrypoint never creates it (`mkdir` forbidden).
- Startup probes the active attachment key via `EnvAttachmentKeyProvider`
  before creating the runner; keys stay outside Settings.
- `DATABASE_URL` is required only when enabled
  (`validate_attachment_maintenance_runtime`).
- SIGINT/SIGTERM only set `stop_event` (Windows unsupported handlers ignored);
  in-flight cycles are not cancelled by stop; waits remain interruptible.
- Exit: disabled/clean stop/KeyboardInterrupt → `0`; startup or unexpected
  runtime `Exception` → `1` with stderr `error_code={TypeName}` only;
  `CancelledError` is not masked as `Exception`.
- Engine `dispose()` is attempted once after successful `create_engine`; success
  is not guaranteed. `_dispose_engine` catches only ordinary `Exception` and
  returns it for lifecycle classification (no logging inside the helper).
- `attachment_maintenance_process_stopped` means fully successful completion:
  runner returned normally **and** engine dispose succeeded. It is **not**
  logged after cancellation, startup failure, runtime fatal, or dispose failure.
- When a primary exception is already pending (startup / runtime /
  `CancelledError`), a secondary dispose `Exception` is suppressed and must not
  create a second `process_fatal` (or any cleanup-warning event).
- Clean-run dispose failure is the sole `process_fatal` for that path and exits
  `1`. Runtime fatal logs exactly one `process_fatal` for the primary exception.
- Process logs use fixed event names and safe `error_code` scalars only (no
  paths, URLs, keys, IDs, exception text, or `exc_info`).
- Stage 2A ships unit/process tests only (no live PostgreSQL in this stage).
- Docker runtime allowlist includes the entrypoint and Stage 1 maintenance
  modules; **no** Compose service, volume, or healthcheck.

### Stage 2B — PostgreSQL integration tests (accepted)

- Isolated real-PostgreSQL integration tests for the process construction path
  (safe `BOT_TV_TEST_DATABASE_URL` only).

### Stage 3A — Compose wiring (accepted, default-off)

- Compose service `attachment-maintenance` with command
  `python -B -m app.attachment_maintenance`.
- Activation uses Compose profile `attachment-maintenance`. Default
  `docker compose up -d` does **not** start the service.
- Second independent gate: `ATTACHMENT_MAINTENANCE_ENABLED` remains default
  `false` in Compose; enabling requires an explicit deployment env change.
- Historical Stage 3A choice: `restart: on-failure` (not `unless-stopped` /
  `always`) so a clean disabled exit `0` could not create a restart loop.
  CURSOR-14 supersedes this (see below) by keeping the process alive in
  idle-wait when disabled or when known startup configuration/keyring
  validation fails, then using `restart: unless-stopped` for host/daemon
  reboot survival after an intentional first start.
- `stop_grace_period: 60s`. SIGTERM only sets `stop_event`; an in-flight cycle
  is not cancelled. There is no hard cycle duration bound; after grace Docker
  may SIGKILL. Durable `WRITING` / `DELETE_PENDING` reconciliation recovers the
  crash window. 60s is an operational allowance, not an absolute guarantee.
- Historical Stage 3A choice: no container `healthcheck`; progress was observed
  through structured process/runner logs. CURSOR-14 adds a SUCCESS-only
  heartbeat healthcheck (see below).
- One operational replica only. Do not `--scale attachment-maintenance=N`.
  Brief overlap remains correctness-safe via row-level `FOR UPDATE SKIP LOCKED`;
  that is not a recommendation for permanent multi-replica operation.
- Named volume `attachment-spool` mounted read-write only on
  `attachment-maintenance` at `/var/lib/bot-tv/attachment-spool`, matching
  `ATTACHMENT_SPOOL_ROOT`. Stage 3A does **not** mount the volume on `api` or
  `worker` while they do not use `AttachmentSpoolStore`.
- Shared-spool invariant for any future producer/consumer: same named volume,
  same container path, same database, and a compatible keyring.
- Attachment keyring is **not** listed in Compose `environment`. Keys arrive
  only from an external host-local `env_file` (Compose >= 2.24.0,
  `required: false`):
  path `${ATTACHMENT_SPOOL_KEYS_ENV_FILE:-/etc/bot-tv/attachment-spool-keys.env}`.
  The file must contain `ATTACHMENT_SPOOL_ACTIVE_KEY_ID`, the active
  `ATTACHMENT_SPOOL_KEY_<ID>`, and every older `ATTACHMENT_SPOOL_KEY_<ID>` still
  required to decrypt existing spool objects. The file stays outside Git; do
  not print rendered Compose environment or the file contents.
- Stage 3A does **not** activate staging/production. Server rollout remains
  Stage 3B/3C with separate owner authorization.

### CURSOR-14 — Supervision hardening (accepted for implementation)

- SUCCESS-only filesystem heartbeat under existing tmpfs:
  `/tmp/bot-tv-attachment-maintenance-heartbeat.json`.
- Exact schema: `{"v":1,"completed_at":"<UTC ISO-8601>"}` only. No PII,
  secrets, object IDs, paths, key IDs, counters, or exception text. No DB row
  and no migration.
- Atomic write: exclusive temp in the same directory → fsync → `os.replace`;
  mode `0600`; symlink/non-regular final path rejected.
- Heartbeat updates only after `AttachmentMaintenanceCycleStatus.SUCCESS`
  (both reconcile and purge completed). PARTIAL/FAILED/CANCELLED/INTERNAL_ERROR
  do not refresh the file. Writer failures are best-effort and do not kill the
  maintenance loop.
- Docker healthcheck:
  `python -B -m app.attachment_maintenance_healthcheck`
  with `interval: 10s`, `timeout: 5s`, `retries: 3`, `start_period: 90s`.
- Default freshness: `ATTACHMENT_MAINTENANCE_HEARTBEAT_STALE_SECONDS=180`
  (fail-closed parsing; no silent fallback for invalid values). Future skew
  allowance: 30 seconds.
- Healthcheck is read-only: no DB, Settings, keyring, encrypt/decrypt, or
  maintenance cycle. Success is silent; failure prints one safe code on stderr.
- Restart policy: `unless-stopped`. Default-off profile still prevents start
  until an explicit `compose up` with the profile. Manual `compose stop` keeps
  the service stopped. Previously running containers return after host/Docker
  daemon restart.
- Disabled mode and known startup `ValueError` / `AttachmentError` paths:
  log safely, write no heartbeat, idle-wait on `stop_event` (no busy-loop, no
  restart storm). Unexpected programming exceptions at startup or mid-loop
  remain fatal (non-zero exit → Docker may restart).
- Honest limit: Docker `unhealthy` does **not** itself restart the container;
  restart policy reacts to process exit. External monitoring remains a future
  option. Host/daemon reboot survival is a separate owner-approved runtime gate
  after staging acceptance.

### Stage 3B/3C (staging/production gates)

- Staging runtime verification, ownership/permissions preflight for volume user
  `bot-tv`, Compose >= 2.24.0 on the host, controlled enablement of profile +
  `ATTACHMENT_MAINTENANCE_ENABLED=true`, and separate owner-approved
  reboot-survival testing. Production remains out of scope until explicitly
  authorized.

## Consequences

- Domain lifecycle rules remain unchanged; Stage 1 only adds an execution
  mechanism; Stage 2A adds a safe opt-in process host that stays off by default.
- Stage 3A makes Compose wiring available behind a profile while keeping the
  process fail-closed until operators intentionally enable it.
- ADR 013 “operators schedule externally” remains true until an authorized
  Stage 3B/3C rollout starts the profiled service with enablement set.
- Residual risk: graceful stop may wait on a long active reconcile/purge cycle.

## Non-claims

This ADR does **not** activate production maintenance, grant `api`/`worker`
spool mounts, change `EnvAttachmentKeyProvider` / crypto transport, add a DB
heartbeat table/migration, auto-restart on Docker `unhealthy` alone, or
replace an owner-approved reboot-survival runtime gate.
