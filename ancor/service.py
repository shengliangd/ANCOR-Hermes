"""Business adapter; native task execution remains outside this module."""
from pathlib import Path
from contextlib import contextmanager
import json
import sqlite3
import re


class Service:
    def __init__(self, ctx, store=None, board=None):
        self.ctx, self._store, self.board = ctx, store, board

    @property
    def store(self):
        if self._store is None:
            from .store import Store
            path = self.ctx.get_config('store_path')
            if not path:
                raise ValueError('configure shared absolute store_path for ANCOR')
            if not Path(path).is_absolute():
                raise ValueError('store_path must be absolute')
            self._store = Store(path)
        return self._store

    def requirement(self, args):
        action = args['action']
        if action == 'draft':
            draft = self.store.create_draft(args['brief'], args['acceptance_criteria'], args.get('replacement_of'))
            return self.presentation(draft)
        if action == 'confirm':
            # Preparation only: consent is exclusively consumed by the public post hook.
            return self.presentation(self.store.get_draft(args['draft_id']))
        if action == 'link':
            self.store.link_task(args['requirement_id'], args['task_id'])
            return self.store.snapshot()
        if action == 'cancel':
            return self.store.cancel(args['requirement_id'], args['reason'])
        if action == 'cancel_many':
            return self.store.cancel_many(args['requirement_ids'], args['reason'])
        if action == 'replace_many':
            draft = self.store.create_draft(args['brief'], args['acceptance_criteria'], args['requirement_ids'])
            return self.presentation(draft)
        raise ValueError('unknown requirement action')

    def presentation(self, draft):
        """Offer the immutable draft through native clarify in the current chat.

        Surface rendering and delivery belong to Hermes; no separate destination
        or presentation receipt is required for confirmation.
        """
        text = (f"ANCOR draft {draft['id']}\nReplacement of: {draft['replacement_of'] or 'none'}\n\n"
                f"Requirement:\n{draft['brief']}\n\nAcceptance criteria:\n{draft['acceptance_criteria']}")
        directory = Path(self.store.path + '.drafts')
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (draft['id'] + '.txt')
        try:
            with path.open('x', encoding='utf-8') as output:
                output.write(text)
        except FileExistsError:
            if path.read_text(encoding='utf-8') != text:
                raise ValueError('immutable draft file differs from stored draft')
        question = (text + f"\n\nConfirm ANCOR requirement {draft['id']}? "
                    'This confirms the requirement, not deployment or payment.')
        return {**draft, 'draft_file': str(path), 'clarify_args': {'questions': [{
            'question': question,
            'choices': [f"Do not confirm {draft['id']}", f"Confirm ANCOR draft {draft['id']}"],
            'multi_select': False}]},
            'next_step': 'Call native clarify as an actual agent tool with clarify_args unchanged. Then read ancor_get_context.'}

    @contextmanager
    def _confirmations(self):
        # Separate service-owned sidecar; no changes to the shared Store schema.
        db = sqlite3.connect(self.store.path + '.confirmations.db', timeout=30)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS pending (session TEXT, call TEXT, draft TEXT NOT NULL, '
                       'args TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(session, call))')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def pre_tool_call(self, tool_name, args, session_id='', tool_call_id='', **kwargs):
        if tool_name != 'clarify':
            return
        encoded = json.dumps(args, sort_keys=True, ensure_ascii=False)
        if 'ANCOR draft' not in encoded:
            return
        try:
            if not session_id or not tool_call_id:
                raise ValueError('formal confirmation requires session and tool-call identity')
            matches = re.findall(r'Confirm ANCOR draft ([0-9a-f]{32})', encoded)
            if len(matches) != 1:
                raise ValueError('exact ANCOR confirmation choice required')
            draft = self.store.get_draft(matches[0])
            expected = self.presentation(draft)['clarify_args']
            if args != expected:
                raise ValueError('native clarify arguments must match the complete immutable draft exactly')
            with self._confirmations() as db:
                existing = db.execute('SELECT draft, args, consumed FROM pending WHERE session=? AND call=?',
                                      (session_id, tool_call_id)).fetchone()
                if existing and existing != (draft['id'], encoded, 0):
                    raise ValueError('confirmation call identity already used')
                db.execute('INSERT OR IGNORE INTO pending(session, call, draft, args) VALUES (?, ?, ?, ?)',
                           (session_id, tool_call_id, draft['id'], encoded))
        except Exception as exc:
            return {'action': 'block', 'message': str(exc)}

    def post_tool_call(self, tool_name, args, result, session_id='', tool_call_id='', status='ok', **kwargs):
        if tool_name != 'clarify' or not session_id or not tool_call_id:
            return
        with self._confirmations() as db:
            row = db.execute('SELECT draft, args, consumed FROM pending WHERE session=? AND call=?',
                             (session_id, tool_call_id)).fetchone()
            if not row or row[2]:
                return
            # Consume failures as well; a cancelled call cannot later be replayed as consent.
            db.execute('UPDATE pending SET consumed=1 WHERE session=? AND call=?', (session_id, tool_call_id))
            if status != 'ok' or json.dumps(args, sort_keys=True, ensure_ascii=False) != row[1]:
                return
            try:
                payload = json.loads(result) if isinstance(result, str) else result
                question = self.presentation(self.store.get_draft(row[0]))['clarify_args']['questions'][0]
                expected = {'question': question['question'], 'choices_offered': question['choices'],
                            'status': 'answered', 'user_response': f'Confirm ANCOR draft {row[0]}'}
                if not isinstance(payload, dict) or payload.get('outcome') != 'submitted' or payload.get('responses') != [expected]:
                    return
            except (ValueError, TypeError, KeyError):
                return
            # Store.confirm is idempotent: a crash after commit before consuming the sidecar
            # can safely retry this same genuine result without duplicating activation events.
            self.store.confirm(row[0], json.dumps({'source': 'native_clarify', 'session_id': session_id,
                                                  'tool_call_id': tool_call_id, 'draft_id': row[0]}, sort_keys=True))

    def get_context(self, args):
        ids = args.get('ack_event_ids', [])
        if not isinstance(ids, list) or any(type(i) is not int or i < 1 for i in ids):
            raise ValueError('ack_event_ids must contain positive integer IDs')
        self.store.ack_events(ids)
        return self.store.snapshot()

    def acceptance(self, args):
        requirement = self.store.get_requirement(args['requirement_id'])
        if requirement['status'] != 'active':
            raise ValueError('requirement is not active')
        if args['action'] == 'reject':
            return self.store.reject(requirement['id'], args['reason'])
        if args['action'] != 'accept':
            raise ValueError('unknown acceptance action')
        evidence = args.get('evidence')
        if not isinstance(evidence, dict) or evidence.get('criteria') != requirement['acceptance_criteria']:
            raise ValueError('evidence must bind exact acceptance criteria')
        for field in ('verification', 'artifacts', 'task_ids'):
            values = evidence.get(field)
            if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
                raise ValueError(f'evidence requires nonempty {field}')
        links = {x['task_id'] for x in self.store.snapshot()['task_links'] if x['requirement_id'] == requirement['id']}
        if not set(evidence['task_ids']) <= links:
            raise ValueError('evidence references unlinked tasks')
        import json
        return self.store.accept(requirement['id'], json.dumps(evidence, ensure_ascii=False))
