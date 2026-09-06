"""SDK constructor inputs whose provider authority is pinned to Hermes config."""
from __future__ import annotations

from typing import Any

from hermes_cli.provider_policy import ProviderAuthPolicy, ProviderPolicyError, provider_auth_scope


def configured_client_kwargs(
    policy: ProviderAuthPolicy, kwargs: dict[str, Any], *, provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_cli.runtime_provider_policy import _literal

    with provider_auth_scope(policy):
        selected = provider or _provider_for_endpoint(policy, str(kwargs.get("base_url") or ""))
        runtime = resolve_runtime_provider(
            requested=selected, target_model=model,
            explicit_api_key=kwargs.get("api_key"), explicit_base_url=kwargs.get("base_url"),
        )
        entry = policy.provider_config(runtime["requested_provider"])
    result = dict(kwargs)
    result["api_key"] = runtime["api_key"]
    result["base_url"] = runtime["base_url"]
    # SDKs interpret None as permission to consult os.environ; empty strings
    # are explicit absence for these optional provider-account selectors.
    result["organization"] = _literal(entry.get("organization"), "Provider organization")
    result["project"] = _literal(entry.get("project"), "Provider project")
    headers = {}
    for block in (entry.get("default_headers"), entry.get("extra_headers")):
        if block is not None and not isinstance(block, dict):
            raise ProviderPolicyError("Provider headers must be a mapping of literal values")
        for name, value in (block or {}).items():
            headers[_literal(name, "Provider header name")] = _literal(value, "Provider header value")
    result["default_headers"] = headers
    return result


def _provider_for_endpoint(policy: ProviderAuthPolicy, base_url: str) -> str:
    from hermes_cli.auth import PROVIDER_REGISTRY
    from hermes_constants import OPENROUTER_BASE_URL

    def normalized(value):
        return str(value or "").rstrip("/").removesuffix("/v1")

    target = normalized(base_url)
    matches = []
    for provider in policy.providers:
        entry = policy.provider_config(provider)
        default = PROVIDER_REGISTRY.get(provider)
        endpoint = entry.get("base_url") or entry.get("baseUrl") or entry.get("url")
        endpoint = endpoint or (default.inference_base_url if default else "")
        if provider == "openrouter":
            endpoint = endpoint or OPENROUTER_BASE_URL
        if target and normalized(endpoint) == target:
            matches.append(provider)
    if policy.default_provider in matches:
        return policy.default_provider
    if len(matches) == 1:
        return matches[0]
    raise ProviderPolicyError("Client endpoint must identify a configured provider in config_only mode")
