"""A long-lived request must not turn a state check into a history dump."""
import json
from types import SimpleNamespace

import pytest

import ancor
from ancor.notification import NotificationOutbox
from ancor.store import Store


@pytest.fixture
def long_lived_request(tmp_path, monkeypatch):
    store = Store(tmp_path / 'store.db')
    fields = {
        field: [f'{field} {i}: ' + '完整要求不可截断。' * 15 for i in range(count)]
        for field, count in [('outcome', 13), ('constraint', 29), ('context', 9), ('advice', 3)]}
    proposal = store.propose_change(None, [{'op': 'create', 'title': 'Production', 'fields': fields}],
                                    actor='ancor-interactor', source={'input': 'confirmed request'})
    rid = store.commit_change(proposal['id'], 'human:confirmed')['request_id']
    proposal = store.propose_change(rid, [{'op': 'grant', 'action': 'render',
        'target': 'approved GPU', 'scope': 'production only'}], actor='ancor-interactor',
        source={'input': 'approved GPU'}, base=store.get_request(rid))
    store.commit_change(proposal['id'], 'human:grant')
    versions = [p['version_id'] for ps in store.content(rid)['fields'].values() for p in ps]
    generation = store.get_request(rid)['generation']
    cards = []
    for i in range(27):
        tid = f'task-{i}'
        store.link_task(rid, tid)
        store.bind_task(rid, tid, generation, versions)
        cards.append({'id': tid, 'title': f'Task {i}',
                      'status': 'scheduled' if i == 26 else 'done'})
    store.review_impact(rid, generation, [{'task_id': card['id'], 'decision': 'unaffected',
        'reason': 'Current contract still applies'} for card in cards], 'All requirements covered')
    for i in range(105):
        store.add_note(rid, f'Historical scheduling note {i}: ' + 'past observation ' * 50)
    box = NotificationOutbox(store.path + '.notifications.db',
        coordinator_profile='ancor-coordinator', target='test:destination')
    for i in range(19):
        box.enqueue(f'q-{i}', 'question', f'Obsolete question {i}: ' + 'past question ' * 100,
                    actor='ancor-coordinator', request_id=rid)
        box.close_questions({f'q-{i}': 'resolved'}, closed_by='host')
    store.ack_events([e['id'] for e in store.pending_events()])
    monkeypatch.setattr('ancor.board.NativeBoard.list', lambda self: cards)
    config = {'store_path': store.path, 'board': 'ancor', 'human_target': 'test:destination'}

    def register(role):
        tools = {}
        ctx = SimpleNamespace(profile_name=role,
            get_config=lambda key, default=None: config.get(key, default),
            register_tool=lambda **kw: tools.update({kw['name']: kw}),
            register_hook=lambda *a: None, register_platform_handler=lambda *a: None)
        ancor.register(ctx)
        return lambda name, args: json.loads(tools[name]['handler'](args))

    return store, rid, fields, register


