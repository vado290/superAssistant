from dataclasses import replace
from types import SimpleNamespace
import json

import pytest
import yaml

from assistant_kernel.core import models, model_plan, model_secrets, model_gateway
from assistant_kernel.core.agents import AgentConfig
from assistant_kernel.core.prompt_cache import from_prompt, merge_results
from assistant_kernel.harness.base import AgentTurnResult
from assistant_kernel.harness.context_budget import TurnUsage
from assistant_kernel.model_providers.chat import CatalogChatProvider, ChatResult, PromptCachePolicy


@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(data_root=tmp_path, provider_secrets_path=tmp_path / 'config/provider-secrets.json',
                           shared_volcengine_path=tmp_path / 'shared.json', default_codex_provider='openai',
                           default_codex_model=None, default_codex_reasoning_effort=None)


def configure(settings, data):
    path = models.path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


def agent():
    return AgentConfig('pm', 'project:p:manager', 'manager', 'role', None, None, None, 'max',
                       None, None, 60, (), 'sha')


def test_v1_and_v2_route_priority(settings):
    configure(settings, {'schema_version': 'kernel-models.v1', 'profiles': {'strong': {
        'engine': 'codex', 'provider': 'openai', 'model': 'old', 'reasoning_effort': 'high'}},
        'default_profile': 'strong'})
    assert models.resolve(settings, 'pm', agent())[0].model == 'old'
    data = models.load(settings)
    data.update(schema_version='kernel-models.v2', routes={'pm.design': 'agent.quality', 'pm': 'agent.standard'},
                projects={'p': {'purposes': {'write': 'agent.economy'}}})
    configure(settings, data)
    assert models.resolve(settings, 'pm', agent(), stage='design')[0].model == 'gpt-6-astra'
    assert models.resolve(settings, 'pm', agent(), stage='build')[0].model == 'gpt-6.1-sol'
    assert models.resolve(settings, 'pm', agent(), project_id='p', purpose='write')[0].reasoning_effort == 'medium'
    assert models.resolve(settings, 'pm', replace(agent(), model='exception'), stage='design')[0].model == 'exception'


@pytest.mark.asyncio
async def test_custom_profile_reaches_actual_provider_request(settings, monkeypatch):
    configure(settings, {'schema_version': 'kernel-models.v2', 'profiles': {'cheap': {
        'capability': 'chat', 'vendor': 'custom_openai', 'model': 'foreign-small',
        'base_url': 'https://example.invalid/v1'}}, 'projects': {'p': {'purposes': {'summarize': 'cheap'}}}})
    model_secrets.put(settings, 'custom_openai', 'test-secret', validated=True, base_url='https://example.invalid/v1')
    seen = {}
    async def chat(self, prompt, model, **kwargs):
        seen.update(provider=self.provider_name, model=model, endpoint=self._base_url(), prompt=prompt)
        return ChatResult(provider=self.provider_name, model=model, text='done')
    monkeypatch.setattr(CatalogChatProvider, 'chat', chat)
    result = await model_gateway.call(settings, 'p', 'summarize', 'chat', {'prompt': 'hello'})
    assert seen == {'provider': 'custom_openai', 'model': 'foreign-small', 'endpoint': 'https://example.invalid/v1', 'prompt': 'hello'}
    assert result['route']['profile'] == 'cheap'
    assert 'test-secret' not in json.dumps(result)
    with pytest.raises(ValueError, match='released binding'):
        await model_gateway.call(settings, 'p', 'other', 'chat', {'prompt': 'hello'})


