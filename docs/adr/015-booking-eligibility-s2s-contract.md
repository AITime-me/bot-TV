# ADR-015: Booking eligibility S2S contract (CURSOR-17)

## Status

Accepted for CURSOR-17. Source of truth: `online-zapis-tv` PR A implementation
(`POST /api/internal/bot/v1/eligibility`), not ecosystem backlog docs that still
say `NOT DONE`.

## Context

`bot-TV` ships an HTTP adapter and config/DI for booking eligibility without
wiring it into dialog/worker/outbound yet. The adapter must match the real
backend contract before any call path is enabled.

## Decision

Lock the client to this contract:

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

## Consequences

- Changing reason codes, path, or auth on the backend requires a coordinated
  contract update and failing unit/contract tests here.
- Enabling dialog/worker wiring is a separate gate after this contract lock.
