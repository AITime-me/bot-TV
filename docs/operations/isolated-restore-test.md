# Isolated PostgreSQL restore-test (bot-TV)

Автоматическая проверка **полноценного** восстановления существующего PostgreSQL dump
во **временной изолированной** среде (не `pg_restore -l` и не operational restore).

**Контур:** только **staging**. Production не поддерживается и не должен вызываться.
**Статус server verification:** код в репозитории ≠ проверка на сервере, пока не прогнан
controlled proof на хосте.

## Канонические файлы

| Файл | Роль |
| --- | --- |
| `scripts/ops/staging-backup-db.sh` | Scheduled `pg_dump -Fc` staging |
| `scripts/ops/lib/staging-ops-common.sh` | Paths, lock, dump helpers |
| `scripts/ops/isolated-restore-test.sh` | Oneshot restore-test + `--emergency-cleanup` + `--reap-orphans` |
| `scripts/ops/lib/isolated-restore-test-common.sh` | Path validation, labels, evidence helpers |
| `scripts/ops/lib/isolated-restore-test-policy.sh` | Freshness / orphan TTL |
| `scripts/ops/lib/fake-docker-irt.sh` | Fake Docker для локального harness |
| `scripts/ops/tests/isolated-restore-test-harness.sh` | Failure-path сценарии |

Prisma migration-proof / offline runner из online-zapis-tv **не** переносились:
bot-TV проверяет restore dump → схема/пользовательские таблицы.

## Isolation guarantees

- Temp container: `--network none`, `--pull=never`, no published ports
- Memory / CPU / pids limits
- Private dump snapshot mounted read-only (`dump.snapshot`)
- Labels: `com.bot-tv.component=isolated-restore-test`, `environment=staging`, `run-id`
- Name: `bot-tv-rt-staging-<16hex>`
- Working staging containers (`tv_bot_stage-postgres-1` и siblings) **не** stop/restart;
  metadata snapshot до/после run (fail rc=80 при изменении)
- Credentials / row data / TOC lists **не** логируются

## Dump source

Default dump dir: `/srv/automation-data/bot-tv/stage/backups/postgres/`  
(override: `IRT_DUMP_DIR_OVERRIDE` / `BOT_TV_STAGING_BACKUPS_DIR`)

Имя: `YYYYMMDDTHHMMSSZ_<label>.dump`. Возраст ≤ `IRT_DUMP_MAX_AGE_HOURS` (36).

## Evidence layout

```text
/var/lib/bot-tv/restore-test/
  staging/
    last-attempt.env
    last-success.env       # не затирается неудачной попыткой
    last-pg-restore-error.log  # diagnostic; удаляется при success
    history/
    runtime/               # 0700: cidfile, current.env, private dump snapshot
```

Permissions: evidence dirs `0750`, runtime `0700`, evidence files `0600`.

## Lifecycle (единый EXIT-финализатор)

1. `trap finalize_once EXIT`; `ERR` / `INT` / `TERM` только фиксируют код.
2. Parent `SIGINT`/`SIGTERM` → `IRT_SIGNAL_RECEIVED=1`, итоговый **rc=50**.
3. Interruptible: `docker run`, ready-loop, `pg_restore`, integrity queries.
4. `finalize_once`: cleanup → verify absent → evidence → exit.
5. Успех только если restore + integrity + cleanup proofs + evidence OK.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 10 | dump missing / stale / invalid / TOCTOU |
| 20 | docker / image / start |
| 30 | pg_restore |
| 40 | integrity |
| 50 | cleanup / interrupt / evidence |
| 60 | lock |
| 70 | usage |
| 80 | forbidden container metadata changed |

## Local harness (без реального Docker / staging DB)

```bash
bash scripts/ops/tests/isolated-restore-test-harness.sh
```

## Controlled staging proof (сервер)

Один полный прогон после свежего scheduled backup (не трогает рабочую БД):

```bash
cd /path/to/bot-TV && \
bash scripts/ops/staging-backup-db.sh && \
bash scripts/ops/isolated-restore-test.sh --environment staging
```

Evidence: `/var/lib/bot-tv/restore-test/staging/last-attempt.env` и `last-success.env`
(`STATUS=success`, `INTEGRITY_OK=1`, `USER_TABLE_COUNT>=1`, `CLEANUP_OK=1`,
`TEMP_RESOURCES_ABSENT=1`, `SNAPSHOT_ABSENT=1`).

Emergency cleanup (если нужно вручную):

```bash
bash scripts/ops/isolated-restore-test.sh --emergency-cleanup --environment staging
```

## Overrides (harness)

`IRT_EVIDENCE_ROOT`, `IRT_DUMP_DIR_OVERRIDE`, `IRT_SKIP_FORBIDDEN_CHECK=1`,
`IRT_PG_READY_TIMEOUT_SEC`.
