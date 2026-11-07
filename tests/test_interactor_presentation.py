"""Readable confirmations preserve the same exact-consent and concurrency boundary."""
import copy
import json
import re

import pytest

from ancor.service import Service
from ancor.store import Store
from test_atomic_requests import atomic, apply, points, propose, verify


def presentation(store, proposal):
    return Service(None, store=store).change_presentation(proposal)


def without_formatting(text):
    # Original Markdown is escaped; only renderer-added emphasis is removed.
    return re.sub(r'\\([^\w\s])', r'\1', text.replace('**', ''))


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
    assert '将目标改为建议' in text and point['body'] in without_formatting(text)
    assert '原内容：' in text and '新内容：' in text
    assert '\n>   可选做法  \n> 保持这个换行  \n' in text
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
    assert '验收请求「Report」' in text and 'report.md' in without_formatting(text)
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
    assert '允许的动作：\n\n> 部署' in text
    assert '对象：\n\n> 测试环境' in text
    assert '适用范围：\n\n> 仅本次交付' in text
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
        assert text.count(f'\n{label} 1：\n') == 1
    legacy = store.confirm(store.create_draft('原始正文\n  保留格式')['id'], 'legacy')
    p = propose(store, legacy['id'], [{'op': 'convert', 'fields': {'outcome': ['新目标']}}])
    text = presentation(store, p)['presentation']
    assert '原始正文  \n>   保留格式' in text and '整理后的完整要求' in text


def test_multiline_content_stays_separate_from_confirmation_instructions(atomic):
    store, rid = atomic
    body = '原文中的格式：\n\n```python\nprint("hello")\n```\n\n提交说明\n  保留缩进  '
    p = propose(store, rid, [{'op': 'append', 'field': 'constraint', 'body': body}])
    text = presentation(store, p)['clarify_args']['questions'][0]['question']
    displayed = text.split('**1. 新增约束**\n\n', 1)[1].rsplit('\n\n---', 1)[0].split('\n')
    assert all(line.startswith('> ') for line in displayed)
    displayed = [line[2:] for line in displayed]
    restored = '\n'.join(without_formatting(line[:-2] if line and i < len(displayed) - 1 else line)
                         for i, line in enumerate(displayed))
    assert restored == body
    assert '\n```' not in text  # Source fences cannot capture the confirmation UI.
    assert '\n---\n\n**提交说明**\n' in text
    assert text.endswith('确认单：' + p['id'][:12])


def test_mixed_change_has_counted_sections_and_one_question_body(atomic):
    store, rid = atomic
    apply(store, rid, [
        {'op': 'append', 'field': 'constraint', 'body': '每题累计 512k token。'},
        {'op': 'grant', 'action': '运行旧方案甲', 'target': '五题', 'scope': '旧范围甲完整原文'},
        {'op': 'grant', 'action': '运行旧方案乙', 'target': '五题', 'scope': '旧范围乙完整原文'},
    ])
    constraints = store.content(rid)['fields']['constraint']
    grants = store.active_grants(rid)
    store.create_question(rid, identifier='budget', body='是否调整本次测试的累计上限？',
                          reason='执行范围需要确认', actor='coordinator')
    ops = [
        {'op': 'reply', 'question_id': 'budget', 'revision': 1, 'body': '按新的执行范围继续。'},
        {'op': 'replace', 'version_id': constraints[0]['version_id'], 'body': '仍不得包含个人数据。'},
        {'op': 'replace', 'version_id': constraints[1]['version_id'], 'body': '每题累计 1024k token。'},
        *[{'op': 'revoke', 'grant_id': grant['id']} for grant in grants],
        {'op': 'grant', 'action': '运行新方案', 'target': '同五题', 'scope': '新范围完整原文；仅本次。'},
        {'op': 'resolve_question', 'question_id': 'budget', 'revision': 1, 'reason': '执行范围已明确。'},
    ]
    p = propose(store, rid, ops)
    text = presentation(store, p)['presentation']
    assert '修改约束 2 项 · 撤回授权 2 项 · 授予授权 1 项 · 回复并解决问题 1 个' in text
    assert text.index('### 内容变更') < text.index('### 授权调整') < text.index('### 问题处理')
    assert re.findall(r'^\*\*(\d+)\. ', text, re.M) == ['1', '2', '3', '4', '5', '6']
    assert text.count('是否调整本次测试的累计上限？') == 1
    for grant in grants:
        assert grant['scope'] in text
    assert '新范围完整原文；仅本次。' in text
    assert '执行范围已明确。' in text
    assert store.get_change(p['id'])['operations'] == ops
    # Preparing or displaying a proposal has no permission/state effect.
    assert len(store.active_grants(rid)) == 2
    assert next(q for q in store.questions(rid) if q['id'] == 'budget')['state'] == 'open'


