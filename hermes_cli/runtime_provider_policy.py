"""Native provider runtime construction from a config-only authority snapshot.

Protocol selection and OAuth refresh remain owned by their native adapters;
the automatic discovery ladder is intentionally not an input to this resolver.
"""
from __future__ import annotations

from typing import Any

from hermes_cli.provider_policy import ProviderAuthPolicy, ProviderPolicyError, _canonical_provider


def _literal(value: Any, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or "${" in value:
        raise ProviderPolicyError(f"{label} must be a literal value in provider_auth.mode: config_only")
    return value.strip()


def _configured_key(policy: ProviderAuthPolicy, entry: dict[str, Any], provider: str) -> tuple[str, str]:
    from hermes_cli.auth import PROVIDER_REGISTRY

    if entry.get("key_cmd"):
        policy.require_external_source("key_cmd")
    key = _literal(entry.get("api_key"), "Provider api_key")
    if key:
        return key, "config"
    ref = entry.get("key_env") or entry.get("api_key_env") or entry.get("api_key_env_var")
    if ref:
        name = _literal(ref, "Provider key_env")
        key = policy.env_value(name).strip()
        if not key:
            raise ProviderPolicyError(f"Provider credential {name!r} is missing from the active home's .env")
        return key, "local_env"
    pconfig = PROVIDER_REGISTRY.get(provider)
    env_names = pconfig.api_key_env_vars if pconfig else ()
    if provider == "openrouter":
        env_names = ("OPENROUTER_API_KEY",)
    for name in env_names:
        key = policy.env_value(name).strip()
        if key:
            return key, "local_env"
    return "", ""


def _oauth_runtime(provider: str, entry: dict[str, Any], model: str, force_refresh: bool) -> dict[str, Any]:
    from hermes_cli import auth, runtime_provider as rp

    resolvers = {
        "nous": auth.resolve_nous_runtime_credentials,
        "openai-codex": auth.resolve_codex_runtime_credentials,
        "xai-oauth": auth.resolve_xai_oauth_runtime_credentials,
        "qwen-oauth": auth.resolve_qwen_runtime_credentials,
    }
    creds = resolvers[provider](force_refresh=force_refresh)
    spec = rp._OAUTH_RUNTIME_PROVIDERS[provider]
    mode = spec.api_mode(model) if callable(spec.api_mode) else spec.api_mode
    return rp._runtime(
        provider, mode, (creds.get("base_url") or spec.default_base_url).rstrip("/"),
        creds.get("api_key", ""), source=creds.get("source", spec.default_source),
        **{spec.expiry_key: creds.get(spec.expiry_key)}, requested_provider=provider,
    )


def _pool_runtime(provider: str, credential_hint: str | None = None) -> dict[str, Any] | None:
    from agent.credential_pool import load_pool

    pool = load_pool(provider)
    credential = None
    if credential_hint:
        credential = next((row for row in pool.entries() if row.runtime_api_key == credential_hint), None)
    if credential is None and pool.has_credentials():
        credential = pool.select()
    if credential is None:
        return None
    key = credential.runtime_api_key or credential.access_token
    if not key:
        return None
    # The selected pool credential must not change the configured endpoint.
    return {"api_key": key, "source": "local_pool", "credential_pool": pool}


def resolve_config_only_runtime(
    policy: ProviderAuthPolicy, *, requested: str | None = None,
    explicit_api_key: str | None = None, explicit_base_url: str | None = None,
    target_model: str | None = None, force_refresh: bool = False,
) -> dict[str, Any]:
    from hermes_cli import auth, runtime_provider as rp
    from hermes_cli.runtime_provider_custom import _apply_custom_provider_extras

    selected = str(requested or "").strip().lower()
    provider = policy.default_provider if selected in {"", "auto"} else _canonical_provider(selected)
    policy.require_provider(provider)
    entry = policy.provider_config(provider)
    if provider not in policy.providers and "custom:" + provider in policy.providers:
        provider = "custom:" + provider
    pconfig = auth.PROVIDER_REGISTRY.get(provider)
    if pconfig and pconfig.auth_type not in {"api_key", "oauth_device_code", "oauth_external", "oauth_minimax"}:
        policy.require_external_source(f"{provider} {pconfig.auth_type} adapter")
    if provider in {"bedrock", "vertex", "azure-foundry", "moa"}:
        policy.require_external_source(f"{provider} adapter with external credential resolution")
    configured_model = entry.get("default") or entry.get("default_model") or entry.get("model") or ""
    if isinstance(configured_model, dict):
        from hermes_cli.config import split_model_config_default
        configured_model = split_model_config_default(configured_model)[0]
    model = str(target_model or configured_model)
    key, source = _configured_key(policy, entry, provider)
    base_url = _literal(entry.get("base_url") or entry.get("baseUrl") or entry.get("url"), "Provider base_url").rstrip("/")
    if not base_url and pconfig:
        base_url = pconfig.inference_base_url.rstrip("/")
        if pconfig.base_url_env_var:
            base_url = policy.env_value(pconfig.base_url_env_var, base_url).strip().rstrip("/")
    if not base_url and provider == "openrouter":
        from hermes_constants import OPENROUTER_BASE_URL
        base_url = policy.env_value("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL).rstrip("/")

    if provider == "copilot":
        creds = auth.resolve_api_key_provider_credentials(provider)
        key, source = creds["api_key"], creds["source"]
        base_url = creds["base_url"]

    oauth = provider in rp._OAUTH_RUNTIME_PROVIDERS
    if oauth and not key:
        result = _oauth_runtime(provider, entry, model, force_refresh)
    elif provider == "minimax-oauth" and not key:
        result = rp._minimax_oauth_runtime(provider, provider)
    else:
        if not base_url:
            raise ProviderPolicyError(f"Provider {provider!r} needs an explicit base_url in config.yaml")
        mode = _literal(entry.get("api_mode") or entry.get("transport"), "Provider api_mode")
        if mode and mode not in {"chat_completions", "codex_responses", "anthropic_messages"}:
            policy.require_external_source(f"{mode} transport")
        if entry.get("openai_runtime") == "codex_app_server":
            policy.require_external_source("codex_app_server transport")
        custom = provider == "custom" or provider.startswith("custom:")
        mode = mode or rp._configured_or_fallback_api_mode(
            "custom" if custom else provider, entry, base_url, model, opencode_by_model=True,
        )
        pooled = _pool_runtime(provider, explicit_api_key) if not key else None
        if pooled:
            key, source = pooled["api_key"], pooled["source"]
        configured_keyless = auth._provider_is_keyless(provider)
        if not key and not custom and not configured_keyless:
            raise ProviderPolicyError(
                f"Provider {provider!r} has no permitted local credentials. "
                "Save a key or log in within this Hermes home."
            )
        result = rp._runtime("custom" if custom else provider, mode, base_url,
                             key or "no-key-required", source=source or "configured_keyless",
                             requested_provider=provider)
        if pooled:
            result["credential_pool"] = pooled["credential_pool"]
    if explicit_base_url and explicit_base_url.strip().rstrip("/") != result["base_url"].rstrip("/"):
        raise ProviderPolicyError("A runtime base_url override cannot replace the configured provider endpoint in config_only mode")
    if explicit_api_key and explicit_api_key != result["api_key"]:
        raise ProviderPolicyError("A runtime api_key override cannot replace the configured credential in config_only mode")
    headers = entry.get("extra_headers")
    if isinstance(headers, dict):
        for value in headers.values():
            _literal(value, "Provider extra_headers value")
    _apply_custom_provider_extras(entry, model, result)
    result["provider_auth_policy"] = policy
    return result
