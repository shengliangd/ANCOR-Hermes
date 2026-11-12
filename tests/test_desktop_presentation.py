"""Desktop confirmations bind to Markdown files while native clarify stays authoritative."""
import json
from pathlib import Path
import sqlite3
import sys
from types import ModuleType

import pytest

from ancor.notification import NotificationOutbox
from ancor.service import Service
from ancor.store import Store


class PreviewContext:
    profile_name = 'ancor-interactor'

    def __init__(self, response=None):
        self.response = response if response is not None else {'success': True}
        self.calls = []
        self.config = {'coordinator_profile': 'ancor-coordinator', 'human_target': 'test:user'}

    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        return json.dumps(self.response)

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def set_surface(monkeypatch, surface):
    hermes = ModuleType('hermes_cli')
    hermes.__path__ = []
    plugin_session = ModuleType('hermes_cli.plugin_session')
    plugin_session.session_surface = lambda: surface
    monkeypatch.setitem(sys.modules, 'hermes_cli', hermes)
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugin_session', plugin_session)


def create_change(tmp_path, monkeypatch, *, surface='desktop', preview_response=None):
    set_surface(monkeypatch, surface)
    store = Store(tmp_path / 'requests.db')
    ctx = PreviewContext(preview_response)
    service = Service(ctx, store=store)
    result = service.change({
        'action': 'propose',
        'input': 'Create a desktop request',
        'operations': [{
            'op': 'create',
            'title': 'Desktop request',
            'fields': {'outcome': ['Render **Markdown**']},
        }],
    })
    return service, store, ctx, result


def submitted(question):
    return {
        'outcome': 'submitted',
        'responses': [{
            'question': question['question'],
            'choices_offered': question['choices'],
            'status': 'answered',
            'user_response': question['choices'][1],
        }],
    }


def make_case(tmp_path, monkeypatch, kind, *, surface='desktop', preview_response=None):
    set_surface(monkeypatch, surface)
    store = Store(tmp_path / 'requests.db')
    ctx = PreviewContext(preview_response)
    service = Service(ctx, store=store)
    request = store.confirm(store.create_draft('Base request')['id'], 'fixture')
    rid = request['id']

    if kind == 'change':
        response = service.change({
            'action': 'propose', 'input': 'Create another request',
            'operations': [{'op': 'create', 'title': 'Another request',
                            'fields': {'outcome': ['Complete the request']}}],
        })
        target = response['id']
    elif kind == 'draft':
        response = service.request({
            'action': 'draft', 'title': 'Draft request', 'summary': 'Summary',
            'brief': 'Exact brief',
        })
        target = response['id']
    elif kind == 'cancellation':
        response = service.request({
            'action': 'cancel', 'request_ids': [rid], 'reason': 'No longer needed',
            'summary': 'Cancellation summary',
        })
        target = response['id']
    elif kind == 'pause':
        response = service.request({
            'action': 'pause', 'request_id': rid, 'reason': 'Wait',
            'summary': 'Pause summary',
        })
        target = response['id']
    elif kind == 'resume':
        pause = store.create_request_command('pause', rid, 'Wait')
        store.confirm_request_command(pause['id'], 'fixture')
        store.finalize_transition(rid)
        response = service.request({
            'action': 'resume', 'request_id': rid, 'reason': 'Continue',
            'summary': 'Resume summary',
        })
        target = response['id']
    elif kind == 'human_decision':
        store.submit_acceptance(rid, json.dumps({'summary': 'Verified'}))
        response = service.request({'action': 'accept', 'request_id': rid})
        target = response['id']
    elif kind == 'signal':
        response = service.signal({'text': 'Review the shared processor'})
        target = response['id']
    elif kind == 'response':
        box = NotificationOutbox(
            service.store.path + '.notifications.db',
            coordinator_profile='ancor-coordinator', target='test:user')
        box.enqueue('question', 'question', 'Which format?', actor='ancor-coordinator', request_id=rid)
        proposal = box.propose_response('question', 'Markdown')
        response = service.prepared_response_presentation(proposal)
        target = response['id']
    else:
        raise AssertionError(kind)

    return service, store, ctx, response, target


