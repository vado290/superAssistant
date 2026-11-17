import json
import yaml
import pytest

from assistant_kernel.core import models, model_secrets
from .sim import next_step


def configure(settings):
    path = models.path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({'schema_version': 'kernel-models.v2',
        'projects': {'p1': {'purposes': {'brief': 'chat.economy'}}}}))
    model_secrets.put(settings, 'dashscope', 'test-key', validated=True)


@pytest.mark.parametrize('action,expected', [('approve', 'chat.quality'), ('reject', 'chat.economy')])
async def test_model_change_preserves_active_task(kernel, kernel_settings, sim, action, expected):
    configure(kernel_settings)
    model_secrets.capability_evidence(kernel_settings, models.route(kernel_settings, 'chat', profile='chat.quality'), success=True)
    received = await kernel.receive('p1', role='pm', text='Build my report')
    sim.script('supervisor:evaluate', next_step('requirements'))
    await sim.drain()
    before = await sim.task(received['task_id'])
    await kernel.receive('p1', role='supervisor', text='Use a better model for the brief')
    sim.script('supervisor:consult', {'reply': 'I propose a change', 'propose_model_change': {
        'scope': 'project', 'purpose': 'brief', 'profile': 'chat.quality', 'reason': 'Improve accuracy'}})
    await sim.drain()
    item = next(i for i in (await kernel.card('p1'))['items'] if i['content'].get('decision') == 'model_change')
    await kernel.act('p1', item['item_id'], action)
    after = await sim.task(received['task_id'])
    for key in ('state', 'stage', 'cursor', 'documents', 'approvals'):
        if key in before:
            assert after[key] == before[key]
    assert models.load(kernel_settings)['projects']['p1']['purposes']['brief'] == expected
    await sim.assert_consistent()


async def test_unverified_operation_target_keeps_existing_route(kernel, kernel_settings, sim):
    configure(kernel_settings)
    await kernel.receive('p1', role='supervisor', text='Change the brief model')
    sim.script('supervisor:consult', {'reply': 'Check target', 'propose_model_change': {
        'scope': 'project', 'purpose': 'brief', 'profile': 'chat.quality', 'reason': 'Accuracy'}})
    await sim.drain()
    assert not any(i.get('content', {}).get('decision') == 'model_change'
                   for i in (await kernel.card('p1') or {}).get('items', []))
    assert models.load(kernel_settings)['projects']['p1']['purposes']['brief'] == 'chat.economy'
    await sim.assert_consistent()


async def test_operation_setup_hides_key_and_resumes_without_activating(kernel, kernel_settings, sim, monkeypatch):
    configure(kernel_settings)
    monkeypatch.setattr(model_secrets, 'probe', lambda *a, **kw: 'valid')
    await kernel.receive('p1', role='supervisor', text='Use another provider for the brief')
    sim.script('supervisor:consult', {'reply': 'Configure the provider', 'model_setup_questions': [{
        'id': 'm1', 'text': 'Provider setup', 'model_setup': {'capability': 'chat', 'purpose': 'brief',
                                                          'recommended_vendor': 'custom_openai'}}]})
    await sim.drain()
    item = (await kernel.card('p1'))['items'][0]
    answer = json.dumps({'provider': 'foreign', 'model': 'small', 'profile': 'foreign.chat',
                         'base_url': 'https://example.invalid/v1', 'api_key': 'private-test-value'})
    await kernel.act('p1', item['item_id'], 'submit', {'answers': {'m1': answer}})
    run = await kernel.claim('setup-test')
    assert run['purpose'] == 'consult'
    assert 'foreign.chat' in json.dumps(run['input'])
    assert 'private-test-value' not in json.dumps(run['input'])
    assert models.load(kernel_settings)['projects']['p1']['purposes']['brief'] == 'chat.economy'
    assert 'foreign.chat' in models.profiles(kernel_settings)
    messages = await kernel.recent_messages('p1', limit=50)
    assert 'private-test-value' not in json.dumps(messages, default=str)
    await kernel.complete(run, {'reply': 'Setup saved; route unchanged until approval'})
    await sim.assert_consistent()
