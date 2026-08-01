# ADR 011: Attachment Spool Secure Read (Stage 1A2B1)

## Status

Accepted — Stage 1A2B1 implementation scope.

## Context

Stage 1A2A introduced lease-gated access to encrypted attachment metadata.
Consumers need plaintext bytes without exposing database identifiers, filesystem
paths, raw tokens, or crypto metadata. Concurrent release, reclaim, and future
ack/purge must not allow time-of-check/time-of-use races.

## Decision

### Public API

- `AttachmentSpoolStore.read(lease_token) -> AttachmentPlaintext`
- `AttachmentPlaintext` contains only:
  - `data: bytes` (decrypted plaintext)
  - `mime: AttachmentMime` (server-detected at store time)

No UUID, reference, digest, path, key, nonce, state, or timestamps in the DTO.

### Three-phase read

**Phase A — short authorization transaction**

1. Exact-type `AttachmentLeaseToken` validation.
2. `FOR UPDATE` by lease digest.
3. Fresh PostgreSQL `statement_timestamp()` after lock.
4. Require `LEASED`, exact digest, active lease (`lease_expires_at > now`).
5. Build frozen internal `_ReadCryptoSnapshot` (fully redacted repr).
6. Commit. No filesystem access. No lease/object expiry mutation.

**Phase B — outside any DB transaction**

1. `read_ciphertext_verified` (UUID-derived final path only).
2. Reconstruct `AttachmentCiphertext` and exact `AttachmentAad` from snapshot.
3. `decrypt_bytes` using recorded `key_id` (not active key only).
4. Hold plaintext in a local variable only.

**Phase C — second authorization transaction**

1. `FOR UPDATE` by row id.
2. Fresh `statement_timestamp()`.
3. Revalidate lease authorization and every immutable snapshot field.
4. Commit.
5. Return `AttachmentPlaintext` only after successful commit.

Any Phase C failure: drop plaintext reference; raise `ATTACHMENT_ACCESS_DENIED`
without oracle on cause.

### Error mapping

- Credential/state/revalidation failures: `ATTACHMENT_ACCESS_DENIED`
- Filesystem verify/read failures: `ATTACHMENT_FILESYSTEM_FAILED`
- Unexpected failures: `ATTACHMENT_STORE_FAILED`
- Crypto/decrypt failures during read: `ATTACHMENT_ACCESS_DENIED`

All public errors use `from None`; no path/UUID/digest/SQL leakage.

### Invariants

- Read does not extend `leased_at`, `lease_expires_at`, or object `expires_at`.
- Read does not change row state.
- Object `expires_at` is not checked during read authorization (lease governs access).

### Crash behavior

- After Phase A: lease unchanged; caller may retry read.
- During Phase B: no plaintext returned; retry safe.
- After decrypt, before Phase C commit: plaintext not returned.
- After Phase C commit: plaintext returned to caller.

### Out of scope (Stage 1A2B2 / 1A2B3)

- `ack` and `DELETE_PENDING` transitions
- Purge
- DELETE_PENDING finalization
- Reconcile extensions for DELETE_PENDING
- Worker wiring, adapters, HTTP API

## Consequences

- For Stage 1A2B1, `reconcile` targeted only `WRITING` and `STORED`.
- `release` and `reclaim_expired_leases` remain unchanged.
- No schema migration required for 1A2B1.

See also [ADR 012](012-attachment-spool-acknowledge-finalizer.md) for Stage 1A2B2
acknowledge and `DELETE_PENDING` reconciliation.