def test_multiple_replies_are_preserved_and_identical_questions_are_distinct(atomic):
    store, rid = atomic
    for qid in ('first', 'second'):
        store.create_question(rid, identifier=qid, body='选哪一种？', reason='需要选择', actor='coordinator')
    p = propose(store, rid, [
        {'op': 'reply', 'question_id': 'first', 'revision': 1, 'body': '先选甲。'},
        {'op': 'reply', 'question_id': 'second', 'revision': 1, 'body': '选乙。'},
        {'op': 'reply', 'question_id': 'first', 'revision': 1, 'body': '补充甲的条件。'},
        {'op': 'resolve_question', 'question_id': 'first', 'revision': 1, 'reason': '甲及其条件已明确。'},
    ])
    text = presentation(store, p)['presentation']
    assert text.count('选哪一种？') == 2
    assert text.index('先选甲。') < text.index('补充甲的条件。')
    assert '回复 1：' in text and '回复 2：' in text
    assert text.count('将问题标记为已解决') == 1
    assert text.count('问题保持开放') == 1
    assert '选乙。' in text and '甲及其条件已明确。' in text


def test_identical_reply_and_resolution_reason_are_shown_once(atomic):
    store, rid = atomic
    store.create_question(rid, identifier='format', body='报告格式？', reason='缺少格式', actor='coordinator')
    p = propose(store, rid, [
        {'op': 'reply', 'question_id': 'format', 'revision': 1, 'body': '采用 Markdown。'},
        {'op': 'resolve_question', 'question_id': 'format', 'revision': 1, 'reason': '采用 Markdown。'},
    ])
    text = presentation(store, p)['presentation']
    assert text.count('采用 Markdown。') == 1
    assert '回复及解决依据：' in text and '将问题标记为已解决' in text


def test_resolution_without_reply_does_not_invent_an_answer(atomic):
    store, rid = atomic
    store.create_question(rid, identifier='format', body='报告格式？', reason='缺少格式', actor='coordinator')
    p = propose(store, rid, [{'op': 'resolve_question', 'question_id': 'format',
                             'revision': 1, 'reason': '已不再需要此决定。'}])
    text = presentation(store, p)['presentation']
    assert '解决问题' in text and '解决依据：' in text
    assert '回复：' not in text and '已不再需要此决定。' in text


def test_repeated_title_changes_show_each_transition(atomic):
    store, rid = atomic
    p = propose(store, rid, [{'op': 'title', 'title': '中间名称'}, {'op': 'title', 'title': '最终名称'}])
    text = presentation(store, p)['presentation']
    assert '原名称：\n\n> Report\n' in text
    assert '新名称：\n\n> 中间名称\n' in text
    assert '原名称：\n\n> 中间名称\n' in text
    assert '新名称：\n\n> 最终名称\n' in text


def test_prose_changes_are_emphasized_without_losing_either_version(atomic):
    store, rid = atomic
    apply(store, rid, [{'op': 'append', 'field': 'constraint', 'body': '单次使用 512k token。'}])
    point = store.content(rid)['fields']['constraint'][-1]
    p = propose(store, rid, [{'op': 'replace', 'version_id': point['version_id'],
                             'body': '单次使用 1024k token。'}])
    text = presentation(store, p)['presentation']
    before, after = text.split('原内容：\n\n', 1)[1].split('新内容：\n\n', 1)
    assert before.startswith('> ')
    assert after.startswith('> ')
    assert '**' in before and '**' in after.split('\n\n---')[0]
    assert '**512k**' in before and '**1024k**' in after
    assert point['body'] in without_formatting(before)
    assert '单次使用 1024k token。' in without_formatting(after)


def test_source_markdown_cannot_hide_labels_or_fixed_instructions(atomic):
    markdown = pytest.importorskip('markdown_it').MarkdownIt('commonmark')
    store, rid = atomic
    body = '# 标题\n\n普通标题\n===\n```python\nprint("test")\n\n---\n**提交说明**\n<details>\n&copy;'
    p = propose(store, rid, [{'op': 'append', 'field': 'constraint', 'body': body}])
    text = presentation(store, p)['presentation']
    tokens = markdown.parse(text)
    assert not any(token.type in {'fence', 'code_block', 'html_block', 'heading_open'} for token in tokens)
    inline = [token for token in tokens if token.type == 'inline']
    assert inline[-1].content == '确认单：' + p['id'][:12]
    assert any(token.content == '**提交说明**' for token in inline)
    rendered = markdown.render(text)
    assert '&lt;details&gt;' in rendered and '&amp;copy;' in rendered
    assert '<br />' in rendered
