"""Native auxiliary calls obey the active home's provider authority."""

import json
from pathlib import Path

import httpx
import pytest
import yaml

from agent import auxiliary_client as aux
from hermes_cli.provider_policy import get_provider_auth_policy, ProviderPolicyError


@pytest.fixture
def strict_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in (
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
    ):
        monkeypatch.setenv(name, "ambient-must-not-be-used")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "ambient-model")
    config = {
        "provider_auth": {"mode": "config_only"},
        "model": {"provider": "custom:proxy", "default": "first-model"},
        "providers": {
            "proxy": {
                "base_url": "https://proxy.test/v1",
                "api_key": "local-key",
                "api_mode": "chat_completions",
                "default_model": "first-model",
            }
        },
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    aux.shutdown_cached_clients()
    aux.clear_runtime_main()
    yield home, config
    aux.shutdown_cached_clients()
    aux.clear_runtime_main()


def wire(monkeypatch, handler):
    from agent import process_bootstrap

    def build(base_url, *, async_mode=False, **kwargs):
        transport = httpx.MockTransport(handler)
        cls = httpx.AsyncClient if async_mode else httpx.Client
        return cls(transport=transport, trust_env=False)

    monkeypatch.setattr(process_bootstrap, "build_keepalive_http_client", build)


def answer(request, text="okay"):
    model = json.loads(request.content)["model"]
    return httpx.Response(
        200,
        json={
            "id": "local",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": text},
                }
            ],
        },
    )


def test_native_aux_calls_and_failed_recovery_never_discover_other_credentials(
    strict_home, monkeypatch
):
    seen = []
    fail = False

    def handler(request):
        seen.append(request)
        if fail:
            return httpx.Response(
                402,
                json={
                    "error": {
                        "message": "insufficient credits",
                        "type": "billing_error",
                    }
                },
            )
        return answer(request)

    wire(monkeypatch, handler)
    import subprocess

    run = subprocess.run

    def guarded_run(command, *args, **kwargs):
        if any(str(part) in {"gh", "op", "bw", "security"} for part in command):
            pytest.fail("external credential process")
        return run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    for task, model in [("compression", "first-model"), ("vision", "second-model")]:
        result = aux.call_llm(
            task, model=model, messages=[{"role": "user", "content": "hello"}]
        )
        assert result.choices[0].message.content == "okay"
    assert [json.loads(r.content)["model"] for r in seen] == [
        "first-model",
        "second-model",
    ]
    assert {str(r.url) for r in seen} == {"https://proxy.test/v1/chat/completions"}
    assert all(r.headers["authorization"] == "Bearer local-key" for r in seen)
    fail = True
    with pytest.raises(Exception, match="insufficient credits"):
        aux.call_llm(
            "compression",
            model="first-model",
            messages=[{"role": "user", "content": "hello"}],
        )
    assert {r.url.host for r in seen} == {"proxy.test"}
    with pytest.raises(ProviderPolicyError, match="not configured"):
        aux.resolve_provider_client("openrouter", "foreign-model")


def test_configured_auxiliary_fallback_is_permitted(strict_home, monkeypatch):
    home, config = strict_home
    config["providers"]["backup"] = {
        "base_url": "https://backup.test/v1",
        "api_key": "backup-key",
    }
    config["auxiliary"] = {
        "compression": {
            "fallback_chain": [{"provider": "custom:backup", "model": "backup-model"}]
        }
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    seen = []

    def handler(request):
        seen.append(request)
        return (
            httpx.Response(402, json={"error": {"message": "insufficient credits"}})
            if request.url.host == "proxy.test"
            else answer(request)
        )

    wire(monkeypatch, handler)
    result = aux.call_llm(
        "compression", messages=[{"role": "user", "content": "hello"}]
    )
    assert result.model == "backup-model"
    assert {r.url.host for r in seen} == {"proxy.test", "backup.test"}
    assert seen[-1].headers["authorization"] == "Bearer backup-key"


def test_policy_stays_pinned_after_disk_mode_changes_and_separates_cache(
    strict_home, monkeypatch
):
    home, config = strict_home
    policy = get_provider_auth_policy()
    wire(monkeypatch, answer)
    token = aux.set_runtime_main(
        "custom",
        "first-model",
        requested_provider="custom:proxy",
        provider_auth_policy=policy,
    )
    strict_key = aux._client_cache_key("auto", async_mode=False)
    config["provider_auth"]["mode"] = "auto"
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    client, model = aux.resolve_provider_client("auto")
    assert str(client.base_url) == "https://proxy.test/v1/"
    assert client.api_key == "local-key"
    aux.reset_runtime_main(token)
    assert aux._client_cache_key("auto", async_mode=False) != strict_key


def test_responses_route_preserves_native_adapter_and_model_switching(
    strict_home, monkeypatch
):
    home, config = strict_home
    config["providers"]["proxy"]["api_mode"] = "codex_responses"
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    seen = []

    def handler(request):
        seen.append(request)
        model = json.loads(request.content)["model"]
        response = {
            "id": "response-local",
            "object": "response",
            "model": model,
            "status": "completed",
            "output": [
                {
                    "id": "msg-local",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "response okay",
                            "annotations": [],
                        }
                    ],
                }
            ],
        }
        events = [
            {
                "type": "response.output_item.done",
                "item": response["output"][0],
                "output_index": 0,
                "sequence_number": 1,
            },
            {"type": "response.completed", "response": response, "sequence_number": 2},
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(f"data: {json.dumps(event)}\n\n" for event in events),
        )

    wire(monkeypatch, handler)
    for model in ("first-model", "second-model"):
        result = aux.call_llm(
            "compression", model=model, messages=[{"role": "user", "content": "hello"}]
        )
        assert result.choices[0].message.content == "response okay"
        assert result.model == model
    assert [str(r.url) for r in seen] == ["https://proxy.test/v1/responses"] * 2
    assert [json.loads(r.content)["model"] for r in seen] == [
        "first-model",
        "second-model",
    ]


def test_removing_local_auth_store_invalidates_cached_auxiliary_credentials(
    strict_home, monkeypatch
):
    home, config = strict_home
    config["providers"] = {"openrouter": {}}
    config["model"]["provider"] = "openrouter"
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    policy = get_provider_auth_policy()
    path = home / "auth.json"
    path.write_text(
        json.dumps({
            "version": 1,
            "credential_pool": {
                "openrouter": [
                    {
                        "id": "local",
                        "source": "manual",
                        "access_token": "local-key",
                        "provenance": policy.local_provenance("local_key"),
                    }
                ]
            },
        })
    )
    wire(monkeypatch, answer)
    client, _ = aux._get_cached_client("openrouter", "first-model")
    assert client.api_key == "local-key"
    path.unlink()
    with pytest.raises(ProviderPolicyError, match="no permitted local credentials"):
        aux._get_cached_client("openrouter", "first-model")
