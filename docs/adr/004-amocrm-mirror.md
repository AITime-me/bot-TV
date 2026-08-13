# ADR-004: amoCRM mirror (CURSOR-09)

## Status

Accepted for CURSOR-09.

## Context

01A stores conversation/inbox/outbox, 01B durably accepts provider events, and
01C (ADR-003) adds ReplyPlan orchestration plus the single Outbound Arbiter.
Nothing in bot-TV reports domain progress to amoCRM yet: `AMOCRM_CLIENT_ID` and
`AMOCRM_CLIENT_SECRET` exist in `.env.example` with no consumer, and ADR-003
lists amoCRM as out of scope.

This stage introduces the *transport-free* half of that integration: a durable,
idempotent, fenced queue of domain events that a future real adapter can drain.
No amoCRM API client, no HTTP, no secrets, and no client-facing send exist here.

## Decision

### Direction

Only `bot-TV → amoCRM`. A future reverse flow (`amoCRM → bot-TV`, e.g. manager
replies or entity updates arriving from CRM) is acknowledged as a possible later
stage, but CURSOR-09 deliberately adds no table, enum value, handler, or public
interface for it. Reserving an unused inbound direction would create dead schema
that the real adapter stage would have to migrate away from.

### Job source: transactional outbox

Mirror jobs are enqueued *inside the existing domain transaction* that produced
the event. A job therefore exists if and only if the domain change committed:
rollback of `InboundService.accept` leaves no mirror job, and a committed inbox
message always leaves exactly one.

Enqueue points, all inside transactions that already hold the dialog lock:

- `InboundService.accept` — `CLIENT_MESSAGE_RECEIVED_META`, only when a new inbox
  row was created (duplicate deliveries enqueue nothing).
- `ManagerTakeoverService.apply` / `apply_manager_takeover_in_session` —
  `MANAGER_TAKEOVER`, only on the transition (`changed is True`).
- `ReplyPlanWorker.dispatch_claimed` — `REPLY_PLAN_STATE_CHANGED` (`DISPATCHED`).
- `ReplyPlanWorker.fail_claimed` — `REPLY_PLAN_STATE_CHANGED` (`DEAD`).
- `OutboundArbiter._admit_in_session` — `OUTBOUND_DELIVERED_META`.

### Mirrored events

Exactly four, and they describe **bot-TV domain events**, never amoCRM entities:

- `CLIENT_MESSAGE_RECEIVED_META` — a new client message exists (metadata only).
- `REPLY_PLAN_STATE_CHANGED` — a reply plan reached a terminal worker state.
- `MANAGER_TAKEOVER` — the dialog moved to manager ownership.
- `OUTBOUND_DELIVERED_META` — the synthetic outbound message was admitted.

Message text and client contacts are never mirrored. `CONVERSATION_UPSERT` is
intentionally absent: without a real entity mapping it would pretend to carry
amoCRM semantics. `conversation_id` travels only as internal subject identity
inside the technical payload.

`REPLY_PLAN_STATE_CHANGED` covers `DISPATCHED` and `DEAD` only — the states
reached through single-row, lease-fenced worker paths. `SUPERSEDED` and
`CANCELLED` are applied by bulk `UPDATE`s (`supersede_open_plans`,
`cancel_open_plans_for_takeover`) that return only a row count; mirroring them
individually would require changing 01C repository signatures. Takeover is
covered by `MANAGER_TAKEOVER`, and superseding is implied by the next
`CLIENT_MESSAGE_RECEIVED_META` carrying a higher `context_version`. This is an
accepted limitation of the stage, not an oversight.

### No amoCRM entity semantics

CURSOR-09 does not decide how a domain event becomes a lead, contact, note, or
task. `job_type` and `subject_kind` stay in bot-TV vocabulary
(`CONVERSATION`, `INBOX_MESSAGE`, `REPLY_PLAN`, `OUTBOX_MESSAGE`). The mapping
belongs to the real adapter stage, where the actual amoCRM entity model is known.

### Internal identity vs external amoCRM ID

Mandatory contract for the future real adapter, deliberately *not* implemented
here: bot-TV keeps its own stable internal identity (UUID), and any amoCRM entity
id lives in a separate mapping table (planned `amocrm_entity_links`, with a
unique internal key and a nullable, separately unique external id). That table
ships with the adapter stage, once the entity model is known; adding it now would
guess at a schema and create an empty table with no writer. External identifiers
must never leak into `mirror_key` or `payload_json`.

### Job lifecycle

Statuses: `PENDING → PROCESSING → MIRRORED | SKIPPED | FAILED | DEAD`, plus
`FAILED → PROCESSING` (retry) and `FAILED → DEAD` (exhausted). Terminals are
`MIRRORED`, `SKIPPED`, `DEAD`.

- `MIRRORED` means *required amoCRM entity state for this mirror job
  converged successfully*. It is not "message content copied to CRM".
  When CRM REST / deal-create is disabled or fail-closed invalid, the
  adapter performs zero CRM HTTP and the job may still reach `MIRRORED`
  because no CRM writes were required.
