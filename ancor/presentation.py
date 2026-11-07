"""Deterministic human-facing confirmation text, frozen with each change proposal."""
from collections import Counter
from difflib import SequenceMatcher
import json
import re

LABELS = {'outcome': '目标', 'constraint': '约束', 'advice': '建议', 'context': '背景'}
FIELD_ORDER = ('outcome', 'context', 'advice', 'constraint')
CONTENT_OPS = {'create', 'convert', 'append', 'replace', 'invalidate', 'title'}


def _literal(text):
    """Render source text literally, so its Markdown cannot swallow the UI."""
    return re.sub(r'([\\`*_{}\[\]()<>#+\-.!|~&=])', r'\\\1', text)


def _comparison(before, after):
    """Emphasize small prose edits; retain full literal text for complex content."""
    if (max(len(before), len(after)) > 2000
            or re.search(r'[\n\r\\`*_{}\[\]<>~]', before + after)):
        return _literal(before), _literal(after)
    # Keep numbers and Latin words together instead of bolding isolated digits.
    tokens = r'[A-Za-z0-9]+(?:[./-][A-Za-z0-9]+)*|\s+|.'
    before_parts, after_parts = re.findall(tokens, before), re.findall(tokens, after)
    edits = SequenceMatcher(None, before_parts, after_parts, autojunk=False).get_opcodes()
    if sum(kind != 'equal' for kind, *_ in edits) > 6:
        return _literal(before), _literal(after)

    def emphasize(text):
        stripped = text.strip()
        if not stripped or not any(char.isalnum() for char in stripped):
            return _literal(text)
        start = len(text) - len(text.lstrip())
        end = len(text.rstrip())
        return _literal(text[:start]) + '**' + _literal(text[start:end]) + '**' + _literal(text[end:])

    old, new = [], []
    for kind, a, b, c, d in edits:
        render = _literal if kind == 'equal' else emphasize
        old.append(render(''.join(before_parts[a:b])))
        new.append(render(''.join(after_parts[c:d])))
    return ''.join(old), ''.join(new)


