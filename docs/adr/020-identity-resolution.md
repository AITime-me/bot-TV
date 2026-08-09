# ADR-020: Identity Resolution & Buyer Card Reconciliation (CURSOR-30)

## Status

Accepted for CURSOR-30. Durable channel/provider-agnostic identity foundation
only. No live amoCRM, n8n, VK/MAX ingress, booking DB access, deploy, or
automatic CRM merge.

## Context

Future client-facing actions (amoCRM tasks/invoices, n8n, VK/MAX dialogue) must
address exactly one canonical client and its Buyer Card. Without a durable
identity graph, systems re-create contacts/deals/cards from ambiguous phone,
email, or name signals. Master channel binding (ADR-017) solves master↔account
mapping and must not be overloaded as client identity.

## Decision

### Canonical identity graph

- Internal **canonical client identity** is a stable bot-TV UUID
  (`canonical_identities.id`). It is never a name, phone, amo id, or channel id.
- Canonical status: only `ACTIVE` is resolvable. `ARCHIVED` identities are
  ignored by `resolve` / `reconcile_buyer_card` even if ACTIVE links remain.
- `external_identity_links` map
  `(provider, connection_scope, entity_kind, external_id)` → canonical identity.
- `provider` is an opaque extensible token (vk/max/site/amocrm/phone/email/…);
  new providers do not require a schema rewrite.
- `entity_kind` is a closed CHECK set expanded by migration when needed:
  `CHANNEL_ACCOUNT`, `PHONE`, `EMAIL`, `ONLINE_ZAPIS_CLIENT`, `AMOCRM_CONTACT`,
  `AMOCRM_BUYER_CARD`, `AMOCRM_TECHNICAL_DEAL`.
- Link lifecycle: `ACTIVE` (resolvable) / `REVOKED` (audit history, ignored).
- Partial unique index:
  `UNIQUE (provider, connection_scope, entity_kind, external_id) WHERE status = 'ACTIVE'`.
- Additional partial unique for amoCRM deal roles
  (`uq_external_identity_links_active_amocrm_deal_role`):
  `UNIQUE (provider, connection_scope, external_id) WHERE status = 'ACTIVE'
  AND entity_kind IN ('AMOCRM_BUYER_CARD', 'AMOCRM_TECHNICAL_DEAL')`.
  One deal id cannot be both Buyer Card and technical/chat deal at once.

### Matching priority

Strict order in `IdentityResolutionService.resolve`:

1. Exact channel account / already stored durable external key
2. Other **CONFIRMED** durable links supplied as signals
3. Normalized phone (`entity_kind=PHONE`) — primary matching signal
4. Email only as **SECONDARY/corroborating** signal:
   - may confirm the same primary candidate from (1–3);
   - may conflict → `MANUAL_REVIEW_REQUIRED`;
   - multiple email canonicals → `MANUAL_REVIEW_REQUIRED`;
   - **email alone never returns `RESOLVED`** — `NOT_FOUND` with safe reason
     `EMAIL_ONLY_SECONDARY`
5. **Never** resolve by name or free text (`IdentityResolveSignals` has no
   name/client_name/free-text fields; AST contract tests enforce this)

Zero primary candidates → `NOT_FOUND` (or `EMAIL_ONLY_SECONDARY` when email
matched without primary). More than one distinct canonical candidate, or
conflicting confirmed links → `MANUAL_REVIEW_REQUIRED`. No arbitrary pick, merge,
or silent create inside `resolve`.

### Ambiguity / manual review

All critical APIs return typed outcomes. `MANUAL_REVIEW_REQUIRED` carries a fixed
`reason` code (no PII). Callers must stop automated CRM/booking side-effects and
route to human review. No automatic mass merge of amo entities.

### Buyer Card semantics

- Buyer Card = canonical CRM card for purchases/invoices/tasks
  (`AMOCRM_BUYER_CARD`).
- If exactly one ACTIVE Buyer Card link exists for the canonical identity,
  `reconcile_buyer_card` **reuses** it.
