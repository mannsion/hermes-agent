"""Config-only provider resolution against real, isolated native configuration."""
import asyncio
import json
from pathlib import Path

import pytest
import yaml

from hermes_cli.provider_policy import (
    ProviderPolicyError,
    get_provider_auth_policy,
    provider_auth_scope,
)


@pytest.fixture
def provider_home(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def configure(home, **overrides):
    config = {
        "provider_auth": {"mode": "config_only"},
        "model": {"provider": "custom:proxy", "default": "first-model"},
        "providers": {"proxy": {"base_url": "http://127.0.0.1:4321/v1", "api_mode": "codex_responses"}},
    }
    config.update(overrides)
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    return config


def test_native_runtime_ignores_ambient_routing_and_credentials(provider_home, monkeypatch):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    configure(provider_home)
    monkeypatch.setenv("OPENAI_API_KEY", "foreign-openai-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "foreign-router-token")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://foreign.invalid/v1")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "openrouter")
    for model in ("first-model", "another-model"):
        runtime = resolve_runtime_provider(target_model=model)
        assert runtime["base_url"] == "http://127.0.0.1:4321/v1"
        assert runtime["api_key"] == "no-key-required"
        assert runtime["model"] == model
        assert runtime["api_mode"] == "codex_responses"
        assert runtime["provider_auth_policy"].config_only


def test_literal_local_key_reference_beats_same_ambient_key(provider_home, monkeypatch):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    configure(provider_home, providers={"proxy": {"base_url": "http://proxy.test/v1", "key_env": "PROXY_KEY"}})
    (provider_home / ".env").write_text("PROXY_KEY=local-secret\n")
    monkeypatch.setenv("PROXY_KEY", "foreign-secret")
    runtime = resolve_runtime_provider()
    assert runtime["api_key"] == "local-secret"
    assert "local-secret" not in repr(runtime["provider_auth_policy"])


def test_local_env_interpolation_cannot_import_parent_value(provider_home, monkeypatch):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    configure(provider_home, providers={"proxy": {"base_url": "http://proxy.test/v1", "key_env": "PROXY_KEY"}})
    (provider_home / ".env").write_text("PROXY_KEY=${EXTERNAL_KEY}\n")
    monkeypatch.setenv("EXTERNAL_KEY", "foreign-secret")
    with pytest.raises(ProviderPolicyError, match="literal"):
        resolve_runtime_provider()


@pytest.mark.parametrize("override", [{"explicit_api_key": "foreign-key"}, {"explicit_base_url": "https://elsewhere.invalid"}])
def test_runtime_overrides_cannot_replace_configured_authority(provider_home, override):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    configure(provider_home)
    with pytest.raises(ProviderPolicyError, match="override"):
        resolve_runtime_provider(**override)


def test_unconfigured_and_disabled_providers_are_not_eligible(provider_home, monkeypatch):
    from hermes_cli.auth import is_provider_explicitly_configured, resolve_provider
    from hermes_cli.runtime_provider import resolve_runtime_provider

    configure(provider_home, providers={"proxy": {"base_url": "http://proxy.test/v1"}, "anthropic": {"enabled": False}})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-foreign-token")
    assert not is_provider_explicitly_configured("anthropic")
    for resolver in (lambda: resolve_provider("openrouter"), lambda: resolve_runtime_provider(requested="anthropic")):
        with pytest.raises(ProviderPolicyError, match="not configured"):
            resolver()


def test_empty_strict_config_cannot_discover_provider(provider_home, monkeypatch):
    from hermes_cli.auth import resolve_provider

    configure(provider_home, model={}, providers={})
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-valid-looking")
    with pytest.raises(ProviderPolicyError, match="not configured"):
        resolve_provider()


def test_selected_builtin_uses_only_its_local_env_key(provider_home, monkeypatch):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    configure(provider_home, model={"provider": "anthropic", "default": "claude-model"}, providers={})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "foreign-key")
    (provider_home / ".env").write_text("ANTHROPIC_API_KEY=local-anthropic-key\n")
    runtime = resolve_runtime_provider()
    assert runtime["api_key"] == "local-anthropic-key"
    assert runtime["api_mode"] == "anthropic_messages"


