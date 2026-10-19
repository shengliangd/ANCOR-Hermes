"""Coordinator operations over the native CLI."""
from .board import NativeBoard
from .service import Service


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
        if self.ctx.profile_name != expected:
            raise PermissionError('coordinator profile required')
        action = args['action']
        if action not in {'create', 'list', 'show', 'block', 'unblock', 'revise', 'comment', 'link', 'unlink', 'edit', 'associate'}:
            raise ValueError('unknown board action')
        store = self.service.store
        if action not in {'list', 'show'}:
            self.reconcile()
        if action == 'create':
            req = store.get_requirement(args['requirement_id'])
            if req['status'] != 'active':
                raise ValueError('requirement is not active')
            for field in ('key', 'title', 'body'):
                store._text(args[field])
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
            task = self.board.create(req['id'], 1, args['key'], title=args['title'],
                                     body=req['brief'] + '\nAcceptance criteria:\n' + req['acceptance_criteria'] + '\nTask:\n' + args['body'],
                                     assignee=worker_profile, parent_ids=parent_ids, hold=hold)
            actual = self.detail(task['id'])
            if actual['task']['assignee'] != worker_profile:
                raise RuntimeError('native task assignee verification failed')
            store.link_task(req['id'], task['id'])
            return actual
        snapshot = store.snapshot()
        links = {link['task_id']: link['requirement_id'] for link in snapshot['task_links']}
        if action == 'list':
            return {'tasks': [self.detail(tid) for tid, rid in links.items()
                              if not args.get('requirement_id') or rid == args['requirement_id']]}
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
            return self.board.dependency(action, parent, child)
        tid = args['task_id']
        if tid not in links:
            raise ValueError('unlinked task')
        detail = self.detail(tid)
        if action == 'show':
            return detail
        if action == 'associate':
            return store.associate_task(tid, args['requirement_id'], args['reason'])
        if action == 'revise':
            reason = store._text(args['reason'])
            if store.get_requirement(links[tid])['status'] != 'active':
                raise ValueError('requirement is not active')
            if detail['task']['status'] != 'triage' or detail['task'].get('archived_at'):
                raise ValueError('revise requires an unarchived triage task')
            self.require_quiescence(detail)
            return self.board.revise(tid, reason)
        if action == 'edit':
            req = store.get_requirement(links[tid])
            if req['status'] != 'active':
                raise ValueError('requirement is not active')
            fields = {key: args[key] for key in ('title', 'body', 'priority') if key in args}
            if 'priority' in fields and type(fields['priority']) is not int:
                raise ValueError('priority must be an integer')
            if 'title' in fields:
                store._text(fields['title'])
            if 'body' in fields:
                store._text(fields['body'])
                metadata = self.board.link(detail['task'])
                if not metadata or metadata['requirement_id'] != req['id'] or metadata['revision'] != 1:
                    raise ValueError('native task association mismatch')
                fields['body'] = (detail['task']['body'].split('\n', 1)[0] + '\n' + req['brief']
                                  + '\nAcceptance criteria:\n' + req['acceptance_criteria']
                                  + '\nTask:\n' + fields['body'])
            return self.board.edit(tid, **fields)
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
        return actual