- Multiple ACTIVE Buyer Cards, or candidate set disagreeing with the linked card
  → `MANUAL_REVIEW_REQUIRED`.
- Revoked/closed duplicate cards are ignored (must be REVOKED, not ACTIVE).
- Dual-kind ACTIVE Buyer Card + technical deal for the same id →
  `MANUAL_REVIEW_REQUIRED` (`buyer_card_technical_deal_conflict`); never `REUSED`.

### Technical deal semantics

- `AMOCRM_TECHNICAL_DEAL` is a conversation/technical deal id, **not** a Buyer
  Card.
- Attach fail-closed + DB partial unique prevent the same ACTIVE amoCRM
  `(provider, connection_scope, external_id)` from occupying both roles.
- Reconciliation rejects treating a technical deal id as a Buyer Card candidate
  (`technical_deal_is_not_buyer_card`).
- Technical deals may be linked for audit/routing but never selected as the
  purchase card.

### Future amoCRM / n8n boundary

- CURSOR-30 ships `IdentityExternalLookupPort` (Protocol only). No fake/live
  adapter, no HTTP, no n8n workflow.
- Future adapters must: normalize ids at the boundary, call resolve/reconcile
  before any create/update, and honour `MANUAL_REVIEW_REQUIRED` fail-closed.
- amoCRM mirror jobs (ADR-004) remain domain-event sinks; they must not invent
  identity mappings.

### One-time Sellbot reconciliation strategy

- Prefer attach/reuse of durable links over merge.
- Import historical mappings as REVOKED+ACTIVE history preserving audit.
- Ambiguous Sellbot duplicates → manual review queues; never bulk-merge contacts
  or deals inside bot-TV.
- Technical deals stay linked separately from Buyer Cards so cleanup can retire
  chat deals without destroying purchase history.

### Phone / email privacy tradeoff

- Store **canonical normalized** phone (E.164) and email (trim+lowercase) as
  `external_id` so CRM/booking systems can deterministically reconcile.
- Do **not** store raw free-text variants or names for matching.
- Never put phone/email/external ids into logs, exception text, repr, or
  idempotency keys (redacted projections + fixed log codes).
- Alternative (HMAC-only storage) was rejected for CURSOR-30: cross-system
  booking/CRM match would require a shared secret and break provider-side id
  equality. Revisit if a persistent HMAC secret and dual-write strategy land.

### Phone normalization domain assumption

- Deterministic RU-oriented national rules: leading `8XXXXXXXXXX` → `+7…`;
  bare 10-digit national → `+7…`; already-canonical E.164 accepted as-is.
- Incomplete/invalid lengths fail closed (`INVALID_INPUT`). This is a
  **RU-domain assumption** for bot-TV; non-RU 10-digit nationals are out of
  scope for CURSOR-30 and must not be “guessed” into other country codes.

### Concurrency / idempotency

- Caller owns UoW; repositories/services only `flush()`.
- Attach creates use savepoints; `IntegrityError` → savepoint rollback → re-read
  → `ALREADY_LINKED` / `CONFLICT` / `MANUAL_REVIEW_REQUIRED` (never invent a
  second ACTIVE key; outer transaction remains usable).
- Partial unique ACTIVE indexes are the source of truth for ≤1 ACTIVE link per
  external key and ≤1 ACTIVE amoCRM deal role per id.
- Duplicate attach of the same key to the same canonical is idempotent.

### Non-goals

- Live amoCRM / n8n / VK / MAX / booking DB
- Automatic contact/deal merge
- Name as identity signal
- BOT_MODE / EMERGENCY_LOCK changes (`OFF` / `true` remain defaults)
- Changing C27/C28/C29 behaviour

## Consequences

- Future client ingress and CRM writers must depend on this service before
  side-effects.
- Docker default-deny must allowlist the new core/model/repo/service/migration
  paths.
- PG integration tests exercise constraints and races; local runs without
  `BOT_TV_TEST_DATABASE_URL` skip PG cases.
