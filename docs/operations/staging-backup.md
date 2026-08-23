# Staging PostgreSQL backup (bot-TV)

Регулярный backup staging-базы `bot_tv_stage` выполняется скриптом
`scripts/ops/staging-backup-db.sh`. Скрипт **не** останавливает api/worker/postgres
и **не** трогает production.

## Важно о рисках

Локальные dump на том же сервере **не защищают** от потери хоста или диска.
Для аварийного восстановления нужна копия dump **вне** staging-хоста.

## Пути (server defaults)

| Что | Путь |
| --- | --- |
| Stage root | `/srv/automation-data/bot-tv/stage` |
| Dump dir | `/srv/automation-data/bot-tv/stage/backups/postgres/` |
| Password file | `/srv/automation-data/bot-tv/stage/postgres/postgres_password` |
| Ops lock | `/srv/automation-data/bot-tv/stage/backups/deploy-state/.staging-ops.lock` |
| Postgres container | `tv_bot_stage-postgres-1` |
| Database / user | `bot_tv_stage` / `bot_tv_stage` |

Dump **не** должны лежать внутри git worktree (скрипт это проверяет).

## Тип scheduled backup

| Тип | Имя файла | Retention |
| --- | --- | --- |
| Scheduled | `YYYYMMDDTHHMMSSZ_scheduled.dump` | по `--retention-days` (default 14) |

Retention удаляет только `*_scheduled.dump`. Другие имена скрипт не трогает.

## Требования

- Docker; контейнер `tv_bot_stage-postgres-1` running + healthy
- Password file (не symlink), readable
- `flock`
- Запуск из checkout bot-TV

Секреты читаются только из password file в `docker exec -e PGPASSWORD=…`.
Пароли и `DATABASE_URL` **не** печатаются.

## Dry-run

```bash
cd /path/to/bot-TV
bash scripts/ops/staging-backup-db.sh --dry-run
```

## Ручной backup

```bash
cd /path/to/bot-TV
bash scripts/ops/staging-backup-db.sh
# опционально:
bash scripts/ops/staging-backup-db.sh --retention-days 30
```

Результат:

- dump: `…/backups/postgres/YYYYMMDDTHHMMSSZ_scheduled.dump` (mode `600`)
- manifest рядом: `….dump.manifest.env` (без секретов)
- формат: `pg_dump -Fc`, проверка `pg_restore -l` через рабочий staging postgres
  (только list TOC; restore в рабочую БД не делается)

## Проверка dump (list only)

```bash
ls -l /srv/automation-data/bot-tv/stage/backups/postgres/*_scheduled.dump
# TOC list через контейнер (без restore в рабочую БД) — делает сам backup-скрипт
```

Полный restore-proof — только во временный изолированный контейнер:
см. [isolated-restore-test.md](./isolated-restore-test.md).

## Overrides (harness / non-canonical layout)

`BOT_TV_STAGE_ROOT`, `BOT_TV_STAGING_POSTGRES_CONTAINER`, `BOT_TV_STAGING_DB`,
`BOT_TV_STAGING_USER`, `BOT_TV_STAGING_PASSWORD_FILE`, `BOT_TV_STAGING_BACKUPS_DIR`.
