# ADR-018: Master Command Flow (CURSOR-28)

## Status

Accepted for CURSOR-28. Application flow only. No live VK/MAX adapters,
webhook wiring, deploy, or `BOT_MODE` enablement.

## Context

CURSOR-26 exposes typed master S2S endpoints on online-zapis-tv. CURSOR-27
binds channel accounts to `masterId`. This stage owns the safe command
application flow that sits between a future channel adapter and those APIs.

## Decision

### Boundary

```text
Channel adapter (future VK/MAX)
  → C27 master channel binding (resolve fail-closed)
  → durable pending / clarification / confirmation
  → purpose-bound ephemeral PII non-destructive read
  → C26 idempotent mutation (stable idempotency key)
  → durable terminal commit
  → PII eventually removed by TTL / maintenance (not sync delete)
```

Business logic must not import VK/MAX SDKs. Synthetic envelopes are enough
to exercise the flow. `master_id` is never returned to the master in
structured results, logs, or repr.

### Supported commands (minimal)

| Intent | C26 endpoint | Confirm |
|---|---|---|
| Close interval | `POST .../blocks/close-interval` | yes |
| Day off | `POST .../blocks/close-day` | yes |
| Create booking | `POST .../master/bookings` | yes |
| Schedule read | `POST .../master/schedule` | no |

Parsing is deterministic Russian pattern matching. Incomplete args →
`CLARIFICATION_REQUIRED`. Unknown text → `MANUAL_HELP` (no booking call).

### State machine

`PARSED` → optional `AWAITING_CLARIFICATION` → `AWAITING_CONFIRMATION` →
explicit confirm → `EXECUTING` (lease) → `SUCCEEDED` / `FAILED` /
`CANCELLED` / `EXPIRED`.

- Confirm tokens (`да`, …) bind to the active pending row + `command_version`
  + TTL; without a matching pending command they execute nothing.
- Cancel works from clarification/confirmation (terminal; not re-executable).
- One ACTIVE pending per `(channel, connection_scope, external_account_id)`.
- Inbound `(…, inbound_message_id)` is unique for dedupe/retry safety.
- Mutation `idempotency_key` is minted once at confirmation start and reused
  on every retry; crash/timeout never mints a new key.
- `EXECUTING` holds a bounded lease. A live lease blocks a second executor.
  An expired lease is recovered via CAS reclaim (same version + same key) or
  returned to `AWAITING_CONFIRMATION` so the identity cannot stay ACTIVE forever.
  Concurrent reclaim/claim admits only one executor.

### PII lifecycle (CREATE_BOOKING)

Phone/name are stored only via ephemeral PII (`PHONE` / `CLIENT_NAME`, purpose
`MASTER_BOOKING_CLIENT_WRITE`). Pending rows keep opaque reference tokens,
never plaintext. Schedule responses omit client names/phones/ids.

Ordering invariant:

1. Confirmation stores ciphertext; pending holds opaque refs.
2. Confirm reads plaintext via purpose-bound **non-destructive** decrypt
   (`read_plaintext`). Ciphertext is not deleted by read.
3. Remote C26 mutation uses the stable idempotency key.
4. Flow writes a durable terminal pending state in the caller UoW
   (`SUCCEEDED` / `FAILED` / `CANCELLED` / `EXPIRED`). Terminal rows are never
   re-executed.
5. The flow does **not** call irreversible ephemeral `delete`/`consume` inside
   the transactional command path. Immediate delete in a separate committed
   transaction would reopen a stale-ref crash window if the outer UoW rolls
   back after remote success.
6. Ciphertext cleanup is authoritative via ephemeral TTL / existing maintenance
   purge. Bounded orphan ciphertext after terminal commit is an intentional
   safety tradeoff: `remote result → durable terminal commit → PII eventually
   removed by TTL/maintenance`.
7. Retryable / unknown outcomes release back to `AWAITING_CONFIRMATION` with
   the same refs + key and never destroy PII:
   `TIMEOUT`, `IDEMPOTENCY_IN_PROGRESS`, `TRANSPORT_ERROR`,
   `RESPONSE_INVALID`, `RESPONSE_TOO_LARGE`, plus local `INTERNAL_RETRYABLE`.

There is no distributed transaction across bot-TV DB and online-zapis-tv.
Safety comes from: stable C26 idempotency key + retained TTL-bound PII until
(and after) durable terminal completion.

### Crash windows

| Window | Local state | PII | Retry |
|---|---|---|---|
| Before remote (after claim / after PII read) | Release to `AWAITING_CONFIRMATION` or expired-lease reclaim | Retained | Same key + explicit later «да» |
| Unknown / timeout / in-progress / invalid 2xx body | `AWAITING_CONFIRMATION` + result_code | Retained | Same key; remote is safe to replay |
| After remote success, before local terminal commit | Process crash leaves `EXECUTING` until lease expiry → reclaim / recover; outer UoW rollback restores confirmable pending | Retained (no sync delete) | Same key yields same logical C26 result |
| After durable terminal commit | Terminal; not executable | Ciphertext until TTL/maintenance | No booking rollback |

Bounded retry uses release-to-confirmation + explicit subsequent confirm —
not a blind automatic rapid loop.

Fail-closed for unknown post-accept outcomes means: never report `SUCCESS`
without a proven local terminal path, and never destroy the ability to
idempotently replay with the same key.

### Non-goals

Live VK/MAX, amoCRM, Identity Resolution, n8n, general Teya dialogue,
deploy, admin UI for bindings, direct online-zapis-tv DB access.

## Consequences

- Docker default-deny allowlist must include all runtime modules reachable
  from the booking/master-command factory closure (including
  `booking_create_http` / `booking_create_remote`) plus migration
  `20260808_18_master_commands`.
- Safety defaults `BOT_MODE=OFF` / `EMERGENCY_LOCK=true` remain unchanged.
