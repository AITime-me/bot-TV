# CP-04 production S2S wiring (bot-TV → online-zapis-tv)

## Status

Accepted for CP-04-PROD-WIRING-01. Prepares production compose for safe
read-only consumption of published Settings / Knowledge and Live Facts.
Does **not** enable `BOT_MODE`, clear `EMERGENCY_LOCK`, or open channels.

## Inventory (staging pattern)

Staging already wires S2S on **worker only** via host-local
`compose.stage.yaml`:

- `BOOKING_ELIGIBILITY_BASE_URL` / `BOOKING_ELIGIBILITY_BEARER_TOKEN` from `.env`
- external network `online-zapis-tv_staging_internal`
- BASE_URL host `tvoe-vremya-staging-app:3000` (container port, not host bind)

Production mirrors that contour with a **tracked** overlay
`compose.prod.s2s.yaml` (reviewable in Git) plus host-local
`compose.prod.yaml` (postgres bind/secrets/ports — unchanged).

## Why worker only

Control-plane snapshot refresh and runtime-context live-facts acquisition run
in the worker process. Api does not need the shared network or S2S env for
this stage. Postgres must not join the online-zapis-tv network.

## Future production env (do not commit secrets)

online-zapis-tv (`.env.production`):

```text
BOT_INTERNAL_API_TOKEN=<shared secret>
```

bot-TV (`/srv/automation-data/bot-tv/prod/config/.env`):

```text
BOOKING_ELIGIBILITY_BASE_URL=http://tvoe-vremya-production-app:3000
BOOKING_ELIGIBILITY_BEARER_TOKEN=<same shared secret>
```

Optional: `BOOKING_ELIGIBILITY_TIMEOUT_SECONDS` (default 5.0),
`BOOKING_ELIGIBILITY_MAX_RESPONSE_BYTES` (tracked default **262144** —
fits ~85 KiB ACTIVE knowledge; S2S transport hard cap 1_000_000).

`CONTROL_PLANE_POLL_SECONDS` (default 30, alias `CONTROL_PLANE_REFRESH_SECONDS`)
is the `control_plane_snapshot` loop cadence. It is independent of
`WORKER_POLL_SECONDS`. One refresh = 2 GETs (settings + knowledge).

Future production `DATABASE_URL` host must be the unique compose alias
`bot-tv-postgres` (see `compose.prod.s2s.yaml`), not hostname `postgres`
(collides with online-zapis-tv on the shared worker network) and not the
ephemeral container name `tv_bot_prod-postgres-1`.

## Canonical compose stack

```text
docker compose -p tv_bot_prod \
  --env-file /srv/automation-data/bot-tv/prod/config/.env \
  -f .../docker-compose.yml \
  -f .../config/compose.prod.yaml \
  -f .../compose.prod.s2s.yaml \
  -f .../docker-compose.production.yml \
  ...
```

## Migration without recreating PostgreSQL

Main includes Alembic revision `20260829_37_control_plane`.

Preferred future invoke (postgres already healthy; avoid dependency recreate):

```bash
docker compose -p tv_bot_prod \
  --env-file /srv/automation-data/bot-tv/prod/config/.env \
  -f .../docker-compose.yml \
  -f .../config/compose.prod.yaml \
  -f .../compose.prod.s2s.yaml \
  -f .../docker-compose.production.yml \
  run --rm --no-deps migrate
```

`--no-deps` is compatible with the one-shot `migrate` service (`alembic upgrade
head`) and skips recreating `postgres`. Do not redesign the deploy architecture
here; keep postgres volume/bind intact.

## Fail-closed (unchanged)

- Local `BOT_MODE` / `EMERGENCY_LOCK` remain authoritative.
- Missing/partial `BOOKING_ELIGIBILITY_*` → S2S clients unset / startup reject
  for partial pairs; control-plane refresh logs `CONTROL_PLANE_NOT_CONFIGURED`.
- S2S 401 / timeout / stale cache never flips outbound or clears emergency lock.
- Published `desiredAdminState` is owner intent only — not effective runtime mode.