- `SKIPPED` is a deliberate refusal to mirror a stale event, not an error.
- `DEAD` is terminal on this stage.

### Lock order

`conversations → inbox_messages → reply_plans → outbox_messages →
amocrm_mirror_jobs`

`amocrm_mirror_jobs` is always last. Its foreign key to `conversations` takes
`FOR KEY SHARE` on the dialog row, so the enqueue is the final row-touching
statement of a transaction that already holds `conversations FOR UPDATE` through
`conversations.lock_for_update()`. The mirror worker follows the same order:
conversation lock first, then the job row. As in ADR-003, no bounded transaction
retry is added; it would only hide a future ordering regression.

### Clock of record

PostgreSQL only, through `app/db/clock.py`. `next_attempt_at`, `lease_until`,
`created_at`, and `updated_at` all resolve to the transaction timestamp or an
explicitly injected instant on the same timeline. Reading `datetime.now()` in
mirror code is forbidden and enforced by a static test guard.

### Idempotency

`mirror_key` is deterministic and built only from internal UUIDs, versions, and
statuses:

- `client-message-meta:{inbox_id}`
- `reply-plan-state:{plan_id}:{status}`
- `manager-takeover:{conversation_id}`
- `outbound-delivered:{outbound_id}`

Enqueue is `INSERT ... ON CONFLICT DO NOTHING` on `uq_amocrm_mirror_key`, so a
duplicate inbound delivery, a repeated dispatch, or a repeated takeover create no
second job. Repeated processing of one job is prevented by the
`(lease_token, lease_version)` fencing pair.

### Revalidation gate

A job is validated against live state *at claim time*, under the conversation
lock, before the sink is called. The worker first re-locks the job row and
proves `(status, lease_token, lease_version, lease_owner)` still match the
claim — a superseded lease *before* the adapter raises
`StaleAmoCrmMirrorLeaseError` with no adapter call (and therefore no CRM HTTP).
After that fence check the DB lock is released for CRM HTTP; the mirror lease
may be reclaimed mid-flight. Stale completion/`fail` remains fenced. Divergence
of live domain state produces a terminal `SKIPPED` with a
fixed `skip_reason`:

- `MANAGER_TAKEOVER` — ownership became `MANAGER` or `manager_takeover_at` is set.
- `STALE_CONTEXT` — the job's `context_version` no longer matches the dialog.
- `SUBJECT_STATE_CHANGED` — the subject row is gone or no longer in the expected
  state recorded at enqueue time.

The ownership and context gates apply to **bot-action** events only
(`REPLY_PLAN_STATE_CHANGED`, `OUTBOUND_DELIVERED_META`): those describe what the
bot did for one specific context version, and reporting them after a takeover or
a newer client message would assert bot activity in a dialog that no longer
belongs to the bot. **Domain facts** (`CLIENT_MESSAGE_RECEIVED_META`,
`MANAGER_TAKEOVER`) stay true regardless of later ownership or context changes
and are never suppressed by those two gates — dropping them would silently lose
events when messages arrive in quick succession. `MANAGER_TAKEOVER` carries no
`context_version` at all, since the transition is not context-bound. The
subject-state check applies to every job.

### Retry and DEAD

`FAILED` schedules `next_attempt_at = db_now() + delay`; after `max_attempts` the
job becomes `DEAD`. There is no automatic resurrection, and editing `status` or
`attempt_count` with ad-hoc SQL is explicitly **not** a supported operational
contract. A safe operator replay/requeue — issuing a fresh lease and writing an
audit trail — is a separate future action, deliberately not implemented here.

BOT-TV-10 makes the persisted job `max_attempts` the only limit used by claim,
explicit failure, and recovery. At the start of a claim cycle, an expired
`PROCESSING` job with `attempt_count >= max_attempts` is atomically moved to
`DEAD`, its lease is cleared, and the adapter is not called. The old
token/version/owner therefore cannot complete or fail the terminal job. The
same exhausted-lease rule applies to ingress, ReplyPlan, and synthetic outbound;
ingress receives its persisted `max_attempts` column in
`20260728_10_attempt_exhaustion`.

### Personal data

`payload_json` is produced by a single whitelist builder, `safe_mirror_payload()`,
and validated by `assert_mirror_payload_is_safe()` on every enqueue. The whitelist
is exactly `schema`, `job_type`, `subject_kind`, `subject_id`, `conversation_id`,
`context_version`, `subject_status`, and values must be flat scalars so nothing can
be smuggled in a nested structure. Timing lives in the row's own columns, not in the
payload. Forbidden: `text`, `draft_text`, `phone`, `email`, `client_name`, and any
external identifier. The
only client text in the system lives in `inbox_messages.payload_json.text` and
`outbox_messages.payload_json.draft_text`; neither is ever copied into a mirror
job. `error_code` is a fixed short code, never a driver message, and `__repr__`
renders `payload=<redacted>`.

### Configuration and secrets