@pytest.mark.asyncio
async def test_product_cache_key_tracks_stable_prompt_not_dynamic_input(settings, monkeypatch):
    configure(settings, {'schema_version': 'kernel-models.v2', 'profiles': {'cheap': {
        'capability': 'chat', 'vendor': 'custom_openai', 'model': 'small',
        'protocol': 'openai_responses', 'base_url': 'https://example.invalid/v1'}},
        'projects': {'p': {'purposes': {'summarize': 'cheap', 'other': 'cheap'}}}})
    model_secrets.put(settings, 'custom_openai', 'secret', validated=True, base_url='https://example.invalid/v1')
    policies = []
    async def chat(self, prompt, model, **kwargs):
        policies.append(kwargs['cache_policy'])
        return ChatResult(provider=self.provider_name, model=model, text='done')
    monkeypatch.setattr(CatalogChatProvider, 'chat', chat)
    for purpose, stable, dynamic in [('summarize', 'rules', 'one'), ('summarize', 'rules', 'two'),
                                     ('summarize', 'new rules', 'two'), ('other', 'rules', 'one'),
                                     ('summarize', '', 'one')]:
        await model_gateway.call(settings, 'p', purpose, 'chat', {'system_prompt': stable, 'prompt': dynamic})
    assert policies[0].enabled and policies[0].cache_key == policies[1].cache_key
    assert policies[0].cache_key != policies[2].cache_key
    assert policies[0].cache_key != policies[3].cache_key
    assert not policies[4].enabled


def test_release_binding_is_frozen_and_idempotent(settings):
    configure(settings, {'schema_version': 'kernel-models.v2'})
    task = {'project_id': 'p', 'track': 'full', 'documents': {'solution_design': {'sha256': 'doc',
        'model_plan': [{'purpose': 'write', 'profile': 'chat.economy', 'capability': 'chat'}]}}}
    task['approvals'] = {'release': {'model_bindings': model_plan.release_bindings(settings, task)}}
    assert not models.load(settings).get('projects')
    first = model_plan.activate_release(settings, task, {'run_id': 'r1'})
    second = model_plan.activate_release(settings, task, {'run_id': 'r1'})
    assert first == second
    data = models.load(settings)
    data['profiles'] = {'chat.economy': {'capability': 'chat', 'vendor': 'dashscope', 'model': 'changed'}}
    configure(settings, data)
    with pytest.raises(ValueError, match='changed'):
        model_plan.activate_release(settings, task, {'run_id': 'r2'})
    assert models.load(settings)['projects']['p']['purposes']['write'] == 'chat.economy'


def test_unknown_usage_anthropic_denominator_and_resume():
    unknown = CatalogChatProvider.normalize_usage({})
    assert unknown['input_tokens'] is None and unknown['cache_hit'] is None
    result = CatalogChatProvider.normalize_usage({'input_tokens': 20, 'cache_read_input_tokens': 60,
        'cache_creation_input_tokens': 20, 'output_tokens': 3}, protocol='anthropic')
    assert result['input_tokens'] == 100 and result['cache_hit_ratio'] == .6
    assert result['raw_usage']['input_tokens'] == 20
    assert from_prompt('delta', 'full-prefix', session_reused=True)['prompt_cache']['stable_prefix_match'] is None
    assert CatalogChatProvider._cache_protocol(PromptCachePolicy(True, 'key', 'anthropic'), 'openai_compatible', {}) == 'none'


def test_repair_usage_preserves_unknown_and_deduplicates():
    def result(key, usage):
        return AgentTurnResult('s', TurnUsage(input_tokens=3), [], '{}', metadata={
            'invocations': [{'invocation_id': key, 'usage': usage}]})
    first = result('a', {'input_tokens': 100, 'cached_input_tokens': 70, 'output_tokens': 5})
    second = result('b', {'input_tokens': 20, 'cached_input_tokens': 10, 'output_tokens': 2})
    merged = merge_results(first, second)
    assert merged.metadata['run_usage']['input_tokens'] == 120
    assert len(merge_results(merged, second).metadata['invocations']) == 2
    unknown = merge_results(merged, result('c', {}))
    assert unknown.metadata['run_usage']['input_tokens'] is None
    assert unknown.metadata['run_usage']['complete'] is False


