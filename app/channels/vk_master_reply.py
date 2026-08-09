"""Pure MasterCommandFlowResult → Russian VK reply text (CURSOR-29)."""

from __future__ import annotations

from app.core.master_command_types import (
    MasterCommandClarificationNeed,
    MasterCommandFlowOutcome,
    MasterCommandFlowResult,
    MasterCommandPreview,
)

__all__ = ("render_vk_master_reply",)

_NEED_RU: dict[MasterCommandClarificationNeed, str] = {
    MasterCommandClarificationNeed.DATE: "укажите дату",
    MasterCommandClarificationNeed.TIME: "укажите время начала",
    MasterCommandClarificationNeed.END_TIME: "укажите время окончания",
    MasterCommandClarificationNeed.SLOT_ID: "укажите слот записи",
    MasterCommandClarificationNeed.CLIENT_NAME: "укажите имя клиента",
    MasterCommandClarificationNeed.PHONE: "укажите телефон клиента",
    MasterCommandClarificationNeed.BLOCK_TYPE: "укажите тип блокировки",
    MasterCommandClarificationNeed.AMBIGUOUS: "уточните неоднозначные данные",
}


def render_vk_master_reply(result: MasterCommandFlowResult) -> str | None:
    """Return reply text or None for silent outcomes (duplicates / no reply).

    Ownership for concurrent deliveries: only the C28 delivery that wins the
    inbound insert / claim-execution path may surface a non-silent outcome.
    Inbound losers are ``DUPLICATE_IGNORED``. Claim losers are local
    ``CONFLICT`` (result_code ``CONFLICT``) and stay silent. Control no-ops
    with ``MANUAL_HELP``/``MANUAL_HELP`` (orphan/expired confirm) stay silent
    so a late concurrent «да» does not spam after the winner finishes.
    Business remote conflicts, ``PENDING_COMMAND_ACTIVE``, and unknown-command
    ``MANUAL_HELP``/``UNKNOWN_COMMAND`` still reply.
    """

    if type(result) is not MasterCommandFlowResult:
        return None

    outcome = result.outcome
    if outcome is MasterCommandFlowOutcome.DUPLICATE_IGNORED:
        return None
    if outcome is MasterCommandFlowOutcome.BINDING_REQUIRED:
        return None
    if outcome is MasterCommandFlowOutcome.BINDING_AMBIGUOUS:
        return None
    if outcome is MasterCommandFlowOutcome.REJECTED:
        return None

    if outcome is MasterCommandFlowOutcome.CONFIRMATION_REQUIRED:
        return _confirmation_text(result.preview)
    if outcome is MasterCommandFlowOutcome.CLARIFICATION_REQUIRED:
        return _clarification_text(result.clarification_needs)
    if outcome is MasterCommandFlowOutcome.SUCCESS:
        return _success_text(result)
    if outcome is MasterCommandFlowOutcome.CANCELLED:
        return "Команда отменена."
    if outcome is MasterCommandFlowOutcome.CONFLICT:
        # Local claim/execution race loser (control noop result_code=CONFLICT).
        if result.result_code == "CONFLICT":
            return None
        return "Сейчас нельзя выполнить команду: конфликт или уже есть активная заявка."
    if outcome is MasterCommandFlowOutcome.UNAVAILABLE:
        return (
            "Временно недоступно. Если нужно подтверждение — "
            "отправьте «да» ещё раз позже."
        )
    if outcome is MasterCommandFlowOutcome.MANUAL_HELP:
        # Control no-ops (orphan/expired «да», nothing to confirm) use
        # result_code MANUAL_HELP — silent on VK to avoid reply storms when a
        # concurrent confirm loser observes a already-terminal command.
        # Unknown free-text still uses UNKNOWN_COMMAND and stays vocal.
        if result.result_code == "MANUAL_HELP":
            return None
        return (
            "Не понял команду. Примеры: «выходной завтра», "
            "«закрыть интервал …», «запись клиенту …», «расписание»."
        )
    if outcome is MasterCommandFlowOutcome.COMMAND_ACCEPTED:
        return "Команда принята."
    return None


def _confirmation_text(preview: MasterCommandPreview | None) -> str:
    lines = ["Подтвердите команду."]
    if type(preview) is MasterCommandPreview:
        if type(preview.action) is str and preview.action:
            lines.append(f"Действие: {preview.action}.")
        if type(preview.date_key) is str and preview.date_key:
            lines.append(f"Дата: {preview.date_key}.")
        if type(preview.start_time) is str and preview.start_time:
            end = preview.end_time if type(preview.end_time) is str else None
            if end:
                lines.append(f"Время: {preview.start_time}–{end}.")
            else:
                lines.append(f"Время: {preview.start_time}.")
        if type(preview.service_hint) is str and preview.service_hint:
            lines.append(f"Детали: {preview.service_hint}.")
    lines.append("Ответьте: да — подтвердить, отмена — отменить.")
    return " ".join(lines)


def _clarification_text(
    needs: tuple[MasterCommandClarificationNeed, ...],
) -> str:
    parts: list[str] = []
    for need in needs:
        if type(need) is not MasterCommandClarificationNeed:
            continue
        label = _NEED_RU.get(need)
        if label:
            parts.append(label)
    if not parts:
        return "Нужны уточнения по команде."
    return "Уточните: " + ", ".join(parts) + "."


def _success_text(result: MasterCommandFlowResult) -> str:
    summary = result.schedule_summary
    if type(summary) is tuple and summary:
        safe_lines: list[str] = []
        for line in summary[:30]:
            if type(line) is not str or not line:
                continue
            # schedule_summary is already stripped of client PII by C28.
            if len(line) > 200:
                continue
            safe_lines.append(line)
        if safe_lines:
            return "Готово.\n" + "\n".join(safe_lines)
    return "Готово."
