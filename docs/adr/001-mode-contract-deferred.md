# ADR-001: Mode contract deferred

## Status

Accepted for BOT-CORE-FOUNDATION-01A.

## Context

`bot-TV` uses `BOT_MODE` values `OFF | HINTS | DRAFT | AUTO_READ | AUTO_WRITE`.
The booking control plane in `online-zapis-tv` uses a different enum
(`OFF | TEST | HINTS | DRAFT | AUTO`). Ecosystem backlog item
`CONTRACT-MODE-01` owns reconciliation.

## Decision

Do **not** rename `BotMode`, invent a silent mapping, or sync modes with
`online-zapis-tv` in this foundation stage. Storage and inbound persistence
must work under the existing fail-closed outbound policy regardless of mode.

## Consequences

Any future control-plane integration requires an explicit OWNER-approved
contract (`CONTRACT-MODE-01`) before modes are interpreted across systems.
