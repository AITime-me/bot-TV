# ADR-017: Master Channel Binding (CURSOR-27)

## Status

Accepted for CURSOR-27. Channel-agnostic durable binding only. No live VK/MAX
adapters, command parsing, deploy, or online-zapis-tv database access.

## Context

Future master-facing ingress must map a channel account identity to an
online-zapis-tv `masterId` without inferring identity from name, phone, or
free-text messages. Conversation `Channel` remains synthetic-only; binding
uses a separate closed channel set reserved for future adapters.

## Decision

### Identity key

`(channel, connection_scope, external_account_id)` → `master_id`

| Field | Rule |
|---|---|
| `channel` | closed: `synthetic`, `vk`, `max` (no live wiring in this stage) |
| `connection_scope` | opaque scope so accounts from different connections never collide; default `default` |
| `external_account_id` | opaque channel account id; **no case folding** (avoids false merges) |
| `master_id` | canonical lowercase UUID from online-zapis-tv |

Whitespace, control characters, empty values, and non-printable-ASCII fail
closed as `INVALID_INPUT`. DB CHECK uses locale-independent `^[!-~]+$`
(ASCII 0x21–0x7E), matching the service validator `^[\x21-\x7E]+$`.

### Lifecycle

| Status | Meaning |
|---|---|
| `ACTIVE` | sole resolvable binding for the identity |
| `REVOKED` | retained for audit (`revoked_at` set); never resolved |

Partial unique index:

`UNIQUE (channel, connection_scope, external_account_id) WHERE status = 'ACTIVE'`

This makes two ACTIVE rows for one identity (different masters or same)
impossible at the database layer. Concurrent bind races classify as
`ALREADY_BOUND` (same master) or `CONFLICT` (different master).

### Service API

`MasterChannelBindingService` (session-scoped, no commit):

- `resolve` — ACTIVE only; 0 → `NOT_FOUND`, 1 → `RESOLVED`, >1 → `AMBIGUOUS`
- `bind` — create ACTIVE; existing same master → `ALREADY_BOUND`; other master → `CONFLICT`
- `rebind` — **atomic** revoke+insert in one savepoint (or insert if absent);
  same master → `ALREADY_BOUND`; IntegrityError → savepoint rollback + re-read →
  `ALREADY_BOUND` / `CONFLICT` / `AMBIGUOUS` (**never** `INVALID_INPUT`);
  corruption (`count > 1` / lost ACTIVE under lock) → `AMBIGUOUS`
- `revoke` — ACTIVE → REVOKED; absent → `NOT_FOUND`; `count > 1` → `AMBIGUOUS`

Rebind must not soft-return after a revoke that then commits with zero ACTIVE.
A failed replacement rolls back the savepoint so the previous ACTIVE remains.

Typed outcomes never embed identities, `masterId`, tokens, or exception text in
logs/repr.

### Error code `REVOKED`

`MasterChannelBindingErrorCode.REVOKED` is reserved for future
exception-raising call sites that need to name a revoked row. Soft APIs expose
revoked rows as resolve `NOT_FOUND` or revoke outcome `REVOKED`, not by raising
this code.

### Non-goals

- VK / MAX / Telegram / WhatsApp API clients or webhook parsing
- Live FastAPI / worker wiring of resolve into ingress
- Inferring master from name, phone, display name, or message text
- Direct PostgreSQL / Prisma access to online-zapis-tv
- BOT_MODE / EMERGENCY_LOCK changes; deploy

## Consequences

- Future channel adapters must pass already-normalized opaque account ids and
  an explicit `connection_scope` per installed connection.
- Rebind preserves history via REVOKED rows; callers must not delete ACTIVE
  without going through `revoke`/`rebind`.
- Concurrent rebind/bind races are classified by re-read after savepoint
  rollback; partial unique index remains the source of truth for ≤1 ACTIVE.
