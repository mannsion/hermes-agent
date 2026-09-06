"""Provider policy at the auxiliary request boundary.

Strict requests reuse the native runtime resolver; recovery never gets a second,
less restrictive credential resolver.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import wraps
from inspect import iscoroutinefunction
import hashlib

from hermes_cli.provider_policy import get_provider_auth_policy, provider_auth_scope


def request_policy(main_runtime=None):
    current = get_provider_auth_policy()
    if current.config_only:
        return current
    if main_runtime is None:
        from agent.auxiliary_client import _RUNTIME_MAIN_CONTEXT

        main_runtime = _RUNTIME_MAIN_CONTEXT.get()
    if (
        isinstance(main_runtime, dict)
        and main_runtime.get("provider_auth_policy") is not None
    ):
        return main_runtime["provider_auth_policy"]
    return current


def scoped_auxiliary_call(function):
    """Pin policy for the whole request, including every retry and fallback."""
    if iscoroutinefunction(function):

        @wraps(function)
        async def async_scoped(*args, **kwargs):
            with provider_auth_scope(request_policy(kwargs.get("main_runtime"))):
                return await function(*args, **kwargs)

        return async_scoped

    @wraps(function)
    def scoped(*args, **kwargs):
        policy = request_policy(kwargs.get("main_runtime"))
        with provider_auth_scope(policy):
            result = function(*args, **kwargs)
        return (
            _scoped_stream(result, policy)
            if policy.config_only
            and kwargs.get("stream")
            and isinstance(result, Iterator)
            else result
        )

    return scoped


def _scoped_stream(stream, policy):
    try:
        while True:
            with provider_auth_scope(policy):
                try:
                    item = next(stream)
                except StopIteration:
                    return
            yield item
    finally:
        with provider_auth_scope(policy):
            close = getattr(stream, "close", None)
            if close:
                close()


def resolve_configured_client(req):
    from agent import auxiliary_client as aux
    from hermes_cli.provider_policy import ProviderPolicyError
    from hermes_cli.runtime_provider import resolve_runtime_provider

    policy = get_provider_auth_policy()
    main = aux._normalize_main_runtime(req.main_runtime)
    provider = req.original_provider
    if provider in {"", "auto"}:
        provider = (
            main.get("requested_provider")
            or main.get("provider")
            or policy.default_provider
        )
    policy.require_provider(provider)
    runtime = resolve_runtime_provider(
        requested=provider,
        target_model=req.model or main.get("model"),
        explicit_base_url=req.explicit_base_url,
        explicit_api_key=req.explicit_api_key,
    )
    model = req.model or runtime.get("model") or main.get("model")
    if not model:
        raise ProviderPolicyError(
            f"Configure a model for provider {provider!r} in config.yaml."
        )
    mode = req.api_mode or runtime.get("api_mode") or "chat_completions"
    base_url, key = runtime["base_url"], runtime["api_key"]
    headers = runtime.get("extra_headers") or runtime.get("default_headers") or {}
    if mode == "anthropic_messages":
        from agent.anthropic_adapter import build_anthropic_client
        from agent.anthropic_credentials import _is_oauth_token

        client = aux.AnthropicAuxiliaryClient(
            build_anthropic_client(key, base_url),
            model,
            key,
            base_url,
            is_oauth=_is_oauth_token(key),
        )
    elif mode in {"chat_completions", "codex_responses"}:
        client = aux._create_openai_client(
            api_key=key,
            base_url=base_url,
            default_headers=headers,
            organization="",
            project="",
        )
        if mode == "codex_responses" and not req.raw_codex:
            client = aux.CodexAuxiliaryClient(client, model)
    else:
        raise ProviderPolicyError(
            f"Auxiliary transport {mode!r} cannot enforce config_only provider authentication."
        )
    if aux._aux_probe_active():
        return client, model
    if req.async_mode:
        client, model = aux._to_async_client(client, model, is_vision=req.is_vision)
    client._hermes_aux_effective_provider = provider
    client._hermes_provider_auth_policy = policy
    return client, model


def credential_store_identity(policy):
    """Do not let a cached client outlive local logout or credential rotation."""
    digest = hashlib.blake2b(digest_size=16)
    for name in ("auth.json", ".anthropic_oauth.json"):
        path = policy.local_path(name)
        digest.update(name.encode())
        try:
            digest.update(path.read_bytes())
        except FileNotFoundError:
            digest.update(b"missing")
    return digest.digest()
