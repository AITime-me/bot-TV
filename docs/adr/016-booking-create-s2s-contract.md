# ADR-016: Booking create S2S client + application confirm boundary

## Status

Accepted for CURSOR-25. Extends ADR-015. Source of truth for the write
contract: `online-zapis-tv` `POST /api/internal/bot/v1/bookings`
(`booking-create-types.ts`, `BotBookingCreateService.ts`).

## Context

Eligibility and availability S2S reads are already wired into
`BookingFlowService` and the synthetic durable reply-plan path. Confirmed
booking creation requires a strict write client and a typed application
boundary that never claims success without a validated `bookingId`, never
mints a new idempotency key inside HTTP retries, and never stores PII in the
synthetic reply-plan.

## Decision

### Wire contract

| Item | Value |
|---|---|
| Endpoint | `POST /api/internal/bot/v1/bookings` |
| Route constant | `BOOKINGS_ROUTE_PATH` in `booking_create_remote.py` (single source) |
| Auth / base URL | Shared `BOOKING_ELIGIBILITY_*` (same Bearer token and origin) |
| Request keys | exact: `idempotencyKey`, `slotId`, `clientName`, `phone`, `personalDataConsent`, `offerAcknowledgement` |
| Consents | both must be JSON `true`; client never auto-fills missing consent |
| Success | HTTP 200, exact keys, `ok:true`, `status:"SCHEDULED"`, bool `idempotentReplay` |
| Success binding | `startsAt` must equal studio time encoded in `slotId` (`dateKey` + `HH:MM` + `+05:00`) |
| Errors | exact status/code pairs; unknown pair → adapter `REMOTE_REJECTED` |
| Transport | injected `S2sHttpTransport`; no redirects; **no HTTP retries** |
| Idempotency | **caller-owned** persistent lowercase UUID; adapter never generates UUID |

### Retry / fail-closed classification (application)

| Remote / adapter code | Machine outcome |
|---|---|
| `SLOT_NO_LONGER_AVAILABLE`, `BOOKING_CONFLICT` | `SLOT_RESELECT_REQUIRED` |
| `RATE_LIMITED`, `IDEMPOTENCY_IN_PROGRESS`, `INTERNAL_ERROR`, `TIMEOUT`, `TRANSPORT_ERROR`, `RESPONSE_TOO_LARGE` | `RETRY_LATER` (same idempotency key) |
| `CLIENT_AMBIGUOUS` | `MANAGER_HANDOFF` |
| `SERVICE_UNAVAILABLE`, `MASTER_UNAVAILABLE` | `SERVICE_UNAVAILABLE` |
| `BOOKING_REQUEST_CONFLICT` and other contract/config/unknown errors | `FAIL_CLOSED` |
| Untyped / unexpected `Exception` from create port | `FAIL_CLOSED` (`UNEXPECTED_ERROR`) |

До появления канонической backend-семантики `BOOKING_REQUEST_CONFLICT`
классифицируется fail closed. Название кода само по себе не доказывает
конфликт выбранного слота.

### `RESPONSE_TOO_LARGE`

A remote write may have completed before the response was truncated or lost.
Therefore `RESPONSE_TOO_LARGE` does **not** prove that a booking was not
created. The safe next step is a durable retry with the **same**
idempotency key. The HTTP adapter itself does not retry and does not mint a
new key. A successful replay can return the stored idempotent result. If the
violation repeats, a future self-healing / incident contour (out of CURSOR-25
scope) must stop unbounded attempts.

### Request body bound

Serializer enforces a 4096-byte request cap (`encode_booking_create_request_body`).
Under public field validators (bounded name/phone/slot/idempotency) a valid
request cannot reach that cap; the bound remains defense-in-depth and is
covered by a direct helper unit test.

### Application boundary

- Port: `BookingCreatePort` / client: `BookingCreateHttpClient`
- Consumer: `BookingFlowService.confirm_selected_slot`
- Inputs: backend `AvailableSlot`, caller `idempotencyKey`, confirmed name/phone,
  both consents `True`
- At most one create HTTP call per application invoke
- `CONFIRMED` only after validated remote success with `bookingId`
- `idempotentReplay=true` remains `CONFIRMED` (same booking), not an error
- Machine outcomes (closed): `CONFIRMED`, `SLOT_RESELECT_REQUIRED`,
  `RETRY_LATER`, `MANAGER_HANDOFF`, `SERVICE_UNAVAILABLE`, `FAIL_CLOSED`
- Internal reason codes are not rendered to clients

### PII and live flow

- Synthetic booking fixtures / reply-plan remain free of `clientName` / `phone`
- Live VK/MAX/Telegram/WhatsApp invocation is **not** enabled in this stage
- Live dialog create requires a future PII-safe confirmed-booking durable command
- Bot Core has no direct Booking DB / Prisma / public `/api/booking/*` access
- Self-healing, outbox, CRM reconciliation, n8n, DLQ are out of scope
- Deploy is out of scope for this change

### DI

`BookingS2sClients` exposes `eligibility`, `availability`, `booking_create`,
and shared `transport`. Empty config → all `None`. Partial/invalid config →
fail closed without HTTP I/O. App and worker composition roots inject create
via `build_booking_flow_from_settings`.

## Consequences

- Future durable command can call `confirm_selected_slot` without parsing
  exception text.
- Expanding live channels must keep PII outside synthetic reply-plan and keep
  caller-owned idempotency keys across durable retries.
- Backend contract changes require coordinated unit test updates.
