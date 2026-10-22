"""Worker-only filtering for native Kanban task context."""
import json


_HIDDEN_HEADINGS = (
    '## Parent task results',
    '## Recent work by @',
)


def strip_worker_context(text):
    """Remove cross-task handoffs and role history from a Kanban worker context.

    The worker card remains the execution contract. Native Kanban appends
    point-in-time parent results and recent work by the same assignee; neither
    belongs in an ANCOR worker's context.
    """
    if not isinstance(text, str) or not text:
        return text
    kept = []
    skipping = False
    changed = False
    for line in text.splitlines(keepends=True):
        if line.startswith('## '):
            heading = line.rstrip('\r\n')
            skipping = heading.startswith(_HIDDEN_HEADINGS)
            if skipping:
                changed = True
                continue
        if not skipping:
            kept.append(line)
    return ''.join(kept) if changed else text


def transform_kanban_show_result(tool_name, result, **_kwargs):
    """Strip hidden context from one ``kanban_show`` tool result, if present."""
    if tool_name != 'kanban_show' or not isinstance(result, str):
        return None
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    context = payload.get('worker_context')
    if not isinstance(context, str):
        return None
    cleaned = strip_worker_context(context)
    if cleaned == context:
        return None
    payload['worker_context'] = cleaned
    return json.dumps(payload, ensure_ascii=False)
