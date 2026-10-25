"""Coordinator operations over the native CLI."""
from .board import NativeBoard
from .service import Service
from .transitions import require_quiescence as _require_quiescence
from .views import bounded_latest, collection, task_detail, task_ref


_STATUS_ORDER = {
    'running': 0, 'ready': 1, 'blocked': 2, 'triage': 3, 'todo': 4,
    'review': 5, 'done': 6, 'cancelled': 7,
}


def _recency(task):
    for key in ('updated_at', 'last_heartbeat_at', 'completed_at',
                'started_at', 'created_at'):
        value = task.get(key)
        if isinstance(value, (int, float)):
            return value
    return 0


CARD_PARTS = ('outcome', 'inputs', 'advice', 'binding_constraints',
              'expected_output', 'evidence')
CARD_REQUIRED = ('outcome', 'expected_output', 'evidence')


def render_card(contract):
    """Render one worker card. The card holds these parts and nothing else.

    Optional parts stay absent when empty: an empty section reads as a missing
    request to a worker. The authorization boundary is a code-owned binding
    constraint, so it is injected here rather than trusted to the author.
    """
    def section(name, values):
        return '\n'.join([f'<{name}>', *(f'- {value}' for value in values), f'</{name}>'])

    values = {part: [value for value in contract.get(part) or () if value]
              for part in CARD_PARTS}
    values['binding_constraints'] = list(dict.fromkeys(
        ['Do not change authorization or request version.',
         *values['binding_constraints']]))
    parts = [f'<outcome>{contract["outcome"]}</outcome>']
    parts.extend(section(part, values[part]) for part in CARD_PARTS[1:] if values[part])
    return '<work_contract>\n' + '\n'.join(parts) + '\n</work_contract>'


class TaskBasisError(ValueError):
    def __init__(self, generation, versions):
        super().__init__('Task basis must use the current generation and point version_id values, not point IDs')
        self.current_basis = {'generation': generation, 'point_versions': sorted(versions)}


