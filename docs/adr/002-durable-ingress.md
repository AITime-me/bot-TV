# ADR-002: Durable ingress contract (BOT-CORE-INGRESS-01B)

## Status

Accepted for BOT-CORE-INGRESS-01B.

## Context

Bot Core must acknowledge a provider event only after the event is durably
stored. Foundation 01A already persists conversation / inbox / outbox rows;
ingress needs a separate receipt log with lease-based workers, retries, and a
terminal DEAD state.

## Decision

1. Introduce `ingress_events` as the durable receipt table.
2. Unique identity is `(channel, external_event_id)` enforced by PostgreSQL.
3. ACK the synthetic source only after a successful commit of the RECEIVED row.
4. Workers claim rows with `FOR UPDATE SKIP LOCKED`, a time-bounded lease, and a
   fencing pair `(lease_token, lease_version)`.
   `max_attempts` is persisted on the ingress row and is the only attempt limit
   consulted by claim, fail, and exhausted-lease recovery.
5. Statuses are finite: `RECEIVED → PROCESSING → {PROCESSED | FAILED | DEAD}`;
   `FAILED → PROCESSING` for retry; terminals are `PROCESSED` and `DEAD`.
6. Keep only a schema-validated synthetic envelope in `envelope_json`. Never
   store tokens, signatures, or arbitrary raw provider payloads. Repr/log views
   redact envelope contents.
7. Reuse `InboundService` for business persistence after a successful claim.
8. Real channel adapters, public webhooks, AI, ReplyPlan, and Outbound Arbiter
   remain out of scope.
9. Before a normal claim, an expired `PROCESSING` row with
   `attempt_count >= max_attempts` is atomically moved to `DEAD` and its lease is
   cleared. Recovery never invokes `InboundService`; the stale token/version can
   no longer complete or fail the row.

## Consequences

Crash after commit and before processing leaves a RECEIVED (or expired
PROCESSING) row that another worker can safely reclaim. Duplicate provider
deliveries return the existing ACK without creating a second business
operation. If the crash happened during the final allowed attempt, the next
claim cycle terminalizes the expired lease as `DEAD` instead of reclaiming it.
