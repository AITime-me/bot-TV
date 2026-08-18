"""Identity Resolution & Buyer Card reconciliation service (CURSOR-30).

Caller owns the unit of work (session_scope). No network I/O. No live CRM.
Matching priority: exact durable link → other confirmed links → phone → email
(secondary). Name/free text never resolve. Ambiguity → MANUAL_REVIEW_REQUIRED.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identity_resolution import (
    AMOCRM_LEAD_ROLE_ENTITY_KINDS,
    DEFAULT_CONNECTION_SCOPE,
    EMAIL_PROVIDER,
    PHONE_PROVIDER,
    REASON_DEAL_TECH_ROLE_CONFLICT,
    REASON_EMAIL_ONLY_SECONDARY,
    AttachIdentityLinkOutcome,
    AttachIdentityLinkResult,
    CanonicalIdentityGraph,
    CanonicalIdentityStatus,
    IdentityEntityKind,
    IdentityLinkConfidence,
    IdentityLinkStatus,
    IdentityResolutionError,
    IdentityResolveSignals,
    InspectIdentityOutcome,
    InspectIdentityResult,
    ReconcileBuyerCardOutcome,
    ReconcileBuyerCardResult,
    ResolveIdentityOutcome,
    ResolveIdentityResult,
    RevokeIdentityLinkOutcome,
    RevokeIdentityLinkResult,
    normalize_connection_scope,
    normalize_email,
    normalize_external_id,
    normalize_phone_e164,
    normalize_provider,
    require_canonical_identity_id,
    require_entity_kind,
    require_link_confidence,
    require_link_source,
)
from app.repositories import identity_resolution as repo

logger = logging.getLogger(__name__)

_ALLOWED_LOG_CODES: frozenset[str] = frozenset(
    {
        "IDENTITY_RESOLVED",
        "IDENTITY_NOT_FOUND",
        "IDENTITY_MANUAL_REVIEW",
        "IDENTITY_INVALID_INPUT",
        "IDENTITY_LINKED",
        "IDENTITY_ALREADY_LINKED",
        "IDENTITY_CREATED",
        "IDENTITY_LINK_CONFLICT",
        "IDENTITY_LINK_REVOKED",
        "IDENTITY_INSPECTED",
        "IDENTITY_BUYER_CARD_REUSED",
        "IDENTITY_BUYER_CARD_NOT_FOUND",
    }
)

_REASON_CHANNEL = "exact_channel_link"
_REASON_CONFIRMED = "confirmed_durable_link"
_REASON_PHONE = "normalized_phone"
_REASON_AMBIGUOUS_CANDIDATES = "ambiguous_canonical_candidates"
_REASON_CONFLICTING_LINKS = "conflicting_durable_links"
_REASON_BUYER_CARD_REUSE = "existing_buyer_card"
_REASON_BUYER_CARD_AMBIGUOUS = "ambiguous_buyer_cards"


def _log(event: str) -> None:
    if type(event) is not str or event not in _ALLOWED_LOG_CODES:
        return
    try:
        logger.info("%s", event)
    except Exception:
        return


def _db_uuid(value: object) -> uuid.UUID:
    if type(value) is uuid.UUID:
        return value
    return uuid.UUID(str(value))


def _manual_resolve(reason: str) -> ResolveIdentityResult:
    _log("IDENTITY_MANUAL_REVIEW")
    return ResolveIdentityResult(
        outcome=ResolveIdentityOutcome.MANUAL_REVIEW_REQUIRED,
        reason=reason,
    )


class IdentityResolutionService:
    """Application API for durable client identity graph + Buyer Card reconcile."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _canonical_is_active(self, canonical_identity_id: uuid.UUID) -> bool:
        row = await repo.get_canonical(
            self._session, identity_id=canonical_identity_id
        )
        if row is None:
            return False
        return row.status == CanonicalIdentityStatus.ACTIVE.value

    async def _active_canonical_from_links(
        self,
        rows: list[object],
    ) -> set[uuid.UUID]:
        """Unique ACTIVE canonical ids referenced by link rows (ARCHIVED ignored)."""

        out: set[uuid.UUID] = set()
        for row in rows:
            cid = _db_uuid(getattr(row, "canonical_identity_id"))
            if await self._canonical_is_active(cid):
                out.add(cid)
        return out

    async def resolve(
        self,
        signals: IdentityResolveSignals,
    ) -> ResolveIdentityResult:
        """Resolve canonical identity using strict priority. Never uses name."""

        try:
            return await self._resolve_validated(signals)
        except IdentityResolutionError:
            _log("IDENTITY_INVALID_INPUT")
            return ResolveIdentityResult(
                outcome=ResolveIdentityOutcome.INVALID_INPUT
            )

    async def _resolve_validated(
        self,
        signals: IdentityResolveSignals,
    ) -> ResolveIdentityResult:
        candidates: dict[uuid.UUID, str] = {}
        confidence_by_id: dict[uuid.UUID, IdentityLinkConfidence] = {}

        def _add(cid: uuid.UUID, reason: str, conf: IdentityLinkConfidence) -> None:
            prev = candidates.get(cid)
            if prev is None:
                candidates[cid] = reason
                confidence_by_id[cid] = conf
                return
            existing_conf = confidence_by_id[cid]
            if (
                conf is IdentityLinkConfidence.CONFIRMED
                and existing_conf is IdentityLinkConfidence.SECONDARY
            ):
                candidates[cid] = reason
                confidence_by_id[cid] = conf

        # (a) exact channel identity / durable external link
        if (
            signals.channel_provider is not None
            or signals.channel_connection_scope is not None
            or signals.channel_external_account_id is not None
        ):
            if (
                signals.channel_provider is None
                or signals.channel_external_account_id is None
            ):
                raise IdentityResolutionError("INVALID_INPUT")
            provider = normalize_provider(signals.channel_provider)
            scope = normalize_connection_scope(
                signals.channel_connection_scope
                if signals.channel_connection_scope is not None
                else DEFAULT_CONNECTION_SCOPE
            )
            ext = normalize_external_id(signals.channel_external_account_id)
            rows = await repo.list_active_by_key(
                self._session,
                provider=provider,
                connection_scope=scope,
                entity_kind=IdentityEntityKind.CHANNEL_ACCOUNT.value,
                external_id=ext,
            )
            active_ids = await self._active_canonical_from_links(rows)
            if len(rows) > 1 and len(active_ids) > 1:
                return _manual_resolve(_REASON_CONFLICTING_LINKS)
            if len(active_ids) > 1:
                return _manual_resolve(_REASON_CONFLICTING_LINKS)
            if len(active_ids) == 1:
                _add(
                    next(iter(active_ids)),
                    _REASON_CHANNEL,
                    IdentityLinkConfidence.CONFIRMED,
                )

        # (b) other already confirmed durable links
        for raw in signals.confirmed_links:
            if type(raw) is not tuple or len(raw) != 4:
                raise IdentityResolutionError("INVALID_INPUT")
            provider = normalize_provider(raw[0])
            scope = normalize_connection_scope(raw[1])
            kind = require_entity_kind(raw[2])
            if kind is IdentityEntityKind.CHANNEL_ACCOUNT:
                raise IdentityResolutionError("INVALID_INPUT")
            if kind is IdentityEntityKind.PHONE:
                ext = normalize_phone_e164(raw[3])
            elif kind is IdentityEntityKind.EMAIL:
                ext = normalize_email(raw[3])
            else:
                ext = normalize_external_id(raw[3])
            rows = await repo.list_active_by_key(
                self._session,
                provider=provider,
                connection_scope=scope,
                entity_kind=kind.value,
                external_id=ext,
            )
            active_ids = await self._active_canonical_from_links(rows)
            if len(active_ids) > 1:
                return _manual_resolve(_REASON_CONFLICTING_LINKS)
            if len(active_ids) == 1:
                row = next(
                    r
                    for r in rows
                    if _db_uuid(r.canonical_identity_id) in active_ids
                )
                if row.confidence != IdentityLinkConfidence.CONFIRMED.value:
                    continue
                _add(
                    next(iter(active_ids)),
                    _REASON_CONFIRMED,
                    IdentityLinkConfidence.CONFIRMED,
                )

        # (c) normalized phone (primary matching signal)
        if signals.phone is not None:
            phone_norm = normalize_phone_e164(signals.phone)
            rows = await repo.list_active_by_kind_external(
                self._session,
                entity_kind=IdentityEntityKind.PHONE.value,
                external_id=phone_norm,
            )
            phone_canonicals = await self._active_canonical_from_links(rows)
            if len(phone_canonicals) > 1:
                return _manual_resolve(_REASON_AMBIGUOUS_CANDIDATES)
            if len(phone_canonicals) == 1:
                _add(
                    next(iter(phone_canonicals)),
                    _REASON_PHONE,
                    IdentityLinkConfidence.CONFIRMED,
                )

        # (d) email only as secondary/corroborating signal — never sole RESOLVED
        if signals.email is not None:
            email_norm = normalize_email(signals.email)
            rows = await repo.list_active_by_kind_external(
                self._session,
                entity_kind=IdentityEntityKind.EMAIL.value,
                external_id=email_norm,
            )
            email_canonicals = await self._active_canonical_from_links(rows)
            if len(email_canonicals) > 1:
                return _manual_resolve(_REASON_AMBIGUOUS_CANDIDATES)
            if len(email_canonicals) == 1:
                email_cid = next(iter(email_canonicals))
                if not candidates:
                    _log("IDENTITY_NOT_FOUND")
                    return ResolveIdentityResult(
                        outcome=ResolveIdentityOutcome.NOT_FOUND,
                        reason=REASON_EMAIL_ONLY_SECONDARY,
                    )
                if email_cid not in candidates:
                    return _manual_resolve(_REASON_CONFLICTING_LINKS)
                # Same primary candidate — corroboration only; keep primary reason.

        if not candidates:
            _log("IDENTITY_NOT_FOUND")
            return ResolveIdentityResult(outcome=ResolveIdentityOutcome.NOT_FOUND)
        if len(candidates) > 1:
            return _manual_resolve(_REASON_AMBIGUOUS_CANDIDATES)

        cid = next(iter(candidates))
        if not await self._canonical_is_active(cid):
            _log("IDENTITY_NOT_FOUND")
            return ResolveIdentityResult(outcome=ResolveIdentityOutcome.NOT_FOUND)
        reason = candidates[cid]
        conf = confidence_by_id[cid]
        links = await repo.list_links_for_canonical(
            self._session,
            canonical_identity_id=cid,
            active_only=True,
        )
        _log("IDENTITY_RESOLVED")
        return ResolveIdentityResult(
            outcome=ResolveIdentityOutcome.RESOLVED,
            canonical_identity_id=cid,
            confidence=conf,
            reason=reason,
            known_external_ids=tuple(repo.as_link_record(r) for r in links),
        )

    async def attach(
        self,
        *,
        provider: object,
        entity_kind: object,
        external_id: object,
        connection_scope: object = DEFAULT_CONNECTION_SCOPE,
        canonical_identity_id: object | None = None,
        confidence: object = IdentityLinkConfidence.CONFIRMED,
        source: object = "SYSTEM",
        create_canonical: bool = False,
    ) -> AttachIdentityLinkResult:
        """Attach external link to a canonical identity (race-safe).

        If ``create_canonical`` and no ``canonical_identity_id``, creates a new
        canonical + ACTIVE link atomically inside a savepoint. Duplicate same
        link is idempotent (``ALREADY_LINKED``). Conflicting ACTIVE binding →
        ``CONFLICT`` / ``MANUAL_REVIEW_REQUIRED``.
        """

        try:
            prov = normalize_provider(provider)
            scope = normalize_connection_scope(connection_scope)
            kind = require_entity_kind(entity_kind)
            conf = require_link_confidence(confidence)
            src = require_link_source(source)
            if kind is IdentityEntityKind.PHONE:
                if prov != PHONE_PROVIDER:
                    raise IdentityResolutionError("INVALID_INPUT")
                ext = normalize_phone_e164(external_id)
            elif kind is IdentityEntityKind.EMAIL:
                if prov != EMAIL_PROVIDER:
                    raise IdentityResolutionError("INVALID_INPUT")
                ext = normalize_email(external_id)
                if conf is IdentityLinkConfidence.CONFIRMED:
                    # Email is secondary by policy unless explicitly SECONDARY.
                    conf = IdentityLinkConfidence.SECONDARY
            else:
                ext = normalize_external_id(external_id)
            target: uuid.UUID | None
            if canonical_identity_id is None:
                target = None
            else:
                target = uuid.UUID(
                    require_canonical_identity_id(canonical_identity_id)
                )
        except (IdentityResolutionError, ValueError, TypeError):
            _log("IDENTITY_INVALID_INPUT")
            return AttachIdentityLinkResult(
                outcome=AttachIdentityLinkOutcome.INVALID_INPUT
            )

        if target is None and not create_canonical:
            _log("IDENTITY_INVALID_INPUT")
            return AttachIdentityLinkResult(
                outcome=AttachIdentityLinkOutcome.INVALID_INPUT
            )

        # Fail-closed: one ACTIVE amoCRM Lead id cannot be both a business Deal
        # and a technical/chat Lead under the same provider/scope.
        if kind in AMOCRM_LEAD_ROLE_ENTITY_KINDS:
            roles = await repo.lock_active_amocrm_deal_roles(
                self._session,
                provider=prov,
                connection_scope=scope,
                external_id=ext,
            )
            for role in roles:
                if role.entity_kind != kind.value:
                    _log("IDENTITY_LINK_CONFLICT")
                    return AttachIdentityLinkResult(
                        outcome=AttachIdentityLinkOutcome.CONFLICT,
                        reason=REASON_DEAL_TECH_ROLE_CONFLICT,
                    )

        existing = await repo.lock_active_by_key(
            self._session,
            provider=prov,
            connection_scope=scope,
            entity_kind=kind.value,
            external_id=ext,
        )
        if existing is not None:
            count = await repo.count_active_by_key(
                self._session,
                provider=prov,
                connection_scope=scope,
                entity_kind=kind.value,
                external_id=ext,
            )
            if count > 1:
                _log("IDENTITY_MANUAL_REVIEW")
                return AttachIdentityLinkResult(
                    outcome=AttachIdentityLinkOutcome.MANUAL_REVIEW_REQUIRED,
                    reason=_REASON_CONFLICTING_LINKS,
                )
            existing_cid = _db_uuid(existing.canonical_identity_id)
            if not await self._canonical_is_active(existing_cid):
                _log("IDENTITY_LINK_CONFLICT")
                return AttachIdentityLinkResult(
                    outcome=AttachIdentityLinkOutcome.CONFLICT
                )
            if target is not None and existing_cid != target:
                _log("IDENTITY_LINK_CONFLICT")
                return AttachIdentityLinkResult(
                    outcome=AttachIdentityLinkOutcome.CONFLICT
                )
            _log("IDENTITY_ALREADY_LINKED")
            return AttachIdentityLinkResult(
                outcome=AttachIdentityLinkOutcome.ALREADY_LINKED,
                canonical_identity_id=existing_cid,
                link=repo.as_link_record(existing),
            )

        created_new = False
        if target is None:
            created_new = True
            target = uuid.uuid4()

        try:
            async with self._session.begin_nested():
                if created_new:
                    await repo.insert_canonical(
                        self._session, identity_id=target
                    )
                else:
                    locked = await repo.lock_canonical(
                        self._session, identity_id=target
                    )
                    if locked is None:
                        raise IdentityResolutionError("NOT_FOUND")
                    if locked.status != CanonicalIdentityStatus.ACTIVE.value:
                        raise IdentityResolutionError("CONFLICT")
                row = await repo.insert_active_link(
                    self._session,
                    row_id=uuid.uuid4(),
                    canonical_identity_id=target,
                    provider=prov,
                    connection_scope=scope,
                    entity_kind=kind.value,
                    external_id=ext,
                    confidence=conf.value,
                    source=src,
                )
        except IdentityResolutionError as exc:
            if exc.code == "CONFLICT":
                _log("IDENTITY_LINK_CONFLICT")
                return AttachIdentityLinkResult(
                    outcome=AttachIdentityLinkOutcome.CONFLICT
                )
            _log("IDENTITY_INVALID_INPUT")
            return AttachIdentityLinkResult(
                outcome=AttachIdentityLinkOutcome.INVALID_INPUT
            )
        except IntegrityError:
            self._session.expire_all()
            return await self._classify_attach_integrity_race(
                provider=prov,
                connection_scope=scope,
                entity_kind=kind,
                external_id=ext,
                target=target,
                created_new=created_new,
            )

        if created_new:
            _log("IDENTITY_CREATED")
            return AttachIdentityLinkResult(
                outcome=AttachIdentityLinkOutcome.CREATED,
                canonical_identity_id=target,
                link=repo.as_link_record(row),
            )
        _log("IDENTITY_LINKED")
        return AttachIdentityLinkResult(
            outcome=AttachIdentityLinkOutcome.LINKED,
            canonical_identity_id=target,
            link=repo.as_link_record(row),
        )

    async def _classify_attach_integrity_race(
        self,
        *,
        provider: str,
        connection_scope: str,
        entity_kind: IdentityEntityKind,
        external_id: str,
        target: uuid.UUID | None,
        created_new: bool,
    ) -> AttachIdentityLinkResult:
        """After savepoint rollback: re-read and return typed outcome (UoW intact)."""

        if entity_kind in AMOCRM_LEAD_ROLE_ENTITY_KINDS:
            roles = await repo.lock_active_amocrm_deal_roles(
                self._session,
                provider=provider,
                connection_scope=connection_scope,
                external_id=external_id,
            )
            for role in roles:
                if role.entity_kind != entity_kind.value:
                    _log("IDENTITY_LINK_CONFLICT")
                    return AttachIdentityLinkResult(
                        outcome=AttachIdentityLinkOutcome.CONFLICT,
                        reason=REASON_DEAL_TECH_ROLE_CONFLICT,
                    )

        raced = await repo.lock_active_by_key(
            self._session,
            provider=provider,
            connection_scope=connection_scope,
            entity_kind=entity_kind.value,
            external_id=external_id,
        )
        if raced is None:
            _log("IDENTITY_MANUAL_REVIEW")
            return AttachIdentityLinkResult(
                outcome=AttachIdentityLinkOutcome.MANUAL_REVIEW_REQUIRED,
                reason=_REASON_CONFLICTING_LINKS,
            )
        raced_cid = _db_uuid(raced.canonical_identity_id)
        if target is not None and not created_new and raced_cid != target:
            _log("IDENTITY_LINK_CONFLICT")
            return AttachIdentityLinkResult(
                outcome=AttachIdentityLinkOutcome.CONFLICT
            )
        _log("IDENTITY_ALREADY_LINKED")
        return AttachIdentityLinkResult(
            outcome=AttachIdentityLinkOutcome.ALREADY_LINKED,
            canonical_identity_id=raced_cid,
            link=repo.as_link_record(raced),
        )

    async def revoke(
        self,
        *,
        provider: object,
        entity_kind: object,
        external_id: object,
        connection_scope: object = DEFAULT_CONNECTION_SCOPE,
    ) -> RevokeIdentityLinkResult:
        """Revoke ACTIVE link for the external key if present."""

        try:
            prov = normalize_provider(provider)
            scope = normalize_connection_scope(connection_scope)
            kind = require_entity_kind(entity_kind)
            if kind is IdentityEntityKind.PHONE:
                ext = normalize_phone_e164(external_id)
            elif kind is IdentityEntityKind.EMAIL:
                ext = normalize_email(external_id)
            else:
                ext = normalize_external_id(external_id)
        except IdentityResolutionError:
            _log("IDENTITY_INVALID_INPUT")
            return RevokeIdentityLinkResult(
                outcome=RevokeIdentityLinkOutcome.INVALID_INPUT
            )

        existing = await repo.lock_active_by_key(
            self._session,
            provider=prov,
            connection_scope=scope,
            entity_kind=kind.value,
            external_id=ext,
        )
        if existing is None:
            _log("IDENTITY_NOT_FOUND")
            return RevokeIdentityLinkResult(
                outcome=RevokeIdentityLinkOutcome.NOT_FOUND
            )
        count = await repo.count_active_by_key(
            self._session,
            provider=prov,
            connection_scope=scope,
            entity_kind=kind.value,
            external_id=ext,
        )
        if count > 1:
            _log("IDENTITY_MANUAL_REVIEW")
            return RevokeIdentityLinkResult(
                outcome=RevokeIdentityLinkOutcome.MANUAL_REVIEW_REQUIRED,
                reason=_REASON_CONFLICTING_LINKS,
            )
        revoked = await repo.mark_link_revoked(
            self._session, link_id=_db_uuid(existing.id)
        )
        if revoked is None:
            _log("IDENTITY_NOT_FOUND")
            return RevokeIdentityLinkResult(
                outcome=RevokeIdentityLinkOutcome.NOT_FOUND
            )
        _log("IDENTITY_LINK_REVOKED")
        return RevokeIdentityLinkResult(
            outcome=RevokeIdentityLinkOutcome.REVOKED,
            link=repo.as_link_record(revoked),
        )

    async def inspect(
        self,
        *,
        canonical_identity_id: object,
    ) -> InspectIdentityResult:
        """Return the durable identity graph (including REVOKED history)."""

        try:
            cid = uuid.UUID(require_canonical_identity_id(canonical_identity_id))
        except (IdentityResolutionError, ValueError, TypeError):
            _log("IDENTITY_INVALID_INPUT")
            return InspectIdentityResult(
                outcome=InspectIdentityOutcome.INVALID_INPUT
            )

        identity = await repo.get_canonical(self._session, identity_id=cid)
        if identity is None:
            _log("IDENTITY_NOT_FOUND")
            return InspectIdentityResult(outcome=InspectIdentityOutcome.NOT_FOUND)
        links = await repo.list_links_for_canonical(
            self._session,
            canonical_identity_id=cid,
            active_only=False,
        )
        _log("IDENTITY_INSPECTED")
        return InspectIdentityResult(
            outcome=InspectIdentityOutcome.FOUND,
            graph=CanonicalIdentityGraph(
                identity=repo.as_identity_record(identity),
                links=tuple(repo.as_link_record(r) for r in links),
            ),
        )

    async def reconcile_buyer_card(
        self,
        *,
        canonical_identity_id: object,
        candidate_buyer_card_ids: tuple[object, ...] = (),
        candidate_technical_deal_ids: tuple[object, ...] = (),
    ) -> ReconcileBuyerCardResult:
        """Reuse the linked Buyer Card (Customer) or fail closed on ambiguity.

        Buyer Card ids are amoCRM Customer ids, a different namespace from
        Lead. Technical/business Lead ids are never treated as Customers.
        Numeric overlap with a Lead id is allowed. No automatic amoCRM merge.
        """

        try:
            cid = uuid.UUID(require_canonical_identity_id(canonical_identity_id))
            buyer_candidates = tuple(
                normalize_external_id(item) for item in candidate_buyer_card_ids
            )
            tech_candidates = tuple(
                normalize_external_id(item)
                for item in candidate_technical_deal_ids
            )
        except (IdentityResolutionError, ValueError, TypeError):
            _log("IDENTITY_INVALID_INPUT")
            return ReconcileBuyerCardResult(
                outcome=ReconcileBuyerCardOutcome.INVALID_INPUT
            )

        # Customer ids and Lead ids occupy different amoCRM namespaces.
        # Technical Lead ids are never treated as Buyer Card (Customer) ids,
        # but numeric overlap is allowed and is not a role conflict.
        _ = tech_candidates

        identity = await repo.lock_canonical(self._session, identity_id=cid)
        if identity is None:
            _log("IDENTITY_NOT_FOUND")
            return ReconcileBuyerCardResult(
                outcome=ReconcileBuyerCardOutcome.NOT_FOUND,
                canonical_identity_id=cid,
                reason="canonical_not_found",
            )
        if identity.status != CanonicalIdentityStatus.ACTIVE.value:
            _log("IDENTITY_NOT_FOUND")
            return ReconcileBuyerCardResult(
                outcome=ReconcileBuyerCardOutcome.NOT_FOUND,
                canonical_identity_id=cid,
                reason="canonical_not_active",
            )

        active_links = await repo.list_links_for_canonical(
            self._session,
            canonical_identity_id=cid,
            active_only=True,
        )
        known = tuple(repo.as_link_record(r) for r in active_links)

        buyer_rows = [
            r
            for r in active_links
            if r.entity_kind == IdentityEntityKind.AMOCRM_BUYER_CARD.value
        ]

        if len(buyer_rows) > 1:
            _log("IDENTITY_MANUAL_REVIEW")
            return ReconcileBuyerCardResult(
                outcome=ReconcileBuyerCardOutcome.MANUAL_REVIEW_REQUIRED,
                reason=_REASON_BUYER_CARD_AMBIGUOUS,
                known_external_ids=known,
            )
        if len(buyer_rows) == 1:
            card_id = buyer_rows[0].external_id
            if buyer_candidates and card_id not in buyer_candidates:
                _log("IDENTITY_MANUAL_REVIEW")
                return ReconcileBuyerCardResult(
                    outcome=ReconcileBuyerCardOutcome.MANUAL_REVIEW_REQUIRED,
                    reason=_REASON_BUYER_CARD_AMBIGUOUS,
                    known_external_ids=known,
                )
            _log("IDENTITY_BUYER_CARD_REUSED")
            return ReconcileBuyerCardResult(
                outcome=ReconcileBuyerCardOutcome.REUSED,
                canonical_identity_id=cid,
                buyer_card_external_id=card_id,
                confidence=IdentityLinkConfidence(
                    buyer_rows[0].confidence
                ),
                reason=_REASON_BUYER_CARD_REUSE,
                known_external_ids=known,
            )

        # No linked Buyer Card yet — candidate-only reconciliation without auto-create.
        if len(buyer_candidates) > 1:
            _log("IDENTITY_MANUAL_REVIEW")
            return ReconcileBuyerCardResult(
                outcome=ReconcileBuyerCardOutcome.MANUAL_REVIEW_REQUIRED,
                reason=_REASON_BUYER_CARD_AMBIGUOUS,
                known_external_ids=known,
            )
        if len(buyer_candidates) == 1:
            # Fail closed: linking requires explicit attach; reconcile only reuses.
            _log("IDENTITY_BUYER_CARD_NOT_FOUND")
            return ReconcileBuyerCardResult(
                outcome=ReconcileBuyerCardOutcome.NOT_FOUND,
                canonical_identity_id=cid,
                reason="buyer_card_not_linked",
                known_external_ids=known,
            )

        _log("IDENTITY_BUYER_CARD_NOT_FOUND")
        return ReconcileBuyerCardResult(
            outcome=ReconcileBuyerCardOutcome.NOT_FOUND,
            canonical_identity_id=cid,
            reason="buyer_card_not_linked",
            known_external_ids=known,
        )
