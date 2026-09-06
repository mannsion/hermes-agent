"""Inventory uses only configured model catalogs, even with a warm auto cache."""

from pathlib import Path

import pytest
import yaml

from hermes_cli import inventory, models
from hermes_cli.provider_policy import ProviderPolicyError


def test_picker_catalog_ignores_ambient_providers_and_warm_auto_catalog(
    tmp_path, monkeypatch
):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-router-key")
    config = {
        "provider_auth": {"mode": "auto"},
        "model": {"provider": "custom:proxy", "default": "selected-model"},
        "providers": {
            "proxy": {"base_url": "https://proxy.test/v1", "api_key": "local-key"}
        },
    }
    path = home / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    seen = []
    catalog = ["old-auto-model"]

    def get_json(url, **kwargs):
        seen.append((url, kwargs["headers"]))
        return {"data": [{"id": m} for m in catalog]}

    monkeypatch.setattr(models, "_get_json", get_json)
    assert (
        models.cached_fetch_api_models("local-key", "https://proxy.test/v1") == catalog
    )
    config["provider_auth"]["mode"] = "config_only"
    path.write_text(yaml.safe_dump(config))
    catalog = ["first-model", "second-model"]
    payload = inventory.build_model_options_payload(
        inventory.load_picker_context(), refresh=True, include_unconfigured=True
    )
    assert [r["slug"] for r in payload["providers"]] == ["custom:proxy"]
    assert payload["providers"][0]["models"] == catalog
    assert all(url == "https://proxy.test/v1/models" for url, _ in seen)
    assert all(headers["Authorization"] == "Bearer local-key" for _, headers in seen)
    assert models.cached_provider_model_ids("custom:proxy") == catalog
    with pytest.raises(ProviderPolicyError, match="not configured"):
        models.cached_provider_model_ids("openrouter")
    with pytest.raises(ProviderPolicyError, match="not configured"):
        models.probe_api_models("ambient-key", "https://foreign.test/v1")

    catalog = []
    empty = inventory.build_model_options_payload(
        inventory.load_picker_context(), refresh=True
    )
    assert empty["providers"][0]["models"] == []
    assert models.cached_provider_model_ids("custom:proxy") == []


def test_codex_catalog_never_uses_another_app_cache_when_local_request_fails(
    tmp_path, monkeypatch
):
    from hermes_cli.codex_models import get_codex_model_ids

    home = tmp_path / "hermes"
    home.mkdir()
    external = tmp_path / "codex"
    external.mkdir()
    (external / "config.toml").write_text('model = "external-model"\n')
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(external))
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "provider_auth": {"mode": "config_only"},
            "model": {"provider": "openai-codex", "default": "selected-model"},
            "providers": {"openai-codex": {"api_key": "local-token"}},
        })
    )
    reads = []
    read_text = Path.read_text

    def recorded_read(path, *args, **kwargs):
        reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", recorded_read)
    requests = []

    def failed_request(url, **kwargs):
        requests.append((url, kwargs["headers"]["Authorization"]))
        raise OSError("offline fixture")

    monkeypatch.setattr(models, "_get_json", failed_request)
    assert get_codex_model_ids(access_token="ambient-token") == []
    assert requests == [
        (
            "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0",
            "Bearer local-token",
        )
    ]
    assert not any(path.is_relative_to(external) for path in reads)