@pytest.mark.parametrize("entry", [{"key_cmd": "must-never-run"}, {"api_mode": "codex_app_server"}])
def test_external_secret_and_process_transports_rejected(provider_home, entry):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    configure(provider_home, providers={"proxy": {"base_url": "http://proxy.test/v1", **entry}})
    with pytest.raises(ProviderPolicyError, match="disabled"):
        resolve_runtime_provider()


def test_local_native_auth_requires_local_provenance_and_selected_provider(provider_home):
    from hermes_cli.auth import get_provider_auth_state

    configure(provider_home, providers={"openai-codex": {}})
    policy = get_provider_auth_policy()
    record = {"tokens": {"access_token": "fake-native-token"}}
    auth_file = provider_home / "auth.json"
    auth_file.write_text(json.dumps({"providers": {"openai-codex": record}}))
    assert get_provider_auth_state("openai-codex") is None
    record["provenance"] = policy.local_provenance()
    auth_file.write_text(json.dumps({"providers": {"openai-codex": record}}))
    assert get_provider_auth_state("openai-codex")["tokens"]["access_token"] == "fake-native-token"
    configure(provider_home)
    assert get_provider_auth_state("openai-codex") is None


def test_symlinked_credential_file_cannot_leave_home(provider_home, tmp_path):
    configure(provider_home)
    foreign = tmp_path / "external.env"
    foreign.write_text("PROXY_KEY=external-secret\n")
    (provider_home / ".env").symlink_to(foreign)
    with pytest.raises(ProviderPolicyError, match="leaves"):
        get_provider_auth_policy()


def test_policy_snapshot_cannot_downgrade_when_config_changes(provider_home):
    configure(provider_home)
    policy = get_provider_auth_policy()
    configure(provider_home, provider_auth={"mode": "auto"})
    with provider_auth_scope(policy):
        assert get_provider_auth_policy().config_only
    assert not get_provider_auth_policy().config_only


def test_unscoped_policy_cache_reloads_rotated_and_removed_credentials(provider_home):
    configure(provider_home)
    env = provider_home / ".env"
    env.write_text("PROXY_KEY=old-key\n")
    initial = get_provider_auth_policy()
    assert get_provider_auth_policy() is initial
    replacement = provider_home / "new.env"
    replacement.write_text("PROXY_KEY=new-key\n")
    replacement.replace(env)
    changed = get_provider_auth_policy()
    assert changed.env_value("PROXY_KEY") == "new-key"
    assert changed.cache_key != initial.cache_key
    env.unlink()
    assert get_provider_auth_policy().env_value("PROXY_KEY") == ""


def test_scoped_policy_keeps_native_auth_paths_in_own_profile(provider_home, tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    from hermes_cli.auth import _auth_file_path

    configure(provider_home)
    policy = get_provider_auth_policy()
    other_home = tmp_path / "other"
    other_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other_home))
    with provider_auth_scope(policy):
        assert get_hermes_home() == provider_home
        assert _auth_file_path() == provider_home / "auth.json"
    assert get_hermes_home() == other_home


def test_concurrent_policy_scopes_remain_independent(provider_home):
    configure(provider_home)
    strict = get_provider_auth_policy()
    automatic = get_provider_auth_policy({"provider_auth": {"mode": "auto"}})

    async def run(policy):
        with provider_auth_scope(policy):
            await asyncio.sleep(0)
            return get_provider_auth_policy().mode

    async def both():
        return await asyncio.gather(run(strict), run(automatic))

    assert asyncio.run(both()) == ["config_only", "auto"]


def test_unknown_policy_is_configuration_error(provider_home):
    from hermes_cli.config import validate_config_structure

    config = configure(provider_home, provider_auth={"mode": "confgi_only"})
    assert any(issue.severity == "error" for issue in validate_config_structure(config))
    with pytest.raises(ProviderPolicyError, match="mode"):
        get_provider_auth_policy()


