"""Factory for the Teя text-generation provider (Yandex GPT foundation).

Default-off: when YANDEX_LLM_ENABLED is false/absent, returns None and performs
zero HTTP. Partial enabled config fails closed (raises YandexLlmConfigError).

Composition roots may inject the returned TextGenerationPort into a future
dialog/reply orchestration stage. This factory never touches ReplyPlanWorker,
OutboundArbiter, booking, or channel adapters.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.core.s2s_http_stdlib import S2sHttpStdlibTransport
from app.core.s2s_http_transport import S2sHttpTransport
from app.core.text_generation_port import TextGenerationPort
from app.core.yandex_gpt_http import YandexGptHttpClient, YandexGptHttpError
from app.core.yandex_llm_config import YandexLlmConfig, YandexLlmConfigError

__all__ = (
    "build_text_generation_port",
    "build_yandex_llm_config",
)


def build_yandex_llm_config(
    environ: Mapping[str, str] | None = None,
) -> YandexLlmConfig:
    """Load Yandex LLM config. Disabled by default. Partial enabled fails closed."""

    return YandexLlmConfig.from_env(environ)


def build_text_generation_port(
    environ: Mapping[str, str] | None = None,
    *,
    transport: S2sHttpTransport | None = None,
    config: YandexLlmConfig | None = None,
) -> TextGenerationPort | None:
    """Return a Yandex GPT client or None when disabled.

    Never performs HTTP during construction. When disabled, ``transport`` is
    unused (including when a fake is injected) — callers can assert zero calls.
    """

    resolved = config if config is not None else build_yandex_llm_config(environ)
    if not resolved.enabled:
        return None
    try:
        resolved.require_runtime()
    except YandexLlmConfigError as exc:
        if exc.code == "YANDEX_LLM_DISABLED":
            return None
        raise
    selected = S2sHttpStdlibTransport() if transport is None else transport
    try:
        return YandexGptHttpClient(resolved, selected)
    except YandexGptHttpError as exc:
        if exc.code == "DISABLED":
            return None
        raise YandexLlmConfigError("YANDEX_LLM_CONFIG_INVALID") from None
