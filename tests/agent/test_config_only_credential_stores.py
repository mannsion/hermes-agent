"""Configuration-only source isolation using native stores and resolvers."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli.provider_policy import get_provider_auth_policy, provider_auth_scope, ProviderPolicyError


@pytest.fixture
def profile(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def configure(home, *providers, dotenv=""):
    config = {"provider_auth": {"mode": "config_only"},
              "providers": {name: {} for name in providers}}
    (home / "config.yaml").write_text(json.dumps(config))
    (home / ".env").write_text(dotenv)
    return get_provider_auth_policy()


def forbidden(*args, **kwargs):
    pytest.fail("external credential source was consulted")


def test_pool_rotates_only_local_accounts_and_never_resurrects_removed_env(profile, monkeypatch):
    from agent.credential_pool import load_pool

    policy = configure(profile, "openrouter", dotenv="OPENROUTER_API_KEY=local-env\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-key")
    entries = [
        {"id": "legacy", "source": "manual", "access_token": "unknown-key"},
        {"id": "foreign", "source": "manual", "access_token": "foreign-key",
         "provenance": {"source": "local_key", "home": "/other/profile"}},
        {"id": "a", "source": "manual", "access_token": "local-a",
         "provenance": policy.local_provenance("local_key")},
        {"id": "b", "source": "manual", "access_token": "local-b", "priority": 1,
         "provenance": policy.local_provenance("local_key")},
    ]
    (profile / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"openrouter": entries}}))
    with provider_auth_scope(policy):
        pool = load_pool("openrouter")
        assert {entry.access_token for entry in pool.entries()} == {"local-a", "local-b", "local-env"}
        assert pool.select().access_token == "local-a"
        assert pool.mark_exhausted_and_rotate(status_code=401, api_key_hint="local-a").access_token == "local-b"
        assert pool.mark_exhausted_and_rotate(status_code=401, api_key_hint="local-b").access_token == "local-env"
        assert pool.mark_exhausted_and_rotate(status_code=401, api_key_hint="local-env") is None
    # A previous seed on disk cannot survive removal from the allowed .env.
    (profile / ".env").write_text("")
    assert "local-env" not in {entry.access_token for entry in load_pool("openrouter").entries()}


def test_copilot_ignores_ambient_and_warm_external_cache(profile, monkeypatch):
    from hermes_cli import copilot_auth

    policy = configure(profile, "copilot", dotenv="COPILOT_GITHUB_TOKEN=gho_local\n")
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_ambient")
    monkeypatch.setattr(copilot_auth, "_gh_cli_token_cache", (time.monotonic(), "gho_cached"))
    monkeypatch.setattr(copilot_auth.subprocess, "run", forbidden)
    with provider_auth_scope(policy):
        assert copilot_auth.resolve_copilot_token() == ("gho_local", "COPILOT_GITHUB_TOKEN")
        assert copilot_auth._try_gh_cli_token() is None
        assert copilot_auth._probe_gh_cli_token() is None
    (profile / ".env").write_text("")
    assert copilot_auth.resolve_copilot_token() == ("", "")


def test_external_oauth_recovery_sources_are_not_read(profile, monkeypatch):
    from agent import anthropic_credentials as anthropic
    from hermes_cli import auth_codex, auth_nous, auth_qwen

    configure(profile, "anthropic", "openai-codex", "nous", "qwen-oauth")
    monkeypatch.setattr(anthropic.subprocess, "run", forbidden)
    monkeypatch.setattr(auth_nous, "_nous_shared_store_path", forbidden)
    assert anthropic.read_claude_code_credentials() is None
    assert anthropic._read_claude_code_credentials_from_keychain() is None
    assert anthropic._read_claude_code_credentials_from_file() is None
    assert anthropic._refresh_oauth_token({"refreshToken": "foreign"}) is None
    assert auth_codex._import_codex_cli_tokens() is None
    assert auth_codex._recover_codex_tokens_from_cli("rejected token") is None
    assert auth_nous._read_shared_nous_state() is None
    assert auth_nous._try_import_shared_nous_state() is None
    assert auth_nous._merge_shared_nous_oauth_state({"access_token": "local"}) is False
    auth_nous._write_shared_nous_state({"access_token": "local"})
    auth_nous._clear_shared_nous_state("failure")
    with auth_nous._nous_shared_store_lock():
        pass
    with pytest.raises(ProviderPolicyError, match="Qwen CLI"):
        auth_qwen.resolve_qwen_runtime_credentials()
    with pytest.raises(ProviderPolicyError, match="Qwen CLI"):
        auth_qwen._refresh_qwen_cli_tokens({"refresh_token": "external"})


def test_native_anthropic_file_requires_provenance_and_refresh_preserves_it(profile, monkeypatch):
    from agent import anthropic_credentials as anthropic
    from agent.credential_pool import load_pool

    policy = configure(profile, "anthropic")
    path = profile / ".anthropic_oauth.json"
    record = {"accessToken": "sk-ant-oat-local", "refreshToken": "local-refresh", "expiresAt": 1}
    path.write_text(json.dumps(record))
    assert anthropic.read_hermes_oauth_credentials() is None
    record["provenance"] = policy.local_provenance("local_login")
    path.write_text(json.dumps(record))
    monkeypatch.setattr(anthropic, "_post_oauth_token", lambda *a, **k: {
        "access_token": "sk-ant-oat-rotated", "refresh_token": "rotated-refresh", "expires_in": 3600,
    })
    monkeypatch.setattr(anthropic.subprocess, "run", forbidden)
    pool = load_pool("anthropic")
    assert pool.select().access_token == "sk-ant-oat-rotated"
    saved = json.loads(path.read_text())
    assert saved["provenance"] == record["provenance"]
    assert saved["refreshToken"] == "rotated-refresh"


def test_local_token_file_symlink_cannot_leave_active_home(profile, tmp_path):
    from agent.anthropic_credentials import read_hermes_oauth_credentials

    configure(profile, "anthropic")
    target = tmp_path / "foreign.json"
    target.write_text('{"accessToken":"foreign"}')
    (profile / ".anthropic_oauth.json").symlink_to(target)
    with pytest.raises(ProviderPolicyError, match="leaves"):
        read_hermes_oauth_credentials()


def test_codex_local_refresh_keeps_provenance_and_failure_cannot_import_cli(profile, monkeypatch):
    import httpx
    from hermes_cli import auth, auth_codex

    policy = configure(profile, "openai-codex")
    record = {"tokens": {"access_token": "local-access", "refresh_token": "local-refresh"},
              "provenance": policy.local_provenance("local_login")}
    path = profile / "auth.json"
    path.write_text(json.dumps({"version": 1, "providers": {"openai-codex": record}}))
    requests = []

    def refresh(request):
        requests.append(request)
        assert request.url.host == "auth.openai.com"
        return httpx.Response(200, json={"access_token": "refreshed-access", "refresh_token": "refreshed-refresh"})

    monkeypatch.setattr(auth_codex, "_codex_http_client", lambda **kwargs: httpx.Client(transport=httpx.MockTransport(refresh)))
    monkeypatch.setenv("HERMES_CODEX_BASE_URL", "https://ambient.invalid")
    result = auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert result["api_key"] == "refreshed-access"
    assert "ambient.invalid" not in result["base_url"]
    assert json.loads(path.read_text())["providers"]["openai-codex"]["provenance"] == record["provenance"]
    assert len(requests) == 1

    monkeypatch.setattr(auth, "_import_codex_cli_tokens", forbidden)
    monkeypatch.setattr(auth_codex, "_codex_http_client", lambda **kwargs: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "invalid_grant"}))))
    with pytest.raises(auth.AuthError, match="invalid_grant|refresh"):
        auth.resolve_codex_runtime_credentials(force_refresh=True)


def test_auto_cli_adoption_does_not_launder_old_local_provenance(profile, monkeypatch, tmp_path):
    from hermes_cli import auth, auth_codex

    policy = configure(profile, "openai-codex")
    record = {"tokens": {"access_token": "local-access", "refresh_token": "local-refresh"},
              "provenance": policy.local_provenance("local_login")}
    row = {"id": "singleton", "source": "device_code", "access_token": "local-access",
           "refresh_token": "local-refresh", "provenance": record["provenance"]}
    path = profile / "auth.json"
    path.write_text(json.dumps({"version": 1, "providers": {"openai-codex": record},
                               "credential_pool": {"openai-codex": [row]}}))
    (profile / "config.yaml").write_text('{"model":{"provider":"openai-codex"}}')
    cli_home = tmp_path / "codex"
    cli_home.mkdir()
    (cli_home / "auth.json").write_text(json.dumps({"tokens": {
        "access_token": "foreign-access", "refresh_token": "foreign-refresh"}}))
    monkeypatch.setenv("CODEX_HOME", str(cli_home))
    assert auth_codex._recover_codex_tokens_from_cli("test")["access_token"] == "foreign-access"
    saved = json.loads(path.read_text())
    assert saved["providers"]["openai-codex"]["provenance"]["source"] == "external_store"
    assert saved["credential_pool"]["openai-codex"][0]["provenance"]["source"] == "external_store"
    configure(profile, "openai-codex")
    with pytest.raises((auth.AuthError, ProviderPolicyError)):
        auth.resolve_codex_runtime_credentials(refresh_if_expiring=False)


def test_pool_retains_strict_policy_after_context_exit_and_rejects_foreign_refresh(profile, monkeypatch):
    from agent.credential_pool import CredentialPool, PooledCredential
    from agent import anthropic_credentials

    policy = configure(profile, "anthropic")
    foreign = PooledCredential(provider="anthropic", id="foreign", label="foreign", auth_type="oauth",
                               priority=0, source="claude_code", access_token="foreign-access", refresh_token="foreign-refresh")
    with provider_auth_scope(policy):
        pool = CredentialPool("anthropic", [foreign])
    (profile / "config.yaml").write_text("{}")
    monkeypatch.setattr(anthropic_credentials, "_post_oauth_token", forbidden)
    assert pool.select() is None
    with pytest.raises(ProviderPolicyError, match="saved in this Hermes home"):
        pool._refresh_entry(foreign, force=True)


@pytest.mark.parametrize("provider", ["minimax-oauth", "xai-oauth"])
def test_native_device_login_establishes_local_provenance_and_refresh_preserves_it(profile, monkeypatch, provider):
    import httpx
    from urllib.parse import parse_qs
    from hermes_cli import auth, auth_minimax, auth_xai
    from hermes_cli.auth_constants import XAI_OAUTH_ISSUER

    policy = configure(profile, provider)
    grants = []

    def send(client, request, **kwargs):
        fields = parse_qs(request.content.decode())
        if request.url.path.endswith("openid-configuration"):
            payload = {"authorization_endpoint": f"{XAI_OAUTH_ISSUER}/oauth2/authorize",
                       "token_endpoint": f"{XAI_OAUTH_ISSUER}/oauth2/token"}
        elif request.url.path.endswith("/code"):
            payload = {"device_code": "fixture-device", "user_code": "fixture-user",
                       "verification_uri": "https://example.invalid/device",
                       "verification_uri_complete": "https://example.invalid/device?code=fixture", "expires_in": 30,
                       "expired_in": 30, "interval": 0, "state": fields.get("state", [""])[0]}
        else:
            grants.append(fields["grant_type"][0])
            suffix = "refreshed" if grants[-1] == "refresh_token" else "login"
            payload = {"status": "success", "access_token": f"access-{suffix}",
                       "refresh_token": f"refresh-{suffix}", "expires_in": 3600, "expired_in": 3600}
        return httpx.Response(200, json=payload, request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setattr(auth, "_print_device_code_instructions", lambda *args, **kwargs: None)
    if provider == "minimax-oauth":
        fresh = auth_minimax._minimax_oauth_login(open_browser=False)
        refreshed = auth_minimax._refresh_minimax_oauth_state(fresh, force=True)
        assert refreshed["provenance"] == fresh["provenance"]
    else:
        monkeypatch.setenv("HERMES_XAI_BASE_URL", "https://api.x.ai/v1/ambient")
        fresh = auth_xai._xai_oauth_device_code_login(open_browser=False)
        auth_xai._save_xai_oauth_tokens(fresh["tokens"], discovery=fresh["discovery"], provenance=fresh["provenance"])
        refreshed = auth_xai.resolve_xai_oauth_runtime_credentials(force_refresh=True)
        assert "ambient" not in refreshed["base_url"]
        assert refreshed["api_key"] == "access-refreshed"
    saved = json.loads((profile / "auth.json").read_text())["providers"][provider]
    assert saved["provenance"] == policy.local_provenance("local_login")
    assert fresh["provenance"] == saved["provenance"]
    assert grants[-1] == "refresh_token"
