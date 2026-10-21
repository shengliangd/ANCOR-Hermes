"""Business adapter; native task execution remains outside this module."""
from pathlib import Path
from contextlib import contextmanager
import json
import logging
import sqlite3
import re


log = logging.getLogger(__name__)


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
            summary = self.store._optional_text(args.get('summary'))
            draft = self.store.create_draft(args['brief'], args['acceptance_criteria'],
                                           args.get('replacement_of'), summary,
                                           title=args['title'])
            return self.presentation(draft)
        if action == 'confirm':
            # Preparation only: consent is exclusively consumed by the public post hook.
            return self.presentation(self.store.get_draft(args['draft_id']))
        if action == 'cancel':
            summary = self.store._optional_text(args.get('summary'))
            cancellation = self.store.create_cancellation(
                args['requirement_ids'], args.get('reason'), summary)
            return self.cancellation_presentation(cancellation)
        if action in {'pause', 'resume'}:
            summary = self.store._optional_text(args.get('summary'))
            command = self.store.create_requirement_command(
                action, args['requirement_id'], args['reason'], summary)
            return self.command_presentation(command)
        raise ValueError('unknown requirement action')

    def _write_presentation(self, identifier, text):
        directory = Path(self.store.path + '.drafts')
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (identifier + '.txt')
        try:
            with path.open('x', encoding='utf-8') as output:
                output.write(text)
        except FileExistsError:
            if path.read_text(encoding='utf-8') != text:
                raise ValueError('immutable presentation file differs from stored proposal')
        return str(path)

    def presentation(self, draft):
        """Offer the immutable draft through native clarify in the current chat.

        Surface rendering and delivery belong to Hermes; no separate destination
        or presentation receipt is required for confirmation.
        """
        summary = draft['summary'] or 'No high-level summary was provided.'
        text = (f"ANCOR draft {draft['id']}\n"
                f"Title: {draft['title']}\n"
                f"High-level summary:\n{summary}\n\n"
                f"Exact requirement to be stored:\n"
                f"Replacement of: {draft['replacement_of'] or 'none'}\n"
                f"Requirement:\n{draft['brief']}\n\n"
                f"Acceptance criteria:\n{draft['acceptance_criteria']}")
        path = self._write_presentation(draft['id'], text)
        question = (text + f"\n\nConfirm ANCOR requirement {draft['id']}? "
                    'This confirms the requirement, not deployment or payment.')
        return {**draft, 'draft_file': path, 'clarify_args': {'questions': [{
            'question': question,
            'choices': [f"Do not confirm {draft['id']}", f"Confirm ANCOR draft {draft['id']}"],
            'multi_select': False}]},
            'next_step': 'Call native clarify as an actual agent tool with clarify_args unchanged. Then read ancor_get_context.'}

    def cancellation_presentation(self, cancellation):
        """Offer an immutable cancellation proposal without changing state."""
        requirements = [self.store.get_requirement(i) for i in cancellation['requirement_ids']]
        blocks = []
        for requirement in requirements:
            blocks.append(
                f"- {requirement['id']}\n"
                f"  Title: {requirement['title']}")
        summary = cancellation['summary'] or 'No high-level summary was provided.'
        text = (f"ANCOR cancellation {cancellation['id']}\n"
                f"High-level summary:\n{summary}\n\n"
                f"Exact cancellation to be stored:\n"
                f"Reason: {cancellation['reason'] or 'not provided'}\n\n"
                f"Requirements to cancel ({len(requirements)}):\n" + '\n'.join(blocks))
        path = self._write_presentation(cancellation['id'], text)
        question = (text + f"\n\nConfirm cancellation of ANCOR requirements {cancellation['id']}? "
                    'This stops coordination; requirement history remains.')
        return {**cancellation, 'draft_file': path, 'clarify_args': {'questions': [{
            'question': question,
            'choices': [f"Keep ANCOR requirements {cancellation['id']}",
                        f"Cancel ANCOR requirements {cancellation['id']}"],
            'multi_select': False}]},
            'next_step': 'Call native clarify as an actual agent tool with clarify_args unchanged. Then read ancor_get_context.'}

    def command_presentation(self, command):
        """Offer an immutable pause/resume proposal without changing status."""
        requirement = self.store.get_requirement(command['requirement_id'])
        action = command['action']
        verb = 'pause' if action == 'pause' else 'resume'
        summary = command['summary'] or 'No high-level summary was provided.'
        text = (f"ANCOR {verb} {command['id']}\n"
                f"High-level summary:\n{summary}\n\n"
                f"Exact {verb} to be stored:\n"
                f"Requirement: {requirement['id']}\n"
                f"Title: {requirement['title']}\n"
                f"Requirement text:\n{requirement['brief']}\n\n"
                f"Acceptance criteria:\n{requirement['acceptance_criteria']}\n\n"
                f"Reason: {command['reason']}")
        path = self._write_presentation(command['id'], text)
        if action == 'pause':
            keep = f"Keep ANCOR requirement {requirement['id']} running"
            confirm = f"Pause ANCOR requirement {requirement['id']}"
            prompt = f"Confirm pause of ANCOR requirement {requirement['id']}?"
        else:
            keep = f"Keep ANCOR requirement {requirement['id']} paused"
            confirm = f"Resume ANCOR requirement {requirement['id']}"
            prompt = f"Confirm resume of ANCOR requirement {requirement['id']}?"
        return {**command, 'draft_file': path, 'clarify_args': {'questions': [{
            'question': text + f"\n\n{prompt}",
            'choices': [keep, confirm], 'multi_select': False}]},
            'next_step': 'Call native clarify as an actual agent tool with clarify_args unchanged. Then read ancor_get_context.'}

    @contextmanager
    def _confirmations(self):
        # Separate service-owned sidecar; no changes to the shared Store schema.
        db = sqlite3.connect(self.store.path + '.confirmations.db', timeout=30)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS confirmations ('
                       'session TEXT, call TEXT, kind TEXT NOT NULL, target TEXT NOT NULL, '
                       'args TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0, '
                       'outcome TEXT, error TEXT, '
                       'PRIMARY KEY(session, call))')
            present = {row[1] for row in db.execute('PRAGMA table_info(confirmations)')}
            for column in ('outcome', 'error'):
                if column not in present:
                    db.execute(f'ALTER TABLE confirmations ADD COLUMN {column} TEXT')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _finish_confirmation(db, session_id, tool_call_id, outcome, error=None):
        db.execute('UPDATE confirmations SET consumed=1, outcome=?, error=? '
                   'WHERE session=? AND call=?',
                   (outcome, None if error is None else str(error), session_id, tool_call_id))

    @staticmethod
    def _confirmation_mismatch(payload, question, answer):
        if not isinstance(payload, dict):
            return 'clarify result is not an object'
        if payload.get('outcome') != 'submitted':
            return f"clarify outcome is {payload.get('outcome')!r}, not 'submitted'"
        responses = payload.get('responses')
        if not isinstance(responses, list) or len(responses) != 1:
            return 'clarify result must contain exactly one response'
        response = responses[0]
        if not isinstance(response, dict):
            return 'clarify response is not an object'
        expected = {'question': question['question'], 'choices_offered': question['choices'],
                    'status': 'answered', 'user_response': answer}
        for field, value in expected.items():
            if response.get(field) != value:
                return f'clarify response {field} does not match the exact confirmation'
        return None

    def pre_tool_call(self, tool_name, args, session_id='', tool_call_id='', **kwargs):
        if tool_name != 'clarify':
            return
        encoded = json.dumps(args, sort_keys=True, ensure_ascii=False)
        if 'ANCOR draft ' in encoded:
            kind, pattern = 'draft', r'Confirm ANCOR draft ([0-9a-f]{32})'
        elif 'ANCOR cancellation ' in encoded:
            kind, pattern = 'cancellation', r'ANCOR cancellation ([0-9a-f]{32})'
        elif 'ANCOR pause ' in encoded:
            kind, pattern = 'pause', r'ANCOR pause ([0-9a-f]{32})'
        elif 'ANCOR resume ' in encoded:
            kind, pattern = 'resume', r'ANCOR resume ([0-9a-f]{32})'
        else:
            return
        try:
            if not session_id or not tool_call_id:
                raise ValueError('formal confirmation requires session and tool-call identity')
            matches = re.findall(pattern, encoded)
            if len(matches) != 1:
                raise ValueError('exact ANCOR confirmation choice required')
            target = matches[0]
            if kind == 'draft':
                expected = self.presentation(self.store.get_draft(target))['clarify_args']
            elif kind == 'cancellation':
                expected = self.cancellation_presentation(
                    self.store.get_cancellation(target))['clarify_args']
            else:
                expected = self.command_presentation(
                    self.store.get_requirement_command(target))['clarify_args']
            if args != expected:
                raise ValueError('native clarify arguments must match the complete immutable proposal exactly')
            with self._confirmations() as db:
                existing = db.execute('SELECT kind, target, args, consumed FROM confirmations '
                                      'WHERE session=? AND call=?',
                                      (session_id, tool_call_id)).fetchone()
                if existing and existing != (kind, target, encoded, 0):
                    raise ValueError('confirmation call identity already used')
                db.execute('INSERT OR IGNORE INTO confirmations('
                           'session, call, kind, target, args, outcome) '
                           'VALUES (?, ?, ?, ?, ?, ?)',
                           (session_id, tool_call_id, kind, target, encoded, 'pending'))
        except Exception as exc:
            return {'action': 'block', 'message': str(exc)}

    def post_tool_call(self, tool_name, args, result, session_id='', tool_call_id='', status='ok', **kwargs):
        if tool_name != 'clarify' or not session_id or not tool_call_id:
            return
        with self._confirmations() as db:
            row = db.execute('SELECT kind, target, args, consumed FROM confirmations '
                             'WHERE session=? AND call=?',
                             (session_id, tool_call_id)).fetchone()
            if not row or row[3]:
                return
            kind, target, encoded = row[0], row[1], row[2]
            if status != 'ok':
                self._finish_confirmation(
                    db, session_id, tool_call_id, 'error', f'clarify status is {status!r}')
                return
            if json.dumps(args, sort_keys=True, ensure_ascii=False) != encoded:
                self._finish_confirmation(
                    db, session_id, tool_call_id, 'rejected',
                    'clarify arguments changed after registration')
                return
            try:
                payload = json.loads(result) if isinstance(result, str) else result
                if isinstance(payload, dict) and payload.get('error'):
                    raise RuntimeError(f"clarify failed: {payload['error']}")
                if kind == 'draft':
                    question = self.presentation(self.store.get_draft(target))['clarify_args']['questions'][0]
                    answer = f'Confirm ANCOR draft {target}'
                elif kind == 'cancellation':
                    question = self.cancellation_presentation(
                        self.store.get_cancellation(target))['clarify_args']['questions'][0]
                    answer = f'Cancel ANCOR requirements {target}'
                else:
                    command = self.store.get_requirement_command(target)
                    requirement = self.store.get_requirement(command['requirement_id'])
                    question = self.command_presentation(command)['clarify_args']['questions'][0]
                    answer = (f"Pause ANCOR requirement {requirement['id']}" if kind == 'pause'
                              else f"Resume ANCOR requirement {requirement['id']}")
                mismatch = self._confirmation_mismatch(payload, question, answer)
            except Exception as exc:
                self._finish_confirmation(db, session_id, tool_call_id, 'error', exc)
                log.warning('ANCOR confirmation validation failed for %s: %s', target, exc)
                return
            if mismatch:
                self._finish_confirmation(db, session_id, tool_call_id, 'rejected', mismatch)
                return
            reference = json.dumps({'source': 'native_clarify', 'kind': kind, 'target': target,
                                    'session_id': session_id, 'tool_call_id': tool_call_id},
                                   sort_keys=True)
            try:
                if kind == 'draft':
                    self.store.confirm(target, reference)
                elif kind == 'cancellation':
                    self.store.confirm_cancellation(target, reference)
                else:
                    self.store.confirm_requirement_command(target, reference)
            except Exception as exc:
                self._finish_confirmation(db, session_id, tool_call_id, 'error', exc)
                log.exception('ANCOR confirmation commit failed for %s', target)
                return
            self._finish_confirmation(db, session_id, tool_call_id, 'confirmed')

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
