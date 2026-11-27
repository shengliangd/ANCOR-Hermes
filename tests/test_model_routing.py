"""Tests for thin category -> Hermes model routing (Hermes owns alias resolution & fallback)."""
import asyncio
import json
import sys

import pytest
import yaml

from ancor import _observed_model_hook
from ancor.goals import AttenuationError, GoalStore
from ancor.host import Host, judge_command, root_command
from ancor.model_routing import (CategoryError, CategoryRouter, alias_names_from_config,
                                 load_alias_names)
from ancor.store import Store
from ancor.visualizer import judge_shares_model, render_text_tree
from ancor.watcher import GoalRunner, default_goal_command
from test_goals import _make_request
from test_transitions_and_host import FakeContext

EXEC_CONFIG = {
    'model': {'provider': 'deepseek', 'default': 'deepseek-v4-flash'},
    'model_aliases': {
        'root': {'provider': 'deepseek', 'model': 'deepseek-v4-reasoner'},
        'Research': {'provider': 'deepseek', 'model': 'deepseek-v4-reasoner'},
        'coding': {'provider': 'openrouter', 'model': 'anthropic/claude-sonnet-4'},
        'fast': {'provider': 'deepseek', 'model': 'deepseek-v4-flash'},
        'local': {'provider': 'custom', 'model': 'qwen', 'base_url': 'http://127.0.0.1:9/v1',
                  'key_env': 'LOCAL_KEY'},
    },
    'fallback_providers': [
        {'provider': 'deepseek', 'model': 'deepseek-v4-flash'},
        {'provider': 'openrouter', 'model': 'anthropic/claude-sonnet-4'},
    ],
}


def _write_exec_config(tmp_path, config=EXEC_CONFIG):
    path = tmp_path / 'ancor-exec' / 'config.yaml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config))
    return path


def _spec(req, goal_id, **extra):
    return {
        'goal_id': goal_id, 'request_ref': {'id': req['id'], 'generation': req['generation']},
        'objective': 'toy', 'done_criteria': 'done',
        'scope': {'readable_requests': [req['id']], 'can_ask_human': False},
        **extra,
    }


def test_alias_names_follow_hermes_keys(tmp_path):
    names = alias_names_from_config(EXEC_CONFIG)
    assert names == {'root', 'research', 'coding', 'fast', 'local'}
    assert load_alias_names(_write_exec_config(tmp_path)) == names
    assert load_alias_names(tmp_path / 'missing.yaml') is None
    assert alias_names_from_config({'model': {'aliases': {
        'simple': 'custom/toy', 'structured': {'model': 'toy'},
        'empty': {}, 'invalid': {'provider': 'custom'}, 'number': 42,
    }}}) == {'simple', 'structured'}


def test_cli_args_pass_alias_only_never_provider():
    router = CategoryRouter(alias_names=alias_names_from_config(EXEC_CONFIG),
                            reasoning={'research': 'high'})
    assert router.cli_args('research') == ['-m', 'research', '--reasoning', 'high']
    # custom endpoint alias: base_url/key_env stay inside Hermes alias resolution
    assert router.cli_args('local') == ['-m', 'local']
    assert router.cli_args(None) == []
    cmd = default_goal_command('ancor-exec', 's1', 'hi', resume=False, model_args=router.cli_args('coding'))
    assert cmd == ['hermes', '-p', 'ancor-exec', '--cli', 'chat', '--format', 'stream-json', '-m', 'coding', '-q', 'hi']
    assert '--provider' not in cmd
    with pytest.raises(ValueError):
        CategoryRouter(alias_names=set(), reasoning={'fast': 'turbo'})


def test_role_defaults_try_root_and_judge_aliases():
    router = CategoryRouter(alias_names=alias_names_from_config(EXEC_CONFIG))
    assert router.role_category('root') == 'root'
    assert router.role_category('judge') is None  # no judge alias -> ancor-exec default model
    assert '-m' not in judge_command('ancor-exec', 'p', router.cli_args(router.role_category('judge')))
    rc = root_command('ancor-exec', 'p', router.cli_args(router.role_category('root')))
    assert rc[rc.index('-m') + 1] == 'root' and 'ANCOR_ROLE=root' in rc


