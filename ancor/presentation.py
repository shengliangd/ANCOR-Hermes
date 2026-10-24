"""Deterministic human-facing confirmation text, frozen with each change proposal."""
import json

LABELS = {'outcome': '目标', 'constraint': '约束', 'advice': '建议', 'context': '背景'}


def change_confirmation(store, db, rid, operations, identifier):
    creating = operations[0]['op'] == 'create'
    requirement = None if creating else store._get(db, 'requirements', rid)
    title = operations[0]['title'] if creating else requirement['title']
    verb = '创建需求' if creating else '更新需求'
    if len(operations) == 1:
        verb = {'accept': '验收需求', 'pause': '暂停需求', 'resume': '恢复需求',
                'cancel': '取消需求', 'request_changes': '要求返工',
                'reply': '回复问题', 'title': '修改需求名称'}.get(operations[0]['op'], verb)
    lines = [f'{verb}「{title}」', '']

    def value(label, text):
        # Indent exact text, preserving newlines and whitespace rather than summarizing.
        lines.append(label)
        lines.extend('    ' + line for line in text.split('\n'))

    content_changed = False
    for op in operations:
        kind = op['op']
        if kind in {'create', 'convert'}:
            if kind == 'convert':
                value('将原需求正文整理为条目。原文：', requirement['brief'])
                lines.append('整理后的完整要求：')
            for field, label in LABELS.items():
                for body in op['fields'].get(field, []):
                    value(f'• {label}', body)
            content_changed = True
        elif kind == 'append':
            value(f"• 新增{LABELS[op['field']]}", op['body'])
            content_changed = True
        elif kind in {'replace', 'invalidate'}:
            point = dict(db.execute('SELECT * FROM point_versions WHERE version_id=?', (op['version_id'],)).fetchone())
            old_label = LABELS[point['field']]
            if kind == 'invalidate':
                value(f'• 撤回{old_label}', point['body'])
            else:
                new_label = LABELS[op.get('field', point['field'])]
                lines.append(f'• 修改{old_label}' if old_label == new_label else f'• 将{old_label}改为{new_label}')
                value('  原内容：', point['body'])
                value('  新内容：', op['body'])
            content_changed = True
        elif kind == 'title':
            lines.append('• 修改需求名称')
            value('  原名称：', title)
            value('  新名称：', op['title'])
            title = op['title']
        elif kind in {'reply', 'resolve_question'}:
            q = store._get(db, 'questions', op['question_id'])
            value('• 回复问题' if kind == 'reply' else '• 将问题标记为已解决', q['body'])
            value('  回复：' if kind == 'reply' else '  解决依据：', op['body'] if kind == 'reply' else op['reason'])
            if kind == 'reply' and not any(o['op'] == 'resolve_question' and o['question_id'] == q['id'] for o in operations):
                lines.append('  本次仅记录回复，问题保持开放。')
        elif kind in {'pause', 'resume', 'cancel'}:
            lines.append('• ' + {'pause': '暂停推进此需求', 'resume': '恢复推进此需求', 'cancel': '取消此需求'}[kind])
            value('  原因：', op['reason'])
            lines.append({'pause': '  确认后，系统将停止相关执行；暂停完成前会显示处理中。',
                          'resume': '  确认后，协调者将重新检查现有工作并安排继续执行。',
                          'cancel': '  确认后，系统将结束相关任务，历史记录会保留。'}[kind])
        elif kind == 'request_changes':
            value('• 将交付退回继续处理', op['feedback'])
        elif kind == 'accept':
            event = store._event(db.execute('SELECT * FROM events WHERE id=?', (op['verification_id'],)).fetchone())
            lines.append('• 接受本次已验证的交付')
            raw = event['payload']['evidence']
            try:
                evidence = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                evidence = raw
            if isinstance(evidence, dict):
                for artifact in evidence.get('artifacts', []):
                    value('  交付成果：', artifact)
                for check in evidence.get('verification', []):
                    value('  验证结果：', check)
                for coverage in evidence.get('coverage', []):
                    point = dict(db.execute('SELECT * FROM point_versions WHERE version_id=?', (coverage['version_id'],)).fetchone())
                    value('  已核对的' + LABELS[point['field']] + '：', point['body'])
                    value('  依据：', coverage['evidence'])
            else:
                value('  验证结果：', str(evidence))
        elif kind in {'grant', 'revoke'}:
            grant = op if kind == 'grant' else store._get(db, 'grants', op['grant_id'])
            lines.append('• 授予授权' if kind == 'grant' else '• 撤回授权')
            value('  允许的动作：', grant['action'])
            value('  对象：', grant['target'])
            value('  适用范围：', grant['scope'])
        else:
            raise ValueError('unsupported confirmation operation')
        lines.append('')
    if content_changed:
        lines.append('确认后，协调者会评估这些要求，并据此安排或调整工作。')
    lines.extend(['请确认以上内容。', f'确认单：{identifier[:12]}'])
    return {'questions': [{'question': '\n'.join(lines),
                           'choices': ['暂不提交', '确认提交'], 'multi_select': False}]}
