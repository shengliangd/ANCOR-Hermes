"""ANCOR execution identity carried by Hermes' frozen per-session launch policy."""
import json
import os
from uuid import uuid4

PREFIX = 'ANCOR execution identity: '


def launch_identity(role, goal_id=None):
    return PREFIX + json.dumps({'role': role, 'goal_id': goal_id, 'run_id': uuid4().hex}, separators=(',', ':'))


def session_value(name, default=''):
    try:
        from agent.session_client_env import session_env
    except ImportError:
        return os.environ.get(name, default)
    return session_env(name, default)


def identity():
    text = session_value('HERMES_EPHEMERAL_SYSTEM_PROMPT')
    if text.startswith(PREFIX):
        try:
            value = json.loads(text[len(PREFIX):])
            if isinstance(value, dict) and value.get('role') in {'root', 'goal', 'judge'}:
                return value
        except ValueError:
            pass
    return {'role': session_value('ANCOR_ROLE'),
            'goal_id': session_value('GOAL_ID') or session_value('ANCOR_GOAL_ID')}
