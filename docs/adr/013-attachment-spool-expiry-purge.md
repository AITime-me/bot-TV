# ADR 013: Attachment Spool Expiry Purge (Stage 1A2B3)

## Status

Accepted — Stage 1A2B3 implementation scope.

## Context

Stages 1A1–1A2B2 established encrypted spool storage, leases, secure read,
acknowledgement into `DELETE_PENDING`, and a shared filesystem-first finalizer.
Expired `STORED` and dual-expired `LEASED` objects still require a domain purge
API that transitions into `DELETE_PENDING` without deleting rows directly and
without worker/HTTP wiring.

## Decision

### Public API

- `AttachmentSpoolStore.purge_expired(*, limit: int) -> AttachmentPurgeResult`
- Keyword-only required `limit`; no default; no `purge` alias.
- Exact `int`; `bool` rejected; `1 <= limit <= MAX_PURGE_BATCH` (`1000`).
- Invalid limit → `ATTACHMENT_POLICY_INVALID` (`raise ... from None`).
- No HTTP endpoint; no worker/cron scheduling in this stage.

### Eligibility (PostgreSQL `statement_timestamp()` only)

**STORED:** `state = STORED` AND `expires_at <= statement_timestamp()`.

**LEASED:** `state = LEASED` AND `expires_at <= statement_timestamp()` AND
`lease_expires_at IS NOT NULL` AND `lease_expires_at <= statement_timestamp()`.

**Never selected:**

- Active `LEASED` (`lease_expires_at > statement_timestamp()`), even if object
  expiry has passed.
- Expired lease with still-active object expiry → reclaim zone only.
- `WRITING` → Stage 1A1 reconciliation.
- `DELETE_PENDING` → Stage 1A2B2 finalizer/reconciliation.

### Linearization and batching

- Combined selector: `FOR UPDATE SKIP LOCKED`, `ORDER BY expires_at, id`,
  single global `LIMIT` (not per-state; never `2 × limit`).
- Uses existing partial index `ix_attachment_spool_objects_object_expiry_purge`.
- Per-row conditional `UPDATE ... RETURNING` re-checks eligibility with
  `statement_timestamp()` (point of linearization).
- Selector and transitions share one short service-owned transaction; commit
  before any filesystem unlink.

### Transitions

- `STORED → DELETE_PENDING`: set state + `updated_at`; lease fields remain NULL.
- Dual-expired `LEASED → DELETE_PENDING`: set state + `updated_at`; clear
  `lease_token_digest`, `leased_at`, `lease_expires_at` to NULL.
- Immutable crypto/identity metadata unchanged.
- No schema migration: CHECK constraints already allow `DELETE_PENDING` with a
  full-NULL lease tuple.

### Finalizer reuse

- After transition commit, call shared `_finalize_delete_pending` from Stage
  1A2B2 (origin-neutral; supports NULL lease tuple).
- Do not duplicate finalizer; do not add origin column/enum.
- Public acknowledge remains digest-gated: NULL-lease `DELETE_PENDING` is not
  public-ack-retryable; old tokens after purge receive `ATTACHMENT_ACCESS_DENIED`.

### Result counters (`AttachmentPurgeResult`)

- `transitioned_stored` / `transitioned_leased`: durable transitions committed.
- `deleted`: finalizer `DELETED` only.
- `unsafe_skipped` / `io_unavailable_skipped`: non-fatal FS outcomes; batch continues.
- `skipped`: transition `None`, rare skippable state, finalizer `ALREADY_GONE`,
  finalizer `CONFLICT` only.

### Fatal vs non-fatal

- Non-fatal: FS unsafe/IO, `ALREADY_GONE`, `CONFLICT`, transition `None`.
- Fatal (`ATTACHMENT_RECONCILE_FAILED`, no DTO): select/update/commit/snapshot
  failure; finalizer `STORE_FAILED`; unexpected repository/session failure.
- On `STORE_FAILED`: stop remaining snapshots; prior successful work is retained;
  durable `DELETE_PENDING` remains (file may already be gone); next purge ignores
  DP; reconciliation completes deletion.

### Read security wording

Existing Stage 1A2B1 Phase C revalidation already fail-closes when state becomes
`DELETE_PENDING` or lease tuple is cleared/mismatched. Public
`AttachmentPlaintext` is not constructed or returned. Stage 1A2B3 does **not**
add a memory-zeroization guarantee beyond existing 1A2B1 behavior.

### Crash windows

| Window | Recovery |
|--------|----------|
| After transition commit, before unlink | Durable DP; reconcile/finalizer completes |
| Unlink OK, Phase C `STORE_FAILED` | DP retained; file may be missing; raise; reconcile retries |
| Partial batch then fatal | Prior successes kept; remaining snapshots not finalized this call |

### Explicit exclusions

- Worker/cron, HTTP, adapters, compose, config, deploy
- Model/migration changes
- Weakening public acknowledge authorization
- Folding purge into `reconcile`
- Stage beyond 1A2B3

## Consequences

- CURSOR-12 / BOT-10 attachment spool domain lifecycle is complete for
  store → lease → read → acknowledge → expiry purge → shared finalization.
- Operators must schedule `purge_expired` and `reconcile` externally.
- ADR 010 purge-supporting index is now used by this service API.
- ADR 012 NULL-lease `DELETE_PENDING` from purge is delivered here.
