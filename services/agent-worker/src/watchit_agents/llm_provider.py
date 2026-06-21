from __future__ import annotations

from langchain_ollama import ChatOllama

from watchit_core.config import settings
from watchit_core.logging import get_logger

logger = get_logger("watchit.llm_provider")


def build_chat_model(model: str | None = None, base_url: str | None = None, temperature: float = 0):
    provider = (settings.llm_provider or "ollama").lower()
    local_fallback = ChatOllama(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        temperature=temperature,
    )
    if provider in {"ollama", "local", "local_ollama"}:
        logger.info("llm_provider_selected", provider="ollama", model=settings.ollama_model)
        return local_fallback
    if provider in {"anthropic", "claude"}:
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:
            raise RuntimeError(
                "WATCHIT_LLM_PROVIDER=anthropic requires langchain-anthropic to be installed."
            ) from exc
        primary = ChatAnthropic(
            model=model or settings.anthropic_model,
            api_key=settings.anthropic_api_key,
            temperature=temperature,
        )
        logger.info(
            "llm_provider_selected",
            provider="anthropic",
            model=model or settings.anthropic_model,
            fallback_provider="ollama",
            fallback_model=settings.ollama_model,
        )
        return primary.with_fallbacks([local_fallback])
    if provider in {"openai", "cloud"}:
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise RuntimeError(
                "WATCHIT_LLM_PROVIDER=openai requires langchain-openai to be installed."
            ) from exc
        primary = ChatOpenAI(
            model=model or settings.cloud_llm_model,
            api_key=settings.cloud_llm_api_key,
            base_url=base_url or settings.cloud_llm_base_url,
            temperature=temperature,
        )
        logger.info(
            "llm_provider_selected",
            provider="openai",
            model=model or settings.cloud_llm_model,
            fallback_provider="ollama",
            fallback_model=settings.ollama_model,
        )
        return primary.with_fallbacks([local_fallback])
    raise ValueError(f"Unsupported WATCHIT_LLM_PROVIDER: {settings.llm_provider}")