No `AMOCRM_*` setting is added to `Settings`, and the existing
`AMOCRM_CLIENT_ID` / `AMOCRM_CLIENT_SECRET` names in `.env.example` are not read
by any code. OAuth, token refresh, and secret handling belong to the real adapter
stage. Ad-hoc `os.environ` access in mirror modules is forbidden.

### Migration

One new revision, `20260728_09_amocrm_mirror`, on top of
`20260727_01c_reply_outbound`. It creates a single table with a reversible
`downgrade` and touches no 01A/01B/01C object.

### Out of scope

CURSOR-09 originally deferred the real API client. AMO-01B2 adds CRM REST OAuth
and TECHNICAL_DEAL convergence only. Still out of scope: notes/tasks, mirroring
of message text, contact create/guess, reverse `amoCRM → bot-TV` flow, automatic
retention and anonymization, operator replay of `DEAD`, VK/MAX/Telegram/site
adapters, AI, client writes, `online-zapis-tv` access, using the mirror as a
message transport, and any change to the source of truth.

### Deferred requirements before a production amoCRM connection

1. ~~Real adapter stage: HTTP client, OAuth, error taxonomy, rate limits, and the
   domain-event → amoCRM entity mapping.~~ **AMO-01B2 (partial):** CRM REST
   OAuth + TECHNICAL_DEAL convergence on the existing `amocrm_mirror_jobs`
   worker. Notes/tasks and message-text mirroring remain out of scope.
2. ~~`amocrm_entity_links` (or equivalent) implementing the internal/external
   identity contract above.~~ **Shipped** for `TECHNICAL_DEAL` and deterministic
   `CONTACT` reuse. Contact create/guess is forbidden.
3. Retention and anonymization policy for `amocrm_mirror_jobs`, which is de facto
   a durable domain journal. Unresolved and mandatory before production.
4. Safe operator replay/requeue for `DEAD` jobs, with a new lease and audit.
5. Reverse flow design, if it is ever needed.
6. Operator resolution of `RECONCILE_REQUIRED` entity links after an ambiguous
   create (5xx / transport / uncertain POST). Blind resend is forbidden.

## AMO-01B2 amendment: CRM REST entity convergence

Accepted for AMO-01B2 on top of CURSOR-09.

**MIRRORED** is defined as: "required amoCRM entity state for this mirror job converged successfully"
— not "message content copied to CRM".

The existing `amocrm_mirror_jobs` queue, lease, and fencing stay the only
drain path. No second queue. The worker revalidates the claimed job/fence
under the conversation lock, **then** releases that lock before CRM HTTP.
Mirror lease may be reclaimed mid-flight while CRM HTTP is in progress; a
stale worker cannot `complete`/`fail` the job (lease fence). Concurrent
`ensure_technical_deal` still preserves exactly one open `TECHNICAL_DEAL`
via reservation + unique open index (no second blind create).

`CrmRestMirrorAdapter` calls `ensure_technical_deal`: exactly one
`TECHNICAL_DEAL` per conversation; reuse an existing deterministic `CONTACT`
link and attach it if needed; never create or guess a contact; never write
notes, tasks, or message text.

CRM REST and deal-create remain **disabled by default**. Invalid config or
missing tokens produce zero CRM entity writes. Chat HMAC (`AMOCRM_CHAT_*`)
is never used on this path.

HTTP taxonomy:

| Status | Existing deal GET | Deal create POST |
| ------ | ----------------- | ---------------- |
| 2xx | ENSURED; attach CONTACT if needed | ACTIVE + id |
| 404 | revoke stale link; may recreate | explicit 4xx; release reservation; no RECONCILE |
| 401 | refresh once under token-store fencing, retry that request once; still 401 → TRANSIENT, no revoke | same; then release reservation for a later retry |
| 402 / 403 / 429 / 5xx / transport | TRANSIENT; do **not** revoke or create a replacement | `RECONCILE_REQUIRED`; never a second blind POST |

Proactive OAuth refresh runs when the stored access token is expired or within
60s of expiry. Concurrent refresh is fenced by the token-store lease. Before
the remote refresh POST the worker renews/validates that lease; after HTTP 200
with a valid pair it persists immediately under fencing (bounded local retries;
guarded recovery only if the DB still holds the exact pre-refresh pair). The
remote refresh POST is never retried after a successful 200. Unrecoverable
post-200 local persist fails closed with
`AMOCRM_CRM_OAUTH_ROTATE_PERSIST_FAILED` / `…_ROTATE_SUPERSEDED` — never a
silent “healthy” auth state. Residual window: crash between remote 200 and the
first durable local write can still require operator re-seed; that dual-write
gap is not claimed eliminated.

## Consequences

Domain progress becomes durably queued and replay-safe without any external
call, so the real adapter stage can be built and tested against a populated
queue. Workers recover after a crash through leased rows, and stale workers can
neither mirror superseded events nor resurrect terminal jobs. The cost is a
growing journal table with an explicitly deferred retention policy.
`MIRRORED` means required amoCRM entity state for that job converged
successfully — not that message content was copied to CRM.
