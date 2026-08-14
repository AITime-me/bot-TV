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
- `ADMITTED` is the durable cancellation boundary; `DELIVERED` means acceptance
  by the synthetic sink only. `SENT` remains forbidden.
- Authoritative user-facing bot reply body is `outbox_messages.payload_json.text`
  (BOT-REPLY-DURABLE-01). It is rendered from the booking domain client-message
  path and written into the immutable outbound payload **before** INSERT.
  Delivery and retry read only that persisted `text` — they never re-render and
  never fall back to inbound text, `INTERNAL_DRAFT`/`draft_text`, manager hints,
  or `synthetic_token` (token remains technical metadata only). Machine-only
  `OFFER_DAYS` may omit `text`; missing/invalid text otherwise fails closed.
  After authoritative `DELIVERED` commits (with mirror meta), AMO-01B1b may
  enqueue a Chat projection of that same persisted `text` in a **separate**
  post-commit transaction (not a second client-delivery path). Projection
  enqueue failure must not roll back `DELIVERED` or re-invoke the sink.
  Id-scoped repair (`repair-bot-outbound --outbound-id`) may restore a missing
  projection row only; no bulk backfill and no Chat HTTP from the repair itself.

### Outbound Arbiter
The only path to move `SYNTHETIC_OUTBOUND` through
`PROCESSING → ADMITTED → DELIVERED`.
Checks under per-conversation `FOR UPDATE` locks:
bot ownership, `BOT_ACTIVE`, no takeover, matching `context_version`,
`manager_epoch` and `event_seq_hwm`, plan not
cancelled/superseded, `not_before` elapsed, lease fencing, not already
delivered, and fail-closed mode (automatic real outbound always false).

The `ADMITTED` commit is the linearization point against manager/client ingress.
The sink runs after that transaction closes and must treat `outbound_id` as an
idempotency key. Success is finalized in a second transaction. A crash after
admission leaves an `ADMITTED` row that another worker can lease and retry;
manager ingress cannot move it back to a cancellable state.

### Lease / fencing
ReplyPlan and OutboundMessage use `FOR UPDATE SKIP LOCKED`, lease TTL,
`lease_token` + `lease_version`, retries, and `DEAD` after max attempts.
`max_attempts` persisted on each row limits pre-admission claims and confirmed
sink failures. Before a normal pre-admission claim, an expired
`PROCESSING` row whose final attempt was already issued is moved to `DEAD`
without dispatching a ReplyPlan or invoking the outbound sink. ReplyPlan
recovery locks Conversation first and transactionally enqueues the same
`REPLY_PLAN_STATE_CHANGED(DEAD)` mirror fact as explicit final failure.
An expired `ADMITTED` lease is always recoverable even if the claim counter has
reached the configured limit: a crash is not evidence of a provider rejection.
The next confirmed transient/permanent sink result applies the terminal limit.

### Out of scope
Real VK/MAX/Telegram/site adapters, n8n, AI, public webhooks, real sends, and
online-zapis-tv integration.

## Consequences

Workers can recover after crash using leased rows. An expired pre-admission
`PROCESSING` final attempt is terminalized; an `ADMITTED` row is retried with
the same transport idempotency key. Stale workers cannot complete superseded
plans, admit outdated outbound messages, or finalize a reclaimed admission.
