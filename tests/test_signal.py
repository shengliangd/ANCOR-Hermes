"""Interactor information uses the ordinary durable coordinator inbox."""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

import ancor
from ancor.host import Host
from ancor.runtime import Coordinator
from ancor.store import Store
from ancor.service import Service


def register(path, role):
    tools = {}
    config = {'store_path': str(path), 'board': 'test'}
    ctx = SimpleNamespace(
        profile_name=role,
        get_config=lambda key, default=None: config.get(key, default),
        register_tool=lambda **kw: tools.update({kw['name']: kw}),
        register_hook=lambda *args: None)
    ancor.register(ctx)
    return ctx, tools


def test_signal_wakes_without_requests_and_uses_normal_read_and_ack(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    interactor, tools = register(path, 'ancor-interactor')
    schema = tools['ancor_signal']['schema']['parameters']
    assert set(schema['properties']) == {'text'}
    assert schema['required'] == ['text']
    text = '刚释放了一个容器。\n' + '补充信息 ' * 200 + 'Confirm ANCOR signal ' + '0' * 32
    result = json.loads(tools['ancor_signal']['handler']({'text': text}))
    store = Store(path)
    assert store.pending_events() == []
    service = Service(interactor, store)
    args, response = confirmation(result)
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None
    # Restart between preparation and confirmation, then repeat the callback.
    service = Service(interactor, Store(path))
    for _ in range(2):
        service.post_tool_call('clarify', args, response, session_id='s', tool_call_id='c')
    event, = store.pending_events()
    assert event['source_key'] == 'signal:' + result['id']
    assert json.loads(event['payload']['confirmation_reference'])['kind'] == 'signal'
    # Even a separate confirmation of the same proposal cannot enqueue twice.
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c2') is None
    service.post_tool_call('clarify', args, response, session_id='s', tool_call_id='c2')
    assert len(store.pending_events()) == 1
    assert event['kind'] == 'interactor.signal'
    assert event['payload']['text'] == text
    assert event['payload']['source'] == 'ancor-interactor'
    assert event['payload']['created_at'] > 0
    assert store.snapshot()['requests'] == []

    ctx, coordinator_tools = register(path, 'ancor-coordinator')
    monkeypatch.setattr('ancor.board.NativeBoard.list', lambda self: [])
    host = Host(ctx, store=store, board=SimpleNamespace())
    assert [e['id'] for e in host.pending(100)] == [event['id']]
    prompt_file = tmp_path / 'prompt'
    runner = Coordinator(tmp_path / 'lock', pending=host.pending,
        snapshot=host.wake_packet, scan=lambda: None,
        command=lambda prompt: [sys.executable, '-c',
            'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])',
            str(prompt_file), prompt])
    asyncio.run(runner.tick())
    assert 'interactor.signal' in prompt_file.read_text()
    assert store.pending_events()  # Process exit never acknowledges delivery.
    call = lambda name, args: json.loads(coordinator_tools[name]['handler'](args))
    assert call('ancor_get_context', {'event_id': event['id']})['event'] == event
    assert call('ancor_ack_events', {'event_ids': [event['id']]})['acked'] == [event['id']]
    assert store.pending_events() == []
    assert call('ancor_get_context', {'event_id': event['id']})['event']['payload']['text'] == text


@pytest.mark.parametrize('args', [{}, {'text': ''}, {'text': ' \n'}, {'text': None},
                                  {'text': 12}, {'text': 'hello', 'request_id': 'x'}])
def test_invalid_signal_does_not_enqueue(tmp_path, args):
    path = tmp_path / 'state.db'
    _, tools = register(path, 'ancor-interactor')
    result = json.loads(tools['ancor_signal']['handler'](args))
    assert result['code'] == 'invalid_request'
    assert Store(path).pending_events() == []


def test_signal_checks_current_role(tmp_path):
    path = tmp_path / 'state.db'
    ctx, tools = register(path, 'ancor-interactor')
    ctx.profile_name = 'ancor-worker'
    assert json.loads(tools['ancor_signal']['handler']({'text': 'hello'}))['code'] == 'forbidden'
    assert Store(path).pending_events() == []


def confirmation(proposal):
    args = proposal['clarify_args']
    question = args['questions'][0]
    return args, {'outcome': 'submitted', 'responses': [{
        'question': question['question'], 'choices_offered': question['choices'],
        'status': 'answered', 'user_response': question['choices'][1]}]}


@pytest.mark.parametrize('failure', ['declined', 'timed_out', 'changed_args', 'wrong_call', 'wrong_question'])
def test_unconfirmed_signal_never_enters_inbox(tmp_path, failure):
    path = tmp_path / 'state.db'
    ctx, tools = register(path, 'ancor-interactor')
    proposal = json.loads(tools['ancor_signal']['handler']({'text': '刚释放了一个容器'}))
    service = Service(ctx, Store(path))
    args, response = confirmation(proposal)
    if failure == 'changed_args':
        args['questions'][0]['question'] += ' altered'
        assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c')['action'] == 'block'
    else:
        assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None
    if failure == 'declined':
        response['responses'][0]['user_response'] = args['questions'][0]['choices'][0]
    elif failure == 'timed_out':
        response['outcome'] = 'timed_out'
    elif failure == 'wrong_question':
        response['responses'][0]['question'] = 'Something else'
    service.post_tool_call('clarify', args, response, session_id='s',
                          tool_call_id='other' if failure == 'wrong_call' else 'c')
    assert service.store.pending_events() == []
    assert service.store.snapshot()['requests'] == []
