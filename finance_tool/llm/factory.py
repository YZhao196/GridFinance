"""Which provider is configured, and building it from settings and secrets (§24).

No module-level cache holds a provider: a cached client keeps the plaintext key in memory
for the life of the process. Building one per call is cheap next to the API call it makes.
"""

from __future__ import annotations

from typing import Any

from finance_tool.llm.base import Provider
from finance_tool.store.secrets import Secrets
from finance_tool.store.store import Store

PROVIDERS = ("anthropic", "openai", "deepseek", "openrouter", "ollama")
KEY_FIELDS = {
    "anthropic": "llm_anthropic",
    "openai": "llm_openai",
    "deepseek": "llm_deepseek",
    "openrouter": "llm_openrouter",
}
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-chat",
    "openrouter": "",
    "ollama": "llama3.1",
}
BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "ollama": "http://localhost:11434/v1",
}
LOCAL_PROVIDERS = {"ollama"}


def _infer(secrets: Secrets) -> str:
    for provider in ("anthropic", "openai", "deepseek", "openrouter"):
        if secrets.has(KEY_FIELDS[provider]):
            return provider
    return ""


def resolve(store: Store, secrets: Secrets) -> dict[str, Any]:
    settings = store.settings
    provider = (settings.ai_provider or "").strip().lower()
    if provider not in PROVIDERS:
        provider = _infer(secrets)
    model = settings.ai_model or DEFAULT_MODELS.get(provider, "")
    api_key = getattr(secrets, KEY_FIELDS[provider], None) if provider in KEY_FIELDS else None
    base_url = settings.ai_base_url or BASE_URLS.get(provider, "")
    return {
        "provider": provider,
        "model": model,
        "effort": settings.ai_effort or "high",
        "api_key": api_key,
        "base_url": base_url,
        "configured": bool(provider) and (provider in LOCAL_PROVIDERS or bool(api_key)),
    }


def build(config: dict[str, Any]) -> Provider | None:
    provider = (config.get("provider") or "").lower()
    if provider not in PROVIDERS:
        return None
    if provider not in LOCAL_PROVIDERS and not config.get("api_key"):
        return None
    if not config.get("model"):
        return None
    try:
        if provider == "anthropic":
            from finance_tool.llm.anthropic_provider import AnthropicProvider
            return AnthropicProvider(api_key=config["api_key"], model=config["model"],
                                     effort=config.get("effort") or "high")
        from finance_tool.llm.openai_provider import OpenAIProvider
        return OpenAIProvider(api_key=config.get("api_key") or "ollama",
                              model=config["model"],
                              base_url=config.get("base_url") or None,
                              label=provider)
    except ImportError:
        return None


def _sdk_ready(config: dict[str, Any]) -> bool:
    try:
        return build(config) is not None
    except Exception:
        return False


def get_provider(store: Store, secrets: Secrets) -> Provider | None:
    return build(resolve(store, secrets))


def provider_status(store: Store, secrets: Secrets) -> dict[str, Any]:
    config = resolve(store, secrets)
    ready = config["configured"] and _sdk_ready(config)
    return {
        "provider": config["provider"],
        "model": config["model"],
        "configured": ready,
        "reason": None if ready else _missing_reason(config),
    }


def _missing_reason(config: dict[str, Any]) -> str:
    provider = config["provider"]
    if not provider:
        return "Choose a provider and paste an API key in Settings."
    if not config["model"]:
        return f"Choose a model for {provider}."
    if not config["configured"]:
        if provider in LOCAL_PROVIDERS:
            return "Ollama needs no key — check that it is running."
        return f"Paste a {provider} API key in Settings."
    return f"Install the 'ai' extra to use {provider}: pip install gridfinance[ai]."


def test_key(provider: str, model: str, api_key: str,
             base_url: str | None = None) -> tuple[bool, str]:
    provider = (provider or "").strip().lower()
    if provider not in PROVIDERS:
        return False, f"Unknown provider {provider!r}."
    config = {"provider": provider, "model": model, "api_key": api_key,
              "base_url": base_url or BASE_URLS.get((provider or "").lower(), ""),
              "effort": "low"}
    if not model:
        return False, f"Choose a model for {provider}."
    client = build(config)
    if client is None:
        if provider in LOCAL_PROVIDERS and not api_key:
            return False, f"Install the 'ai' extra to use {provider}."
        if not api_key:
            return False, "No API key provided."
        return False, f"Install the 'ai' extra to use {provider}."
    try:
        turn = client.complete(system="You are a connection test. Reply with OK.",
                               messages=[{"role": "user", "content": "ping"}],
                               max_tokens=256)
    except Exception as exc:  # the caller shows the reason
        return False, str(exc)[:300]
    return True, (turn.text or "OK").strip()[:80]
