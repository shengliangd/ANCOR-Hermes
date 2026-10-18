"""Leader coordination via the native CLI; no acceptance or scheduler ownership."""
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
            self._board = NativeBoard(name)
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
        if self.ctx.profile_name != self.ctx.get_config('leader_profile', 'ancor-leader'):
            raise PermissionError('leader profile required')
        action = args['action']
        if action not in {'create', 'list', 'show', 'block', 'unblock', 'comment'}:
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
            worker_profile = self.ctx.get_config('worker_profile', 'ancor-worker')
            task = self.board.create(req['id'], 1, args['key'], title=args['title'],
                                     body=req['brief'] + '\nAcceptance criteria:\n' + req['acceptance_criteria'] + '\nTask:\n' + args['body'],
                                     assignee=worker_profile)
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
        tid = args['task_id']
        if tid not in links:
            raise ValueError('unlinked task')
        detail = self.detail(tid)
        if action == 'show':
            return detail
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
            self.board._call('comment', tid, text, '--author', 'ancor-leader', json_output=False)
        actual = self.detail(tid)
        if action == 'unblock' and actual['task']['status'] not in {'ready', 'todo', 'running'}:
            raise RuntimeError('native unblock verification failed')
        if action == 'comment' and not any(c['body'] == text and c['author'] == 'ancor-leader' for c in actual['comments']):
            raise RuntimeError('native comment verification failed')
        return actual
