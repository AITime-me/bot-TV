# ADR-015: Booking eligibility S2S contract (CURSOR-17) + consumer boundary (CURSOR-19)

## Status

Accepted for CURSOR-17 (S2S contract) and extended by CURSOR-19 (application
consumer boundary). Source of truth for the wire contract: `online-zapis-tv`
PR A (`POST /api/internal/bot/v1/eligibility`), not ecosystem backlog docs that
still say `NOT DONE`.

## Context

`bot-TV` ships an HTTP adapter, config/DI, an eligibility→policy orchestrator,
and a booking consumer boundary without wiring that consumer into live
channels, inbound, worker, or outbound yet.

## Decision

### Wire contract (CURSOR-17)

| Item | Value |
|---|---|
| Method / path | `POST /api/internal/bot/v1/eligibility` |
| Auth | `Authorization: Bearer <token>` only (no HMAC, no query token) |
| Token | 32..512 printable ASCII (`\x21-\x7E`); bot env `BOOKING_ELIGIBILITY_BEARER_TOKEN` ↔ backend `BOT_INTERNAL_API_TOKEN` |
| Request body | `{ serviceId, masterId?, includeAlternatives }` JSON, max 4096 bytes; IDs only |
| `includeAlternatives` | Backend default `false`; client default matches and always sends the boolean |
| Success | HTTP **200** for both eligible (`SELF_BOOKING_ALLOWED`) and ineligible (`MANAGER_HANDOFF`) |
| Reason codes | `STUDIO_ONLINE_DISABLED`, `SERVICE_INACTIVE`, `MASTER_INACTIVE`, `ONLINE_DISABLED`, `MASTER_SERVICE_UNAVAILABLE`, `MANAGER_ONLY` |
| Alternatives | `otherOnlineMasters[{id, publicName}]` only when requested; `publicName` stays remote-only |
| Errors | 401 / 400 / 413 / 429 / 500 with `{ok:false,code,error}`; client maps any non-200 to fail-closed `REMOTE_REJECTED` |
| Retry / idempotency | No client retries; no `Idempotency-Key` (read/eval endpoint) |
| Timeout | Client default 5s (local policy; backend does not prescribe) |

### Application boundary (CURSOR-19)

- `BookingFlowService` is the sole prepared application boundary for self-booking
  decisions. It is composed in `create_app` and published only as
  `application.state.booking_flow`.
- Raw eligibility HTTP client and `BookingEligibilityFlowService` are built
  locally inside `create_app` and are **not** exposed on `application.state`.
- `create_app(..., booking_flow=None)` normalizes to
  `BookingFlowService(None)` (fail-closed); `state.booking_flow` is never `None`.
- Dialog policy (`decide_booking_dialog`) may be called only from
  `BookingEligibilityFlowService`. Future application callers must not import
  policy or eligibility flow in bypass of `BookingFlowService`.
- Channel / inbound / worker / outbound wiring remains a **separate next gate**.

## Consequences

- Changing reason codes, path, or auth on the backend requires a coordinated
  contract update and failing unit/contract tests here.
- Enabling live channel wiring must inject/use `BookingFlowService` (via DI),
  not call dialog policy or eligibility flow directly.
