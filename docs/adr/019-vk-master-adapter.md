# ADR-019: VK Master Adapter (CURSOR-29)

## Status

Accepted for CURSOR-29. Thin VK Callback boundary only. Default-off.
No live enablement, deploy, client Teya dialogue, MAX, or amoCRM.

## Context

CURSOR-27/28 already own binding, parsing, confirmation, idempotency, PII,
and C26 S2S. Conversation/ingress remain synthetic-only (ADR-017/002).
This stage adds a dedicated VK master private-dialog adapter that feeds
`MasterCommandFlowService` without expanding client ingress constraints.

## Decision

### Boundary

```text
VK Callback API
  → parse/auth/eligibility (thin)
  → C27 resolve precheck (ACTIVE only; else silent)
  → MasterCommandEnvelope → MasterCommandFlowService (C28)
  → commit UoW
  → optional messages.send (after commit only)
```

VK-specific code must not duplicate C27/C28/C26 logic.

### Identity contract

| Field | Source |
|---|---|
| `channel` | always `vk` |
| `external_account_id` | `str(from_id)` from trusted normalized message |
| `connection_scope` | `vk-group-{group_id}` from **configured** group id after exact payload `group_id` match |
| `external_message_id` | `str(conversation_message_id)` only — stable across Callback retries; missing/invalid → fail closed (no synthesize) |
| `peer_id` | trusted normalized peer for reply target only |

Accept only direct private master dialogue: `message_new`, `out=0`,
`from_id == peer_id`, both positive user ids, non-empty text, no service
`action`. Ignore/reject chat/group/service/outbound/non-text.

### Unbound = silent

Adapter-side C27 resolve: `NOT_FOUND` / invalid / ambiguous / non-master
traffic → no C28 call, no VK reply, no C26. C28 still resolves binding as
defence in depth. Never reply «кабинет не привязан» on this path.

### Safety gates

Business execution and VK send require all of:

1. `VK_MASTER_ADAPTER_ENABLED=true`
2. complete VK runtime config (group, secret, confirmation, access token)
3. `BOT_MODE != OFF`
4. `EMERGENCY_LOCK=false`

Defaults remain `BOT_MODE=OFF`, `EMERGENCY_LOCK=true`, adapter disabled.
Do not weaken `is_automatic_outbound_allowed` / global defaults.

Callback `confirmation` handshake is separate from business execution but
still requires complete trusted callback config (group + secret +
confirmation string).

### Transaction / send ordering

1. Open DB UoW, run C28, commit.
2. Only then call `messages.send` to trusted `peer_id`.
3. Transport failure must not roll back persisted C28 state.
4. No network I/O inside the command transaction.
5. Duplicate inbound (`external_message_id`) → no second mutation and no
   reply storm (`DUPLICATE_IGNORED` → silent).
6. Reply ownership: only the delivery that wins the durable inbound insert
   or claim/execution path may emit a VK reply. Concurrent losers/replays
   are silent (no in-memory lock; no extra business state).

### Ephemeral PII wiring (startup degrade)

Production VK wiring uses the canonical
`build_ephemeral_pii_store_from_env` factory (same `EphemeralPiiStore` /
`EnvEphemeralPiiKeyProvider`; no second implementation).

| Env state | Behavior |
|---|---|
| Fully unset | `pii_store=None` — CREATE_BOOKING unavailable; other commands OK |
| Fully valid | inject real `EphemeralPiiStore` |
| Partial / invalid | factory still fails closed with `EphemeralPiiError`; **VK wiring boundary catches it**, logs only the constant code `VK_MASTER_PII_UNAVAILABLE`, and degrades to `pii_store=None` |

Invalid/partial PII must **not** abort `create_app()`: `/health`, Callback
confirmation, and non-PII master commands remain available. CREATE_BOOKING
stays fail-closed until operators fix `EPHEMERAL_PII_*`. Never log exception
contents, key ids, key material, or env values.

### Non-goals

Live webhook enablement/deploy, client VK bot, MAX, amoCRM, n8n,
online-zapis-tv DB access, expanding `ingress`/`conversations` to `vk`,
VK SDK dependencies.

## Consequences

Docker default-deny must allowlist new `app/channels/vk_master_*` and
`app/services/vk_master_adapter.py`. Route registration stays fail-closed
when callback config is incomplete.
