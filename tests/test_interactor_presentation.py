"""Readable confirmations preserve the same exact-consent and concurrency boundary."""
import copy
import json

import pytest

from ancor.service import Service
from ancor.store import Store
from test_atomic_requests import atomic, apply, points, propose, verify


def presentation(store, proposal):
    return Service(None, store=store).change_presentation(proposal)


def test_point_changes_show_exact_before_after_and_field_strength(atomic):
    store, rid = atomic
    point = points(store, rid)[0]
    p = propose(store, rid, [
        {'op': 'replace', 'version_id': point['version_id'], 'field': 'advice', 'body': '  可选做法\n保持这个换行  '},
        {'op': 'append', 'field': 'constraint', 'body': '不得联网'},
        {'op': 'invalidate', 'version_id': points(store, rid)[-1]['version_id']},
    ])
    view = presentation(store, p)
    text = view['clarify_args']['questions'][0]['question']
    assert '更新请求「Report」' in text
    assert '将目标改为建议' in text and point['body'] in text
    assert '原内容：' in text and '新内容：' in text
    assert '>   可选做法\n> 保持这个换行  ' in text
    assert '新增约束' in text and '撤回约束' in text
    assert '协调者会评估' in text
    for hidden in (rid, point['version_id'], p['id'], 'Base versions', 'Exact operations', 'version_id'):
        assert hidden not in text
    assert view['clarify_args']['questions'][0]['choices'] == ['暂不提交', '确认提交']


def test_reply_names_question_and_does_not_claim_resolution(atomic):
    store, rid = atomic
    store.create_question(rid, identifier='format', body='报告需要什么格式？', reason='缺少格式', actor='coordinator')
    p = propose(store, rid, [{'op': 'reply', 'question_id': 'format', 'revision': 1, 'body': '还没决定'}])
    text = presentation(store, p)['presentation']
    assert '报告需要什么格式？' in text and '还没决定' in text
    assert '问题保持开放' in text and 'format' not in text


def test_answer_and_resolution_render_both_effects(atomic):
    store, rid = atomic
    store.create_question(rid, identifier='format', body='报告格式？', reason='缺少格式', actor='coordinator')
    p = propose(store, rid, [
        {'op': 'reply', 'question_id': 'format', 'revision': 1, 'body': 'Markdown'},
        {'op': 'append', 'field': 'outcome', 'body': '交付 Markdown 报告'},
        {'op': 'resolve_question', 'question_id': 'format', 'revision': 1, 'reason': '格式已明确'},
    ])
    text = presentation(store, p)['presentation']
    assert '新增目标' in text and '将问题标记为已解决' in text
    assert '问题保持开放' not in text


def test_title_change_does_not_rewrite_existing_confirmation(atomic):
    store, rid = atomic
    p = propose(store, rid, [{'op': 'append', 'field': 'context', 'body': '输入目录'}])
    before = presentation(store, p)['clarify_args']
    apply(store, rid, [{'op': 'title', 'title': 'New title'}])
    restarted = Store(store.path)
    assert presentation(restarted, restarted.get_change(p['id']))['clarify_args'] == before
    restarted.commit_change(p['id'], 'confirmed')
    assert restarted.get_request(rid)['title'] == 'New title'


def test_tampering_with_human_text_or_choice_is_rejected(atomic):
    store, rid = atomic
    p = propose(store, rid, [{'op': 'append', 'field': 'constraint', 'body': '不得外传'}])
    service = Service(None, store=store)
    original = service.change_presentation(p)['clarify_args']
    changed = copy.deepcopy(original)
    changed['questions'][0]['question'] = changed['questions'][0]['question'].replace('不得外传', '允许外传')
    assert service.pre_tool_call('clarify', changed, session_id='s', tool_call_id='c')['action'] == 'block'
    changed = copy.deepcopy(original)
    changed['questions'][0]['choices'][1] = '自动确认'
    assert service.pre_tool_call('clarify', changed, session_id='s', tool_call_id='c')['action'] == 'block'
    assert store.get_change(p['id'])['committed_at'] is None


