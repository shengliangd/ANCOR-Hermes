import json
from types import SimpleNamespace

import ancor
from ancor.worker_context import strip_worker_context, transform_kanban_show_result


WORKER_CONTEXT = '''# Kanban task t1: Bounded task

## Body
Card body.

## Prior attempts on this task
One prior attempt.

## Parent task results
_Handoffs from upstream tasks._
### t_parent
Old parent summary.

## Recent work by @ancor-worker
- t_old - Old task: old pattern

## Comment thread
comment from worker `ancor-worker`:
Current comment.
'''


def test_strip_worker_context_removes_only_cross_task_sections():
    cleaned = strip_worker_context(WORKER_CONTEXT)
    assert '## Body' in cleaned
    assert 'Card body.' in cleaned
    assert '## Prior attempts on this task' in cleaned
    assert 'One prior attempt.' in cleaned
    assert '## Parent task results' not in cleaned
    assert 'Old parent summary.' not in cleaned
    assert '## Recent work by @ancor-worker' not in cleaned
    assert 'old pattern' not in cleaned
    assert '## Comment thread' in cleaned
    assert 'Current comment.' in cleaned


def test_strip_worker_context_is_a_noop_without_hidden_sections():
    text = '# Kanban task t1\n\n## Body\nCard body.\n'
    assert strip_worker_context(text) is text


def test_transform_kanban_show_result_rewrites_worker_context():
    result = json.dumps({'task': {'id': 't1'}, 'worker_context': WORKER_CONTEXT})
    transformed = transform_kanban_show_result('kanban_show', result)
    payload = json.loads(transformed)
    assert payload['task']['id'] == 't1'
    assert '## Parent task results' not in payload['worker_context']
    assert '## Recent work by @ancor-worker' not in payload['worker_context']
    assert 'Card body.' in payload['worker_context']


def test_transform_kanban_show_result_ignores_other_tools_and_invalid_json():
    assert transform_kanban_show_result('kanban_list', '{"worker_context": "x"}') is None
    assert transform_kanban_show_result('kanban_show', 'not json') is None
    assert transform_kanban_show_result('kanban_show', '{}') is None


def _ctx(profile):
    config = {'worker_profile': 'ancor-worker',
              'interactor_profile': 'ancor-interactor',
              'coordinator_profile': 'ancor-coordinator'}
    hooks = {}

    def register_hook(name, callback):
        hooks.setdefault(name, []).append(callback)

    context = SimpleNamespace(
        profile_name=profile,
        get_config=lambda key, default=None: config.get(key, default),
        register_tool=lambda **_kwargs: None,
        register_hook=register_hook,
        register_platform_handler=lambda *_args: None,
    )
    return context, hooks


def test_worker_registers_only_the_result_transform_hook():
    context, hooks = _ctx('ancor-worker')
    ancor.register(context)
    assert set(hooks) == {'transform_tool_result'}
    transformed = hooks['transform_tool_result'][0](
        tool_name='kanban_show',
        result=json.dumps({'worker_context': WORKER_CONTEXT}),
    )
    assert '## Parent task results' not in json.loads(transformed)['worker_context']


def test_non_worker_does_not_register_the_result_transform_hook():
    context, hooks = _ctx('ancor-coordinator')
    ancor.register(context)
    assert 'transform_tool_result' not in hooks
