"""Coordinator operations over the native CLI."""
from .board import NativeBoard
from .service import Service
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


def build_work_contract(*, objective, scope, task, inputs, dependencies,
                        constraints, artifacts, verification, stop_conditions,
                        report=()):
    """Render a self-contained English contract for one bounded worker task."""
    def bullets(values):
        return '\n'.join(f'- {value}' for value in values) or '- None specified'

    return f'''<work_contract>
<identity>
You are the execution owner for this bounded task. Produce an inspectable result
within this contract; do not silently expand scope or authorization.
</identity>
<objective>{objective}</objective>
<scope>{scope}\nTask: {task}</scope>
<inputs>
{bullets(inputs)}
</inputs>
<dependencies>
{bullets(dependencies)}
</dependencies>
<constraints>
{bullets(constraints)}
</constraints>
<expected_artifacts>
{bullets(artifacts)}
</expected_artifacts>
<verification>
{bullets(verification)}
</verification>
<stop_conditions>
{bullets(stop_conditions)}
</stop_conditions>
<report>
{bullets(report)}
</report>
</work_contract>'''


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

    def detail(self, task_id):
        # NativeBoard.show intentionally returns only the task; coordination needs
        # comments, attempt summaries, events and dependency context as well.
        return self.board._call('show', task_id)

    def tasks(self, *, requirement_id=None, status=None, include_archived=False):
        """Return compact linked-task summaries without loading each card history."""
        store = self.service.store
        snapshot = store.snapshot()
        links = {link['task_id']: link['requirement_id']
                 for link in snapshot['task_links']}
        titles = {req['id']: req['title'] for req in snapshot['requirements']}
        items = []
        for task in self.board.list():
            linked_requirement = links.get(task['id'])
            if linked_requirement is None:
                continue
            if requirement_id is not None and linked_requirement != requirement_id:
                continue
            if status is not None and task.get('status') != status:
                continue
            archived = bool(task.get('archived_at') or task.get('archived'))
            if archived and not include_archived:
                continue
            items.append(task_ref(task, linked_requirement,
                                  titles.get(linked_requirement)))
        items.sort(key=lambda task: (
            bool(task.get('archived_at') or task.get('archived')),
            _STATUS_ORDER.get(task.get('status'), 50),
            -_recency(task), task['id']))
        return items

    def _receipt(self, detail, requirement_id, changed):
        task = detail['task']
        try:
            requirement_title = self.service.store.get_requirement(
                requirement_id)['title']
        except KeyError:
            requirement_title = None
        return {
            'task_id': task['id'],
            'title': task.get('title'),
            'status': task.get('status'),
            'assignee': task.get('assignee'),
            'requirement_id': requirement_id,
            'requirement_title': requirement_title,
            'parents': list(detail.get('parents') or ()),
            'changed': list(changed),
        }

    @staticmethod
    def _contract(contract):
        if not isinstance(contract, dict):
            raise ValueError('contract is required')
        allowed = {'objective', 'scope', 'inputs', 'dependencies', 'artifacts',
                   'verification', 'constraints', 'stop_conditions', 'report'}
        required = allowed - {'report'}
        if set(contract) - allowed:
            raise ValueError('contract contains unknown fields')
        if required - set(contract):
            raise ValueError('contract is missing required fields')
        for field in ('objective', 'scope'):
            value = contract[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'contract {field} must be nonempty text')
        for field in ('inputs', 'dependencies', 'artifacts', 'verification',
                      'constraints', 'stop_conditions', 'report'):
            if field not in contract and field == 'report':
                continue
            values = contract.get(field)
            if not isinstance(values, list) or any(
                    not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f'contract {field} must be a list of nonempty strings')
        for field in ('artifacts', 'verification', 'stop_conditions'):
            if not contract[field]:
                raise ValueError(f'contract {field} must not be empty')
        return contract

    def reconcile(self):
        """Recover durable native links; immutable requirement IDs use revision 1."""
        store = self.service.store
        requirements = {r['id']: r for r in store.snapshot()['requirements']}
        for task in self.board.list():
            link = self.board.link(task)
            if link and link['revision'] == 1 and link['requirement_id'] in requirements:
                store.link_task(link['requirement_id'], task['id'])
        return requirements

    @staticmethod
    def require_quiescence(detail):
        # Native block clears the task claim and ends the run, but does NOT
        # kill its executor. Inspect retained run PIDs, not terminal status.
        # PID existence is conservative (including reuse); never signal a kill.
        import os
        for run in detail['runs']:
            pid = run.get('worker_pid')
            if pid:
                try:
                    os.kill(int(pid), 0)
                except ProcessLookupError:
                    continue
                except (PermissionError, ValueError, OSError):
                    pass
                raise RuntimeError('worker quiescence not established')
            if not run.get('ended_at'):
                raise RuntimeError('worker quiescence not established')

    def call(self, args):
        expected = self.ctx.get_config('coordinator_profile', 'ancor-coordinator')
        interactor = self.ctx.get_config('interactor_profile', 'ancor-interactor')
        action = args['action']
        if self.ctx.profile_name != expected and not (
                self.ctx.profile_name == interactor and action in {'list', 'show'}):
            raise PermissionError('coordinator profile required')
        if action not in {'create', 'list', 'show', 'history', 'block', 'unblock',
                          'revise', 'comment', 'link', 'unlink', 'edit', 'associate'}:
            raise ValueError('unknown board action')
        store = self.service.store
        if action not in {'list', 'show'}:
            self.reconcile()
        if action == 'create':
            req = store.get_requirement(args['requirement_id'])
            if req['status'] != 'active':
                raise ValueError('requirement is not active')
            contract = self._contract(args.get('contract'))
            for field in ('key', 'title'):
                store._text(args[field])
            task_text = store._text(args.get('body') or contract['objective'])
            parent_ids, hold = args.get('parent_ids', []), args.get('hold', False)
            if not isinstance(parent_ids, list) or any(not isinstance(p, str) or not p.strip() for p in parent_ids):
                raise ValueError('parent_ids must be a list of task IDs')
            if type(hold) is not bool:
                raise ValueError('hold must be a boolean')
            linked = {item['task_id']: item['requirement_id'] for item in store.snapshot()['task_links']}
            for parent in parent_ids:
                if parent not in linked:
                    raise ValueError('unlinked parent task')
                if store.get_requirement(linked[parent])['status'] != 'active':
                    raise ValueError('parent requirement is not active')
            worker_profile = self.ctx.get_config('worker_profile', 'ancor-worker')
            body = build_work_contract(
                objective=contract['objective'], scope=contract['scope'], task=task_text,
                inputs=contract['inputs'], dependencies=contract['dependencies'],
                constraints=list(dict.fromkeys(['Do not change authorization or requirement version.',
                                                 *contract['constraints']])),
                artifacts=contract['artifacts'], verification=contract['verification'],
                stop_conditions=contract['stop_conditions'],
                report=contract.get('report') or [
                    'Result, artifacts, checks and outcomes, assumptions, and remaining limitations.'])
            task = self.board.create(req['id'], 1, args['key'], title=args['title'],
                                     body=body,
                                     assignee=worker_profile, parent_ids=parent_ids, hold=hold)
            actual = self.detail(task['id'])
            if actual['task']['assignee'] != worker_profile:
                raise RuntimeError('native task assignee verification failed')
            store.link_task(req['id'], task['id'])
            return self._receipt(actual, req['id'], ['create'])
        snapshot = store.snapshot()
        links = {link['task_id']: link['requirement_id'] for link in snapshot['task_links']}
        if action == 'list':
            limit = args.get('limit', 100)
            if type(limit) is not int or not 1 <= limit <= 200:
                raise ValueError('limit must be an integer between 1 and 200')
            include_archived = args.get('include_archived', False)
            if type(include_archived) is not bool:
                raise ValueError('include_archived must be a boolean')
            items = self.tasks(requirement_id=args.get('requirement_id'),
                               status=args.get('status'),
                               include_archived=include_archived)
            return collection(items[:limit], total=len(items))
        if action in {'link', 'unlink'}:
            parent, child = args['parent_id'], args['child_id']
            for tid in (parent, child):
                if tid not in links:
                    raise ValueError('unlinked task')
                # Removing an obsolete prerequisite must not strand active work.
                # Creating an edge still requires both requirements to be active.
                if (action == 'link' or tid == child) and store.get_requirement(links[tid])['status'] != 'active':
                    raise ValueError('requirement is not active')
            # Native CLI owns cycle, running-child and run-ownership checks.
            actual = self.board.dependency(action, parent, child)
            return self._receipt(actual, links[child], [action])
        tid = args['task_id']
        if tid not in links:
            raise ValueError('unlinked task')
        detail = self.detail(tid)
        if action == 'show':
            req = store.get_requirement(links[tid])
            return task_detail(detail, requirement_id=links[tid],
                               requirement_title=req['title'])
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
            return store.associate_task(tid, args['requirement_id'], args['reason'])
        if action == 'revise':
            reason = store._text(args['reason'])
            if store.get_requirement(links[tid])['status'] != 'active':
                raise ValueError('requirement is not active')
            if detail['task']['status'] != 'triage' or detail['task'].get('archived_at'):
                raise ValueError('revise requires an unarchived triage task')
            self.require_quiescence(detail)
            return self._receipt(self.board.revise(tid, reason), links[tid], ['revise'])
        if action == 'edit':
            req = store.get_requirement(links[tid])
            if req['status'] != 'active':
                raise ValueError('requirement is not active')
            if 'contract' in args and 'body' not in args:
                raise ValueError('contract edits require body')
            fields = {key: args[key] for key in ('title', 'body', 'priority') if key in args}
            if 'priority' in fields and type(fields['priority']) is not int:
                raise ValueError('priority must be an integer')
            if 'title' in fields:
                store._text(fields['title'])
            if 'body' in fields:
                store._text(fields['body'])
                contract = self._contract(args.get('contract'))
                metadata = self.board.link(detail['task'])
                if not metadata or metadata['requirement_id'] != req['id'] or metadata['revision'] != 1:
                    raise ValueError('native task association mismatch')
                link_line = detail['task']['body'].split('\n', 1)[0]
                fields['body'] = link_line + '\n' + build_work_contract(
                    objective=contract['objective'], scope=contract['scope'], task=fields['body'],
                    inputs=contract['inputs'], dependencies=contract['dependencies'],
                    constraints=list(dict.fromkeys(['Do not change authorization or requirement version.',
                                                     *contract['constraints']])),
                    artifacts=contract['artifacts'], verification=contract['verification'],
                    stop_conditions=contract['stop_conditions'],
                    report=contract.get('report') or [
                        'Result, artifacts, checks and outcomes, assumptions, and remaining limitations.'])
            return self._receipt(self.board.edit(tid, **fields), links[tid], list(fields))
        text = None
        if action == 'block':
            self.board.block(tid, store._text(args['reason']))
        elif action == 'unblock':
            self.require_quiescence(detail)
            if store.get_requirement(links[tid])['status'] != 'active':
                raise ValueError('requirement is not active')
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