class Operations:
    def __init__(self, ctx, store=None, board=None):
        self.ctx = ctx
        self.service = Service(ctx, store=store)
        self._board = board

    @property
    def board(self):
        if self._board is None:
            name = self.ctx.get_config('board')
            if not name:
                raise ValueError('configure ANCOR board')
            self._board = NativeBoard(name, profile=self.ctx.get_config('coordinator_profile', 'ancor-coordinator'))
        return self._board

    def _require_task_basis(self, request, args):
        versions = {point['version_id'] for points in self.service.store.content(request['id'])['fields'].values()
                    for point in points}
        supplied = args.get('point_versions')
        if (args.get('generation') != request['generation'] or not isinstance(supplied, list)
                or not supplied or any(not isinstance(v, str) or v not in versions for v in supplied)):
            raise TaskBasisError(request['generation'], versions)

    def detail(self, task_id):
        # NativeBoard.show intentionally returns only the task; coordination needs
        # comments, attempt summaries, events and dependency context as well.
        return self.board._call('show', task_id)

    def tasks(self, *, request_id=None, status=None, include_archived=False):
        """Return compact linked-task summaries without loading each card history."""
        store = self.service.store
        snapshot = store.snapshot()
        links = {link['task_id']: link['request_id']
                 for link in snapshot['task_links']}
        titles = {req['id']: req['title'] for req in snapshot['requests']}
        items = []
        for task in self.board.list():
            linked_request = links.get(task['id'])
            if linked_request is None:
                continue
            if request_id is not None and linked_request != request_id:
                continue
            if status is not None and task.get('status') != status:
                continue
            archived = bool(task.get('archived_at') or task.get('archived'))
            if archived and not include_archived:
                continue
            items.append(task_ref(task, linked_request,
                                  titles.get(linked_request)))
        items.sort(key=lambda task: (
            bool(task.get('archived_at') or task.get('archived')),
            _STATUS_ORDER.get(task.get('status'), 50),
            -_recency(task), task['id']))
        return items

    def _receipt(self, detail, request_id, changed):
        task = detail['task']
        try:
            request_title = self.service.store.get_request(
                request_id)['title']
        except KeyError:
            request_title = None
        return {
            'task_id': task['id'],
            'title': task.get('title'),
            'status': task.get('status'),
            'assignee': task.get('assignee'),
            'request_id': request_id,
            'request_title': request_title,
            'parents': list(detail.get('parents') or ()),
            'changed': list(changed),
        }

    @staticmethod
    def _contract(contract):
        if not isinstance(contract, dict):
            raise ValueError('contract is required')
        allowed, required = set(CARD_PARTS), set(CARD_REQUIRED)
        if set(contract) - allowed:
            raise ValueError('contract contains unknown fields')
        missing = required - set(contract)
        if missing:
            raise ValueError('contract is missing required fields: '
                             + ', '.join(sorted(missing)))
        outcome = contract['outcome']
        if not isinstance(outcome, str) or not outcome.strip():
            raise ValueError('contract outcome must be nonempty text')
        for field in CARD_PARTS[1:]:
            values = contract.get(field, [])
            if not isinstance(values, list) or any(
                    not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f'contract {field} must be a list of nonempty strings')
            if field in required and not values:
                raise ValueError(f'contract {field} must not be empty')
        return contract

    def reconcile(self):
        """Recover durable native links; association protocol stays at revision 1."""
        store = self.service.store
        requests = {r['id']: r for r in store.snapshot()['requests']}
        for task in self.board.list():
            link = self.board.link(task)
            if link and link['revision'] == 1 and link['request_id'] in requests:
                store.link_task(link['request_id'], task['id'])
        return requests

    @staticmethod
    def require_quiescence(detail):
        # Native block clears the task claim and ends the run, but does NOT
        # kill its executor. Inspect retained run PIDs, not terminal status.
        _require_quiescence(detail)

    @staticmethod
    def _require_stable_active(request):
        if request['status'] != 'active' or request.get('target_status') is not None:
            raise ValueError('request is not stably active')

    @staticmethod
    def _allow_dispatch(store, rid, tid):
        req = store.get_request(rid)
        if req['content_mode'] != 'points':
            return
        state = store.coordination(rid)
        if state['needs_review']:
            raise RuntimeError('review current request changes before releasing work')
        review = next((r for r in state['reviews'] if r['task_id'] == tid), None)
        basis = next((b for b in state['task_basis'] if b['task_id'] == tid), None)
        if review and (review['state'] != 'applied' or review['decision'] == 'retire'):
            raise RuntimeError('apply the task impact decision before releasing work; after adjusting the stopped card, '
                               'use ancor_reconcile action=finish with this request_id, generation and task_id, then unblock')
        if not basis or (basis['generation'] != req['generation'] and not (review and review['decision'] == 'unaffected')):
            raise RuntimeError('task requires current basis or explicit unaffected review')

    def call(self, args):
        expected = self.ctx.get_config('coordinator_profile', 'ancor-coordinator')
        interactor = self.ctx.get_config('interactor_profile', 'ancor-interactor')
        action = args['action']
        if self.ctx.profile_name != expected and not (
                self.ctx.profile_name == interactor and action in {'list', 'show'}):
            raise PermissionError('coordinator profile required')
        if action not in {'create', 'list', 'show', 'history', 'block', 'unblock',
                          'revise', 'comment', 'link', 'unlink', 'edit', 'archive',
                          'associate'}:
            raise ValueError('unknown board action')
        store = self.service.store
        if action not in {'list', 'show'}:
            self.reconcile()
        if action == 'create':
            req = store.get_request(args['request_id'])
            self._require_stable_active(req)
            contract = self._contract(args.get('contract'))
            if 'body' in args:
                raise ValueError('body is not a card part; state the outcome in the contract')
            for field in ('key', 'title'):
                store._text(args[field])
            parent_ids, hold = args.get('parent_ids', []), args.get('hold', False)
            if not isinstance(parent_ids, list) or any(not isinstance(p, str) or not p.strip() for p in parent_ids):
                raise ValueError('parent_ids must be a list of task IDs')
            if type(hold) is not bool:
                raise ValueError('hold must be a boolean')
            linked = {item['task_id']: item['request_id'] for item in store.snapshot()['task_links']}
            for parent in parent_ids:
                if parent not in linked:
                    raise ValueError('unlinked parent task')
                self._require_stable_active(store.get_request(linked[parent]))
            worker_profile = self.ctx.get_config('worker_profile', 'ancor-worker')
            if req['content_mode'] == 'points':
                self._require_task_basis(req, args)
            body = render_card(contract)
            task = self.board.create(req['id'], 1, args['key'], title=args['title'],
                                     body=body,
                                     assignee=worker_profile, parent_ids=parent_ids,
                                     hold=hold or req['content_mode'] == 'points')
            actual = self.detail(task['id'])
            if req['content_mode'] == 'points' and actual['task']['body'].split('\n', 1)[-1] != body:
                raise RuntimeError('existing task key has a different contract; review and edit the stopped card')
            if actual['task']['assignee'] != worker_profile:
                raise RuntimeError('native task assignee verification failed')
            store.link_task(req['id'], task['id'])
            if req['content_mode'] == 'points':
                store.bind_task(req['id'], task['id'], args['generation'], args['point_versions'])
            return self._receipt(actual, req['id'], ['create'])
        snapshot = store.snapshot()
        links = {link['task_id']: link['request_id'] for link in snapshot['task_links']}
        if action == 'list':
            limit = args.get('limit', 100)
            if type(limit) is not int or not 1 <= limit <= 200:
                raise ValueError('limit must be an integer between 1 and 200')
            include_archived = args.get('include_archived', False)
            if type(include_archived) is not bool:
                raise ValueError('include_archived must be a boolean')
            items = self.tasks(request_id=args.get('request_id'),
                               status=args.get('status'),
                               include_archived=include_archived)
            return collection(items[:limit], total=len(items))
        if action in {'link', 'unlink'}:
            parent, child = args['parent_id'], args['child_id']
            for tid in (parent, child):
                if tid not in links:
                    raise ValueError('unlinked task')
            self._require_stable_active(store.get_request(links[child]))
            if action == 'link':
                self._require_stable_active(store.get_request(links[parent]))
            # Native CLI owns cycle, running-child and run-ownership checks.
            actual = self.board.dependency(action, parent, child)
            return self._receipt(actual, links[child], [action])
        tid = args['task_id']
        if tid not in links:
            raise ValueError('unlinked task')
        detail = self.detail(tid)
        if action == 'show':
            req = store.get_request(links[tid])
            return task_detail(detail, request_id=links[tid],
                               request_title=req['title'])
        if action == 'history':
            kind = args.get('kind')
            if kind not in {'comments', 'events', 'runs'}:
                raise ValueError('kind must be comments, events or runs')
            limit = args.get('limit', 20)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError('limit must be an integer between 1 and 100')
            before_id = args.get('before_id')
            if before_id is not None and (type(before_id) is not int or before_id < 1):
                raise ValueError('before_id must be a positive integer')
            return bounded_latest(detail.get(kind) or (), limit,
                                  before_id=before_id)
        if action == 'associate':
            return store.associate_task(tid, args['request_id'], args['reason'])
        if action == 'archive':
            self.board.archive(tid, store._text(args['reason']))
            actual = self.detail(tid)
            return self._receipt(actual, links[tid], ['archive'])
        if action == 'revise':
            self._allow_dispatch(store, links[tid], tid)
            reason = store._text(args['reason'])
            self._require_stable_active(store.get_request(links[tid]))
            if detail['task']['status'] != 'triage' or detail['task'].get('archived_at'):
                raise ValueError('revise requires an unarchived triage task')
            self.require_quiescence(detail)
            return self._receipt(self.board.revise(tid, reason), links[tid], ['revise'])
        if action == 'unblock':
            self._require_stable_active(store.get_request(links[tid]))
        terminal = detail['task'].get('archived_at') or detail['task']['status'] in {'done', 'archived', 'cancelled'}
        if terminal and (action in {'block', 'unblock'} or (action == 'edit' and 'contract' in args)):
            raise RuntimeError('completed or archived cards cannot be restarted or have their contract rewritten; '
                               'preserve the result, create a follow-up card, and retire obsolete work through reconciliation')
        if action == 'edit':
            req = store.get_request(links[tid])
            self._require_stable_active(req)
            if 'body' in args:
                raise ValueError('body is not a card part; edit the contract instead')
            fields = {key: args[key] for key in ('title', 'priority') if key in args}
            if 'priority' in fields and type(fields['priority']) is not int:
                raise ValueError('priority must be an integer')
            if 'title' in fields:
                store._text(fields['title'])
            if 'contract' in args:
                if req['content_mode'] == 'points':
                    if detail['task']['status'] in {'running', 'ready'}:
                        raise ValueError('block the affected task before revising its contract')
                    self.require_quiescence(detail)
                    self._require_task_basis(req, args)
                contract = self._contract(args['contract'])
                metadata = self.board.link(detail['task'])
                if not metadata or metadata['request_id'] != req['id'] or metadata['revision'] != 1:
                    raise ValueError('native task association mismatch')
                link_line = detail['task']['body'].split('\n', 1)[0]
                fields['body'] = link_line + '\n' + render_card(contract)
            edited = self.board.edit(tid, **fields)
            if 'contract' in args and req['content_mode'] == 'points':
                store.bind_task(req['id'], tid, args['generation'], args['point_versions'])
            return self._receipt(edited, links[tid], list(fields))
        text = None
        if action == 'block':
            self.board.block(tid, store._text(args['reason']))
        elif action == 'unblock':
            self._allow_dispatch(store, links[tid], tid)
            self.require_quiescence(detail)
            self._require_stable_active(store.get_request(links[tid]))
            self.board._call('unblock', tid, '--reason', store._text(args['reason']), json_output=False)
        elif action == 'comment':
            text = store._text(args['body']).strip()
            author = expected
            self.board._call('comment', tid, text, '--author', author, json_output=False)
        actual = self.detail(tid)
        if action == 'unblock' and actual['task']['status'] not in {'ready', 'todo', 'running'}:
            raise RuntimeError('native unblock verification failed')
        if action == 'comment' and not any(c['body'] == text and c['author'] == expected for c in actual['comments']):
            raise RuntimeError('native comment verification failed')
        return self._receipt(actual, links[tid], [action])
