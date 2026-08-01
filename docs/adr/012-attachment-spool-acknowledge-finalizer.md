# ADR 012: Attachment Spool Acknowledge and DELETE_PENDING Finalizer (Stage 1A2B2)

## Status

Accepted — Stage 1A2B2 implementation scope.

## Context

Stage 1A2B1 added lease-gated secure read. Consumers must be able to acknowledge
successful delivery, transitioning durable state to `DELETE_PENDING` and removing
ciphertext from the spool without leaking credentials, paths, or row identifiers.
Stranded `DELETE_PENDING` rows and crash windows between filesystem unlink and DB
delete must be recoverable via idempotent retry and reconciliation.

## Decision

### Public API

- `AttachmentSpoolStore.acknowledge(lease_token) -> None`
- Exact-type `AttachmentLeaseToken` only; `str`, `AttachmentReference`, subclasses,
  and any other value → `ATTACHMENT_ACCESS_DENIED`.
- Digest computation occurs only after exact type validation.
- Success returns `None` only after durable authorization and finalization rules
  below; no public DTO.

### Authorization (Phase A)

Short service-owned transaction:

1. `FOR UPDATE` by lease digest.
2. Missing row → `ATTACHMENT_ACCESS_DENIED`.
3. `DELETE_PENDING` with exact digest → build snapshot; do not check lease expiry.
4. `LEASED` → conditional repository transition (see below); `None` → denied.
5. Any other state → denied.
6. Commit Phase A.
7. Only after successful commit invoke shared filesystem-first finalizer.

Any DB/session/commit failure before finalizer → `ATTACHMENT_STORE_FAILED`; unlink
not invoked.

### Lease linearization

Repository `transition_leased_to_delete_pending` performs a single conditional
`UPDATE ... RETURNING`:

- `WHERE`: row id, `state = LEASED`, digest match, `lease_expires_at IS NOT NULL`,
  `lease_expires_at > statement_timestamp()`.
- `SET`: `state = DELETE_PENDING`, `updated_at = statement_timestamp()`.
- Lease tuple (`lease_token_digest`, `leased_at`, `lease_expires_at`) unchanged.

This UPDATE is the sole authority for active-lease checks. No separate Python expiry
gate. Expiry between lock and UPDATE yields no row → denied. Successful UPDATE
remains valid even if lease expires before commit.

### Lease tuple retention

Acknowledgement does not clear lease fields. `DELETE_PENDING` rows retain the lease
tuple for idempotent retry with the same token. Future Stage 1A2B3 may introduce
`DELETE_PENDING` rows with `NULL` lease tuple from purge; that is out of scope here.

### Post-delete behaviour

After the DB row is fully removed, a fresh `acknowledge` with the same token fails
Phase A (`FOR UPDATE` miss) → `ATTACHMENT_ACCESS_DENIED`.

### Immutable snapshot

Private frozen `_DeletePendingFinalizeSnapshot` (slots, redacted repr/str) captures
all immutable identity fields needed for Phase C revalidation, including lease tuple
and `object_expires_at`. No ORM instance, path, plaintext, ciphertext, or raw token.

`matches_locked_row` requires `state == DELETE_PENDING` and compares every immutable
field without external oracle on mismatch.

### Shared filesystem-first finalizer

Used by public `acknowledge` and reconciliation.

**Phase FS (outside DB):**

1. Resolve final object path only via UUID-based filesystem API.
2. `unlink_final`; `REMOVED` and `ALREADY_MISSING` are success.
3. `UNSAFE` and `IO_UNAVAILABLE` are filesystem failure; DB row stays
   `DELETE_PENDING`.

**Phase C (short DB transaction):**

1. `FOR UPDATE` by `row_id`.
2. Missing row → internal `ALREADY_GONE` (success for in-flight ack/reconcile).
3. State not `DELETE_PENDING` or any snapshot mismatch → `CONFLICT` → public
   `ATTACHMENT_ACCESS_DENIED` for acknowledge.
4. Full match → `delete_by_id`; `DELETED` only after commit.

Filesystem unlink always precedes DB row delete.

### Crash windows

| Window | Effect |
|--------|--------|
| After Phase A commit, before unlink | Row `DELETE_PENDING`; retry or reconcile completes |
| After unlink, before Phase C commit | File may be missing; retry uses `ALREADY_MISSING` then deletes row |
| Phase C commit failure after unlink | `ATTACHMENT_STORE_FAILED`; row remains; retry safe |
| Row deleted between unlink and Phase C lock | `ALREADY_GONE` success for in-flight operation |

### Reconciliation extension

Existing WRITING / STORED / orphan phases unchanged. Additional phase:

1. Short transaction: `select_delete_pending_for_finalize` (`FOR UPDATE SKIP LOCKED`).
2. Build snapshots inside transaction; commit.
3. Run shared finalizer per snapshot outside transaction.

`AttachmentReconcileResult.deleted_delete_pending` increments only on finalizer
`DELETED` (successful Phase C commit). `ALREADY_GONE`, filesystem failure,
`CONFLICT`, and store failures do not increment.

Active `LEASED` rows are never selected or deleted by this phase.

### Error mapping

| Condition | Code |
|-----------|------|
| Wrong type, unknown token, wrong state, expired transition, digest mismatch, Phase C conflict, fresh ack after delete | `ATTACHMENT_ACCESS_DENIED` |
| Unlink failure after durable `DELETE_PENDING` | `ATTACHMENT_FILESYSTEM_FAILED` |
| Unexpected DB failure (ack path) | `ATTACHMENT_STORE_FAILED` |
| Unexpected reconcile failure | `ATTACHMENT_RECONCILE_FAILED` |

Fixed messages; `raise ... from None`; no token/state oracle; no UUIDs, digests,
paths, or SQL in errors.

### Residual risk (owner-approved)

Filesystem unlink occurs before Phase C revalidation. An out-of-band immutable
metadata mutation (no public mutator exists today) could cause unlink of a file
while Phase C refuses row delete (`CONFLICT`). The row remains `DELETE_PENDING`
for operator investigation. This is accepted residual risk.

### Explicit exclusions

- Stage 1A2B3 purge of expired `STORED` / `LEASED`
- Clearing lease fields on acknowledgement
- `ack` alias, public purge API
- Worker, adapters, HTTP API, compose, deploy changes
- Schema migration (none required)

## Consequences

- `read`, `release`, and `reclaim_expired_leases` unchanged.
- Reconciliation gains `deleted_delete_pending` counter; prior counters unchanged.
- Idempotent acknowledge and reconcile can complete stranded finalization.
