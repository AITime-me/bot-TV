"""Sanitize amoCRM CRM Platform outgoing_message[add] form fields (CAPTURE-ONLY).

Never retain text, names, avatars, attachment URLs, phones, or raw body.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import parse_qs

__all__ = (
    "NativeOutgoingCaptureCandidate",
    "extract_outgoing_message_adds",
    "parse_native_outgoing_form_body",
)

_MESSAGE_ID_MAX = 128
_TECH_ID_MAX = 128


@dataclass(frozen=True, slots=True, repr=False)
class NativeOutgoingCaptureCandidate:
    """Technical fields only. No PII / message body."""

    amocrm_message_id: str
    talk_id: int
    chat_id: str
    contact_id: int
    origin: str
    source_id: int | None
    author_id: str | None
    author_type: str | None
    author_user_id: str | None
    recipient_id: str | None
    recipient_type: str | None
    outgoing_type: str
    message_type: str
    provider_created_at: datetime | None
    account_id: str | None

    def __repr__(self) -> str:
        return (
            "NativeOutgoingCaptureCandidate("
            f"amocrm_message_id=<redacted>, "
            f"talk_id={self.talk_id!r}, "
            f"chat_id={self.chat_id!r}, "
            f"contact_id={self.contact_id!r}, "
            f"origin={self.origin!r}, "
            f"source_id={self.source_id!r}, "
            f"author_type={self.author_type!r}, "
            f"outgoing_type={self.outgoing_type!r}, "
            f"message_type={self.message_type!r})"
        )


def _first(values: list[str] | None) -> str | None:
    if not values:
        return None
    raw = values[0]
    if type(raw) is not str:
        return None
    stripped = raw.strip()
    return stripped if stripped else None


def _safe_tech_id(value: str | None, *, max_len: int = _TECH_ID_MAX) -> str | None:
    if value is None:
        return None
    if len(value) > max_len:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    if any(ch.isspace() for ch in value):
        return None
    return value


def _parse_positive_int(value: str | None) -> int | None:
    if value is None or not value.isdigit() or value.startswith("0"):
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _parse_created_at(value: str | None) -> datetime | None:
    if value is None or not value.isdigit():
        return None
    # Reject leading-zero weirdness except plain epoch.
    if value.startswith("0") and value != "0":
        return None
    try:
        epoch = int(value)
    except ValueError:
        return None
    if epoch <= 0:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse_native_outgoing_form_body(raw_body: bytes) -> dict[str, list[str]]:
    """Parse form-urlencoded body into multi-dict. Never logs body."""

    if type(raw_body) is not bytes:
        return {}
    if not raw_body:
        return {}
    try:
        text = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    parsed = parse_qs(text, keep_blank_values=False, strict_parsing=False)
    out: dict[str, list[str]] = {}
    for key, values in parsed.items():
        if type(key) is not str or not key:
            continue
        cleaned = [v for v in values if type(v) is str and v != ""]
        if cleaned:
            out[key] = cleaned
    return out


def extract_outgoing_message_adds(
    form: Mapping[str, list[str]],
) -> tuple[NativeOutgoingCaptureCandidate, ...]:
    """Extract sanitized outgoing_message[add][*] candidates only.

    Ignores message[add] and other entities. Skips malformed elements.
    """

    if not isinstance(form, Mapping):
        return ()

    account_id = _safe_tech_id(_first(form.get("account[id]")))

    indices: set[int] = set()
    prefix = "outgoing_message[add]["
    for key in form:
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix) :]
        bracket = rest.find("]")
        if bracket <= 0:
            continue
        idx_raw = rest[:bracket]
        if not idx_raw.isdigit():
            continue
        indices.add(int(idx_raw))

    candidates: list[NativeOutgoingCaptureCandidate] = []
    for index in sorted(indices):
        base = f"outgoing_message[add][{index}]"
        message_id = _safe_tech_id(
            _first(form.get(f"{base}[id]")),
            max_len=_MESSAGE_ID_MAX,
        )
        chat_id = _safe_tech_id(_first(form.get(f"{base}[chat_id]")))
        origin = _safe_tech_id(_first(form.get(f"{base}[origin]")), max_len=64)
        outgoing_type = _safe_tech_id(_first(form.get(f"{base}[type]")), max_len=32)
        message_type = _safe_tech_id(
            _first(form.get(f"{base}[message_type]")),
            max_len=32,
        )
        talk_id = _parse_positive_int(_first(form.get(f"{base}[talk_id]")))
        contact_id = _parse_positive_int(_first(form.get(f"{base}[contact_id]")))
        source_id = _parse_positive_int(_first(form.get(f"{base}[source_id]")))

        if (
            message_id is None
            or chat_id is None
            or origin is None
            or outgoing_type is None
            or message_type is None
            or talk_id is None
            or contact_id is None
        ):
            continue

        # Proof gate fields: only text outgoing is capturable in this step.
        if outgoing_type != "outgoing" or message_type != "text":
            continue

        author_id = _safe_tech_id(_first(form.get(f"{base}[author][id]")))
        author_type = _safe_tech_id(
            _first(form.get(f"{base}[author][type]")),
            max_len=32,
        )
        author_user_id = _safe_tech_id(
            _first(form.get(f"{base}[author][user_id]")),
            max_len=64,
        )
        recipient_id = _safe_tech_id(_first(form.get(f"{base}[recipient][id]")))
        recipient_type = _safe_tech_id(
            _first(form.get(f"{base}[recipient][type]")),
            max_len=32,
        )
        provider_created_at = _parse_created_at(
            _first(form.get(f"{base}[created_at]"))
        )

        candidates.append(
            NativeOutgoingCaptureCandidate(
                amocrm_message_id=message_id,
                talk_id=talk_id,
                chat_id=chat_id,
                contact_id=contact_id,
                origin=origin,
                source_id=source_id,
                author_id=author_id,
                author_type=author_type,
                author_user_id=author_user_id,
                recipient_id=recipient_id,
                recipient_type=recipient_type,
                outgoing_type=outgoing_type,
                message_type=message_type,
                provider_created_at=provider_created_at,
                account_id=account_id,
            )
        )
    return tuple(candidates)


def candidate_public_view(candidate: NativeOutgoingCaptureCandidate) -> dict[str, Any]:
    """Redacted diagnostic view — never includes text/names."""

    return {
        "talk_id": candidate.talk_id,
        "chat_id": candidate.chat_id,
        "contact_id": candidate.contact_id,
        "origin": candidate.origin,
        "source_id": candidate.source_id,
        "author_type": candidate.author_type,
        "outgoing_type": candidate.outgoing_type,
        "message_type": candidate.message_type,
    }