def test_acceptance_names_artifacts_and_verification_without_internal_ids(atomic):
    store, rid = atomic
    store.link_task(rid, 't')
    verify(store, rid)
    p = propose(store, rid, [{'op': 'accept', 'verification_id': store.latest_verification(rid)['id']}])
    text = presentation(store, p)['presentation']
    assert '验收请求「Report」' in text and 'report.md' in text
    assert 'independent check' in text and 'No personal data' in text
    assert 'version_id' not in text and 'verification_id' not in text


def test_revocation_shows_exact_original_authorization(atomic):
    store, rid = atomic
    apply(store, rid, [{'op': 'grant', 'action': '部署', 'target': '测试环境', 'scope': '仅本次交付'}])
    grant = store.active_grants(rid)[0]
    p = propose(store, rid, [{'op': 'revoke', 'grant_id': grant['id']}])
    text = presentation(store, p)['presentation']
    assert '撤回授权' in text
    assert all(value in text for value in ('部署', '测试环境', '仅本次交付'))
    assert grant['id'] not in text


def test_old_proposal_keeps_original_confirmation_protocol(atomic):
    store, rid = atomic
    p = propose(store, rid, [{'op': 'title', 'title': 'Legacy pending rename'}])
    # Simulate a proposal created before readable confirmations existed.
    with store._transaction() as db:
        db.execute('DELETE FROM change_presentations WHERE change_id=?', (p['id'],))
    service = Service(None, store=store)
    args = service.change_presentation(p)['clarify_args']
    q = args['questions'][0]
    assert q['choices'][1] == 'Apply ANCOR change ' + p['id']
    assert service.pre_tool_call('clarify', args, session_id='old', tool_call_id='old') is None
    service.post_tool_call('clarify', args, {'outcome': 'submitted', 'responses': [{
        'question': q['question'], 'choices_offered': q['choices'], 'status': 'answered', 'user_response': q['choices'][1]
    }]}, session_id='old', tool_call_id='old')
    assert store.get_request(rid)['title'] == 'Legacy pending rename'


def test_creation_and_conversion_present_full_content(tmp_path):
    store = Store(tmp_path / 'db')
    p = propose(store, None, [{'op': 'create', 'title': '新报告', 'fields': {
        'outcome': ['生成报告'], 'constraint': ['不能联网'], 'advice': ['参考旧版'], 'context': ['数据在本地']}}])
    text = presentation(store, p)['presentation']
    assert '创建请求「新报告」' in text
    assert all(body in text for body in ('生成报告', '不能联网', '参考旧版', '数据在本地'))
    assert text.index('目标') < text.index('背景') < text.index('建议') < text.index('约束')
    for label in ('目标', '背景', '建议', '约束'):
        assert text.count(f'**{label} 1**') == 1
    legacy = store.confirm(store.create_draft('原始正文\n  保留格式')['id'], 'legacy')
    p = propose(store, legacy['id'], [{'op': 'convert', 'fields': {'outcome': ['新目标']}}])
    text = presentation(store, p)['presentation']
    assert '原始正文\n>   保留格式' in text and '整理后的完整要求' in text


def test_multiline_content_stays_separate_from_confirmation_instructions(atomic):
    store, rid = atomic
    body = '原文中的格式：\n\n```python\nprint("hello")\n```\n\n提交说明\n  保留缩进  '
    p = propose(store, rid, [{'op': 'append', 'field': 'constraint', 'body': body}])
    text = presentation(store, p)['clarify_args']['questions'][0]['question']
    quoted = '\n'.join('> ' + line for line in body.split('\n'))
    assert f'**新增约束**\n\n{quoted}\n\n---\n\n**提交说明**' in text
    # Removing only the display prefix recovers every source line, including blanks.
    assert '\n'.join(line[2:] for line in quoted.split('\n')) == body
    assert text.endswith('确认单：' + p['id'][:12])
