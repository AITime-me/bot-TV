"""Provider-neutral text generation port for Teя model replies.

This is the integration seam for a future ReplyPlanWorker / dialog orchestration
stage. Implementations must generate text only — never perform CRM, booking,
channel, or outbound writes. Safety gates (BotMode, EMERGENCY_LOCK, handoff,
OutboundArbiter) remain outside this port.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, Protocol, Sequence, runtime_checkable

TextGenerationRole = Literal["system", "user", "assistant"]

_ALLOWED_ROLES: Final[frozenset[str]] = frozenset({"system", "user", "assistant"})


def _text_has_forbidden_controls(value: str) -> bool:
    for ch in value:
        code = ord(ch)
        if code == 127:
            return True
        if code < 32 and ch not in "\n\t":
            return True
    return False


@dataclass(frozen=True, slots=True, repr=False)
class TextGenerationMessage:
    """One chat turn. Text never appears in repr/logs."""

    role: TextGenerationRole
    text: str

    def __post_init__(self) -> None:
        if self.role not in _ALLOWED_ROLES:
            raise ValueError("role invalid") from None
        if type(self.text) is not str or not self.text:
            raise ValueError("text invalid") from None
        if _text_has_forbidden_controls(self.text):
            raise ValueError("text invalid") from None

    def __repr__(self) -> str:
        return f"TextGenerationMessage(role={self.role!r}, text=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class TextGenerationResult:
    """Model completion. Generated text never appears in repr."""

    text: str

    def __post_init__(self) -> None:
        if type(self.text) is not str or not self.text.strip():
            raise ValueError("text invalid") from None

    def __repr__(self) -> str:
        return f"TextGenerationResult(text_len={len(self.text)!r})"


@runtime_checkable
class TextGenerationPort(Protocol):
    """Sync text generation boundary. Unit tests inject fakes; live = Yandex GPT."""

    def generate(
        self,
        messages: Sequence[TextGenerationMessage],
    ) -> TextGenerationResult:
        """Return final assistant text or raise a provider-local fixed-code error."""
        ...