def test_spawn_validates_category_and_budget_allowlist(tmp_path):
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    router = CategoryRouter(alias_names=lambda: load_alias_names(_write_exec_config(tmp_path)))
    goals = GoalStore(store, category_router=router)

    l1 = goals.spawn_goal(_spec(req, 'g_l1', category='Research',
                                budget={'allowed': {'category': ['research', 'fast']}}))
    assert l1['category'] == 'research'

    with pytest.raises(CategoryError):
        goals.spawn_goal(_spec(req, 'g_bad', category='research-xl'), caller_goal_id='root')
    # child inherits allowed.category and cannot pick an expensive category
    with pytest.raises(AttenuationError):
        goals.spawn_goal(_spec(req, 'g_c1', parent_goal_id='g_l1', category='coding'), caller_goal_id='g_l1')
    # cannot widen the allowlist either
    with pytest.raises(AttenuationError):
        goals.spawn_goal(_spec(req, 'g_c2', parent_goal_id='g_l1', category='fast',
                               budget={'allowed': {'category': ['fast', 'coding']}}), caller_goal_id='g_l1')
    child = goals.spawn_goal(_spec(req, 'g_c3', parent_goal_id='g_l1', category='fast'), caller_goal_id='g_l1')
    assert child['category'] == 'fast' and child['budget']['allowed']['category'] == ['research', 'fast']
    # omitted category = ancor-exec default model, always allowed, not inherited
    dflt = goals.spawn_goal(_spec(req, 'g_c4', parent_goal_id='g_l1'), caller_goal_id='g_l1')
    assert dflt['category'] is None

    # no routing configured -> explicit category rejected loudly
    with pytest.raises(CategoryError):
        GoalStore(store).spawn_goal(_spec(req, 'g_x', category='fast'))


def test_runner_launches_with_category_and_survives_recovery(tmp_path):
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    cfg_path = _write_exec_config(tmp_path)
    router = CategoryRouter(alias_names=lambda: load_alias_names(cfg_path), reasoning={'research': 'high'})
    goals = GoalStore(store, category_router=router)
    goals.spawn_goal(_spec(req, 'g_r', category='research'))
    # The fake model yields to tracked work rather than silently abandoning the goal.
    goals.register_resource_usage('g_r', handle_id='ongoing-work')
    out = tmp_path / 'argv.json'
    captured = {}

    def builder(profile, session_id, prompt, *, resume, model_args=()):
        captured['prompt'] = json.loads(prompt.split('\n', 1)[1])
        code = f'import json,sys; open({str(out)!r},"w").write(json.dumps(sys.argv[1:])); print(json.dumps(dict(type="result", session_id="real-session")))'
        return [sys.executable, '-c', code, *default_goal_command(profile, session_id, 'P',
                                                                   resume=resume, model_args=model_args)]

    runner = GoalRunner(goals, command_builder=builder)
    assert runner.run_goal_sync('g_r') == 0
    argv = json.loads(out.read_text())
    assert argv[argv.index('-m') + 1] == 'research' and argv[argv.index('--reasoning') + 1] == 'high'
    assert captured['prompt']['category'] == 'research'
    assert 'fast' in captured['prompt']['model_categories']

    # category persisted -> a recovery relaunch reuses the same alias
    goals.signal_goal('g_r', 'recovery', {'reason': 'pid died'})
    assert runner.run_goal_sync('g_r') == 0
    argv = json.loads(out.read_text())
    assert '--resume' in argv and argv[argv.index('-m') + 1] == 'research'

    # alias removed from config after spawn -> fail loud instead of silent model swap
    cfg = dict(EXEC_CONFIG, model_aliases={'fast': EXEC_CONFIG['model_aliases']['fast']})
    _write_exec_config(tmp_path, cfg)
    goals.signal_goal('g_r', 'steer', {'message': 'continue'})
    with pytest.raises(RuntimeError, match='no longer exists'):
        runner.run_goal_sync('g_r')

    # Unreadable/missing config must not turn an alias into a literal model ID.
    cfg_path.unlink()
    goals.signal_goal('g_r', 'recovery', {'reason': 'config missing'})
    with pytest.raises(RuntimeError, match='config is unavailable'):
        runner.run_goal_sync('g_r')