def test_auto_mode_retains_ambient_discovery(provider_home, monkeypatch):
    from hermes_cli.auth import resolve_provider

    configure(provider_home, provider_auth={"mode": "auto"}, model={}, providers={})
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-ambient")
    assert resolve_provider() == "openrouter"


def test_primary_sdk_cannot_inherit_account_or_header_credentials(provider_home, monkeypatch):
    from types import SimpleNamespace
    import httpx
    from agent.agent_runtime_helpers import create_openai_client

    configure(provider_home, providers={"proxy": {
        "base_url": "http://proxy.test/v1", "api_key": "local-key",
        "extra_headers": {"x-provider-key": "local-header"},
    }})
    monkeypatch.setenv("OPENAI_API_KEY", "foreign-key")
    monkeypatch.setenv("OPENAI_ORG_ID", "foreign-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "foreign-project")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "response", "choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    agent = SimpleNamespace(
        provider="custom", requested_provider="custom:proxy", model="first-model",
        _provider_auth_policy=get_provider_auth_policy(),
        _build_keepalive_http_client=lambda *args, **kwargs: httpx.Client(transport=httpx.MockTransport(respond)),
        _client_log_context=lambda: "test",
    )
    client = create_openai_client(agent, {
        "base_url": "http://proxy.test/v1", "api_key": "local-key",
        "default_headers": {"x-provider-key": "foreign-header", "Authorization": "Bearer foreign-key"},
    }, reason="test", shared=False)
    client.chat.completions.create(model="selected-model", messages=[{"role": "user", "content": "hello"}])
    client.close()
    assert requests[0].headers["authorization"] == "Bearer local-key"
    assert requests[0].headers["x-provider-key"] == "local-header"
    assert "foreign-org" not in str(requests[0].headers)
    assert "foreign-project" not in str(requests[0].headers)
    assert json.loads(requests[0].content)["model"] == "selected-model"


def test_anthropic_sdk_cannot_inherit_opposite_auth_scheme(provider_home, monkeypatch):
    from agent.anthropic_adapter import build_anthropic_client

    configure(provider_home, model={"provider": "anthropic", "default": "claude-model"},
              providers={"anthropic": {"base_url": "https://api.anthropic.com", "api_key": "sk-ant-api-local-token"}})
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "foreign-bearer-token")
    client = build_anthropic_client("sk-ant-api-local-token", "https://api.anthropic.com")
    assert client.api_key == "sk-ant-api-local-token"
    assert not client.auth_token
    assert "foreign-bearer-token" not in str(client.default_headers)
    client.close()


def test_native_agent_chat_and_auxiliary_share_pinned_proxy_authority(provider_home, monkeypatch):
    import httpx
    from run_agent import AIAgent
    from agent.turn_context import _publish_runtime_main
    from agent.auxiliary_client import resolve_provider_client, _RUNTIME_MAIN_CONTEXT

    configure(provider_home, model={"provider": "custom:proxy", "default": "chosen-model", "context_length": 128000},
              providers={"proxy": {"base_url": "http://proxy.test/v1", "api_key": "local-key", "api_mode": "chat_completions"}})
    monkeypatch.setenv("OPENAI_API_KEY", "foreign-openai-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-foreign-router")
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.host == "proxy.test"
        if json.loads(request.content).get("stream"):
            chunk = {"id": "chat-completion", "object": "chat.completion.chunk", "created": 0,
                     "model": "chosen-model", "choices": [{"index": 0, "finish_reason": "stop",
                     "delta": {"role": "assistant", "content": "proxy answer"}}]}
            return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "id": "chat-completion", "object": "chat.completion", "created": 0, "model": "chosen-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "proxy answer"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        })

    monkeypatch.setattr(AIAgent, "_build_keepalive_http_client",
                        lambda self, *args, **kwargs: httpx.Client(transport=httpx.MockTransport(respond)))
    agent = AIAgent(
        api_key="foreign-constructor-key", base_url="https://foreign.invalid/v1", model="chosen-model",
        quiet_mode=True, skip_context_files=True, skip_memory=True, skip_background_review=True,
        enabled_toolsets=[], max_iterations=2,
    )
    assert agent.api_key == "local-key"
    assert agent.base_url == "http://proxy.test/v1"
    assert agent.requested_provider == "custom:proxy"
    # Editing the file after construction cannot weaken this running agent.
    configure(provider_home, provider_auth={"mode": "auto"}, model={}, providers={})
    assert agent.chat("Say hello.") == "proxy answer"
    assert requests and json.loads(requests[-1].content)["model"] == "chosen-model"
    assert all(request.headers["authorization"] == "Bearer local-key" for request in requests)
    context_token = _RUNTIME_MAIN_CONTEXT.set(None)
    try:
        _publish_runtime_main(agent)
        main = _RUNTIME_MAIN_CONTEXT.get()
    finally:
        _RUNTIME_MAIN_CONTEXT.reset(context_token)
    assert main["provider_auth_policy"].config_only
    with provider_auth_scope(main["provider_auth_policy"]):
        auxiliary, model = resolve_provider_client("auto", model="summary-model", main_runtime=main)
        assert str(auxiliary.base_url).rstrip("/") == "http://proxy.test/v1"
        assert model == "summary-model"
        auxiliary.close()
    agent.client.close()


