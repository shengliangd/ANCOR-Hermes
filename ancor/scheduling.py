"""Durable one-shot wakes and global liveness, using the requirement transaction."""
import math
import time
from uuid import uuid4

from .views import WAKE_LIMIT, collection, preview, wake_ref


class Scheduling:
    INTERVAL = 7200
    HEARTBEAT_STALE_SECONDS = 3600
    DELIVERY_GRACE_SECONDS = 210

    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        with store._transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS scheduled_wakes (
                    id TEXT PRIMARY KEY, requirement_id TEXT NOT NULL,
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
                rid = args.get('requirement_id', old['requirement_id'] if old else None)
                if old and rid != old['requirement_id']:
                    raise ValueError('replacement must retain requirement')
                if self.store._get(db, 'requirements', rid)['status'] != 'active':
                    raise ValueError('requirement is not active')
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
        requirement_id = args.get('requirement_id')
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
            'SELECT * FROM scheduled_wakes WHERE (? IS NULL OR requirement_id=?) '
            'ORDER BY due_at,id', (requirement_id, requirement_id))]
        rows = [row for row in rows if row['state'] in states]
        return collection([wake_ref(row) for row in rows[:limit]], total=len(rows))

    def pending_wakes(self, requirement_id=None):
        with self.store._transaction() as db:
            self._suppress(db)
            return [dict(row) for row in db.execute(
                "SELECT * FROM scheduled_wakes WHERE state IN ('scheduled','queued') "
                "AND (? IS NULL OR requirement_id=?) ORDER BY due_at,id",
                (requirement_id, requirement_id))]

    def _suppress(self, db):
        db.execute("UPDATE scheduled_wakes SET state='suppressed' WHERE state IN ('scheduled','queued') AND requirement_id IN (SELECT id FROM requirements WHERE status!='active')")
        db.execute("UPDATE events SET acknowledged=1 WHERE source_key IN (SELECT 'wake:' || id FROM scheduled_wakes WHERE state IN ('suppressed','cancelled'))")
        db.execute("UPDATE scheduled_wakes SET state='handled' WHERE state='queued' AND EXISTS (SELECT 1 FROM events WHERE source_key='wake:' || scheduled_wakes.id AND acknowledged=1)")

    def scan(self):
        now = self.clock()
        with self.store._transaction() as db:
            self._suppress(db)
            for row in db.execute("SELECT * FROM scheduled_wakes WHERE state='scheduled' AND due_at<=?", (now,)).fetchall():
                self.store._add_event(db, 'wake.due', dict(row), 'wake:' + row['id'])
                db.execute("UPDATE scheduled_wakes SET state='queued' WHERE id=?", (row['id'],))
            active = db.execute("SELECT 1 FROM requirements WHERE status='active' LIMIT 1").fetchone()
            state = dict(db.execute('SELECT * FROM coordination_clock').fetchone())
            if not active:
                db.execute('UPDATE coordination_clock SET active_since=NULL')
                db.execute("UPDATE events SET acknowledged=1 WHERE kind='host.liveness'")
                return
            if state['active_since'] is None:
                db.execute('UPDATE coordination_clock SET active_since=?', (now,))
                state['active_since'] = now
            anchor = max(state['active_since'], state['last_started'] or state['active_since'])
            if now >= anchor + self.INTERVAL:
                event = self.store._add_event(db, 'host.liveness', {'reason': 'Active work has no successful coordinator start within two hours', 'due_at': anchor + self.INTERVAL}, 'liveness:' + str(state['generation']))
                db.execute('UPDATE events SET acknowledged=0 WHERE id=?', (event['id'],))

    def started(self):
        # Production calls this once when the child invokes its first ANCOR tool.
        # Enqueue, process creation and CLI startup failure are not coordination.
        with self.store._transaction() as db:
            db.execute('UPDATE coordination_clock SET last_started=?, generation=generation+1', (self.clock(),))
            db.execute("UPDATE events SET acknowledged=1 WHERE kind='host.liveness'")

    @staticmethod
    def _fresh(timestamp, now, *, seconds):
        return isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) and now - timestamp <= seconds

    @staticmethod
    def _question(notifications, requirement_id, task_id, *, open_only=False):
        return [row for row in notifications or ()
                if row.get('kind') == 'question'
                and row.get('requirement_id') == requirement_id
                and row.get('task_id') == task_id
                and (not open_only or (row.get('answer') is None
                                       and row.get('closed_at') is None))]

    def _blocked_wait(self, notifications, requirement_id, task_id, now):
        questions = self._question(notifications, requirement_id, task_id)
        # A recorded answer is progress even if transport state stayed uncertain.
        if any(row.get('answer') is not None for row in questions):
            return 'stalled', 'question_answered_not_resumed'
        open_questions = [row for row in questions if row.get('closed_at') is None]
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
        if any(row.get('closed_at') is not None for row in questions):
            return 'stalled', 'question_superseded'
        return 'stalled', 'blocked_without_wait'

    def health(self, data, tasks, notifications, now=None):
        """One requirement health result: active, waiting_on_human, or stalled."""
        now = self.clock() if now is None else now
        tasks = list(tasks or ())
        by_id = {task['id']: task for task in tasks}
        linked = {}
        for link in data['task_links']:
            linked.setdefault(link['requirement_id'], []).append(by_id.get(link['task_id'], {'id': link['task_id']}))
        wakes = self.pending_wakes()
        scope = {r['id'] for r in data['requirements']}
        wakes = [wake for wake in wakes if wake['requirement_id'] in scope]
        results = []
        for requirement in data['requirements']:
            rid = requirement['id']
            items = linked.get(rid, [])
            timers = [{'kind': 'scheduled_wake', **wake_ref(wake)} for wake in wakes
                      if wake['requirement_id'] == rid and wake['state'] in {'scheduled', 'queued'}]
            base = {'requirement_id': rid, 'tasks': [
                {key: task[key] for key in ('id', 'status', 'last_heartbeat_at',
                                            'block_kind', 'block_recurrences') if key in task}
                for task in items], 'timers': timers}
            for task in base['tasks']:
                if task.get('id') in by_id and by_id[task['id']].get('blocked_reason'):
                    shown, truncated = preview(by_id[task['id']]['blocked_reason'], 160)
                    task['blocked_reason_preview'] = shown
                    if truncated:
                        task['blocked_reason_truncated'] = True
            if requirement['status'] == 'paused':
                results.append({**base, 'state': 'paused', 'reason': 'requirement_paused',
                                'waiting_on': None,
                                'task_ids': [task['id'] for task in items]})
                continue
            if requirement['status'] != 'active':
                continue
            triage = [task for task in items if task.get('status') == 'triage']
            if triage:
                results.append({**base, 'state': 'stalled', 'reason': 'triage',
                                'waiting_on': None, 'task_ids': [task['id'] for task in triage]})
                continue
            blocked = [task for task in items if task.get('status') == 'blocked']
            blocked_health = [(task, *self._blocked_wait(notifications, rid, task['id'], now))
                              for task in blocked]
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
            waiting = [(task, reason) for task, state, reason in blocked_health if state == 'waiting']
            if waiting:
                questions = [row for task, _ in waiting for row in self._question(
                    notifications, rid, task['id'], open_only=True)]
                results.append({**base, 'state': 'waiting_on_human', 'reason': 'blocked_waiting_human',
                                'waiting_on': {'question_ids': [row['id'] for row in questions]},
                                'task_ids': [task['id'] for task, _ in waiting]})
                continue
            done = [task for task in items if task.get('status') == 'done']
            if items and len(done) == len(items):
                results.append({**base, 'state': 'stalled', 'reason': 'acceptance_pending',
                                'waiting_on': None, 'task_ids': [task['id'] for task in done]})
                continue
            reason = 'no_work' if not items else 'no_active_task'
            results.append({**base, 'state': 'stalled', 'reason': reason,
                            'waiting_on': None, 'task_ids': []})
        return results

    def sync_health(self, data, tasks, notifications):
        """Publish one host.stalled condition event per active requirement."""
        states = self.health(data, tasks, notifications)
        stalled = {item['requirement_id'] for item in states if item['state'] == 'stalled'}
        with self.store._transaction() as db:
            for rid in stalled:
                event = self.store._add_event(db, 'host.stalled', {'requirement_id': rid}, 'stalled:' + rid)
                db.execute('UPDATE events SET acknowledged=0 WHERE id=?', (event['id'],))
            for row in db.execute("SELECT id, source_key FROM events "
                                  "WHERE kind='host.stalled' AND acknowledged=0").fetchall():
                if row['source_key'] not in {'stalled:' + rid for rid in stalled}:
                    db.execute('UPDATE events SET acknowledged=1 WHERE id=?', (row['id'],))

    def observation(self, data, tasks=(), notifications=None):
        wakes = self.pending_wakes()
        scope_ids = {r['id'] for r in data['requirements']}
        wakes = [wake for wake in wakes if wake['requirement_id'] in scope_ids]
        visible_wakes = wakes[:WAKE_LIMIT]
        with self.store._transaction() as db:
            state = dict(db.execute('SELECT * FROM coordination_clock').fetchone())
        active = [r for r in data['requirements'] if r['status'] == 'active']
        anchor = max(state['active_since'] if state['active_since'] is not None else self.clock(), state['last_started'] or 0)
        deadline = anchor + self.INTERVAL if active else None
        if tasks is None:
            tracked = [r for r in data['requirements'] if r['status'] in {'active', 'paused'}]
            progression = [{'requirement_id': req['id'], 'state': 'unknown', 'reason': 'board_unavailable',
                            'waiting_on': None, 'task_ids': [],
                            'timers': [{'kind': 'scheduled_wake', **wake_ref(wake)} for wake in visible_wakes
                                       if wake['requirement_id'] == req['id'] and wake['state'] in {'scheduled', 'queued'}]}
                           for req in tracked]
        else:
            progression = self.health(data, tasks, notifications)
        return {'scheduled_wakes': collection(
                    [wake_ref(wake) for wake in visible_wakes], total=len(wakes)),
                'progression': progression,
                'liveness': {**state, 'due_at': deadline,
                             'overdue': deadline is not None and self.clock() >= deadline}}
