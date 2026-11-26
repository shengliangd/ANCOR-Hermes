"""Desktop/Webapp use Markdown file cards; other surfaces use full native clarify."""
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


@pytest.mark.parametrize('surface', ['desktop', 'webapp', 'gui'])
def test_desktop_clarify_uses_media_file_confirmation(tmp_path, monkeypatch, surface):
    service, store, ctx, proposal = create_change(tmp_path, monkeypatch, surface=surface)
    question = proposal['clarify_args']['questions'][0]
    path = Path(proposal['draft_file'])
    digest = proposal['presentation_digest']

    assert ctx.calls == []
    assert path.suffix == '.md' and digest[:12] in path.name
    assert path.read_text(encoding='utf-8') == proposal['presentation']
    assert path.stat().st_mode & 0o777 == 0o600
    assert question['question'].startswith('请打开上方 ANCOR 确认文件')
    assert f'内容摘要：{digest[:12]}' in question['question']
    assert f'确认单：change:{proposal["id"]}' in question['question']
    assert proposal['presentation'] not in question['question']
    assert proposal['confirmation_media'] == f'MEDIA:{path}'
    assert proposal['confirmation_media'] in proposal['next_step']
    assert proposal['clarify_args']['timeout_seconds'] == 3600
    assert str(path) not in question['question']
    assert 'file://' not in question['question']

    args = proposal['clarify_args']
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None
    assert ctx.calls == []
    service.post_tool_call('clarify', args, submitted(question), session_id='s', tool_call_id='c')
    assert store.get_change(proposal['id'])['committed_at'] is not None
    assert ctx.calls == []


def test_rejected_desktop_confirmation_does_not_commit(tmp_path, monkeypatch):
    service, store, ctx, proposal = create_change(tmp_path, monkeypatch)
    args = proposal['clarify_args']
    question = args['questions'][0]
    assert service.pre_tool_call('clarify', args, session_id='s', tool_call_id='c') is None

    result = submitted(question)
    result['responses'][0]['user_response'] = question['choices'][0]
    service.post_tool_call('clarify', args, result, session_id='s', tool_call_id='c')

    assert store.get_change(proposal['id'])['committed_at'] is None
    assert ctx.calls == []


def test_preview_tool_is_not_used_for_file_confirmation(tmp_path, monkeypatch):
    service, store, ctx, proposal = create_change(
        tmp_path, monkeypatch, preview_response={'success': False, 'message': 'declined'})

    assert service.pre_tool_call(
        'clarify', proposal['clarify_args'], session_id='s', tool_call_id='c') is None

    assert ctx.calls == []
    service.post_tool_call(
        'clarify', proposal['clarify_args'], submitted(proposal['clarify_args']['questions'][0]),
        session_id='s', tool_call_id='c')
    assert store.get_change(proposal['id'])['committed_at'] is not None


def test_messaging_surface_uses_full_markdown_clarify(tmp_path, monkeypatch):
    _, _, ctx, proposal = create_change(tmp_path, monkeypatch, surface='feishu')
    question = proposal['clarify_args']['questions'][0]['question']

    assert ctx.calls == []
    assert question.startswith('**创建请求「Desktop request」**')
    assert '### 内容变更' in question
    assert '> Render \\*\\*Markdown\\*\\*' in question
    assert proposal['presentation'] in question
    assert 'confirmation_media' not in proposal
    assert proposal['clarify_args']['timeout_seconds'] == 3600
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
def test_every_desktop_confirmation_uses_a_media_file(tmp_path, monkeypatch, kind):
    service, _, ctx, response, target = make_case(tmp_path, monkeypatch, kind)
    question = response['clarify_args']['questions'][0]

    assert ctx.calls == []
    assert Path(response['draft_file']).suffix == '.md'
    assert question['question'].startswith('请打开上方 ANCOR 确认文件')
    assert response['presentation'] not in question['question']
    assert f'确认单：{kind}:{target}' in question['question']
    assert response['confirmation_media'] == f'MEDIA:{response["draft_file"]}'
    assert response['confirmation_media'] in response['next_step']
    assert response['clarify_args']['timeout_seconds'] == 3600
    assert response['draft_file'] not in question['question']
    assert 'file://' not in question['question']
    assert service.pre_tool_call(
        'clarify', response['clarify_args'], session_id='s', tool_call_id='c') is None
    assert ctx.calls == []
    service.post_tool_call(
        'clarify', response['clarify_args'], submitted(question), session_id='s', tool_call_id='c')
    with sqlite3.connect(service.store.path + '.confirmations.db') as db:
        assert db.execute(
            'SELECT consumed,outcome FROM confirmations WHERE session=? AND call=?',
            ('s', 'c')).fetchone() == (1, 'confirmed')


@pytest.mark.parametrize('kind', CONFIRMATION_KINDS)
def test_every_messaging_confirmation_uses_full_markdown_clarify(tmp_path, monkeypatch, kind):
    _, _, ctx, response, target = make_case(tmp_path, monkeypatch, kind, surface='feishu')
    question = response['clarify_args']['questions'][0]['question']

    assert ctx.calls == []
    assert f'确认单：{kind}:{target}' not in question
    assert response['presentation'] in question
    assert 'confirmation_media' not in response
    assert response['clarify_args']['timeout_seconds'] == 3600
    assert 'MEDIA:' not in question
