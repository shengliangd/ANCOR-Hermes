"""Request-scoped coordinator working notes."""
import json
from types import SimpleNamespace

import pytest

import ancor
from ancor.store import Store


def test_note_crud_is_revisioned_and_soft_deleted(tmp_path):
    store = Store(tmp_path / 'store.db')
    request = store.confirm(
        store.create_draft('Deliver report')['id'], 'fixture')

    first = store.add_note(request['id'], 'verified input: /tmp/input.csv')
    second = store.add_note(request['id'], 'attempted route: script --dry-run')
    assert first['revision'] == second['revision'] == 1
    assert isinstance(first['created_at'], int) and isinstance(first['updated_at'], int)
    assert store.list_notes(request['id'])['count'] == 2

    updated = store.update_note(
        request['id'], first['id'], 'verified input: /tmp/input-v2.csv')
    assert updated['body'] == 'verified input: /tmp/input-v2.csv'
    assert updated['revision'] == 2
    assert updated['created_at'] == first['created_at']
    assert updated['updated_at'] >= first['updated_at']

    deleted = store.delete_note(request['id'], first['id'])
    assert deleted['revision'] == 3
    assert deleted['deleted_at'] is not None
    assert store.list_notes(request['id'])['count'] == 1

    history = store.list_notes(request['id'], include_deleted=True)
    assert history['count'] == 2
    assert {note['id'] for note in history['items']} == {first['id'], second['id']}
    assert store.get_note(request['id'], first['id'])['body'].endswith('input-v2.csv')

    with pytest.raises(ValueError, match='deleted note'):
        store.update_note(request['id'], first['id'], 'must not update')


def test_note_mutations_are_frozen_outside_stable_requests(tmp_path):
    store = Store(tmp_path / 'store.db')
    request = store.confirm(
        store.create_draft('Deliver report')['id'], 'fixture')
    note = store.add_note(request['id'], 'keep this evidence')

    store.cancel([request['id']], 'withdrawn')
    with pytest.raises(ValueError, match='stable active or paused'):
        store.add_note(request['id'], 'new note')
    with pytest.raises(ValueError, match='stable active or paused'):
        store.update_note(request['id'], note['id'], 'changed')
    with pytest.raises(ValueError, match='stable active or paused'):
        store.delete_note(request['id'], note['id'])

    store.finalize_transition(request['id'])
    with pytest.raises(ValueError, match='stable active or paused'):
        store.add_note(request['id'], 'after terminal')
    assert store.get_note(request['id'], note['id'])['body'] == 'keep this evidence'


def registered(tmp_path, monkeypatch, profile):
    tools = {}
    config = {'store_path': str(tmp_path / 'store.db'), 'board': 'ancor',
              'coordinator_profile': 'ancor-coordinator',
              'interactor_profile': 'ancor-interactor',
              'human_target': 'test:destination', 'transport_profile': 'default'}
    ctx = SimpleNamespace(
        profile_name=profile,
        get_config=lambda key, default=None: config.get(key, default),
        register_tool=lambda **kw: tools.update({kw['name']: kw}),
        register_hook=lambda *args: None,
        register_platform_handler=lambda *args: None)
    monkeypatch.setattr('ancor.board.NativeBoard.list', lambda self: [])
    ancor.register(ctx)
    return tools, Store(config['store_path'])


def call(tools, name, args):
    return json.loads(tools[name]['handler'](args))


def test_note_tool_is_coordinator_only_and_context_lists_previews(tmp_path, monkeypatch):
    tools, store = registered(tmp_path, monkeypatch, 'ancor-coordinator')
    request = store.confirm(
        store.create_draft('Deliver report')['id'], 'fixture')

    added = call(tools, 'ancor_note', {
        'action': 'add', 'request_id': request['id'],
        'body': 'verified artifact: /tmp/report.csv'})
    assert added['body'] == 'verified artifact: /tmp/report.csv'

    listed = call(tools, 'ancor_note', {
        'action': 'list', 'request_id': request['id']})
    assert listed['count'] == 1
    assert listed['items'][0]['body_preview'] == 'verified artifact: /tmp/report.csv'
    assert isinstance(listed['items'][0]['created_at'], int)
    assert isinstance(listed['items'][0]['updated_at'], int)
    assert 'body' not in listed['items'][0]

    context = call(tools, 'ancor_get_context', {'request_id': request['id']})
    assert context['notes']['count'] == 1
    assert context['notes']['items'][0]['body_preview'].startswith('verified artifact')
    assert context['notes']['items'][0]['created_at'] == listed['items'][0]['created_at']
    assert context['notes']['items'][0]['updated_at'] == listed['items'][0]['updated_at']

    full = call(tools, 'ancor_note', {
        'action': 'get', 'request_id': request['id'],
        'note_id': added['id']})
    assert full['body'] == 'verified artifact: /tmp/report.csv'

    deleted = call(tools, 'ancor_note', {
        'action': 'delete', 'request_id': request['id'],
        'note_id': added['id']})
    assert deleted['deleted'] is True
    assert call(tools, 'ancor_note', {
        'action': 'list', 'request_id': request['id']})['count'] == 0


def test_note_tool_is_not_registered_for_interactor(tmp_path, monkeypatch):
    tools, _ = registered(tmp_path, monkeypatch, 'ancor-interactor')
    assert 'ancor_note' not in tools
