# ADR-001: Mode contract (CONTRACT-MODE-01)

## Status

Accepted — OWNER decision 2026-08-11 (variant A: dual enum + explicit mapping).

Supersedes the foundation-stage deferral recorded for BOT-CORE-FOUNDATION-01A.

## Context

`bot-TV` uses `BOT_MODE` values `OFF | HINTS | DRAFT | AUTO_READ | AUTO_WRITE`.
The booking control plane in `online-zapis-tv` uses
`OFF | TEST | HINTS | DRAFT | AUTO`. The enums remain separate:

- control plane mode = **exposure intent**;
- Bot Core mode = **runtime capability**.

## Decision

1. Do **not** migrate/unify enums. Keep dual enums with an explicit contract in
   `app/core/mode_contract.py`.
2. Mapping:
   - `OFF` → Bot Core `OFF`
   - `TEST` → closed-test / admin / synthetic **exposure only** (not a `BotMode`)
   - `HINTS` → `HINTS`
   - `DRAFT` → `DRAFT`
   - `AUTO` → at most Bot Core `AUTO_READ` until a separate OWNER write gate;
     control-plane `AUTO` never silently becomes `AUTO_WRITE`
3. Live Booking Service S2S **reads** (eligibility / availability):
   - allowed only for `AUTO_READ` / `AUTO_WRITE` with `EMERGENCY_LOCK=false`
   - denied for `OFF` / `HINTS` / `DRAFT`
   - `EMERGENCY_LOCK=true` denies reads in every mode
4. Booking writes and public outbound stay under their existing gates and are
   not enabled by this contract.

## Consequences

- M1 is enforced with defense in depth:
  - factory construction gated by `is_live_booking_s2s_read_allowed(Settings)`;
  - HTTP eligibility/availability clients re-check the same Settings-bound
    policy immediately before each network read (no caller-controlled bool);
  - `create_app` / worker composition rebind injected live HTTP clients to
    runtime Settings so DI cannot keep a permissive construction-time policy.
- `BOT-CLOSED-TEST-01` may use control-plane `TEST` as an exposure gate without
  adding `TEST` to `BotMode`.
- Sibling ecosystem docs (`03-runtime-modes-and-gates.md`, `BACKLOG.md`,
  `05`, `08`) should be synced in a separate docs commit after code review.
