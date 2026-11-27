"""Dispatch shared-profile plugin tools using the current session's frozen identity."""
from collections import OrderedDict
import json
import threading

from .session_identity import identity


class _CollectedContext:
    def __init__(self, ctx):
        self.ctx = ctx
        self.tools = {}
        self.hooks = {}

    def __getattr__(self, name):
        return getattr(self.ctx, name)

    def register_tool(self, **definition):
        self.tools[definition['name']] = definition

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def _uncached_check(fn):
    try:
        from tools.registry import no_cache_check_fn
        return no_cache_check_fn(fn)
    except Exception:
        return fn


def register_session_roles(ctx, register_role, resolve_role, *, judge_runner=None):
    def collect(role):
        collected = _CollectedContext(ctx)
        register_role(collected, role_override=role, judge_runner=judge_runner)
        return collected

    templates = {role: collect(role) for role in ('root', 'goal', 'judge')}
    # Each session has its own closures and observed-model sink. A shared gateway
    # must never retain the launch role or observations of its first client.
    sessions = OrderedDict()
    lock = threading.RLock()
    def current():
        role = resolve_role(ctx)
        if role not in templates:
            return None
        key = json.dumps(identity(), sort_keys=True)
        with lock:
            if key not in sessions:
                sessions[key] = collect(role)
                if len(sessions) > 512:
                    sessions.popitem(last=False)
            sessions.move_to_end(key)
            return sessions[key]

    definitions = {}
    for template in templates.values():
        definitions.update(template.tools)
    for name, definition in definitions.items():
        def dispatch(args, _name=name, **kwargs):
            selected = current()
            if selected is None or _name not in selected.tools:
                return json.dumps({'code': 'forbidden', 'error': 'Tool is not authorized for this ANCOR session role'})
            return selected.tools[_name]['handler'](args, **kwargs)

        def check_allowed(_name=name):
            role = resolve_role(ctx)
            if role not in templates or _name not in templates[role].tools:
                return False
            orig = templates[role].tools[_name].get('check_fn')
            return bool(orig()) if callable(orig) else True

        ctx.register_tool(**{**definition, 'handler': dispatch, 'check_fn': _uncached_check(check_allowed)})

    def fence(tool_name, args=None, **kwargs):
        selected = current()
        if selected is None:
            return {'action': 'block', 'message': 'ANCOR execution session has no authorized role'}
        if tool_name.startswith('ancor_') and tool_name not in selected.tools:
            return {'action': 'block', 'message': 'Tool is not authorized for this ANCOR session role'}
        hook = selected.hooks.get('pre_tool_call')
        return hook(tool_name, args, **kwargs) if hook else None

    def observed(**kwargs):
        selected = current()
        hook = selected.hooks.get('post_api_request') if selected else None
        return hook(**kwargs) if hook else None

    ctx.register_hook('pre_tool_call', fence)
    ctx.register_hook('post_api_request', observed)
