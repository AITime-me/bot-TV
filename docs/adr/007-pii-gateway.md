# ADR-007: centralized PII gateway (Stage 1)

## Status

Accepted for CURSOR-11 Stage 1.

## Context

Bot-TV stores canonical client and manager message text in PostgreSQL for
business functions (dialog timeline, handoff, deferred replies). Unsafe
boundaries — application logs, repr/str, diagnostics, and future AI context —
must not leak plaintext PII even when durable storage retains it.

Prior work established partial protections: credential redaction in
`Settings.__repr__`, test-only `scrub_secrets`, schema `extra="forbid"` for
PII-shaped fields, mirror payload whitelists, and ad-hoc `payload=<redacted>`
repr patterns. There was no single fail-closed module for free-text scrubbing,
structured log redaction, or AI-safe projections.

## Decision

### Trust boundaries

| Boundary | Stage 1 policy |
|---|---|
| inbound channel → application | Schema forbid extra PII fields; plaintext accepted for persistence |
| application → PostgreSQL | Plaintext retained (inbox, manager, INTERNAL_DRAFT draft_text) |
| application → logs / repr / diagnostics | **Mandatory** `app.core.pii_gateway` |
| application → AI provider | **Mandatory** `sanitize_for_ai` / `to_ai_safe_messages` (no live AI on Stage 1) |
| application → outbound channel | Synthetic payloads carry no dialog text (unchanged) |
| application → mirror | Existing mirror whitelist unchanged; `assert_safe_mapping` not wired |
| application → monitoring | Health endpoints expose no message bodies (unchanged) |

### Public API (`app/core/pii_gateway.py`)

- `PiiGatewayError` — technical codes only; never embeds raw values; `__cause__`
  is cleared (`from None`) on internal failure paths to avoid user-controlled
  traceback leakage
- `fingerprint_for_log(value, *, purpose)` — HMAC-SHA256 with a **process-local**
  cryptographically random key (≥32 bytes, generated once at import)
- `redact_for_log(value, *, allowed_keys, limits…)` — JSON-like safe structures;
  fail-closed returns `<redaction-error>` on internal failure; non-finite
  ``float`` values (NaN, ±Infinity) are replaced with `<non-finite-number>`
  so output is RFC JSON-serializable via `json.dumps(..., allow_nan=False)`
- `sanitize_for_ai(text, *, known_pii, max_chars)` — masks email/phones/known_pii;
  raises `PiiGatewayError` on internal failure (no raw fallback); `max_chars`
  must be a nonnegative `int` (`bool` rejected); invalid limits raise
  `SANITIZE_LIMIT_INVALID`; `max_chars=0` yields `<truncated>` after masking
- `assert_safe_mapping(payload, *, allowlist)` — allowlist-only dict validation
  for future unsafe sinks (not connected to amoCRM mirror on Stage 1);
  allowlist must be a ``frozenset``, ``set``, ``list``, or ``tuple`` of
  ``str`` elements only (generators and other iterables are rejected);
  invalid allowlist elements raise ``SAFE_MAPPING_ALLOWLIST_INVALID``;
  internal normalization failures raise ``SAFE_MAPPING_FAILED``; both suppress
  user-controlled exception causes (`from None`)

### Fingerprints

- Same `value` + `purpose` in one process → same token
- Different `purpose` → different token
- Token has technical prefix `pii_fp:` and bounded length
- Token never contains the original value
- **Not stable across process restarts** — cross-process / cross-system
  correlation requires a future persistent HMAC secret (out of Stage 1 scope)
- No plain SHA-256, MD5, reversible encryption, or hardcoded repo secret

### Log redaction

- Sensitive key names (text, body_text, draft_text, phone, email, external IDs,
  tokens, envelope_json, payload_json, …) always redacted regardless of
  `allowed_keys`
- Key names are normalized (Unicode NFKC, separators → underscore) and matched
  by exact normalized name or explicit `birth_*` families — not naive substring
  checks (`context`, `filename`, `namespace` are not false positives)
- Unknown keys default unsafe; PII-bearing key names are never echoed
- Non-string mapping keys never call `str()`/`repr()`; indexed `<key:N>` markers
  handle collisions deterministically
- Built-in `dict` and subclasses are traversed via `dict.__iter__` /
  `dict.__getitem__` to avoid hostile `items()` overrides
- Arbitrary non-`dict` `Mapping` values are replaced with
  `<untrusted-mapping>` (fail-closed)
- Allowed string values still scanned for email/phone patterns after Unicode
  normalization (NFKC, strip Cf format characters)
- Unknown objects never serialized via `repr()`, `str()`, `__dict__`, or
  arbitrary `model_dump()`; Pydantic/dataclass use declared fields only
- No SQLAlchemy relationship traversal or lazy loading
- ORM `__repr__` reads column values only from SQLAlchemy local state
  (`inspect(obj).dict`); missing values render as `<unset>`; incomplete or
  transient instances must not raise during repr

### AI sanitization

- Per-message `to_ai_safe_messages()` preserves authors and message boundaries
- `DialogMessage.__repr__` shows only allowlisted authors (`client`, `manager`);
  other authors render as `<redacted>` without calling user `str`/`repr`
- `author` is restricted to `client` and `manager`; unknown authors raise
  `PiiGatewayError("AI_AUTHOR_INVALID")`
- Masking (`known_pii`, email, phone) runs **before** `max_chars` truncation
- No conversation/database IDs or sequences in AI projection
- Free-text names are **not** fully detected by NER; protection is via
  structured key redaction, `known_pii`, and narrow self-introduction patterns
- Stage 1 does not claim full DLP or name recognition for arbitrary prose

### Storage-only payloads

`safe_payload()` and `safe_envelope()` retain historical names but are
**storage-only**: they contain plaintext and must never be used for logs or
diagnostics.

### Explicit non-goals (Stage 1)

- No PostgreSQL schema migration or column encryption
- No masking before durable INSERT
- No live channels, webhooks, or AI provider integrations
- No persistent HMAC secret or env configuration
- No reversible encryption
- No change to handoff FSM, fencing, leases, idempotency, or mirror contracts
- `ValidationError` from Pydantic must not be logged raw when public ingress
  arrives (future gate)

## Consequences

Unsafe boundaries gain a single fail-closed module. Durable plaintext remains
for business logic. Future LLM wiring must call `sanitize_for_ai` /
`to_ai_safe_messages` explicitly; there is no automatic DB masking.

## Safety defaults (unchanged)

- `BOT_MODE=OFF`
- `EMERGENCY_LOCK=true`
- Fail-closed outbound and synthetic-only channels
