from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pii_gateway import PiiGatewayError, safe_fingerprint, sanitize_for_ai
from app.models.conversation import Conversation

MAX_DIALOG_CONTEXT_MESSAGES = 40
MAX_DIALOG_CONTEXT_CHARS = 12_000
_ALLOWED_AI_AUTHORS = frozenset({"client", "manager"})
_REDACTED_AUTHOR = "<redacted>"


def _safe_author_for_repr(author: object) -> str:
    if type(author) is str:
        if author == "client":
            return "client"
        if author == "manager":
            return "manager"
    return _REDACTED_AUTHOR


@dataclass(frozen=True, repr=False)
class DialogMessage:
    conversation_event_seq: int
    author: str
    text: str

    def __repr__(self) -> str:
        return (
            "DialogMessage("
            f"author={_safe_author_for_repr(self.author)!r}, "
            "text=<redacted>)"
        )


@dataclass(frozen=True, repr=False)
class DialogContext:
    conversation_id: uuid.UUID
    event_seq_hwm: int
    messages: tuple[DialogMessage, ...]
    total_chars: int

    def __repr__(self) -> str:
        return (
            "DialogContext("
            f"conversation_id={safe_fingerprint(self.conversation_id, purpose='conversation_id')!r}, "
            f"message_count={len(self.messages)!r}, "
            f"total_chars={self.total_chars!r})"
        )


class DialogContextService:
    """Read a bounded canonical client+manager timeline.

    Client text is read only from ``inbox_messages`` and manager text only from
    ``manager_messages``. ReplyPlan/outbox payloads never receive a history
    copy.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(
        self,
        *,
        conversation_id: uuid.UUID,
        event_seq_hwm: int | None = None,
    ) -> DialogContext:
        if event_seq_hwm is None:
            event_seq_hwm = await self._session.scalar(
                select(Conversation.current_event_seq).where(
                    Conversation.id == conversation_id
                )
            )
            if event_seq_hwm is None:
                raise RuntimeError("CONVERSATION_LOOKUP_FAILED")
        if event_seq_hwm < 0:
            raise ValueError("event_seq_hwm must be nonnegative")

        rows = (
            await self._session.execute(
                text(
                    """
                    SELECT conversation_event_seq, author, body_text
                    FROM (
                        SELECT
                            conversation_event_seq,
                            'client'::text AS author,
                            payload_json ->> 'text' AS body_text
                        FROM inbox_messages
                        WHERE conversation_id = CAST(:conversation_id AS uuid)
                          AND conversation_event_seq <= :event_seq_hwm
                        UNION ALL
                        SELECT
                            conversation_event_seq,
                            'manager'::text AS author,
                            body_text
                        FROM manager_messages
                        WHERE conversation_id = CAST(:conversation_id AS uuid)
                          AND status = 'APPLIED'
                          AND conversation_event_seq <= :event_seq_hwm
                    ) AS dialog_events
                    WHERE body_text IS NOT NULL
                    ORDER BY conversation_event_seq DESC
                    LIMIT :message_limit
                    """
                ),
                {
                    "conversation_id": str(conversation_id),
                    "event_seq_hwm": event_seq_hwm,
                    "message_limit": MAX_DIALOG_CONTEXT_MESSAGES,
                },
            )
        ).mappings()
        newest_first = [
            DialogMessage(
                conversation_event_seq=int(row["conversation_event_seq"]),
                author=str(row["author"]),
                text=str(row["body_text"]),
            )
            for row in rows
        ]
        messages = trim_dialog_messages(newest_first)
        return DialogContext(
            conversation_id=conversation_id,
            event_seq_hwm=event_seq_hwm,
            messages=messages,
            total_chars=sum(len(message.text) for message in messages),
        )


def trim_dialog_messages(
    newest_first: list[DialogMessage],
    *,
    max_messages: int = MAX_DIALOG_CONTEXT_MESSAGES,
    max_chars: int = MAX_DIALOG_CONTEXT_CHARS,
) -> tuple[DialogMessage, ...]:
    """Keep the newest contiguous suffix, returned in dialog order."""
    if max_messages <= 0 or max_chars <= 0:
        raise ValueError("dialog context limits must be positive")

    selected_newest_first: list[DialogMessage] = []
    total_chars = 0
    for message in newest_first[:max_messages]:
        message_chars = len(message.text)
        if total_chars + message_chars > max_chars:
            break
        selected_newest_first.append(message)
        total_chars += message_chars
    selected_newest_first.reverse()
    return tuple(selected_newest_first)


def to_ai_safe_messages(
    context: DialogContext,
    *,
    known_pii: tuple[str, ...] = (),
) -> tuple[dict[str, str], ...]:
    """Return per-message AI-safe projections without IDs or sequences."""
    safe_messages: list[dict[str, str]] = []
    for message in context.messages:
        author = message.author
        if type(author) is not str or author not in _ALLOWED_AI_AUTHORS:
            raise PiiGatewayError("AI_AUTHOR_INVALID") from None
        safe_messages.append(
            {
                "author": author,
                "text": sanitize_for_ai(message.text, known_pii=known_pii),
            }
        )
    return tuple(safe_messages)
