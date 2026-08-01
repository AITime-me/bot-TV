# ADR-009: temporary encrypted attachment spool (Stage 1A1)

## Status

Accepted for CURSOR-12 Stage 1A1 (store + reconciliation foundation).

## Context

BOT-10 requires a temporary store for image attachments until confirmed
external delivery or TTL expiry. Stage 1A1 provides the DB-authoritative
write path and crash reconciliation without delivery leases, channel
adapters, worker wiring, or Compose volumes.

Ephemeral PII (`ADR-008`) is intentionally **not** reused: phone crypto is
`str`/256-byte UTF-8 only and a different threat model.

## Decision

### Storage

- Encrypted ciphertext files on a server-owned filesystem root
- PostgreSQL metadata registry (`attachment_spool_objects`)
- Opaque `AttachmentReference` (CSPRNG → canonical base64url; SHA-256 digest in DB)
- No plaintext, raw reference, client filename, or plaintext content hash in DB

### Crypto

- AES-256-GCM whole-file AEAD for binary bytes (`encrypt_bytes` / internal `decrypt_bytes`)
- Max plaintext: **5 MiB**
- Separate key namespace: `ATTACHMENT_SPOOL_ACTIVE_KEY_ID` / `ATTACHMENT_SPOOL_KEY_<ID>`
- AAD marker `attachment-aad-v1` binds record/object/conversation/kind/purpose/MIME/size
- `ciphertext_sha256` stored; plaintext SHA-256 forbidden

### MIME type gating

- Allowlist: `image/jpeg`, `image/png` only
- Server-side structure checks (stdlib only)
- Filename, extension, and caller Content-Type are ignored
- This is **type gating**, not antivirus and not a full image decoder

### Lifecycle (Stage 1A1)

States: `WRITING` → `STORED`

1. Validate + encrypt in memory
2. INSERT complete metadata as `WRITING` and commit
3. Write ciphertext via exclusive temp + fsync + atomic rename
4. Verify final file size/hash
5. UPDATE to `STORED` and commit
6. Return handle only after STORED commit

### Filesystem inspection

Ciphertext inspection returns a closed status, never an opaque bool:

- `MISSING` — confirmed `FileNotFoundError` / `ENOENT` only
- `VALID` — regular file, not symlink, exact size and SHA-256
- `MISMATCH` — regular file opened safely, size or hash differs
- `UNSAFE` — symlink, non-regular, containment/path violation
- `IO_UNAVAILABLE` — `PermissionError` / `EACCES` / `EPERM` / `EIO` /
  other transient `OSError` that does **not** prove missing, mismatch, or
  corruption

Transient filesystem errors are **not** evidence of corruption.

### Reconciliation

- Explicit `reconcile(*, limit)` service API
- Stale `WRITING` older than **600 seconds** (PostgreSQL `statement_timestamp`)
- `VALID` final → promote to `STORED`
- Confirmed `MISSING` / successful safe unlink after `MISMATCH` → delete row
- `UNSAFE` → skip; do not delete row or follow symlink; count `unsafe_skipped`
- `IO_UNAVAILABLE` → skip; do not delete; count `io_unavailable_skipped`
- Metadata row is deleted only after confirmed missing **or** successful safe
  unlink (or confirmed already-missing after unlink). Unlink/`PermissionError`
  failure must not delete metadata.
- Reconciliation is **fail-closed**, not **fail-destructive**
- Orphan internal UUID `.tmp`/`.bin` without row → bounded cleanup
- Orphan delete counters increment only on `REMOVED` after post-unlink
  absence confirmation; canonical shard/name must match `object_id`
- Filesystem scan budget counts **every inspected entry** (including invalid
  names), capped by reconcile `limit` (1..1000)
- **Not** wired to worker loops in Stage 1A1

### Safety defaults

- `BOT_MODE=OFF`
- `EMERGENCY_LOCK=true`
- No production TTL/path in `app/config.py`; inject `AttachmentSpoolPolicy`

## Explicit non-goals (Stage 1A1)

- Delivery leases / `acquire_delivery` / `read_for_delivery` / ack (Stage 1A2)
- Worker purge/reconcile loop and Compose shared volume (Stage 1B)
- VK/MAX/amoCRM/AI/n8n adapters
- Inbox/outbound schema changes
- Production secrets or deploy

## Consequences

Stage 1A2 can add fencing-aware delivery without changing the
DB-authoritative write contract. Shared volume is required before api and
worker both touch the spool root.