def test_configured_regional_endpoint_precedes_catalog(settings):
    model_secrets.put(settings, 'dashscope', 'key', validated=True, base_url='https://region.example.invalid/v1')
    assert models.route(settings, 'chat', profile='chat.economy')['base_url'] == 'https://region.example.invalid/v1'
    configure(settings, {'schema_version': 'kernel-models.v2', 'providers': {'dashscope': {'base_url': 'https://explicit.example.invalid/v1'}}})
    assert models.route(settings, 'chat', profile='chat.economy')['base_url'] == 'https://explicit.example.invalid/v1'


def test_plain_setup_reuses_key_without_rotating_shared_provider(settings, monkeypatch):
    model_secrets.put(settings, 'dashscope', 'original', validated=True)
    selected = models.route(settings, 'chat', profile='chat.economy')
    model_secrets.capability_evidence(settings, selected, success=True)
    monkeypatch.setattr(model_secrets, 'probe', lambda *a, **kw: pytest.fail('existing credentials need no probe'))
    assert 'new provider instance' in model_secrets.apply_setup_answer(settings, {}, 'replacement')
    assert 'reused' in model_secrets.apply_setup_answer(settings, {}, 'original')
    assert model_secrets.credential(settings, 'dashscope').api_key.get_secret_value() == 'original'
    assert model_secrets.capability_evidence(settings, selected)


def test_key_rotation_preserves_protocol_and_region(settings):
    model_secrets.put(settings, 'custom_openai', 'first', validated=True, base_url='https://example.invalid')
    path = settings.provider_secrets_path
    data = json.loads(path.read_text())
    data['providers']['custom_openai'].update(provider_type='anthropic_native', region='custom', native_base_url='https://native.invalid')
    path.write_text(json.dumps(data))
    model_secrets.put(settings, 'custom_openai', 'second', validated=False, base_url='https://example.invalid')
    current = model_secrets.credential(settings, 'custom_openai')
    assert current.provider_type == 'anthropic_native' and current.region == 'custom'
    assert current.native_base_url == 'https://native.invalid'


def test_unimplemented_tts_does_not_pass_real_gate(settings):
    model_secrets.put(settings, 'dashscope', 'secret', validated=True)
    rows = model_plan.parse('```yaml\nmodel_plan:\n- {id: M1, purpose: speak, capability: tts}\n```', settings)
    task = {'kind': 'delivery', 'stage': 'review', 'track': 'full', 'documents': {'solution_design': {'model_plan': rows}}}
    assert 'release' in model_plan.gate_errors(settings, task, ['release'])


def test_cumulative_usage_delta_and_first_binding(settings):
    from assistant_kernel.core.prompt_cache import cumulative_delta
    assert cumulative_delta({'input_tokens': 120}, {'input_tokens': 100})['input_tokens'] == 20
    assert cumulative_delta({'input_tokens': 120}, None)['input_tokens'] is None
    assert cumulative_delta({'input_tokens': 80}, {'input_tokens': 100})['input_tokens'] is None
    assert cumulative_delta({'input_tokens': 120}, None, fresh=True)['input_tokens'] == 120
    models.bind(settings, 'p', {'write': 'chat.economy'})
    assert models.load(settings)['schema_version'] == 'kernel-models.v2'


def test_candidate_setup_cannot_rotate_active_provider(settings, monkeypatch):
    model_secrets.put(settings, 'custom_openai', 'original', validated=True, base_url='https://example.invalid/v1')
    answer = json.dumps({'provider': 'custom_openai', 'model': 'small', 'api_key': 'replacement',
                         'base_url': 'https://example.invalid/v1'})
    result = model_secrets.apply_setup_answer(settings, {'capability': 'chat'}, answer)
    assert 'new provider instance' in result
    assert model_secrets.credential(settings, 'custom_openai').api_key.get_secret_value() == 'original'