def test_observed_models_hook_records_fallback_and_judge_overlap(tmp_path):
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    router = CategoryRouter(alias_names=alias_names_from_config(EXEC_CONFIG))
    goals = GoalStore(store, category_router=router)
    goals.spawn_goal(_spec(req, 'g_o', category='coding'))
    sink = []
    hook = _observed_model_hook(sink, on_new=lambda m, p: goals.record_observed_model('g_o', m, p))
    hook(model='anthropic/claude-sonnet-4', provider='openrouter', session_id='x')
    hook(model='anthropic/claude-sonnet-4', provider='openrouter')
    hook(model='deepseek-v4-flash', provider='deepseek')  # Hermes fell back in-process
    goal = goals.get_goal('g_o')
    assert [m['model'] for m in goal['observed_models']] == ['anthropic/claude-sonnet-4', 'deepseek-v4-flash']

    text = render_text_tree(tmp_path / 'requests.db')
    assert 'category=coding' in text and 'fallback' in text
    verdict = {'runtime': [{'model': 'deepseek-v4-flash', 'provider': 'deepseek'}]}
    assert judge_shares_model(verdict, [goal]) == ['deepseek-v4-flash']


def test_host_dispatches_judge_with_judge_alias(tmp_path):
    store_path = tmp_path / 'requests.db'
    store = Store(store_path)
    req = _make_request(store)
    cfg = dict(EXEC_CONFIG, model_aliases={**EXEC_CONFIG['model_aliases'],
                                           'judge': {'provider': 'openrouter', 'model': 'anthropic/claude-sonnet-4'}})
    cfg_path = _write_exec_config(tmp_path, cfg)
    launched = []

    async def launcher(packet):
        launched.append(packet)
        host.judge.apply_verdict(req['id'], generation=req['generation'], decision='reject',
                                summary='Needs work', feedback=['Missing evidence'],
                                delivery_event_id=packet['delivery_event_id'])
        return 0

    host = Host(FakeContext('default', store_path, exec_config_path=str(cfg_path)),
                store=store, judge_launcher=launcher)
    packet = host.judge.build_judge_packet(req['id'], summary='done', artifacts=[])
    store.add_event('delivery.submitted', {'request_id': req['id'], 'judge_packet': packet})
    assert all(e['kind'] != 'delivery.submitted' for e in host.pending(10))
    assert len(asyncio.run(host.dispatch_judges())) == 1
    assert launched[0]['request']['id'] == req['id']
    assert not [e for e in store.pending_events() if e['kind'] == 'delivery.submitted']
    cmd = host.judge_command_for(packet)
    assert cmd[cmd.index('-m') + 1] == 'judge' and 'ANCOR_ROLE=judge' in cmd
    assert host.wake_packet()['model_categories'] == sorted(alias_names_from_config(cfg))


@pytest.mark.parametrize('failure', ['launch', 'nonzero', 'no_verdict'])
def test_judge_failure_keeps_delivery_durable_with_bounded_retries(tmp_path, failure):
    store = Store(tmp_path / 'requests.db')
    req = _make_request(store)
    now = [1000.0]
    calls = []
    async def launcher(packet):
        calls.append(packet)
        if failure == 'launch':
            raise OSError('cannot launch')
        return 1 if failure == 'nonzero' else 0
    host = Host(FakeContext('default', store.path), store=store, clock=lambda: now[0], judge_launcher=launcher)
    packet = host.judge.build_judge_packet(req['id'], summary='done', artifacts=[])
    store.add_event('delivery.submitted', {'request_id': req['id'], 'judge_packet': packet})
    for attempt in range(3):
        assert len(asyncio.run(host.dispatch_judges())) == 1
        assert any(e['kind'] == 'delivery.submitted' for e in store.pending_events())
        assert asyncio.run(host.dispatch_judges()) == []
        now[0] += 100
        host = Host(FakeContext('default', store.path), clock=lambda: now[0], judge_launcher=launcher)
    assert asyncio.run(host.dispatch_judges()) == []
    assert len(calls) == 3
    assert len([e for e in store.pending_events() if e['kind'] == 'judge.failed']) == 1
