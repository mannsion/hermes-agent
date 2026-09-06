"""Model inventory restricted to providers in the active configuration."""

from __future__ import annotations

from hermes_cli.provider_policy import (
    ProviderPolicyError,
    get_provider_auth_policy,
    provider_auth_scope,
)


def configured_runtime(provider, *, model=None):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    policy = get_provider_auth_policy()
    policy.require_provider(provider)
    return resolve_runtime_provider(requested=provider, target_model=model)


def configured_model_ids(provider, *, force_refresh=False, cache_only=False):
    from hermes_cli.models import cached_fetch_api_models

    policy = get_provider_auth_policy()
    policy.require_provider(provider)
    entry = policy.provider_config(provider)
    from hermes_cli.model_switch import _declared_model_ids

    declared = _declared_model_ids(entry.get("models"))
    if entry.get("discover_models") is False:
        return declared
    runtime = configured_runtime(provider)
    discovered = cached_fetch_api_models(
        runtime["api_key"],
        runtime["base_url"],
        api_mode=runtime.get("api_mode"),
        headers=runtime.get("extra_headers") or runtime.get("default_headers"),
        force_refresh=force_refresh,
        cache_only=cache_only,
    )
    return discovered if discovered is not None else declared


def configured_provider_rows(
    *,
    current_provider="",
    current_model="",
    current_base_url="",
    max_models=None,
    refresh=False,
    probe_custom_providers=True,
    probe_current_custom_provider=False,
    excluded_providers=None,
):
    policy = get_provider_auth_policy()
    excluded = set(excluded_providers or [])
    rows = []
    with provider_auth_scope(policy):
        for provider in policy.providers:
            if provider in excluded or provider.removeprefix("custom:") in excluded:
                continue
            entry = policy.provider_config(provider)
            current = bool(current_provider) and provider in {
                current_provider,
                "custom:" + current_provider,
            }
            from hermes_cli.inventory import _provider_auth_hint

            auth_type, key_env = _provider_auth_hint(provider)
            row = {
                "slug": provider,
                "provider_id": provider.removeprefix("custom:"),
                "name": str(entry.get("name") or provider.removeprefix("custom:")),
                "is_current": current,
                "is_user_defined": True,
                "source": "user-config",
                "api_url": entry.get("base_url") or "",
                "models": [],
                "total_models": 0,
                "auth_type": auth_type,
                "key_env": entry.get("key_env") or key_env,
            }
            try:
                runtime = configured_runtime(provider)
                row["api_url"] = runtime["base_url"]
                row["api_mode"] = runtime.get("api_mode") or "chat_completions"
                if current_provider == "custom" and current_base_url:
                    current = runtime["base_url"].rstrip(
                        "/"
                    ) == current_base_url.rstrip("/")
                    row["is_current"] = current
                models = configured_model_ids(
                    provider,
                    force_refresh=refresh,
                    cache_only=not (
                        refresh
                        or probe_custom_providers
                        or (current and probe_current_custom_provider)
                    ),
                )
                row["authenticated"] = True
            except (ValueError, RuntimeError) as exc:
                models = []
                row["authenticated"] = False
                row["warning"] = str(exc)
            row["models"] = models[:max_models] if max_models is not None else models
            row["total_models"] = len(models)
            # Current selection is independent of catalog availability; never inject a synthetic model.
            rows.append(row)
    return rows


def require_configured_endpoint(base_url):
    """Authorize a model probe before any provider-specific credential helper runs."""
    policy = get_provider_auth_policy()
    normalized = str(base_url or "").rstrip("/")
    for provider in policy.providers:
        entry = policy.provider_config(provider)
        configured_url = str(entry.get("base_url") or "").rstrip("/")
        if configured_url and configured_url != normalized:
            continue
        runtime = configured_runtime(provider)
        if str(runtime["base_url"]).rstrip("/") == normalized:
            return runtime
    raise ProviderPolicyError(
        "Model discovery endpoint is not configured in the active Hermes home."
    )


def codex_catalog(runtime, timeout):
    from hermes_cli.codex_models import _extract_chatgpt_account_id, _ranked_slugs
    from hermes_cli.models import _get_json, _probe_result

    base_url = runtime["base_url"].rstrip("/")
    url = base_url + "/models?client_version=1.0.0"
    headers = {"Authorization": f"Bearer {runtime['api_key']}"}
    account = _extract_chatgpt_account_id(runtime["api_key"])
    if account:
        headers["ChatGPT-Account-Id"] = account
    try:
        payload = _get_json(url, timeout=timeout, headers=headers)
    except Exception:
        return _probe_result(None, url, base_url)
    return _probe_result(_ranked_slugs(payload.get("models", [])), url, base_url)
