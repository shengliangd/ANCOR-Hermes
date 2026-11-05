"""Durable one-shot wakes and global liveness, using the request transaction."""
import math
import time
from uuid import uuid4

from .views import WAKE_LIMIT, collection, preview, wake_ref


class Scheduling:
    INTERVAL = 7200
    HEARTBEAT_STALE_SECONDS = 3600
    DELIVERY_GRACE_SECONDS = 210
    PROGRESSION_TASK_LIMIT = 8

    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        with store._transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS scheduled_wakes (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                    reason TEXT NOT NULL, due_at REAL NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS coordination_clock (
                    id INTEGER PRIMARY KEY CHECK(id=1), active_since REAL,
                    last_started REAL, generation INTEGER NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO coordination_clock(id) VALUES(1);
            ''')

    def call(self, args):
        action = args['action']
        now = self.clock()
        with self.store._transaction() as db:
            self._suppress(db)
            if action == 'list':
                return self._list(db, args)
            if action not in {'schedule', 'replace', 'cancel'}:
                raise ValueError('unknown wake action')
            old = None
            if action in {'replace', 'cancel'}:
                old = self.store._get(db, 'scheduled_wakes', args['wake_id'])
                if old['state'] not in {'scheduled', 'queued'}:
                    raise ValueError('wake is no longer pending')
            if action != 'cancel':
                rid = args.get('request_id', old['request_id'] if old else None)
                if old and rid != old['request_id']:
                    raise ValueError('replacement must retain request')
                request = self.store._get(db, 'requests', rid)
                if request['status'] != 'active' or request['target_status'] is not None:
                    raise ValueError('request is not stably active')
                reason = self.store._text(args['reason'])
                due = args['due_at']
                if isinstance(due, bool) or not isinstance(due, (int, float)) or not math.isfinite(due) or due <= now:
                    raise ValueError('due_at must be a future finite Unix timestamp')
            if old:
                db.execute("UPDATE scheduled_wakes SET state='cancelled' WHERE id=?", (old['id'],))
                db.execute('UPDATE events SET acknowledged=1 WHERE source_key=?', ('wake:' + old['id'],))
            if action == 'cancel':
                return self.store._get(db, 'scheduled_wakes', old['id'])
            identifier = uuid4().hex
            db.execute('INSERT INTO scheduled_wakes VALUES(?,?,?,?,?)', (identifier, rid, reason, due, 'scheduled'))
            return self.store._get(db, 'scheduled_wakes', identifier)

    def _list(self, db, args):
        request_id = args.get('request_id')
        state = args.get('state')
        limit = args.get('limit', 50)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('limit must be an integer between 1 and 200')
        allowed_states = {'scheduled', 'queued', 'suppressed', 'cancelled', 'handled'}
        if state is None:
            states = {'scheduled', 'queued'}
        elif state in allowed_states:
            states = {state}
        else:
            raise ValueError('state must be one of: ' + ', '.join(sorted(allowed_states)))
        rows = [dict(row) for row in db.execute(
            'SELECT * FROM scheduled_wakes WHERE (? IS NULL OR request_id=?) '
            'ORDER BY due_at,id', (request_id, request_id))]
        rows = [row for row in rows if row['state'] in states]
        return collection([wake_ref(row) for row in rows[:limit]], total=len(rows))

    def pending_wakes(self, request_id=None):
        with self.store._transaction() as db:
            self._suppress(db)
            return [dict(row) for row in db.execute(
                "SELECT * FROM scheduled_wakes WHERE state IN ('scheduled','queued') "
                "AND (? IS NULL OR request_id=?) ORDER BY due_at,id",
                (request_id, request_id))]

    def _suppress(self, db):
        db.execute("UPDATE scheduled_wakes SET state='suppressed' WHERE state IN ('scheduled','queued') AND request_id IN (SELECT id FROM requests WHERE status!='active' OR target_status IS NOT NULL)")
        db.execute("UPDATE events SET acknowledged=1 WHERE source_key IN (SELECT 'wake:' || id FROM scheduled_wakes WHERE state IN ('suppressed','cancelled'))")
        db.execute("UPDATE scheduled_wakes SET state='handled' WHERE state='queued' AND EXISTS (SELECT 1 FROM events WHERE source_key='wake:' || scheduled_wakes.id AND acknowledged=1)")

    def scan(self):
        now = self.clock()
        with self.store._transaction() as db:
            self._suppress(db)
            for row in db.execute("SELECT * FROM scheduled_wakes WHERE state='scheduled' AND due_at<=?", (now,)).fetchall():
                self.store._add_event(db, 'wake.due', dict(row), 'wake:' + row['id'])
                db.execute("UPDATE scheduled_wakes SET state='queued' WHERE id=?", (row['id'],))
            state = dict(db.execute('SELECT * FROM coordination_clock').fetchone())
            if state['active_since'] is None:
                db.execute("UPDATE events SET acknowledged=1 WHERE kind='host.liveness'")
                return
            anchor = max(state['active_since'], state['last_started'] or state['active_since'])
            if now >= anchor + self.INTERVAL:
                event = self.store._add_event(db, 'host.liveness', {'reason': 'Active work has no coordinator tool activity within two hours', 'due_at': anchor + self.INTERVAL}, 'liveness:' + str(state['generation']))
                db.execute('UPDATE events SET acknowledged=0 WHERE id=?', (event['id'],))

    def started(self):
        # Every valid coordinator tool call is activity, including later gateway turns.
        # Enqueue, process creation and CLI startup failure are not coordination.
        with self.store._transaction() as db:
            db.execute('UPDATE coordination_clock SET last_started=?, generation=generation+1', (self.clock(),))
            db.execute("UPDATE events SET acknowledged=1 WHERE kind='host.liveness'")

    @staticmethod
    def _fresh(timestamp, now, *, seconds):
        return isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) and now - timestamp <= seconds

    @staticmethod
    def _question(notifications, request_id, task_id, *, open_only=False):
        return [row for row in notifications or ()
                if row.get('kind') == 'question'
                and row.get('request_id') == request_id
                and row.get('task_id') == task_id
                and (not open_only or (row.get('answer') is None
                                       and row.get('closed_at') is None))]

    def _blocked_wait(self, notifications, request_id, task_id, now):
        questions = self._question(notifications, request_id, task_id)
        # Current unanswered questions govern health; an older answered question
        # must not mask a newer question that is still waiting for the human.
        open_questions = [row for row in questions
                          if row.get('answer') is None and row.get('closed_at') is None]
        if any(row.get('state') == 'sent' for row in open_questions):
            return 'waiting', None
        delivering = [row for row in open_questions if row.get('state') in {'pending', 'sending'}]
        if delivering:
            stamps = [row.get('delivery_started_at') or row.get('queued_at')
                      for row in delivering]
            if any(isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
                   and now - stamp <= self.DELIVERY_GRACE_SECONDS for stamp in stamps):
                return 'active', 'question_delivery_in_progress'
            return 'stalled', 'question_delivery_unconfirmed'
        if any(row.get('state') == 'uncertain' for row in open_questions):
            return 'stalled', 'question_delivery_unconfirmed'
        # A recorded answer is progress even if transport state stayed uncertain.
        if any(row.get('answer') is not None and row.get('closed_at') is None
               for row in questions):
            return 'stalled', 'question_answered_not_resumed'
        if any(row.get('closed_at') is not None for row in questions):
            return 'stalled', 'question_superseded'
        return 'stalled', 'blocked_without_wait'

    def health(self, data, tasks, notifications, now=None):
        """One request health result: active, waiting_on_human, or stalled."""
        now = self.clock() if now is None else now
        tasks = list(tasks or ())
        by_id = {task['id']: task for task in tasks}
        linked = {}
        for link in data['task_links']:
            linked.setdefault(link['request_id'], []).append(by_id.get(link['task_id'], {'id': link['task_id']}))
        wakes = self.pending_wakes()
        scope = {r['id'] for r in data['requests']}
        wakes = [wake for wake in wakes if wake['request_id'] in scope]
        results = []
        for request in data['requests']:
            rid = request['id']
            items = linked.get(rid, [])
            timers = [{'kind': 'scheduled_wake', **wake_ref(wake)} for wake in wakes
                      if wake['request_id'] == rid and wake['state'] in {'scheduled', 'queued'}]
            base = {'request_id': rid, 'tasks': [
                {key: task[key] for key in ('id', 'status', 'last_heartbeat_at',
                                            'block_kind', 'block_recurrences') if key in task}
                for task in items], 'timers': timers}
            for task in base['tasks']:
                if task.get('id') in by_id and by_id[task['id']].get('blocked_reason'):
                    shown, truncated = preview(by_id[task['id']]['blocked_reason'], 160)
                    task['blocked_reason_preview'] = shown
                    if truncated:
                        task['blocked_reason_truncated'] = True
            blocked = [task for task in items if task.get('status') == 'blocked']
            blocked_health = [(task, *self._blocked_wait(notifications, rid, task['id'], now))
                              for task in blocked]
            waiting = [(task, reason) for task, state, reason in blocked_health if state == 'waiting']
            if waiting:
                questions = [row for task, _ in waiting for row in self._question(
                    notifications, rid, task['id'], open_only=True)]
                base['human_waiting'] = {
                    'task_ids': [task['id'] for task, _ in waiting],
                    'question_ids': [row['id'] for row in questions],
                }
            if request.get('target_status') is not None:
                results.append({**base, 'state': 'stalled', 'reason': 'transition_pending',
                                'waiting_on': {'target_status': request['target_status']},
                                'task_ids': [task['id'] for task in items]})
                continue
            if request['status'] == 'paused':
                results.append({**base, 'state': 'paused', 'reason': 'request_paused',
                                'waiting_on': None,
                                'task_ids': [task['id'] for task in items]})
                continue
            if request['status'] == 'awaiting_acceptance':
                results.append({**base, 'state': 'awaiting_human_acceptance',
                                'reason': 'human_acceptance_pending',
                                'waiting_on': None,
                                'task_ids': [task['id'] for task in items]})
                continue
            if request['status'] != 'active':
                continue
            triage = [task for task in items if task.get('status') == 'triage']
            if triage:
                results.append({**base, 'state': 'stalled', 'reason': 'triage',
                                'waiting_on': None, 'task_ids': [task['id'] for task in triage]})
                continue
            invalid_blocked = [item for item in blocked_health if item[1] == 'stalled']
            if invalid_blocked:
                task, _, reason = invalid_blocked[0]
                results.append({**base, 'state': 'stalled', 'reason': reason,
                                'waiting_on': None, 'task_ids': [item[0]['id'] for item in invalid_blocked]})
                continue
            delivering = [task for task, state, _ in blocked_health if state == 'active']
            if delivering:
                results.append({**base, 'state': 'active', 'reason': 'question_delivery_in_progress',
                                'waiting_on': None,
                                'task_ids': [task['id'] for task in delivering]})
                continue
            running = [task for task in items if task.get('status') == 'running']
            fresh = [task for task in running if self._fresh(task.get('last_heartbeat_at'), now,
                                                              seconds=self.HEARTBEAT_STALE_SECONDS)]
            if fresh:
                results.append({**base, 'state': 'active', 'reason': 'running_heartbeat',
                                'waiting_on': None, 'task_ids': [task['id'] for task in fresh]})
                continue
            if running:
                results.append({**base, 'state': 'stalled', 'reason': 'stale_heartbeat',
                                'waiting_on': None, 'task_ids': [task['id'] for task in running]})
                continue
            ready = [task for task in items if task.get('status') == 'ready']
            if ready:
                results.append({**base, 'state': 'active', 'reason': 'ready',
                                'waiting_on': None, 'task_ids': [task['id'] for task in ready]})
                continue
            if waiting:
                results.append({**base, 'state': 'waiting_on_human', 'reason': 'blocked_waiting_human',
                                'waiting_on': {'question_ids': [row['id'] for row in questions]},
                                'task_ids': [task['id'] for task, _ in waiting]})
                continue
            # Request-scoped questions can precede task creation. Independent
            # ready/running work above remains executable while a question is open.
            scoped_questions = self._question(notifications, rid, None, open_only=True)
            if scoped_questions:
                state, reason = self._blocked_wait(notifications, rid, None, now)
                results.append({**base, 'state': 'waiting_on_human' if state == 'waiting' else state,
                                'reason': 'blocked_waiting_human' if state == 'waiting' else reason,
                                'task_ids': [],
                                'waiting_on': {'question_ids': [q['id'] for q in scoped_questions]}})
                continue
            done = [task for task in items if task.get('status') == 'done']
            if items and len(done) == len(items):
                results.append({**base, 'state': 'stalled', 'reason': 'acceptance_pending',
                                'waiting_on': None, 'task_ids': [task['id'] for task in done]})
                continue
            scheduled = [task for task in items if task.get('status') == 'scheduled']
            if (scheduled and timers
                    and all(timer['state'] == 'scheduled' and timer['due_at'] > now for timer in timers)
                    and all(task.get('status') in {'scheduled', 'todo', 'done', 'archived', 'cancelled'}
                            for task in items)):
                results.append({**base, 'state': 'waiting_on_timer', 'reason': 'scheduled_wake_pending',
                                'waiting_on': {'wake_ids': [timer['id'] for timer in timers]},
                                'task_ids': [task['id'] for task in scheduled]})
                continue
            reason = 'no_work' if not items else 'no_active_task'
            results.append({**base, 'state': 'stalled', 'reason': reason,
                            'waiting_on': None, 'task_ids': []})
        for result in results:
            if len(result['tasks']) > self.PROGRESSION_TASK_LIMIT:
                result['tasks'] = result['tasks'][:self.PROGRESSION_TASK_LIMIT]
                result['tasks_truncated'] = True
            if len(result['task_ids']) > self.PROGRESSION_TASK_LIMIT:
                result['task_ids'] = result['task_ids'][:self.PROGRESSION_TASK_LIMIT]
                result['task_ids_truncated'] = True
        return results

    def sync_health(self, data, tasks, notifications):
        """Publish stalled conditions and maintain one global actionable clock."""
        states = self.health(data, tasks, notifications)
        stalled = {item['request_id'] for item in states if item['state'] == 'stalled'}
        actionable = any(
            item['state'] == 'stalled'
            or (item['state'] == 'active'
                and item.get('reason') != 'question_delivery_in_progress')
            for item in states)
        with self.store._transaction() as db:
            for rid in stalled:
                event = self.store._add_event(db, 'host.stalled', {'request_id': rid}, 'stalled:' + rid)
                db.execute('UPDATE events SET acknowledged=0 WHERE id=?', (event['id'],))
            for row in db.execute("SELECT id, source_key FROM events "
                                  "WHERE kind='host.stalled' AND acknowledged=0").fetchall():
                if row['source_key'] not in {'stalled:' + rid for rid in stalled}:
                    db.execute('UPDATE events SET acknowledged=1 WHERE id=?', (row['id'],))
            clock = dict(db.execute('SELECT * FROM coordination_clock').fetchone())
            if actionable and clock['active_since'] is None:
                db.execute('UPDATE coordination_clock SET active_since=?', (self.clock(),))
            elif not actionable:
                db.execute('UPDATE coordination_clock SET active_since=NULL')
                db.execute("UPDATE events SET acknowledged=1 WHERE kind='host.liveness'")
        return states

    def observation(self, data, tasks=(), notifications=None):
        wakes = self.pending_wakes()
        scope_ids = {r['id'] for r in data['requests']}
        wakes = [wake for wake in wakes if wake['request_id'] in scope_ids]
        visible_wakes = wakes[:WAKE_LIMIT]
        with self.store._transaction() as db:
            state = dict(db.execute('SELECT * FROM coordination_clock').fetchone())
        anchor = (max(state['active_since'], state['last_started'] or 0)
                  if state['active_since'] is not None else None)
        deadline = anchor + self.INTERVAL if anchor is not None else None
        if tasks is None:
            tracked = [r for r in data['requests']
                       if r['status'] in {'active', 'paused', 'pending', 'awaiting_acceptance'}
                       or r.get('target_status') is not None]
            progression = [{'request_id': req['id'], 'state': 'unknown', 'reason': 'board_unavailable',
                            'waiting_on': None, 'task_ids': [],
                            'timers': [{'kind': 'scheduled_wake', **wake_ref(wake)} for wake in visible_wakes
                                       if wake['request_id'] == req['id'] and wake['state'] in {'scheduled', 'queued'}]}
                           for req in tracked]
        else:
            progression = self.health(data, tasks, notifications)
        return {'scheduled_wakes': collection(
                    [wake_ref(wake) for wake in visible_wakes], total=len(wakes)),
                'progression': progression,
                'liveness': {**state, 'due_at': deadline,
                             'overdue': deadline is not None and self.clock() >= deadline}}
