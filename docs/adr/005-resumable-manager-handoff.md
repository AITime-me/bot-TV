# ADR-005: ordered manager messages and resumable handoff

## Status

Accepted for CURSOR-10. Runtime handoff, expiry/resume, and worker integration
are implemented; later stages add hardening and live adapters only where
explicitly scoped.

## Context

The previous takeover contract moved a dialog to manager ownership forever and
stored no manager-authored message. Client replies during takeover therefore
had neither a shared dialog timeline nor a durable deferred ReplyPlan.

## Decision

### Synthetic ordering contract

`provider_sequence` is the only manager-event ordering field. It must be a
monotonic, nonnegative sequence from the source. `external_message_id` provides
idempotency only; `provider_occurred_at` and PostgreSQL `received_at` are audit
fields and never establish order.

Under `Conversation FOR UPDATE`, manager ingress first reserves the unique
`(channel, external_message_id)` as a provisional QUARANTINED row. It then
classifies the event:

- missing `provider_sequence` → `QUARANTINED`;
- sequence greater than `manager_sequence_hwm` → `APPLIED`;
- sequence equal to or below the high-water mark → `STALE`.

Duplicate, STALE, QUARANTINED and events against CLOSED conversations have no
FSM, context, ReplyPlan, outbound or mirror side effects. A live adapter must
not be connected until its source supplies the ordering contract.

### Shared dialog order

Every new client message and every APPLIED manager message receives the next
`conversation_event_seq` while the Conversation row is locked. Duplicate and
non-applied manager events receive no sequence. `DialogContextService` merges
only canonical `inbox_messages` and APPLIED `manager_messages`, orders only by
this sequence, and returns at most the newest 40 messages / 12,000 text
characters.

Text is not copied into ReplyPlan, synthetic outbound, mirror metadata, logs or
error fields.

### FSM and plan lifecycle

- APPLIED manager message → `HUMAN_ACTIVE`, fresh configured 10–15-minute deadline,
  `manager_epoch + 1`, `context_version + 1`.
- First new client message in `HUMAN_ACTIVE` → `HUMAN_PAUSE`, fresh configured
  10–15-minute deadline and one deferred PENDING ReplyPlan.
- Later client messages in `HUMAN_PAUSE` increment context, supersede the prior
  deferred plan and create its replacement for the same deadline. The deadline
  never slides.
- A newer APPLIED manager message returns the dialog to `HUMAN_ACTIVE`, creates
  a fresh deadline, clears the pause anchor and cancels all nonterminal plans
  plus unadmitted synthetic outbound.
- `ADMITTED`, `DELIVERED` and terminal outbound are never cancelled by manager
  ingress.

The ReplyPlan worker claim query joins Conversation and can claim only an
`OPEN/BOT/BOT_ACTIVE` dialog. `HandoffExpiryWorker` locks one due Conversation
through `FOR UPDATE SKIP LOCKED` and performs one atomic transition:

- expired `HUMAN_ACTIVE` → `BOT_ACTIVE`, without a reply;
- expired `HUMAN_PAUSE` → `BOT_ACTIVE`, preserving exactly the fenced current
  deferred ReplyPlan.

The paused transition fails closed if that plan is missing, terminal or does
not match `context_version`, `manager_epoch`, event high-water mark and
deadline. It never returns the dialog to the bot while silently dropping the
client reply.

The existing `MANAGER_TAKEOVER` mirror event is mandatory only when an APPLIED
message enters HUMAN_ACTIVE from BOT_ACTIVE. Its deterministic key keeps the
legacy "first takeover fact" semantics.

### Clock

Deadline creation uses PostgreSQL `statement_timestamp()`, obtained after the
Conversation lock. This avoids losing part of the configured interval while a
transaction waits for that lock. Provider time, receipt time and application
host time never schedule handoff. Due selection compares the persisted deadline
directly with PostgreSQL `statement_timestamp()`; no application `now`
parameter exists.

### Restart and concurrency

Expiry needs no durable lease because claim and transition are one PostgreSQL
transaction. A crash before commit rolls back and releases the row lock, so a
new process sees the same due row. A crash after commit leaves a durable
`BOT_ACTIVE` dialog and the existing deferred ReplyPlan for the ReplyPlan
worker. Several expiry instances are compatible: `SKIP LOCKED` prevents them
from processing the same dialog concurrently, while the eligibility predicate
makes the committed transition one-shot.

`HandoffExpiryWorker.tick()` is a bounded application service invoked by the
worker runtime loop (`python -m app.worker`). Process health and supervisor
restart policy are documented in ADR-006.

### Durable outbound admission

Admission and delivery are separate transactions:

1. lock Conversation, then outbound and ReplyPlan;
2. revalidate `context_version`, `manager_epoch`, `event_seq_hwm`, ownership,
   handoff state, deadline and both leases;
3. commit `PROCESSING → ADMITTED` with `admitted_at`;
4. invoke the synthetic sink outside SQL using `outbound_id` as its idempotency
   key;
5. lock Conversation first again and commit `ADMITTED → DELIVERED`.

Manager/client ingress follows the same Conversation-first lock order and may
cancel only `PENDING`, `PROCESSING` or `FAILED`. PostgreSQL therefore chooses
one winner: ingress committed before admission cancels the row; admission
committed first is irreversible. A crash after admission leaves the row
reclaimable in `ADMITTED`; it never regresses to `FAILED` or another
manager-cancellable status. A live transport adapter remains forbidden until
it can enforce the same idempotency contract.

### Safety mode

`BOT_MODE=OFF` remains unchanged. FSM persistence and the local synthetic test
path are allowed, while real outbound remains impossible through the existing
fail-closed policy. No VK, MAX or amoCRM inbound adapter, HTTP route, AI
generator or client sender is added.

## Consequences

Manager and client turns now form one deterministic bounded context, delayed
delivery cannot rewind manager state, and a client response during human work
has exactly one current deferred plan. Automatic transition logic is durable,
restart-safe, and driven by the worker runtime when `DATABASE_URL` is
configured.
