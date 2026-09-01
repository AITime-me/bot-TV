"""Declarative semantic-preservation contract for Settings v2 mainInstruction dedup.

Test-time only: each row documents a v1 mainInstruction snippet removed in v2 and
where equivalent behavior is preserved (safetyRules / handoffRules / code guard).
No runtime parser consumes this map.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PreservationLocation = Literal[
    "immutable_guard",
    "safety_rules",
    "handoff_rules",
    "v2_retained",
]


@dataclass(frozen=True, slots=True)
class SemanticDedupExpectation:
    removed_v1_snippet: str
    preservation_location: PreservationLocation
    canonical_preservation_snippet: str


TEYA_MAIN_INSTRUCTION_SEMANTIC_DEDUP_EXPECTATIONS: tuple[SemanticDedupExpectation, ...] = (
    SemanticDedupExpectation(
        removed_v1_snippet="Если live-данные и текст базы знаний расходятся по динамическому факту, приоритет всегда у live-данных.",
        preservation_location="immutable_guard",
        canonical_preservation_snippet="Trusted source precedence: LIVE FACTS > ACTIVE Managed KB > conversation.",
    ),
    SemanticDedupExpectation(
        removed_v1_snippet="Не придумывай факты.",
        preservation_location="immutable_guard",
        canonical_preservation_snippet="Missing authoritative fact → do not invent; prefer handoff/escalation.",
    ),
    SemanticDedupExpectation(
        removed_v1_snippet=(
            "Актуальные динамические данные студии — цены, длительность услуг, "
            "доступность услуги, мастера, режим записи, свободные окна"
        ),
        preservation_location="safety_rules",
        canonical_preservation_snippet="Динамические факты — только из Live Facts",
    ),
    SemanticDedupExpectation(
        removed_v1_snippet="не ставь диагнозы и не принимай решения, требующие профессиональной медицинской",
        preservation_location="safety_rules",
        canonical_preservation_snippet="Не ставить медицинские диагнозы",
    ),
    SemanticDedupExpectation(
        removed_v1_snippet="передай вопрос человеку согласно правилам handoff",
        preservation_location="handoff_rules",
        canonical_preservation_snippet="Передать диалог человеку обязательно",
    ),
    SemanticDedupExpectation(
        removed_v1_snippet="информация, которой нет в разрешённых источниках",
        preservation_location="handoff_rules",
        canonical_preservation_snippet="подтверждённого ответа нет в доступных источниках",
    ),
    SemanticDedupExpectation(
        removed_v1_snippet="Не придумывай свободные окна и не обещай время, которое не подтверждено системой.",
        preservation_location="safety_rules",
        canonical_preservation_snippet="свободных окон",
    ),
    SemanticDedupExpectation(
        removed_v1_snippet="если соответствующее действие не разрешено runtime-инструментом текущего сценария",
        preservation_location="safety_rules",
        canonical_preservation_snippet="runtime-инструменты",
    ),
    SemanticDedupExpectation(
        removed_v1_snippet="Если менеджер или другой уполномоченный сотрудник вступил в диалог",
        preservation_location="handoff_rules",
        canonical_preservation_snippet="После вступления менеджера",
    ),
    SemanticDedupExpectation(
        removed_v1_snippet="После передачи человеку сохраняй контекст разговора",
        preservation_location="handoff_rules",
        canonical_preservation_snippet="При передаче сохранить краткий контекст",
    ),
    SemanticDedupExpectation(
        removed_v1_snippet="— цены;",
        preservation_location="v2_retained",
        canonical_preservation_snippet="— услуги;",
    ),
    SemanticDedupExpectation(
        removed_v1_snippet="Не обещай гарантированный результат.",
        preservation_location="safety_rules",
        canonical_preservation_snippet="не обещать гарантированный результат",
    ),
    SemanticDedupExpectation(
        removed_v1_snippet=(
            "При недостатке подтверждённых данных, конфликте информации, ошибке системы "
            "или ситуации вне твоих полномочий"
        ),
        preservation_location="handoff_rules",
        canonical_preservation_snippet="есть жалоба, конфликт",
    ),
)