@pytest.mark.parametrize('surface', ['desktop', 'webapp'])
def test_desktop_clarify_binds_to_markdown_file_without_opening_it(tmp_path, monkeypatch, surface):
    service, store, ctx, proposal = create_change(tmp_path, monkeypatch, surface=surface)
    question = proposal['clarify_args']['questions'][0]
    path = Path(proposal['draft_file'])
    digest = proposal['presentation_digest']

    assert ctx.calls == []
    assert path.suffix == '.md' and digest[:12] in path.name
    assert path.read_text(encoding='utf-8') == proposal['presentation']
    assert path.stat().st_mode & 0o777 == 0o600
    assert f'文件：{path}' in question['question']
    assert '如无法直接打开，可复制上面的路径。' in question['question']
    assert f'内容摘要：{digest[:12]}' in question['question']
    assert f'确认单：change:{proposal["id"]}' in question['question']
    assert 'Render **Markdown**' not in question['question']

    args = proposal['clarify_args']
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None
    assert ctx.calls == [('desktop_preview', {
        'action': 'open', 'url': str(path), 'label': f'ANCOR 确认 · {digest[:12]}',
    })]
    service.post_tool_call('clarify', args, submitted(question), session_id='s', tool_call_id='c')
    assert store.get_change(proposal['id'])['committed_at'] is not None


def test_preview_failure_blocks_the_short_desktop_confirmation(tmp_path, monkeypatch):
    service, store, _, proposal = create_change(
        tmp_path, monkeypatch, preview_response={'success': False, 'message': 'declined'})

    blocked = service.pre_tool_call(
        'clarify', proposal['clarify_args'], session_id='s', tool_call_id='c')

    assert blocked['action'] == 'block'
    assert 'preview could not be opened' in blocked['message']
    assert store.get_change(proposal['id'])['committed_at'] is None


def test_messaging_surface_uses_the_full_clarify(tmp_path, monkeypatch):
    _, _, ctx, proposal = create_change(tmp_path, monkeypatch, surface='feishu')
    question = proposal['clarify_args']['questions'][0]['question']

    assert ctx.calls == []
    assert question.startswith('**创建请求')
    assert '> Render \\*\\*Markdown\\*\\*' in question
    assert Path(proposal['draft_file']).suffix == '.md'


def test_changed_markdown_file_cannot_be_confirmed(tmp_path, monkeypatch):
    service, store, _, proposal = create_change(tmp_path, monkeypatch)
    args = proposal['clarify_args']
    question = args['questions'][0]
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None

    Path(proposal['draft_file']).write_text('tampered confirmation', encoding='utf-8')
    service.post_tool_call('clarify', args, submitted(question), session_id='s', tool_call_id='c')

    assert store.get_change(proposal['id'])['committed_at'] is None


CONFIRMATION_KINDS = [
    'change', 'draft', 'cancellation', 'pause', 'resume',
    'human_decision', 'signal', 'response',
]


@pytest.mark.parametrize('kind', CONFIRMATION_KINDS)
def test_every_ancor_confirmation_gets_a_bound_desktop_file(tmp_path, monkeypatch, kind):
    service, _, ctx, response, target = make_case(tmp_path, monkeypatch, kind)
    question = response['clarify_args']['questions'][0]

    assert ctx.calls == []
    assert Path(response['draft_file']).suffix == '.md'
    assert f'文件：{response["draft_file"]}' in question['question']
    assert f'内容摘要：{response["presentation_digest"][:12]}' in question['question']
    assert f'确认单：{kind}:{target}' in question['question']
    assert service.pre_tool_call(
        'clarify', response['clarify_args'], session_id='s', tool_call_id='c') is None
    assert ctx.calls == [('desktop_preview', {
        'action': 'open', 'url': response['draft_file'],
        'label': f'ANCOR 确认 · {response["presentation_digest"][:12]}',
    })]
    service.post_tool_call(
        'clarify', response['clarify_args'], submitted(question), session_id='s', tool_call_id='c')
    with sqlite3.connect(service.store.path + '.confirmations.db') as db:
        assert db.execute(
            'SELECT consumed,outcome FROM confirmations WHERE session=? AND call=?',
            ('s', 'c')).fetchone() == (1, 'confirmed')


@pytest.mark.parametrize('kind', CONFIRMATION_KINDS)
def test_every_messaging_confirmation_keeps_the_full_clarify(tmp_path, monkeypatch, kind):
    _, _, ctx, response, target = make_case(tmp_path, monkeypatch, kind, surface='feishu')
    question = response['clarify_args']['questions'][0]['question']

    assert ctx.calls == []
    assert f'确认单：{kind}:{target}' not in question
    assert response['presentation'] in question
