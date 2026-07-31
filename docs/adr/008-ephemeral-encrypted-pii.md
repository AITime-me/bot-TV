# ADR-008: encrypted ephemeral PII foundation (Stage 2A)

## Status

Accepted for CURSOR-11 Stage 2A (crypto/key-provider foundation).

## Context

CURSOR-11 Stage 1 ([ADR-007](007-pii-gateway.md)) established fail-closed
log/repr/AI scrubbing while durable PostgreSQL message text remained plaintext
for business functions. Stage 1 intentionally omitted reversible encryption and
any recover path.

BOT-09 Stage 2 requires authenticated encryption for short-lived extracted PII
values so that future purpose-bound adapters can recover a value without exposing
raw PII to AI, logs, repr, metrics, or technical alerts. Stage 2 is split into
two independent PRs:

1. **Stage 2A (this ADR)** — crypto primitive, key provider, closed types, unit
   tests, dependency pin. No PostgreSQL.
2. **Stage 2B (future)** — table, migration, opaque reference, TTL, consume,
   purge, repository/service, worker wiring.

## Decision

### Cryptography

- Runtime dependency: `cryptography==49.0.0`
- Primitive: AES-256-GCM via
  `cryptography.hazmat.primitives.ciphers.aead.AESGCM`
- Key length: exactly 32 bytes
- Nonce: exactly 12 random bytes per encrypt (`secrets.token_bytes`); never
  supplied by plaintext, AI, or external callers
- Encrypt post-condition: after nonce generation, `type(nonce) is bytes` and
  `len(nonce) == 12` must hold before `AESGCM.encrypt`; failure raises
  `EPHEMERAL_PII_ENCRYPT_FAILED` (no auto-repair, truncate, or pad)
- `EphemeralPiiCiphertext` enforces structural invariants: nonce exact `bytes`
  length 12; ciphertext exact `bytes` with minimum length **16** (GCM tag)
- Associated data (AAD) is mandatory and canonical UTF-8 with fixed field order:
  schema marker, `crypto_version`, `record_id`, `key_id`, `kind`,
  `conversation_id`, `purpose`
- No Fernet, no custom cryptography, no raw fallback
- Maximum plaintext size for one ephemeral value: **256 UTF-8 bytes**
  (`MAX_PLAINTEXT_BYTES`)

### Key provider

- Interface: `active_key_id()` / `get_key(key_id)`
- Implementation: `EnvEphemeralPiiKeyProvider`
- Env names (values never documented here):
  - `EPHEMERAL_PII_ACTIVE_KEY_ID`
  - `EPHEMERAL_PII_KEY_<KEY_ID>`
- Key id: exact `str`, closed ASCII `[A-Z0-9_]{1,64}`
- Key material: canonical base64url decoding to exactly 32 bytes; whitespace,
  non-canonical encoding, and malformed padding are rejected
- Lazy read on first crypto use — not at import and not at `BOT_MODE=OFF`
  health startup
- No public key enumeration API
- Rotation: ciphertext records `key_id`; decrypt loads that id directly. No key
  list walk. Missing recorded key fails closed. Re-encryption job is out of
  scope.

### Closed enums

- `EphemeralPiiKind`: `PHONE` only in Stage 2A
- `EphemeralPiiPurpose`: `BOOKING_PHONE_WRITE`,
  `APPROVED_STAFF_ALERT_PHONE`, `AMOCRM_CONTACT_SYNC`
- Enum presence does not enable integrations and does not let AI choose purpose
- Arbitrary purpose strings are rejected

### Exception safety

Fixed technical codes only, raised `from None` with `__cause__ is None`:

- `EPHEMERAL_PII_CONFIG_INVALID`
- `EPHEMERAL_PII_KEY_UNAVAILABLE`
- `EPHEMERAL_PII_VALUE_INVALID`
- `EPHEMERAL_PII_ENCRYPT_FAILED`
- `EPHEMERAL_PII_ACCESS_DENIED`

Decrypt authentication failures (wrong key, tampered ciphertext/nonce, wrong
AAD, `InvalidTag`) unify to `EPHEMERAL_PII_ACCESS_DENIED`. On the decrypt
boundary, **all** recorded-key fetch failures — missing env, malformed
base64url, wrong decoded length, wrong key bytes, provider
`CONFIG_INVALID`/`KEY_UNAVAILABLE`, and unexpected provider exceptions —
also unify to `EPHEMERAL_PII_ACCESS_DENIED`, so decrypt cannot oracle whether
a recorded key exists or is correctly provisioned. The direct key-provider API
(`get_key` / `active_key_id`) retains configuration codes for the trusted
configuration boundary; unification applies only inside `decrypt_text`.
Decrypt never falls back to the active key and never enumerates keys.
Errors never embed plaintext, ciphertext, nonce, key id, env values, or
underlying exception text. DTO/`provider` repr and str expose only safe
sizes/markers.

### Trust boundary

- `encrypt_text` / `decrypt_text` accept only exact enum/UUID AAD fields
- `app/core/pii_gateway.py` does not import ephemeral crypto and gains no decrypt
- `sanitize_for_ai` and AI projection modules must not import decrypt
- No universal secrets vault and no mutable decrypted cache

### Safety defaults (unchanged)

- `BOT_MODE=OFF`
- `EMERGENCY_LOCK=true`

## Explicit non-goals (Stage 2A)

- PostgreSQL store, migration, repository, service
- Opaque reference / AI marker contract
- TTL / consume-once / purge / worker loop
- Production retention policy (not approved)
- Channel, booking, amoCRM, staff-alert, n8n, or AI provider wiring
- BOT-10 attachment spool
- Changes to handoff, fencing, leases, idempotency, outbound, or mirror

## Consequences

Future Stage 2B can persist ciphertext envelopes produced by this foundation
without expanding the AI or log surfaces. Dependency lock grows by
`cryptography` and its required transitive pins (`cffi`, `pycparser`) only.
