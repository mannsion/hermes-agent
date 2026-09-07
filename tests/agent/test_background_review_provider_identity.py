"""Named-provider identity survives both inherited and routed review forks."""

from types import SimpleNamespace

import pytest
import yaml

from agent.background_review import _fork_init_kwargs, _resolve_review_runtime
from hermes_cli.provider_policy import get_provider_auth_policy, provider_auth_scope
from hermes_cli.runtime_provider import resolve_runtime_provider


@pytest.mark.parametrize('routed', [False, True])
def test_named_review_provider_reenters_real_config_only_resolver(tmp_path, monkeypatch, routed):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('OPENAI_API_KEY', 'unrelated-shell-key')
    config = {
        'provider_auth': {'mode': 'config_only'},
        'model': {'provider': 'custom:stack', 'default': 'gpt-5.5'},
        'providers': {
            name: {'base_url': f'http://127.0.0.1:{port}/v1', 'api_key': f'{name}-config-key',
                   'api_mode': 'codex_responses', 'default_model': 'gpt-5.5'}
            for name, port in [('stack', 65431), ('review', 65432)]
        },
    }
    (home / 'config.yaml').write_text(yaml.safe_dump(config))
    agent = SimpleNamespace(
        provider='custom', requested_provider='custom:stack', model='gpt-5.5',
        platform='cli', session_id='test-parent', request_overrides={},
        _current_main_runtime=lambda: {'api_key': 'stack-config-key',
            'base_url': 'http://127.0.0.1:65431/v1', 'api_mode': 'codex_responses'},
    )
    task = {'provider': 'custom:review', 'model': 'gpt-5.5'} if routed else {}
    with provider_auth_scope(get_provider_auth_policy(config)):
        runtime = _resolve_review_runtime(agent, task)
        kwargs = _fork_init_kwargs(agent, runtime, runtime['routed'], 3)
        # Same resolver called by AIAgent.__init__; no mock of routing or the policy.
        resolved = resolve_runtime_provider(
            requested=kwargs.get('requested_provider') or kwargs['provider'],
            target_model=kwargs['model'], explicit_api_key=kwargs['api_key'],
            explicit_base_url=kwargs['base_url'],
        )
    name = 'review' if routed else 'stack'
    assert resolved['requested_provider'] == f'custom:{name}'
    assert resolved['api_key'] == f'{name}-config-key'
    assert resolved['base_url'] == config['providers'][name]['base_url']
    assert runtime['routed'] is routed
