# ADR-015: Booking eligibility S2S contract + application/synthetic wiring

## Status

Accepted for CURSOR-17 (S2S contract), CURSOR-19 (application consumer
boundary), and CURSOR-20 (synthetic inbound → two-phase durable reply-plan
booking). Source of truth for the wire contract: `online-zapis-tv` PR A
(`POST /api/internal/bot/v1/eligibility`).

## Context

`bot-TV` ships an HTTP adapter, eligibility→policy orchestrator,
`BookingFlowService` consumer, and a **synthetic-only** durable path that can
carry typed booking fixtures into CLIENT_REPLY reply plans. Live VK/MAX/Telegram
channels and real client sends remain unwired.

## Decision

### Wire contract (CURSOR-17)

| Item | Value |
|---|---|
| Method / path | `POST /api/internal/bot/v1/eligibility` |
| Auth | `Authorization: Bearer <token>` only (no HMAC, no query token) |
| Token | 32..512 printable ASCII (`\x21-\x7E`); bot env `BOOKING_ELIGIBILITY_BEARER_TOKEN` ↔ backend `BOT_INTERNAL_API_TOKEN` |
| Request body | `{ serviceId, masterId?, includeAlternatives }` JSON, max 4096 bytes; IDs only |
| Success | HTTP **200** for `SELF_BOOKING_ALLOWED` and `MANAGER_HANDOFF` |
| Fail-closed | non-200 / transport / parse → `SERVICE_UNAVAILABLE` (no client retries) |

### Application boundary (CURSOR-19)

- `BookingFlowService` is the sole prepared application boundary for self-booking.
- FastAPI publishes only `application.state.booking_flow`.
- Dialog policy may be called only from `BookingEligibilityFlowService`.

### Synthetic durable wiring (CURSOR-20)

```text
SyntheticIngress/Inbound (optional typed booking fixture)
  → InboundService CLIENT_REPLY payload_json.booking
  → ReplyPlanWorker.dispatch_claimed (booking path):
       Txn1: fences + CAS booking_resolution_started + commit
       off-txn: asyncio.to_thread(resolve_booking_outbound_fields)
                → booking_flow.resolve  # at-most-once remote attempt
       Txn2: re-fence + persist booking_resolution_result + idempotent outbound
  → synthetic.outbound.v1 { booking_action, booking_reason?, slot_ids? }
```

Durable state in existing `ReplyPlan.payload_json` (no migration):

| State | Keys | Worker action |
|---|---|---|
| Needs remote | `booking`, no started/result | CAS `booking_resolution_started=true`, then one remote |
| Interrupted | `booking_resolution_started`, no result | **No remote**; fail-closed `SERVICE_UNAVAILABLE` / `BOOKING_RESOLUTION_INTERRUPTED` |
| Has result | `booking_resolution_result` | Reuse result; never resolve again |
| Existing outbound | outbound idempotency hit | Complete plan; never resolve |

Guarantees:

- **At-most-once remote attempt** per reply-plan. Crash after the started marker
  (before or after HTTP) but before result persistence closes fail-closed on
  retry — it does **not** repeat eligibility/HTTP.
- `booking_flow.resolve` never runs inside a DB transaction or while holding
  conversation/reply-plan locks. The worker schedules
  `resolve_booking_outbound_fields` via `asyncio.to_thread`; that helper calls
  `booking_flow.resolve` on the worker thread.
- Non-booking CLIENT_REPLY plans keep the prior single-transaction
  synthetic_token-only outbound path.
- Booking intent/service/master are **never** parsed from free-form text.
- Worker composition injects `BookingFlowService` without reading `app.state`.

## Consequences

- Backend contract changes require coordinated unit/contract test updates.
- Live channel wiring must call `BookingFlowService` (DI), not policy/eligibility
  directly.
- Expanding outbound booking fields must keep the allowlisted reason set and
  must not echo client text or secrets.
- Interrupted resolutions trade a second remote chance for strict at-most-once
  semantics; operators must treat `BOOKING_RESOLUTION_INTERRUPTED` as
  manager-bound fail-closed.
