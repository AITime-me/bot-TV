"""Pre-durability atomic PII admission for self-booking (SELF-BOOKING-COMMAND-03H).

external PII → validate → content MAC → atomic pair store + map → opaque refs.
Does not touch ingress, Inbox, outbox, confirm actions, or booking CREATE.
"""

from __future__ import annotations

import uuid
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.booking_create_remote import require_confirmed_client_name
from app.core.identity_resolution import (
    IdentityResolutionError,
    normalize_phone_e164,
)
from app.core.pii_admission_mac import (
    compute_booking_pii_admission_content_mac,
    verify_booking_pii_admission_content_mac,
)
from app.core.pii_admission_mac_keys import PiiAdmissionMacKeyProvider
from app.core.pii_admission_mac_types import PiiAdmissionMacError
from app.core.self_booking_pii_admission_types import (
    PiiAdmissionError,
    PiiAdmissionResult,
    require_pii_admission_request_id,
)
from app.db.session import session_scope
from app.models.self_booking_pii_admission import SelfBookingPiiAdmission
from app.repositories import self_booking_pii_admissions as admission_repo
from app.services.ephemeral_pii_store import EphemeralPiiStore


class _ConcurrentAdmission(Exception):
    """Internal: map insert lost the race; roll back pair and replay."""


