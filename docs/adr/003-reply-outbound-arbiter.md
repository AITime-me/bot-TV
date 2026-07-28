# ADR-003: ReplyPlan and Outbound Arbiter (BOT-CORE-REPLY-OUTBOUND-01C)

## Status

Accepted for BOT-CORE-REPLY-OUTBOUND-01C.

## Context

Ingress (01B) durably accepts provider events. Foundation (01A) stores
conversation/inbox/outbox drafts. This stage adds reply orchestration and a
single outbound admission gate without real channel delivery.

## Decision

### Conversation context
- `context_version` increments exactly once per newly created inbox message.
- Duplicate ingress/inbox deliveries do not bump the version.
- `ownership` is `BOT` or `MANAGER`; manager takeover sets `HANDOFF`.
- `active_reply_plan_id` is the last/current plan pointer for the dialog. It
  may still reference a terminal plan (`DISPATCHED` / `FAILED` / `DEAD`) until
  manager takeover clears it or a newer inbox message replaces it.

### Clock of record
PostgreSQL is the only clock used for scheduling. `app/db/clock.py` resolves the
transaction timestamp, and repositories either use it or an explicitly injected
instant on the same timeline; `updated_at` is written as `now()` inside the
statement. `created_at` of a new plan is pinned to that same instant, so
`not_before - created_at` is exactly `bot_response_delay_ms` regardless of host
clock skew or round-trip latency. Reading `datetime.now()` in scheduling code is
forbidden and enforced by a static test guard.

### Lock order
Every transaction that writes a dialog subtree locks in one order:
`conversations → inbox_messages → reply_plans → outbox_messages`. The
conversation row is locked once, through `conversations.lock_for_update()`,
before any INSERT whose foreign key implicitly takes `FOR KEY SHARE` on that
same row. This includes `ReplyPlanWorker.dispatch_claimed`: Conversation
`FOR UPDATE` is the first business-row lock of that transaction, before the
`outbox_messages` INSERT and before any exclusive `ReplyPlan` update. Locking
after such an INSERT let two concurrent messages of one dialog escalate
`KEY SHARE → FOR UPDATE` and deadlock; the same reverse order between inbound
(Conversation → ReplyPlan) and an unlocked dispatch (ReplyPlan/Conversation
via FK → ReplyPlan update) closed the same cycle. Waiters in the
`INSERT ... ON CONFLICT DO NOTHING` conversation upsert hold no other row locks,
so the creation race cannot close a cycle either. Because the conversation lock
is always taken first and is exclusive, no bounded transaction retry is needed;
adding one would only hide a future ordering regression.

### Concurrent context bumps
`context_version` is incremented by PostgreSQL on the row locked with
`SELECT ... FOR UPDATE` (loaded with `populate_existing` so a waiter cannot reuse
pre-lock attribute values). Concurrent messages of one dialog therefore receive
strictly monotonic versions and never collide on
`uq_reply_plans_conversation_context_version`.

### ReplyPlan lifecycle
Statuses: `PENDING → READY → PROCESSING → DISPATCHED`, with
`CANCELLED` / `SUPERSEDED` / `FAILED` / `DEAD` as documented terminals/branches.
- Client replies use `bot_response_delay_ms = 5000`.
- Delay is persisted as `not_before` in PostgreSQL; process sleep is never a
  correctness mechanism.
- A new client message supersedes open plans and creates a plan for the new
  context version.
- Manager takeover cancels open plans and blocks new bot plans.

### OutboundMessage
- `SYNTHETIC_OUTBOUND` is distinct from `INTERNAL_DRAFT`.
- Idempotency key `synthetic-outbound:reply-plan:{plan_id}` is unique.
- Delivery statuses include `DELIVERED` (synthetic sink only). `SENT` remains
  forbidden.

### Outbound Arbiter
The only admission path to mark `SYNTHETIC_OUTBOUND` as `DELIVERED`.
Checks under per-conversation `FOR UPDATE` locks:
bot ownership, no takeover, matching context_version, plan not
cancelled/superseded, `not_before` elapsed, lease fencing, not already
delivered, and fail-closed mode (automatic real outbound always false).

### Lease / fencing
ReplyPlan and OutboundMessage use `FOR UPDATE SKIP LOCKED`, lease TTL,
`lease_token` + `lease_version`, retries, and `DEAD` after max attempts.

### Out of scope
Real VK/MAX/Telegram/site adapters, amoCRM, n8n, AI, public webhooks, real
sends, full handoff workflows, and online-zapis-tv integration.

## Consequences

Workers can recover after crash using leased rows. Stale workers cannot
complete superseded plans or admit outdated outbound messages.