def change_confirmation(store, db, rid, operations, identifier):
    creating = operations[0]['op'] == 'create'
    request = None if creating else store._get(db, 'requests', rid)
    title = operations[0]['title'] if creating else request['title']
    verb = '创建请求' if creating else '更新请求'
    if len(operations) == 1:
        verb = {'accept': '验收请求', 'pause': '暂停请求', 'resume': '恢复请求',
                'cancel': '取消请求', 'request_changes': '要求返工',
                'reply': '回复问题', 'title': '修改请求名称'}.get(operations[0]['op'], verb)
    lines = [f'{verb}「{_literal(title)}」', '']
    groups = {'内容变更': [], '授权调整': [], '问题处理': [], '状态与验收': []}
    questions = {}
    for op in operations:
        kind = op['op']
        item = {'op': op}
        if kind in {'reply', 'resolve_question'}:
            qid = op['question_id']
            if qid not in questions:
                questions[qid] = {'op': op, 'question': store._get(db, 'questions', qid),
                                  'replies': [], 'resolution': None}
                groups['问题处理'].append(questions[qid])
            if kind == 'reply':
                questions[qid]['replies'].append(op['body'])
            else:
                questions[qid]['resolution'] = op['reason']
            continue
        if kind in {'replace', 'invalidate'}:
            item['point'] = dict(db.execute('SELECT * FROM point_versions WHERE version_id=?',
                                            (op['version_id'],)).fetchone())
            old_label = LABELS[item['point']['field']]
            new_label = LABELS[op.get('field', item['point']['field'])]
            item['label'] = (f'撤回{old_label}' if kind == 'invalidate' else
                             f'修改{old_label}' if old_label == new_label else
                             f'将{old_label}改为{new_label}')
        elif kind == 'append':
            item['label'] = f"新增{LABELS[op['field']]}"
        else:
            item['label'] = {
                'create': '完整要求', 'convert': '整理为条目', 'title': '修改请求名称',
                'grant': '授予授权', 'revoke': '撤回授权',
                'pause': '暂停推进此请求', 'resume': '恢复推进此请求', 'cancel': '取消此请求',
                'request_changes': '将交付退回继续处理', 'accept': '接受本次已验证的交付',
            }[kind]
        group = ('内容变更' if kind in CONTENT_OPS else
                 '授权调整' if kind in {'grant', 'revoke'} else '状态与验收')
        groups[group].append(item)
    for item in questions.values():
        item['label'] = ('回复并解决问题' if item['replies'] and item['resolution'] is not None else
                         '回复问题' if item['replies'] else '解决问题')
    groups = {label: items for label, items in groups.items() if items}
    if len(operations) > 1:
        counts = Counter(item['label'] for items in groups.values() for item in items)
        lines.extend(['**本次变更**', '', ' · '.join(
            f'{label} {count} {"个" if label.endswith("问题") else "项"}'
            for label, count in counts.items()), ''])

    def body(text, *, rendered=None):
        shown = _literal(text) if rendered is None else rendered
        parts = shown.split('\n')
        for index, part in enumerate(parts):
            # Make source line breaks explicit even on Markdown clients that
            # collapse soft breaks. Empty lines remain paragraph boundaries.
            ending = '  ' if part and index < len(parts) - 1 else ''
            lines.append('> ' + part + ending)
        lines.append('')

    def value(label, text, *, rendered=None):
        lines.extend([label, ''])
        body(text, rendered=rendered)

    number = 0
    content_changed = False
    for group, items in groups.items():
        lines.extend(['---', ''])
        if len(groups) > 1:
            lines.extend([f'### {group}', ''])
        for item in items:
            number += 1
            lines.extend([f"**{number}. {item['label']}**", ''])
            op = item['op']
            kind = op['op']
            if kind in {'create', 'convert'}:
                if kind == 'convert':
                    value('原请求正文：', request['brief'])
                    lines.extend(['整理后的完整要求：', ''])
                for field in FIELD_ORDER:
                    for index, text in enumerate(op['fields'].get(field, []), 1):
                        value(f'{LABELS[field]} {index}：', text)
                content_changed = True
            elif kind in {'append', 'replace', 'invalidate'}:
                if kind == 'append':
                    body(op['body'])
                elif kind == 'invalidate':
                    body(item['point']['body'])
                else:
                    before, after = _comparison(item['point']['body'], op['body'])
                    value('原内容：', item['point']['body'], rendered=before)
                    value('新内容：', op['body'], rendered=after)
                content_changed = True
            elif kind == 'title':
                value('原名称：', title)
                value('新名称：', op['title'])
                title = op['title']
            elif kind in {'reply', 'resolve_question'}:
                value('问题：', item['question']['body'])
                replies, reason = item['replies'], item['resolution']
                shared_reason = len(replies) == 1 and reason == replies[0]
                for index, reply in enumerate(replies, 1):
                    label = ('回复及解决依据：' if shared_reason else '回复：' if len(replies) == 1
                             else f'回复 {index}：')
                    value(label, reply)
                if reason is not None:
                    if not shared_reason:
                        value('解决依据：', reason)
                    lines.extend(['提交后：将问题标记为已解决。', ''])
                else:
                    lines.extend(['本次仅记录回复，问题保持开放。', ''])
            elif kind in {'pause', 'resume', 'cancel'}:
                value('原因：', op['reason'])
                lines.extend([{'pause': '确认后，系统将停止相关执行；暂停完成前会显示处理中。',
                               'resume': '确认后，协调者将重新检查现有工作并安排继续执行。',
                               'cancel': '确认后，系统将结束相关任务，历史记录会保留。'}[kind], ''])
            elif kind == 'request_changes':
                body(op['feedback'])
            elif kind == 'accept':
                event = store._event(db.execute('SELECT * FROM events WHERE id=?',
                                                (op['verification_id'],)).fetchone())
                raw = event['payload']['evidence']
                try:
                    evidence = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError:
                    evidence = raw
                if isinstance(evidence, dict):
                    for artifact in evidence.get('artifacts', []):
                        value('交付成果：', artifact)
                    for check in evidence.get('verification', []):
                        value('验证结果：', check)
                    for coverage in evidence.get('coverage', []):
                        point = dict(db.execute('SELECT * FROM point_versions WHERE version_id=?',
                                                (coverage['version_id'],)).fetchone())
                        value('已核对的' + LABELS[point['field']] + '：', point['body'])
                        value('依据：', coverage['evidence'])
                else:
                    value('验证结果：', str(evidence))
            elif kind in {'grant', 'revoke'}:
                grant = op if kind == 'grant' else store._get(db, 'grants', op['grant_id'])
                value('允许的动作：', grant['action'])
                value('对象：', grant['target'])
                value('适用范围：', grant['scope'])
    lines.extend(['---', '', '**提交说明**', ''])
    if content_changed:
        lines.append('确认后，协调者会评估这些要求，并据此安排或调整工作。')
    lines.extend(['', '请确认以上内容。', '', f'确认单：{identifier[:12]}'])
    return {'questions': [{'question': '\n'.join(lines),
                           'choices': ['暂不提交', '确认提交'], 'multi_select': False}]}
