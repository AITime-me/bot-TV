"""Provider-neutral Identity / CRM lookup port (CURSOR-30 / IR-2).

Contract for CRM/provider lookups. Live amoCRM contact adapter lives in
``app.services.amocrm_identity_lookup`` (IR-2); richer fail-closed outcomes
are the primary API — Protocol ``lookup_by_external_id`` remains lossy.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.core.identity_resolution import IdentityEntityKind

__all__ = ("ExternalEntityRef", "IdentityExternalLookupPort")


class ExternalEntityRef:
    """Opaque external entity reference. Values never appear in logs/repr."""

    __slots__ = ("provider", "connection_scope", "entity_kind", "external_id")

    def __init__(
        self,
        *,
        provider: str,
        connection_scope: str,
        entity_kind: IdentityEntityKind,
        external_id: str,
    ) -> None:
        self.provider = provider
        self.connection_scope = connection_scope
        self.entity_kind = entity_kind
        self.external_id = external_id

    def __repr__(self) -> str:
        return (
            "ExternalEntityRef("
            f"provider={self.provider!r}, "
            "connection_scope=<redacted>, "
            f"entity_kind={self.entity_kind.value!r}, "
            "external_id=<redacted>)"
        )


@runtime_checkable
class IdentityExternalLookupPort(Protocol):
    """Boundary for CRM/provider lookups used by reconciliation.

    Implementations must not embed PII in exceptions. Prefer typed
    AmoCrmIdentityLookupResult for fail-closed phone/id discovery; this
    method stays ``ref | None`` for Protocol compatibility.
    """

    async def lookup_by_external_id(
        self,
        *,
        provider: str,
        connection_scope: str,
        entity_kind: IdentityEntityKind,
        external_id: str,
    ) -> ExternalEntityRef | None:
        """Return a normalized ref if the provider knows the entity, else None."""
        ...