class SelfBookingPiiAdmissionService:
    """Atomic PHONE+CLIENT_NAME admission under BOOKING_PHONE_WRITE."""

    __slots__ = ("_mac_keys", "_pii", "_session_factory")

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        pii_store: EphemeralPiiStore,
        mac_key_provider: PiiAdmissionMacKeyProvider,
    ) -> None:
        if session_factory is None or pii_store is None or mac_key_provider is None:
            raise PiiAdmissionError("PII_ADMISSION_CONFIG_INVALID") from None
        self._session_factory = session_factory
        self._pii = pii_store
        self._mac_keys = mac_key_provider

    async def admit(
        self,
        *,
        conversation_id: UUID,
        request_id: str,
        phone: str,
        client_name: str,
    ) -> PiiAdmissionResult:
        """Validate → MAC → atomic pair+map, or idempotent replay / fail-closed."""

        conv = _require_conversation_id(conversation_id)
        req = _require_request_id(request_id)
        phone_c, name_c = _canonicalize_pii(phone=phone, client_name=client_name)
        try:
            content_mac = compute_booking_pii_admission_content_mac(
                canonical_phone=phone_c,
                canonical_name=name_c,
                key_provider=self._mac_keys,
            )
        except PiiAdmissionMacError as exc:
            if exc.code == "PII_ADMISSION_MAC_KEY_UNAVAILABLE":
                raise PiiAdmissionError("PII_ADMISSION_CONFIG_INVALID") from None
            if exc.code == "PII_ADMISSION_MAC_CONFIG_INVALID":
                raise PiiAdmissionError("PII_ADMISSION_CONFIG_INVALID") from None
            raise PiiAdmissionError("PII_ADMISSION_INPUT_INVALID") from None

        try:
            return await self._admit_once(
                conversation_id=conv,
                request_id=req,
                phone=phone_c,
                client_name=name_c,
                content_mac_digest=content_mac.digest,
                mac_key_id=content_mac.key_id,
            )
        except _ConcurrentAdmission:
            return await self._admit_replay_only(
                conversation_id=conv,
                request_id=req,
                phone=phone_c,
                client_name=name_c,
            )

    async def _admit_once(
        self,
        *,
        conversation_id: UUID,
        request_id: str,
        phone: str,
        client_name: str,
        content_mac_digest: bytes,
        mac_key_id: str,
    ) -> PiiAdmissionResult:
        try:
            async with session_scope(self._session_factory) as session:
                existing = await admission_repo.get_by_request(
                    session,
                    conversation_id=conversation_id,
                    request_id=request_id,
                )
                if existing is not None:
                    return await self._replay_existing(
                        session,
                        existing,
                        phone=phone,
                        client_name=client_name,
                    )

                phone_handle, name_handle = (
                    await self._pii.store_booking_phone_write_pair(
                        session,
                        phone,
                        client_name,
                        conversation_id=conversation_id,
                    )
                )
                inserted = await admission_repo.insert_if_absent(
                    session,
                    row_id=uuid.uuid4(),
                    conversation_id=conversation_id,
                    request_id=request_id,
                    phone_ref_token=phone_handle.reference.to_token(),
                    name_ref_token=name_handle.reference.to_token(),
                    content_mac=content_mac_digest,
                    mac_key_id=mac_key_id,
                )
                if inserted is None:
                    # Another commit won; roll back our ciphertext pair.
                    raise _ConcurrentAdmission()
                return PiiAdmissionResult(
                    conversation_id=conversation_id,
                    request_id=request_id,
                    phone_ref_token=inserted.phone_ref_token,
                    name_ref_token=inserted.name_ref_token,
                    reused=False,
                )
        except _ConcurrentAdmission:
            raise
        except PiiAdmissionError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise PiiAdmissionError("PII_ADMISSION_STORE_FAILED") from None

    async def _admit_replay_only(
        self,
        *,
        conversation_id: UUID,
        request_id: str,
        phone: str,
        client_name: str,
    ) -> PiiAdmissionResult:
        try:
            async with session_scope(self._session_factory) as session:
                existing = await admission_repo.get_by_request(
                    session,
                    conversation_id=conversation_id,
                    request_id=request_id,
                )
                if existing is None:
                    raise PiiAdmissionError("PII_ADMISSION_STORE_FAILED") from None
                return await self._replay_existing(
                    session,
                    existing,
                    phone=phone,
                    client_name=client_name,
                )
        except PiiAdmissionError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise PiiAdmissionError("PII_ADMISSION_STORE_FAILED") from None

    async def _replay_existing(
        self,
        session: AsyncSession,
        existing: SelfBookingPiiAdmission,
        *,
        phone: str,
        client_name: str,
    ) -> PiiAdmissionResult:
        try:
            matched = verify_booking_pii_admission_content_mac(
                canonical_phone=phone,
                canonical_name=client_name,
                stored_digest=existing.content_mac,
                mac_key_id=existing.mac_key_id,
                key_provider=self._mac_keys,
            )
        except PiiAdmissionMacError:
            raise PiiAdmissionError("PII_ADMISSION_CONFLICT") from None
        if not matched:
            raise PiiAdmissionError("PII_ADMISSION_CONFLICT") from None

        alive = await self._pii.booking_phone_write_pair_alive(
            session,
            phone_ref_token=existing.phone_ref_token,
            name_ref_token=existing.name_ref_token,
            conversation_id=existing.conversation_id
            if type(existing.conversation_id) is UUID
            else UUID(str(existing.conversation_id)),
        )
        if not alive:
            # Same request_id must not mint a replacement pair.
            raise PiiAdmissionError("PII_ADMISSION_EXPIRED") from None

        return PiiAdmissionResult(
            conversation_id=existing.conversation_id
            if type(existing.conversation_id) is UUID
            else UUID(str(existing.conversation_id)),
            request_id=existing.request_id,
            phone_ref_token=existing.phone_ref_token,
            name_ref_token=existing.name_ref_token,
            reused=True,
        )


def _require_conversation_id(value: object) -> UUID:
    if type(value) is not UUID:
        raise PiiAdmissionError("PII_ADMISSION_INPUT_INVALID") from None
    return value


def _require_request_id(value: object) -> str:
    try:
        return require_pii_admission_request_id(value)
    except ValueError:
        raise PiiAdmissionError("PII_ADMISSION_INPUT_INVALID") from None


def _canonicalize_pii(*, phone: object, client_name: object) -> tuple[str, str]:
    try:
        phone_c = normalize_phone_e164(phone)
    except IdentityResolutionError:
        raise PiiAdmissionError("PII_ADMISSION_INPUT_INVALID") from None
    try:
        name_c = require_confirmed_client_name(client_name)
    except ValueError:
        raise PiiAdmissionError("PII_ADMISSION_INPUT_INVALID") from None
    return phone_c, name_c