def test_auto_config_symlink_remains_supported_but_strict_cannot_escape(provider_home, tmp_path):
    external = tmp_path / "dotfiles.yaml"
    external.write_text("provider_auth: {mode: auto}\n")
    config_path = provider_home / "config.yaml"
    config_path.unlink(missing_ok=True)
    config_path.symlink_to(external)
    assert get_provider_auth_policy().mode == "auto"
    external.write_text("provider_auth: {mode: config_only}\n")
    with pytest.raises(ProviderPolicyError, match="leaves"):
        get_provider_auth_policy()


@pytest.mark.parametrize("broken", ["provider_auth: [unterminated", "[]", "false", "0"])
def test_corrupt_policy_blocks_credentials_and_recovers_after_repair(provider_home, monkeypatch, broken):
    from hermes_cli.auth import AuthError
    from hermes_cli.runtime_provider import resolve_runtime_provider

    configure(provider_home)
    assert get_provider_auth_policy().config_only
    monkeypatch.setenv("OPENROUTER_API_KEY", "foreign-router-key")
    (provider_home / "config.yaml").write_text(broken)
    # No config load/warning precondition: direct credential and runtime paths
    # must independently refuse an unknown policy, never use ambient secrets.
    for resolve in (get_provider_auth_policy, lambda: resolve_runtime_provider(requested="openrouter")):
        with pytest.raises(AuthError) as error:
            resolve()
        assert error.value.code == "corrupt_config"
    configure(provider_home)
    runtime = resolve_runtime_provider()
    assert runtime["provider_auth_policy"].config_only
    assert runtime["base_url"] == "http://127.0.0.1:4321/v1"
    assert runtime["api_key"] == "no-key-required"


@pytest.mark.linux_only
def test_inaccessible_policy_home_reports_auth_error_and_recovers(provider_home):
    import os

    from hermes_cli.auth_constants import AuthError

    if os.geteuid() == 0:
        pytest.skip("root can read a directory despite chmod(0)")
    configure(provider_home)
    provider_home.chmod(0)
    try:
        with pytest.raises(AuthError) as error:
            get_provider_auth_policy()
        assert error.value.code == "corrupt_config"
    finally:
        provider_home.chmod(0o700)
    assert get_provider_auth_policy().config_only


@pytest.mark.linux_only
@pytest.mark.parametrize("filename", ["config.yaml", ".env"])
def test_policy_symlink_loop_reports_auth_error_and_recovers(provider_home, filename):
    from hermes_cli.auth_constants import AuthError

    configure(provider_home)
    path = provider_home / filename
    path.unlink(missing_ok=True)
    path.symlink_to(path)
    with pytest.raises(AuthError) as error:
        get_provider_auth_policy()
    assert error.value.code == "corrupt_config"
    path.unlink()
    configure(provider_home)
    assert get_provider_auth_policy().config_only
