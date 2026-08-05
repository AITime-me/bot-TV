# ADR-015: Booking eligibility S2S contract + application/synthetic wiring

## Status

Accepted for CURSOR-17 (S2S contract), CURSOR-19 (application consumer
boundary), CURSOR-20 (synthetic inbound → two-phase durable reply-plan
booking), CURSOR-22 (typed read-only availability S2S client), and
CURSOR-23 (availability wired into synthetic durable booking flow). Source of
truth for wire contracts: `online-zapis-tv` internal bot API
(`POST /api/internal/bot/v1/eligibility`, `.../available-days`, `.../slots`).

## Context

`bot-TV` ships an HTTP adapter, eligibility→policy orchestrator,
`BookingFlowService` consumer, and a **synthetic-only** durable path that can
carry typed booking fixtures into CLIENT_REPLY reply plans. Live VK/MAX/Telegram
channels and real client sends remain unwired. CURSOR-23 wires the read-only
availability client into `BookingFlowService` and the durable synthetic
reply-plan path (still no create/hold/live channels).

## Decision

### Wire contract (CURSOR-17 + CURSOR-22)

Authenticated booking S2S boundary (one base URL, one Bearer token
`BOOKING_ELIGIBILITY_BEARER_TOKEN` ↔ backend `BOT_INTERNAL_API_TOKEN`):

| Item | Value |
|---|---|
| Eligibility | `POST /api/internal/bot/v1/eligibility` |
| Available days (read-only) | `POST /api/internal/bot/v1/available-days` |
| Slots (read-only) | `POST /api/internal/bot/v1/slots` |
| Auth | `Authorization: Bearer <token>` only (no HMAC, no query token) |
| Token | 32..512 printable ASCII (`\x21-\x7E`) |
| Eligibility request | `{ serviceId, masterId?, includeAlternatives }` JSON, max 4096 bytes; IDs only |
| Eligibility success | HTTP **200** for `SELF_BOOKING_ALLOWED` and `MANAGER_HANDOFF` |
| Availability | read-only; bot never talks to booking DB / public `/api/booking/*` |
| Canonical availability adapter | `BookingAvailabilityHttpClient` |
| Eligibility façade | `BookingEligibilityHttpClient.get_available_days/slots` reuses the same config+transport (not a second stack) |
| Application order (CURSOR-23) | eligibility → (only if allowed) availability → dialog policy / OFFER_DAYS |
| Availability master | **only** `eligibility.selected_master`; requested≠selected → fail closed; no auto-pick from `other_online_master_ids` |
| Alternate master | explicit future dialog step; CURSOR-23 never queries availability for an alternate |
| Durable attempt | one resolution attempt may perform at most one eligibility + one availability read; started marker forbids repeating either after interruption |
| OFFER_DAYS | machine-only durable/outbound fields; not renderable via client-message helpers yet |
| Out of this stage | booking create, holds, live-channel wiring, user-facing days copy |
| Fail-closed | non-200 / transport / parse → typed adapter failure / eligibility `SERVICE_UNAVAILABLE` (no client retries) |

### Application boundary (CURSOR-19)

- `BookingFlowService` is the sole prepared application boundary for self-booking.
- FastAPI publishes only `application.state.booking_flow`.
- Dialog policy may be called only from `BookingEligibilityFlowService`.

### Synthetic durable wiring (CURSOR-20/23)

```text
SyntheticIngress/Inbound (optional typed booking fixture)
  → InboundService CLIENT_REPLY payload_json.booking
  → ReplyPlanWorker.dispatch_claimed (booking path):
       Txn1: fences + CAS booking_resolution_started + commit
       off-txn: asyncio.to_thread(resolve_booking_outbound_fields)
                → availability_query:
                     BookingFlowService.resolve_available_days
                       | resolve_available_slots
                     → eligibility once
                     → availability at most once (selected master only)
                     → policy / OFFER_DAYS
                   legacy slots fixture:
                     BookingFlowService.resolve
       Txn2: re-fence + persist booking_resolution_result + idempotent outbound
  → synthetic.outbound.v1 { booking_action, booking_reason?, date_keys?/slot_ids? }
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
- Availability resolves only with the eligibility-selected master; caller/selected
  mismatch never reaches the availability adapter.
- PostgreSQL tests cover availability-query marker durability, CAS single-winner,
  and lease expiry during availability (days and slots).
- `booking_flow.resolve*` never runs inside a DB transaction or while holding
  conversation/reply-plan locks. The worker schedules
  `resolve_booking_outbound_fields` via `asyncio.to_thread`.
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
- Alternate-master self-booking remains a future explicit dialog step.
