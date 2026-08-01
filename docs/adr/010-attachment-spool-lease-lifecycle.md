# ADR 010: Attachment Spool Lease Lifecycle (Stage 1A2A)

## Status

Accepted — Stage 1A2A implementation scope.

## Context

Stage 1A1 established encrypted attachment storage with `WRITING` and `STORED`
states. Downstream read/ack/purge flows require a short-lived, revocable lease
credential that does not expose database identifiers, filesystem paths, or raw
reference tokens.

## Decision

### States

Expand the metadata state contract to:

- `WRITING`
- `STORED`
- `LEASED`
- `DELETE_PENDING` (schema only in 1A2A; no service transition yet)

### Lease credential

- `AttachmentLeaseToken`: 32-byte CSPRNG, canonical padded base64url (same shape
  as `AttachmentReference`).
- PostgreSQL stores only SHA-256 digest (`lease_token_digest`, 32 bytes).
- `AttachmentLeaseHandle` exposes only `token` and `lease_expires_at`.
- No `lease_generation`, `leased_by`, fencing counters, or optimistic versions.

### Lease TTL

Fixed internal policy: **300 seconds** (`LEASE_TTL_SECONDS`). Not configurable
via `app/config.py`.

Eligibility timestamps come from fresh PostgreSQL `statement_timestamp()` inside
the owning transaction, never application wall clock.

### Acquire

Under one service-owned transaction:

1. `FOR UPDATE` by reference digest.
2. Fresh `statement_timestamp()` after lock.
3. Eligible when `STORED` and object `expires_at > now`, or expired `LEASED`
   (reclaim then grant).
4. Generate lease token only after eligibility passes.
5. Set `LEASED` with digest and lease timestamps; commit before returning handle.
6. Unique digest collisions: bounded retry (max 3) via nested transaction /
   savepoint; unrelated integrity errors are not retried.

All authorization failures map to `ATTACHMENT_ACCESS_DENIED` without oracle
distinction.

### Release

Under one transaction: lock by lease digest, require active `LEASED` lease,
clear lease fields, return to `STORED`. Does not extend object `expires_at` or
touch filesystem. Repeat release is denied, not a no-op.

### Reclaim expired leases

Bounded internal `reclaim_expired_leases(limit)` (1..1000): select expired
`LEASED` rows with `FOR UPDATE SKIP LOCKED`, recheck with fresh PostgreSQL time,
transition to `STORED`, clear lease fields. Returns numeric counts only.

### Schema

Migration `20260801_16_spool_leases` adds nullable lease columns,
all-or-none lease field CHECK, state-dependent CHECK, partial unique digest
index, reclaim index on `lease_expires_at` where `LEASED`, and purge-supporting
partial index on `expires_at` where `STORED` or `LEASED`.

Downgrade is fail-closed if any `LEASED` or `DELETE_PENDING` rows remain.

### Out of scope (Stage 1A2B)

- Read/decrypt service API
- Ack / `DELETE_PENDING` finalization
- Purge implementation
- Worker wiring, channel adapters, HTTP API

## Consequences

- Reconcile (Stage 1A1) continues to target only `WRITING` and `STORED`
  candidates; it does not mutate `LEASED` or `DELETE_PENDING` rows.
- Release may succeed while object `expires_at` has passed; the row becomes
  `STORED` but remains object-expired for future acquire denial and later purge.
