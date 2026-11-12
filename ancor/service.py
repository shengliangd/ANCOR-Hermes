"""Business adapter; native task execution remains outside this module."""
from pathlib import Path
from contextlib import contextmanager
import hashlib
import json
import logging
import sqlite3
import re

from .views import collection, note_detail, note_summary


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

    @property
    def native_board(self):
        if self.board is None:
            from .board import NativeBoard
            name = self.ctx.get_config('board')
            if not name:
                raise ValueError('configure ANCOR board')
            self.board = NativeBoard(
                name, profile=self.ctx.get_config(
                    'coordinator_profile', 'ancor-coordinator'))
        return self.board

    def request(self, args):
        action = args['action']
        if action in {'pause', 'resume', 'cancel', 'accept', 'request_changes'}:
            ids = args.get('request_ids', [args.get('request_id')])
            if len(ids) == 1 and ids[0] and self.store.get_request(ids[0])['content_mode'] == 'points':
                req = self.store.get_request(ids[0])
                op = {'op': action}
                if action == 'accept':
                    verification = self.store.latest_verification(ids[0])
                    if verification is None:
                        raise ValueError('verification required before acceptance')
                    op['verification_id'] = verification['id']
                elif action == 'request_changes':
                    op['feedback'] = args['feedback']
                else:
                    op['reason'] = args.get('reason') or action
                return self.change({'action': 'propose', 'request_id': ids[0], 'base': req,
                                    'input': args.get('feedback') or args.get('reason') or action, 'operations': [op]})
        if action == 'draft':
            summary = self.store._optional_text(args.get('summary'))
            draft = self.store.create_draft(args['brief'], args.get('replacement_of'),
                                           summary, title=args['title'])
            return self._prepare_confirmation('draft', self.presentation(draft))
        if action == 'confirm':
            # Preparation only: consent is exclusively consumed by the public post hook.
            return self._prepare_confirmation(
                'draft', self.presentation(self.store.get_draft(args['draft_id'])))
        if action == 'cancel':
            summary = self.store._optional_text(args.get('summary'))
            cancellation = self.store.create_cancellation(
                args['request_ids'], args.get('reason'), summary)
            return self._prepare_confirmation(
                'cancellation', self.cancellation_presentation(cancellation))
        if action in {'pause', 'resume'}:
            summary = self.store._optional_text(args.get('summary'))
            command = self.store.create_request_command(
                action, args['request_id'], args['reason'], summary)
            return self._prepare_confirmation(action, self.command_presentation(command))
        if action == 'advance':
            from .transitions import advance_request
            return advance_request(self.store, self.native_board,
                                       args['request_id'])
        if action in {'accept', 'request_changes'}:
            command = self.store.create_human_decision(
                args['request_id'], action, args.get('feedback'))
            return self._prepare_confirmation(
                'human_decision', self.command_presentation(command))
        raise ValueError('unknown request action')

    def signal(self, args):
        text = args.get('text')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('text must be a non-empty string')
        return self._prepare_confirmation(
            'signal', self.signal_presentation(self.store.propose_signal(text, self.ctx.profile_name)))

    def signal_presentation(self, proposal):
        identifier = proposal['id']
        text = '确认将以下提醒发送给协调者？\n\n' + proposal['text']
        path = self._write_presentation('signal', identifier, text)
        return {**proposal, 'presentation': text, 'draft_file': path, 'clarify_args': {'questions': [{
            'question': text,
            'choices': [f'Do not send {identifier}', f'Confirm ANCOR signal {identifier}'],
            'multi_select': False}]},
            'next_step': 'Call native clarify with clarify_args unchanged. Only explicit confirmation enqueues this information; it does not change requests or grant authorization.'}

    def change(self, args):
        action = args['action']
        if action == 'propose':
            # Actor/source identity comes from this adapter, never a model argument.
            actor = getattr(self.ctx, 'profile_name', 'ancor-interactor')
            surface = self._session_surface()
            proposal = self.store.propose_change(
                args.get('request_id'), args['operations'], actor=actor,
                source={'kind': 'interactor_proposal', 'human_identity': None,
                        'input': args['input'], **({'surface': surface} if surface else {})},
                base=args.get('base'))
            response = self.change_presentation(proposal)
            return self._prepare_confirmation('change', response)
        if action == 'get':
            proposal = self.store.get_change(args['change_id'])
            if proposal['source'].get('kind') == 'coordinator_note':
                raise PermissionError('coordinator notes are private')
            return self._prepare_confirmation('change', self.change_presentation(proposal))
        if action == 'history':
            return self.store.list_changes(args['request_id'], before=args.get('before'),
                                           limit=args.get('limit', 50),
                include_notes=getattr(self.ctx, 'profile_name', '') == self.ctx.get_config('coordinator_profile', 'ancor-coordinator'))
        raise ValueError('unknown change action')

    @staticmethod
    def _session_surface():
        """Return the active Hermes client surface without coupling tests to Hermes."""
        try:
            from hermes_cli.plugin_session import session_surface
            return str(session_surface() or '').strip().lower()
        except Exception:
            return ''

    @staticmethod
    def _presentation_digest(text):
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def _desktop_clarify_args(self, kind, target, full_args, path, digest):
        """Concise Desktop clarify bound to the immutable Markdown file."""
        question = full_args['questions'][0]
        return {'questions': [{
            **question,
            'question': ('请打开以下 Markdown 文件核对完整 ANCOR 确认内容。\n'
                         f'文件：{path}\n'
                         '如无法直接打开，可复制上面的路径。\n'
                         f'内容摘要：{digest[:12]}\n'
                         f'确认单：{kind}:{target}'),
        }]}

    def _prepare_confirmation(self, kind, response):
        """Bind a Desktop confirmation to its immutable Markdown file."""
        text = response.get('presentation')
        path = response.get('draft_file')
        identifier = response.get('id')
        if not text or not path or not identifier:
            return response
        digest = self._presentation_digest(text)
        response['presentation_digest'] = digest
        if self._session_surface() in {'desktop', 'webapp'}:
            response['clarify_args'] = self._desktop_clarify_args(
                kind, identifier, response['clarify_args'], path, digest)
            response['next_step'] = ('Use the unchanged concise clarify_args. The immutable Markdown file '
                                     'is the complete confirmation content; only the native confirmation commits it.')
        return response

    def _raw_confirmation(self, kind, target):
        if kind == 'signal':
            return self.signal_presentation(self.store.get_signal_proposal(target))
        if kind == 'change':
            return self.change_presentation(self.store.get_change(target))
        if kind == 'draft':
            return self.presentation(self.store.get_draft(target))
        if kind == 'cancellation':
            return self.cancellation_presentation(self.store.get_cancellation(target))
        if kind == 'response':
            proposal = self._notification_outbox().get_response_proposal(target)
            return self.response_presentation(proposal)
        if kind in {'human_decision', 'pause', 'resume'}:
            return self.command_presentation(self.store.get_request_command(target))
        raise ValueError(f'unknown ANCOR confirmation kind {kind!r}')

    def _clarify_candidates(self, kind, target):
        """Full clarify plus the digest-bound Desktop variant."""
        response = self._raw_confirmation(kind, target)
        full = response['clarify_args']
        text = response['presentation']
        path = response.get('draft_file') or self._write_presentation(kind, target, text)
        digest = self._presentation_digest(text)
        candidates = [full]
        if self._session_surface() in {'desktop', 'webapp'}:
            candidates.append(self._desktop_clarify_args(kind, target, full, path, digest))
        return candidates

    def change_presentation(self, proposal):
        identifier = proposal['id']
        saved = self.store.change_presentation_args(identifier)
        if saved is not None:
            text = saved['questions'][0]['question']
            path = self._write_presentation('change', identifier, text)
            return {**proposal, 'presentation': text, 'draft_file': path, 'clarify_args': saved,
                    'next_step': 'Present the human-readable confirmation unchanged through native clarify. '
                                 'Do not expose internal IDs, base versions, or operation JSON as the user-facing explanation. '
                                 'Only exact native confirmation commits this proposal.'}
        # Pending proposals from older releases retain their exact original presentation.
        text = (f"ANCOR change {identifier}\nRequest: {proposal['request_id']}\n"
                + 'Base versions: ' + json.dumps(proposal['base'], ensure_ascii=False, sort_keys=True)
                + '\nOriginal input:\n' + str(proposal['source'].get('input', ''))
                + '\nExact operations:\n' + json.dumps(proposal['operations'], ensure_ascii=False, indent=2))
        path = self._write_presentation('change', identifier, text)
        return {**proposal, 'presentation': text, 'draft_file': path, 'clarify_args': {'questions': [{
            'question': text + '\n\nApply this exact change?',
            'choices': [f'Do not apply {identifier}', f'Apply ANCOR change {identifier}'],
            'multi_select': False}]},
            'next_step': 'Submit the exact clarify_args through native clarify. A proposal is not effective until confirmed.'}

    def _write_presentation(self, kind, identifier, text):
        directory = Path(self.store.path + '.drafts')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        digest = self._presentation_digest(text)
        path = directory / f'{kind}.{identifier}.{digest[:12]}.md'
        try:
            with path.open('x', encoding='utf-8') as output:
                output.write(text)
            path.chmod(0o600)
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
                f"Exact request to be stored:\n"
                f"Replacement of: {draft['replacement_of'] or 'none'}\n"
                f"Request:\n{draft['brief']}")
        path = self._write_presentation('draft', draft['id'], text)
        question = (text + f"\n\nConfirm ANCOR request {draft['id']}? "
                    'This confirms the request, not deployment or payment.')
        return {**draft, 'presentation': text, 'draft_file': path, 'clarify_args': {'questions': [{
            'question': question,
            'choices': [f"Do not confirm {draft['id']}", f"Confirm ANCOR draft {draft['id']}"],
            'multi_select': False}]},
            'next_step': 'Call native clarify as an actual agent tool with clarify_args unchanged. Then read ancor_get_context.'}

    def cancellation_presentation(self, cancellation):
        """Offer an immutable cancellation proposal without changing state."""
        requests = [self.store.get_request(i) for i in cancellation['request_ids']]
        blocks = []
        for request in requests:
            blocks.append(
                f"- {request['id']}\n"
                f"  Title: {request['title']}")
        summary = cancellation['summary'] or 'No high-level summary was provided.'
        text = (f"ANCOR cancellation {cancellation['id']}\n"
                f"High-level summary:\n{summary}\n\n"
                f"Exact cancellation to be stored:\n"
                f"Reason: {cancellation['reason'] or 'not provided'}\n\n"
                f"Requests to cancel ({len(requests)}):\n" + '\n'.join(blocks))
        path = self._write_presentation('cancellation', cancellation['id'], text)
        question = (text + f"\n\nConfirm cancellation of ANCOR requests {cancellation['id']}? "
                    'This stops coordination; request history remains.')
        return {**cancellation, 'presentation': text, 'draft_file': path, 'clarify_args': {'questions': [{
            'question': question,
            'choices': [f"Keep ANCOR requests {cancellation['id']}",
                        f"Cancel ANCOR requests {cancellation['id']}"],
            'multi_select': False}]},
            'next_step': 'Call native clarify as an actual agent tool with clarify_args unchanged. Then read ancor_get_context.'}

    def command_presentation(self, command):
        """Offer an immutable transition or human-acceptance proposal."""
        request = self.store.get_request(command['request_id'])
        action = command['action']
        if action in {'human_accept', 'human_request_changes'}:
            verification = self.store.latest_verification(request['id'])
            evidence = (verification['payload'].get('evidence')
                        if verification is not None else None)
            decision = 'accept' if action == 'human_accept' else 'request_changes'
            text = (f"ANCOR human acceptance {command['id']}\n"
                    f"Decision: {decision}\n"
                    f"Request: {request['id']}\n"
                    f"Title: {request['title']}\n"
                    f"Request text:\n{request['brief']}\n\n"
                    f"Coordinator verification evidence:\n{evidence or 'not recorded'}")
            if action == 'human_request_changes':
                text += f"\n\nExact feedback to be stored:\n{command['reason']}"
            path = self._write_presentation('human_decision', command['id'], text)
            keep = f"Leave ANCOR request {request['id']} awaiting acceptance"
            confirm = (f"Accept ANCOR request {request['id']}"
                       if action == 'human_accept' else
                       f"Request changes to ANCOR request {request['id']}")
            prompt = (f"Confirm final acceptance of ANCOR request {request['id']}?"
                      if action == 'human_accept' else
                      f"Confirm requesting changes to ANCOR request {request['id']}?")
            return {**command, 'presentation': text, 'draft_file': path, 'clarify_args': {'questions': [{
                'question': text + f"\n\n{prompt}",
                'choices': [keep, confirm], 'multi_select': False}]},
                'next_step': 'Call native clarify as an actual agent tool with clarify_args unchanged.'}
        verb = 'pause' if action == 'pause' else 'resume'
        summary = command['summary'] or 'No high-level summary was provided.'
        text = (f"ANCOR {verb} {command['id']}\n"
                f"High-level summary:\n{summary}\n\n"
                f"Exact {verb} to be stored:\n"
                f"Request: {request['id']}\n"
                f"Title: {request['title']}\n"
                f"Request text:\n{request['brief']}\n\n"
                f"Reason: {command['reason']}")
        path = self._write_presentation(action, command['id'], text)
        if action == 'pause':
            keep = f"Keep ANCOR request {request['id']} running"
            confirm = f"Pause ANCOR request {request['id']}"
            prompt = f"Confirm pause of ANCOR request {request['id']}?"
        else:
            keep = f"Keep ANCOR request {request['id']} paused"
            confirm = f"Resume ANCOR request {request['id']}"
            prompt = f"Confirm resume of ANCOR request {request['id']}?"
        return {**command, 'presentation': text, 'draft_file': path, 'clarify_args': {'questions': [{
            'question': text + f"\n\n{prompt}",
            'choices': [keep, confirm], 'multi_select': False}]},
            'next_step': 'Call native clarify as an actual agent tool with clarify_args unchanged. Then read ancor_get_context.'}

    def _notification_outbox(self):
        from .notification import NotificationOutbox
        if self.ctx is None:
            raise ValueError('notification confirmation requires a profile context')
        target = self.ctx.get_config('human_target')
        coordinator = self.ctx.get_config('coordinator_profile', 'ancor-coordinator')
        return NotificationOutbox(
            self.store.path + '.notifications.db',
            coordinator_profile=coordinator, target=target)

    def response_presentation(self, proposal):
        """Offer one exact human reply for native confirmation."""
        request = self.store.get_request(proposal['request_id'])
        text = (f"ANCOR response {proposal['id']}\n"
                f"Request: {request['id']}\n"
                f"Title: {request['title']}\n"
                f"Task: {proposal.get('task_id') or 'not specified'}\n"
                f"Question asked:\n{proposal['message']}\n\n"
                f"Exact human reply to be recorded:\n{proposal['answer']}")
        path = self._write_presentation('response', proposal['id'], text)
        return {**proposal,
                'presentation': text,
                'draft_file': path,
                'clarify_args': {'questions': [{
                    'question': text + f"\n\nConfirm this reply as ANCOR response {proposal['id']}?",
                    'choices': [f"Do not confirm {proposal['id']}",
                                f"Confirm ANCOR response {proposal['id']}"],
                    'multi_select': False}]},
                'next_step': ('Call native clarify as an actual agent tool with clarify_args '
                              'unchanged. The reply is recorded only by the exact confirmation.')}

    def prepared_response_presentation(self, proposal):
        return self._prepare_confirmation('response', self.response_presentation(proposal))

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
        questions = args.get('questions') if isinstance(args, dict) else None
        question_text = questions[0].get('question', '') if isinstance(questions, list) and questions and isinstance(questions[0], dict) else ''
        typed_reference = re.search(r'\n确认单：([a-z_]+):([0-9a-f]{32,64})$', question_text) if isinstance(question_text, str) else None
        human_reference = re.search(r'\n确认单：([0-9a-f]{12})$', question_text) if isinstance(question_text, str) else None
        choices = questions[0].get('choices') if isinstance(questions, list) and questions and isinstance(questions[0], dict) else None
        if typed_reference:
            kind, target = typed_reference.groups()
            pattern = None
            human_reference = None
        elif isinstance(choices, list) and any(
                isinstance(choice, str) and re.fullmatch(r'Confirm ANCOR signal [0-9a-f]{32}', choice)
                for choice in choices):
            kind, pattern = 'signal', r'Confirm ANCOR signal ([0-9a-f]{32})'
            human_reference = None
        elif human_reference:
            kind, pattern = 'change', None
        elif 'ANCOR change ' in encoded:
            kind, pattern = 'change', r'Apply ANCOR change ([0-9a-f]{32})'
        elif 'ANCOR human acceptance ' in encoded:
            kind, pattern = 'human_decision', r'ANCOR human acceptance ([0-9a-f]{32})'
        elif 'ANCOR draft ' in encoded:
            kind, pattern = 'draft', r'Confirm ANCOR draft ([0-9a-f]{32})'
        elif 'ANCOR cancellation ' in encoded:
            kind, pattern = 'cancellation', r'ANCOR cancellation ([0-9a-f]{32})'
        elif 'ANCOR pause ' in encoded:
            kind, pattern = 'pause', r'ANCOR pause ([0-9a-f]{32})'
        elif 'ANCOR resume ' in encoded:
            kind, pattern = 'resume', r'ANCOR resume ([0-9a-f]{32})'
        elif 'ANCOR response ' in encoded:
            kind, pattern = 'response', r'Confirm ANCOR response ([0-9a-f]{64})'
        else:
            return
        try:
            if not session_id or not tool_call_id:
                raise ValueError('formal confirmation requires session and tool-call identity')
            if typed_reference:
                pass
            elif human_reference:
                target = self.store.change_by_reference(human_reference.group(1))
            else:
                matches = re.findall(pattern, json.dumps(choices) if kind == 'signal' else encoded)
                if len(matches) != 1:
                    raise ValueError('exact ANCOR confirmation choice required')
                target = matches[0]
            expected = self._clarify_candidates(kind, target)
            if args not in expected:
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
                candidates = self._clarify_candidates(kind, target)
                expected_args = next((candidate for candidate in candidates if candidate == args), None)
                if expected_args is None:
                    raise RuntimeError('clarify arguments no longer match the immutable proposal')
                question = expected_args['questions'][0]
                if kind in {'signal', 'change'}:
                    answer = question['choices'][1]
                elif kind == 'draft':
                    answer = f'Confirm ANCOR draft {target}'
                elif kind == 'cancellation':
                    answer = f'Cancel ANCOR requests {target}'
                elif kind == 'response':
                    answer = f'Confirm ANCOR response {target}'
                elif kind == 'human_decision':
                    command = self.store.get_request_command(target)
                    request = self.store.get_request(command['request_id'])
                    answer = (f"Accept ANCOR request {request['id']}"
                              if command['action'] == 'human_accept' else
                              f"Request changes to ANCOR request {request['id']}")
                else:
                    command = self.store.get_request_command(target)
                    request = self.store.get_request(command['request_id'])
                    answer = (f"Pause ANCOR request {request['id']}" if kind == 'pause'
                              else f"Resume ANCOR request {request['id']}")
                mismatch = self._confirmation_mismatch(payload, question, answer)
            except Exception as exc:
                self._finish_confirmation(db, session_id, tool_call_id, 'error', exc)
                log.warning('ANCOR confirmation validation failed for %s: %s', target, exc)
                return
            if mismatch:
                self._finish_confirmation(db, session_id, tool_call_id, 'rejected', mismatch)
                return
            reference = json.dumps({'source': ('native_clarify_response' if kind == 'response'
                                               else 'native_clarify'),
                                    'kind': kind, 'target': target,
                                    'session_id': session_id, 'tool_call_id': tool_call_id},
                                   sort_keys=True)
            try:
                if kind == 'signal':
                    self.store.confirm_signal(target, reference)
                elif kind == 'change':
                    self.store.commit_change(target, reference)
                elif kind == 'draft':
                    self.store.confirm(target, reference)
                elif kind == 'cancellation':
                    self.store.confirm_cancellation(target, reference)
                elif kind == 'response':
                    self._notification_outbox().confirm_response(target, reference)
                elif kind == 'human_decision':
                    request = self.store.confirm_human_decision(target, reference)
                    try:
                        box = self._notification_outbox()
                        box.close_completions(
                            request['id'], closed_by='ancor-interactor',
                            reason=('accepted_by_human'
                                    if request['status'] == 'accepted'
                                    else 'changes_requested_by_human'))
                    except Exception:
                        log.exception('ANCOR completion notice close failed for %s',
                                      request['id'])
                else:
                    self.store.confirm_request_command(target, reference)
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

    def note(self, args):
        action = {'append': 'add', 'replace': 'update', 'invalidate': 'delete'}.get(args['action'], args['action'])
        request_id = args['request_id']
        actor = getattr(self.ctx, 'profile_name', 'ancor-coordinator')
        if args['action'] in {'replace', 'invalidate'} and type(args.get('expected_revision')) is not int:
            raise ValueError('expected_revision required for note replace/invalidate')
        if action == 'history':
            return self.store.note_history(request_id, args['note_id'])
        if action == 'list':
            result = self.store.list_notes(
                request_id, limit=args.get('limit', 50),
                include_deleted=args.get('include_deleted', False))
            return collection([note_summary(note) for note in result['items']],
                              total=result['count'])
        if action == 'get':
            return note_detail(self.store.get_note(request_id, args['note_id']))
        if action == 'add':
            return note_detail(self.store.add_note(request_id, args['body'], actor=actor))
        if action == 'update':
            return note_detail(self.store.update_note(
                request_id, args['note_id'], args['body'], expected_revision=args.get('expected_revision'), actor=actor))
        if action == 'delete':
            return note_detail(self.store.delete_note(request_id, args['note_id'], expected_revision=args.get('expected_revision'), actor=actor))
        raise ValueError('unknown note action')

    def acceptance(self, args):
        request = self.store.get_request(args['request_id'])
        if request['status'] != 'active' or request['target_status'] is not None:
            raise ValueError('request is not stably active')
        if args['action'] == 'reject':
            return self.store.reject(request['id'], args['reason'])
        if args['action'] not in {'accept', 'verify'}:
            raise ValueError('unknown acceptance action')
        open_questions = [q for q in self.store.questions(request['id'])
                          if q['state'] == 'open']
        if open_questions:
            raise ValueError('request has open questions; answer or resolve them before verification')
        evidence = args.get('evidence')
        if not isinstance(evidence, dict):
            raise ValueError('evidence object required')
        if request['content_mode'] == 'points':
            with self.store._transaction() as db:
                self.store._check_verification(db, request, json.dumps(evidence))
        elif evidence.get('brief') != request['brief']:
            raise ValueError('evidence must bind the exact stored brief')
        for field in ('verification', 'artifacts', 'task_ids'):
            values = evidence.get(field)
            if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
                raise ValueError(f'evidence requires nonempty {field}')
        links = {x['task_id'] for x in self.store.snapshot()['task_links']
                 if x['request_id'] == request['id']}
        if not set(evidence['task_ids']) <= links:
            raise ValueError('evidence references unlinked tasks')
        if not links:
            raise ValueError('all linked native tasks must be done or archived before acceptance')
        tasks = {task['id']: task for task in self.native_board.list()}
        for task_id in sorted(links):
            task = tasks.get(task_id)
            if task is None:
                raise ValueError('all linked native tasks must be readable before acceptance')
            status = task.get('status')
            if status == 'archived':
                continue
            if status != 'done':
                raise ValueError('all linked native tasks must be done before acceptance')
            actual = self.native_board.archive(
                task_id,
                f'ANCOR request {request["id"]} accepted; archive verified work.')
            if actual.get('status') != 'archived':
                raise RuntimeError('native archive verification failed')
        return self.store.submit_acceptance(
            request['id'], json.dumps(evidence, ensure_ascii=False), generation=request['generation'])
