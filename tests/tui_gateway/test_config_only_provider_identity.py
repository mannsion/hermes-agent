"""Desktop agent construction preserves the configured provider identity."""

import pytest
import yaml


@pytest.mark.parametrize("selected", ["stack", "alternate"])
def test_named_provider_survives_desktop_agent_construction(tmp_path, monkeypatch, selected):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-shell-key")
    config = {
        "provider_auth": {"mode": "config_only"},
        "model": {"provider": "custom:stack", "default": "test-model"},
        "providers": {
            name: {
                "base_url": f"http://127.0.0.1:{port}/v1",
                "api_key": f"{name}-config-key",
                "api_mode": "codex_responses",
                "context_length": 32768,
            }
            for name, port in [("stack", 65431), ("alternate", 65432)]
        },
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config))

    from hermes_cli.provider_policy import get_provider_auth_policy, provider_auth_scope
    from hermes_state import SessionDB
    from tui_gateway import server

    # Tool discovery is unrelated to the real Desktop -> AIAgent -> auth path.
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda platform: [])
    override = {"model": "test-model", "provider": f"custom:{selected}"} if selected != "stack" else None
    db = SessionDB(db_path=home / "state.db")
    try:
        with provider_auth_scope(get_provider_auth_policy(config)):
            agent = server._make_agent(
                "named-provider-test", "named-provider-test", session_db=db,
                model_override=override, platform_override="gui",
            )
            try:
                assert agent.provider == "custom"
                assert agent.requested_provider == f"custom:{selected}"
                assert agent.base_url == config["providers"][selected]["base_url"]
                assert agent.api_key == f"{selected}-config-key"
            finally:
                agent.close()
    finally:
        db.close()