@pytest.mark.parametrize('role', ['ancor-coordinator', 'ancor-interactor'])
def test_request_state_stays_small_and_content_expands_losslessly(long_lived_request, role):
    store, rid, fields, register = long_lived_request
    call = register(role)
    state = call('ancor_get_context', {'request_id': rid})
    encoded = json.dumps(state, ensure_ascii=False)
    assert len(encoded) < 6000, len(encoded)
    assert state['content']['counts'] == {f: len(rows) for f, rows in fields.items()}
    assert [t['id'] for t in state['tasks']['items']] == ['task-26']
    assert state['counts']['tasks']['terminal'] == 26
    assert state['notifications']['count'] == 0
    assert 'brief' not in state['request']
    assert 'fields' not in state['content']
    assert 'task_basis' not in state['coordination']
    assert state['coordination']['needs_review'] is False
    assert 'Historical scheduling note' not in encoded
    assert 'Obsolete question' not in encoded
    assert state['grants']['count'] == 1
    assert 'exact_via' not in state['grants']
    assert not {'mode', 'generation'} & state['content'].keys()
    assert not {'generation', 'observed_generation'} & state['coordination'].keys()
    assert not {'notes_version', 'verification_id'} & state['request'].keys()
    for key in ('generation', 'observed_generation', 'title_version', 'control_version'):
        assert state['request'][key] == store.get_request(rid)[key]
    assert not {'expand', 'conditions', 'archived_task_count', 'open_question_count', 'acceptance'} & state.keys()
    assert all(not {'request_id', 'request_title'} & task.keys()
               for task in state['tasks']['items'])
    assert all(not {'tasks', 'timers'} & item.keys() for item in state['progression'])
    if role == 'ancor-coordinator':
        assert state['notes']['count'] == 105
        assert 'items' not in state['notes']
        review = state['coordination']['exact_via']
        assert len(call(review['tool'], review['args'])['task_basis']) == 27
        notes = state['notes']['exact_via']
        assert call(notes['tool'], {**notes['args'], 'limit': 1})['returned'] == 1
    content = state['content']['exact_via']
    detail = call(content['tool'], content['args'])
    assert 'brief' not in detail['request']
    assert 'brief' not in detail['content']
    actual = detail['content']['fields']
    assert {f: [p['body'] for p in rows] for f, rows in actual.items()} == fields
    assert all(set(p) == {'point_id', 'version_id', 'body'} for rows in actual.values() for p in rows)
    assert {p['version_id'] for rows in actual.values() for p in rows} == {
        p['version_id'] for rows in store.content(rid)['fields'].values() for p in rows}
    assert detail['grants']['items'][0]['scope'] == 'production only'
    history = call('ancor_get_context', {'request_id': rid, 'include_done': True,
                                      'include_closed_notifications': True})
    assert history['tasks']['count'] == 27
    assert history['notifications']['count'] == 19
    assert history['omitted'] == []


@pytest.mark.parametrize('role', ['ancor-coordinator', 'ancor-interactor'])
def test_audit_history_and_retry_details_are_reachable_without_duplicate_health(long_lived_request, role):
    from pathlib import Path
    from ancor.host import CONDITION_BACKOFF_SECONDS

    store, rid, _, register = long_lived_request
    call = register(role)
    state = {'conditions': {'stalled:' + rid: {
        'since': 50, 'generation': 2, 'last_woken_at': 100}}}
    Path(store.path + '.host.json').write_text(json.dumps(state))
    before = store.snapshot()
    overview = call('ancor_get_context', {})
    assert 'conditions' not in overview and 'progression' not in overview
    assert 'expand' not in overview
    reference = overview['requests']['items'][0]
    assert reference['progression']['reason'] == 'no_active_task'
    assert any(item['request_id'] == rid for item in overview['attention'])
    current = call(reference['exact_via']['tool'], reference['exact_via']['args'])
    health = current['progression'][0]
    assert health['reason'] == 'no_active_task'
    assert health['condition'] == {
        'id': 'stalled:' + rid, 'ack_policy': 'host_owned',
        'since': 50, 'generation': 2, 'last_woken_at': 100,
        'retry_at': 100 + CONDITION_BACKOFF_SECONDS}
    history = call('ancor_get_context', {'request_id': rid, 'include_history': True})
    for key in ('notes_version', 'verification_id'):
        assert history['request'][key] == store.get_request(rid)[key]
    assert store.snapshot() == before


def test_content_expansion_rejects_ambiguous_scope(long_lived_request):
    _, rid, _, register = long_lived_request
    call = register('ancor-coordinator')
    for args in [{'include_content': True}, {'request_id': rid, 'include_content': 'true'},
                 {'request_id': rid, 'include_content': True, 'event_id': 1},
                 {'include_history': True}, {'request_id': rid, 'include_history': 'true'},
                 {'request_id': rid, 'include_history': True, 'event_id': 1},
                 {'request_id': rid, 'before_id': 10},
                 {'request_id': rid, 'include_history': True, 'before_id': True}]:
        assert call('ancor_get_context', args)['code'] == 'invalid_request'
