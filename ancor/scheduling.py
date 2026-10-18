"""Durable one-shot wakes and global liveness, using the requirement transaction."""
import math
import time
from uuid import uuid4


class Scheduling:
    INTERVAL = 7200

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
                return [dict(r) for r in db.execute(
                    'SELECT * FROM scheduled_wakes WHERE (? IS NULL OR requirement_id=?) ORDER BY due_at,id',
                    (args.get('requirement_id'), args.get('requirement_id')))]
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

    def observation(self, data, tasks=()):
        wakes = self.call({'action': 'list'})
        with self.store._transaction() as db:
            state = dict(db.execute('SELECT * FROM coordination_clock').fetchone())
        active = [r for r in data['requirements'] if r['status'] == 'active']
        anchor = max(state['active_since'] if state['active_since'] is not None else self.clock(), state['last_started'] or 0)
        deadline = anchor + self.INTERVAL if active else None
        progression = []
        by_id = {t['id']: t for t in tasks}
        for req in active:
            rid = req['id']
            sources = [{'kind': 'scheduled_wake', **w} for w in wakes if w['requirement_id'] == rid and w['state'] in {'scheduled', 'queued'}]
            sources += [{'kind': 'native_task', 'task_id': l['task_id'], 'status': by_id[l['task_id']]['status']} for l in data['task_links'] if l['requirement_id'] == rid and l['task_id'] in by_id and by_id[l['task_id']].get('status') in {'ready', 'running', 'todo'}]
            sources += [{'kind': 'pending_event', 'event_id': e['id']} for e in data['pending_events'] if e['payload'].get('requirement_id') == rid]
            progression.append({'requirement_id': rid, 'sources': sources, 'warning': None if sources else 'Missing explicit progression source; schedule a justified wake or arrange a real event source. Liveness insurance is not a plan.'})
        return {'scheduled_wakes': wakes, 'progression': progression, 'liveness': {**state, 'due_at': deadline, 'overdue': deadline is not None and self.clock() >= deadline}}
