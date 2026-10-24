"""Versioned points and transactional changes, independent of transport and prompts.

The requirements row is a current projection; versions and committed changes are
retained. Legacy briefs remain authoritative until an explicitly confirmed convert.
"""
import json
import time
from uuid import uuid4

FIELDS = ('outcome', 'constraint', 'advice', 'context')
POINT_OPS = {'append', 'replace', 'invalidate'}
CONTROL_OPS = {'pause', 'resume', 'cancel', 'accept', 'request_changes', 'grant', 'revoke'}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def envelope(items, total=None):
    total = len(items) if total is None else total
    return {'items': items, 'count': total, 'returned': len(items),
            'truncated': total > len(items)}


class Changes:
    def _init_changes(self, db):
        self._ensure_columns(db, 'requirements', {
            'content_mode': "TEXT NOT NULL DEFAULT 'legacy'",
            'generation': 'INTEGER NOT NULL DEFAULT 1',
            'title_version': 'INTEGER NOT NULL DEFAULT 1',
            'control_version': 'INTEGER NOT NULL DEFAULT 1',
            'notes_version': 'INTEGER NOT NULL DEFAULT 0',
            'observed_generation': 'INTEGER NOT NULL DEFAULT 0',
            'verification_id': 'INTEGER',
        })
        db.executescript('''
            CREATE TABLE IF NOT EXISTS changes (
                id TEXT PRIMARY KEY, requirement_id TEXT NOT NULL,
                base TEXT NOT NULL, operations TEXT NOT NULL, source TEXT NOT NULL,
                actor TEXT NOT NULL, created_at REAL NOT NULL,
                committed_at REAL, confirmation_reference TEXT, result TEXT);
            CREATE INDEX IF NOT EXISTS changes_scope ON changes(requirement_id, committed_at);
            CREATE TABLE IF NOT EXISTS point_versions (
                version_id TEXT PRIMARY KEY, point_id TEXT NOT NULL,
                requirement_id TEXT NOT NULL, field TEXT NOT NULL, body TEXT NOT NULL,
                revision INTEGER NOT NULL, created_at REAL NOT NULL,
                created_by TEXT NOT NULL, change_id TEXT NOT NULL,
                replaces TEXT, invalidated_at REAL, invalidated_by TEXT,
                invalidated_change_id TEXT, position INTEGER NOT NULL,
                UNIQUE(point_id, revision));
            CREATE UNIQUE INDEX IF NOT EXISTS point_current
                ON point_versions(point_id) WHERE invalidated_at IS NULL;
            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY, requirement_id TEXT NOT NULL, task_id TEXT,
                body TEXT NOT NULL, reason TEXT NOT NULL, point_ids TEXT NOT NULL,
                state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL, created_by TEXT NOT NULL,
                resolved_change_id TEXT, closed_reason TEXT);
            CREATE TABLE IF NOT EXISTS question_replies (
                id TEXT PRIMARY KEY, question_id TEXT NOT NULL, change_id TEXT NOT NULL,
                body TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS question_delivery (
                question_id TEXT PRIMARY KEY, forwarded INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS grants (
                id TEXT PRIMARY KEY, requirement_id TEXT NOT NULL, action TEXT NOT NULL,
                target TEXT NOT NULL, scope TEXT NOT NULL, change_id TEXT NOT NULL,
                created_at REAL NOT NULL, revoked_change_id TEXT);
            CREATE TABLE IF NOT EXISTS task_basis (
                requirement_id TEXT NOT NULL, task_id TEXT NOT NULL,
                generation INTEGER NOT NULL, point_versions TEXT NOT NULL,
                PRIMARY KEY(requirement_id, task_id));
            CREATE TABLE IF NOT EXISTS impact_reviews (
                requirement_id TEXT NOT NULL, generation INTEGER NOT NULL,
                task_id TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
                state TEXT NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(requirement_id, generation, task_id));
            CREATE TABLE IF NOT EXISTS coverage_reviews (
                requirement_id TEXT NOT NULL, generation INTEGER NOT NULL,
                reason TEXT NOT NULL, PRIMARY KEY(requirement_id, generation));
            CREATE TABLE IF NOT EXISTS command_bindings (
                command_id TEXT PRIMARY KEY, generation INTEGER NOT NULL,
                verification_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS note_versions (
                id TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL,
                created_at INTEGER NOT NULL, deleted_at INTEGER,
                actor TEXT NOT NULL, history_missing INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(id, revision));
        ''')
        # Only extant note bodies can be recovered from pre-versioning databases.
        db.execute('''INSERT OR IGNORE INTO note_versions
            SELECT id,revision,body,updated_at,deleted_at,'legacy_import',1
            FROM requirement_notes''')

    def _points(self, db, rid, history=False):
        query = 'SELECT * FROM point_versions WHERE requirement_id=?'
        if not history:
            query += ' AND invalidated_at IS NULL'
        return [dict(row) for row in db.execute(query + ' ORDER BY position, point_id, revision', (rid,))]

    def content(self, rid, *, history=False):
        with self._transaction() as db:
            req = self._get(db, 'requirements', rid)
            points = self._points(db, rid, history)
            return {'mode': req['content_mode'], 'generation': req['generation'],
                    'fields': {f: [p for p in points if p['field'] == f] for f in FIELDS},
                    **({'legacy_brief': req['brief']} if req['content_mode'] == 'legacy' else {})}

    def _change(self, db, cid):
        row = self._get(db, 'changes', cid)
        for field in ('base', 'operations', 'source', 'result'):
            row[field] = json.loads(row[field]) if row[field] is not None else None
        return row

    def get_change(self, cid):
        with self._transaction() as db:
            return self._change(db, cid)

    def list_changes(self, rid, *, before=None, limit=50, include_notes=True):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('limit must be between 1 and 200')
        with self._transaction() as db:
            self._get(db, 'requirements', rid)
            rows = db.execute('SELECT id FROM changes WHERE requirement_id=? AND committed_at IS NOT NULL '
                              'ORDER BY committed_at DESC, rowid DESC', (rid,)).fetchall()
            ids = [r['id'] for r in rows]
            if not include_notes:
                ids = [cid for cid in ids if self._change(db, cid)['source'].get('kind') != 'coordinator_note']
            if before is not None:
                if before not in ids:
                    raise KeyError(before)
                ids = ids[ids.index(before) + 1:]
            return envelope([self._change(db, i) for i in ids[:limit]], len(ids))

    def propose_change(self, rid, operations, *, actor, source, base=None):
        self._text(actor)
        if not isinstance(source, dict) or not isinstance(operations, list) or not operations:
            raise ValueError('source object and nonempty operations required')
        if len(operations) > 200:
            raise ValueError('at most 200 operations per change')
        allowed = POINT_OPS | CONTROL_OPS | {'create', 'convert', 'title', 'reply', 'resolve_question'}
        for op in operations:
            if not isinstance(op, dict) or op.get('op') not in allowed:
                raise ValueError('unknown change operation')
        shapes = {
            'create': ({'title', 'fields'}, set()), 'convert': ({'fields'}, set()),
            'append': ({'field', 'body'}, set()), 'replace': ({'version_id', 'body'}, {'field'}),
            'invalidate': ({'version_id'}, set()), 'title': ({'title'}, set()),
            'reply': ({'question_id', 'revision', 'body'}, set()),
            'resolve_question': ({'question_id', 'revision', 'reason'}, set()),
            **{a: ({'reason'}, set()) for a in ('pause', 'resume', 'cancel')},
            'accept': ({'verification_id'}, set()), 'request_changes': ({'feedback'}, set()),
            'grant': ({'action', 'target', 'scope'}, set()), 'revoke': ({'grant_id'}, set()),
        }
        for op in operations:
            required, optional = shapes[op['op']]
            if not required <= set(op) or set(op) - required - optional - {'op'}:
                raise ValueError('unexpected or missing operation fields')
            for key in ('revision', 'verification_id'):
                if key in op and (type(op[key]) is not int or op[key] < 1):
                    raise ValueError(f'{key} must be a positive integer')
        lifecycle = [op for op in operations if op['op'] in {'pause', 'resume', 'cancel', 'accept', 'request_changes'}]
        if len(lifecycle) > 1:
            raise ValueError('at most one lifecycle action per change')
        if any(op['op'] == 'accept' for op in operations) and any(op['op'] in POINT_OPS | {'convert'} for op in operations):
            raise ValueError('cannot accept and change requirements together')
        cid = uuid4().hex
        creating = operations[0]['op'] == 'create'
        rid = rid or (uuid4().hex if creating else None)
        with self._transaction() as db:
            if creating:
                if db.execute('SELECT 1 FROM requirements WHERE id=?', (rid,)).fetchone():
                    raise ValueError('requirement already exists')
                expected = {}
            else:
                req = self._get(db, 'requirements', rid)
                expected = {key: req[key] for key in self._touched_versions(operations)}
                if not isinstance(base, dict) or any(type(base.get(k)) is not int or base[k] != v for k, v in expected.items()):
                    raise RuntimeError('stale or missing base versions; read current requirement and propose again')
            # Validate the exact entire batch in a rollback-only savepoint.
            db.execute('SAVEPOINT validate_change')
            try:
                self._apply_operations(db, rid, operations, cid, actor, 'proposal-validation')
            finally:
                db.execute('ROLLBACK TO validate_change')
                db.execute('RELEASE validate_change')
            db.execute('INSERT INTO changes VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL)',
                       (cid, rid, encode(expected), encode(operations), encode(source), actor, time.time()))
            return self._change(db, cid)

    @staticmethod
    def _touched_versions(ops):
        touched = set()
        for op in ops:
            kind = op['op']
            if kind in POINT_OPS or kind == 'convert':
                touched.update(('generation', 'control_version'))
            elif kind == 'title':
                touched.update(('title_version', 'control_version'))
            elif kind in CONTROL_OPS:
                touched.update(('generation', 'control_version'))
            elif kind in {'reply', 'resolve_question'}:
                touched.update(('generation', 'control_version'))
        return touched

    def commit_change(self, cid, confirmation_reference):
        self._text(confirmation_reference)
        with self._transaction() as db:
            change = self._change(db, cid)
            if change['committed_at'] is not None:
                return change['result']
            rid = change['requirement_id']
            if change['base']:
                req = self._get(db, 'requirements', rid)
                if any(req[key] != value for key, value in change['base'].items()):
                    raise RuntimeError('change conflicts with current versions; propose and confirm again')
            result = self._apply_operations(db, rid, change['operations'], cid,
                                            change['actor'], confirmation_reference)
            db.execute('UPDATE changes SET committed_at=?, confirmation_reference=?, result=? WHERE id=?',
                       (time.time(), confirmation_reference, encode(result), cid))
            self._add_event(db, 'requirement.changed', {
                'requirement_id': rid, 'change_id': cid,
                'generation': result['generation'],
                'content_changed': result['content_changed']}, source_key='change:' + cid,
                acknowledged=not result['needs_coordination'])
            return result

    def _apply_operations(self, db, rid, operations, cid, actor, reference):
        now = time.time()
        creating = operations[0]['op'] == 'create'
        if creating:
            op = operations[0]
            title = self._title(op['title'])
            db.execute('INSERT INTO drafts(id,title,brief) VALUES(?,?,?)', (cid, title, ''))
            db.execute('INSERT INTO requirements(id,draft_id,title,brief,confirmation_reference,status,content_mode) '
                       "VALUES(?,?,?,'',?,'active','points')", (rid, cid, title, reference))
        req = self._get(db, 'requirements', rid)
        title_only = all(op['op'] == 'title' for op in operations)
        if not title_only and (req['status'] in {'cancelled', 'invalidated', 'accepted'} or req['target_status'] is not None):
            raise ValueError('requirement is terminal or transitioning')
        content_changed, title_changed, control_changed = creating, False, False
        significant = False
        touched_points = set()
        question_revisions = {}
        for index, op in enumerate(operations):
            kind = op['op']
            if kind == 'create':
                if not creating or index != 0:
                    raise ValueError('create must be the first operation on a new requirement')
                self._append_fields(db, rid, op.get('fields', {}), cid, actor, now)
            elif kind == 'convert':
                if req['content_mode'] != 'legacy' or content_changed:
                    raise ValueError('convert requires a legacy requirement')
                self._append_fields(db, rid, op['fields'], cid, actor, now)
                db.execute("UPDATE requirements SET content_mode='points' WHERE id=?", (rid,))
                content_changed = True
            elif kind in POINT_OPS:
                if self._get(db, 'requirements', rid)['content_mode'] != 'points':
                    raise ValueError('convert legacy brief before editing points')
                if kind == 'append':
                    self._append_point(db, rid, op['field'], op['body'], cid, actor, now)
                else:
                    point = db.execute('SELECT * FROM point_versions WHERE version_id=? AND requirement_id=? '
                                       'AND invalidated_at IS NULL', (op['version_id'], rid)).fetchone()
                    if point is None or point['point_id'] in touched_points:
                        raise RuntimeError('point version is no longer current or modified twice')
                    touched_points.add(point['point_id'])
                    db.execute('UPDATE point_versions SET invalidated_at=?, invalidated_by=?, '
                               'invalidated_change_id=? WHERE version_id=?',
                               (now, actor, cid, point['version_id']))
                    if kind == 'replace':
                        self._append_point(db, rid, op.get('field', point['field']), op['body'], cid, actor, now,
                                           previous=point)
                content_changed = True
            elif kind == 'title':
                db.execute('UPDATE requirements SET title=? WHERE id=?', (self._title(op['title']), rid))
                title_changed = True
            elif kind in {'reply', 'resolve_question'}:
                q = self._get(db, 'questions', op['question_id'])
                initial_revision = question_revisions.setdefault(q['id'], q['revision'])
                if q['requirement_id'] != rid or q['state'] != 'open' or initial_revision != op['revision']:
                    raise RuntimeError('question changed or is closed')
                if kind == 'reply':
                    db.execute('INSERT INTO question_replies VALUES(?,?,?,?,?)',
                               (uuid4().hex, q['id'], cid, self._text(op['body']), now))
                    db.execute('UPDATE questions SET revision=revision+1 WHERE id=?', (q['id'],))
                else:
                    self._text(op['reason'])
                    db.execute("UPDATE questions SET state='resolved', revision=revision+1, "
                               'resolved_change_id=?,closed_reason=? WHERE id=?', (cid, op['reason'], q['id']))
                significant = True
            elif kind in {'pause', 'resume', 'cancel'}:
                current = self._get(db, 'requirements', rid)
                allowed = {'paused'} if kind == 'resume' else {'active'} if kind == 'pause' else {'active', 'paused', 'awaiting_acceptance'}
                if current['status'] not in allowed or current['target_status']:
                    raise ValueError(f'{kind} requires a compatible stable requirement')
                target = {'pause': 'paused', 'resume': 'active', 'cancel': 'cancelled'}[kind]
                self._text(op['reason'])
                db.execute('UPDATE requirements SET target_status=? WHERE id=?', (target, rid))
                self._add_event(db, 'requirement.transition_requested', {
                    'requirement_id': rid, 'from_status': current['status'], 'target_status': target,
                    'reason': op['reason'], 'change_id': cid, 'confirmation_reference': reference})
                control_changed = significant = True
            elif kind == 'request_changes':
                if req['status'] != 'awaiting_acceptance':
                    raise ValueError('request_changes requires awaiting acceptance')
                self._add_event(db, 'requirement.changes_requested', {
                    'requirement_id': rid, 'feedback': self._text(op['feedback']),
                    'change_id': cid, 'confirmation_reference': reference})
                db.execute("UPDATE requirements SET status='active' WHERE id=?", (rid,))
                control_changed = significant = True
            elif kind == 'accept':
                verification = self._latest_verification(db, rid)
                if (content_changed or req['status'] != 'awaiting_acceptance' or verification is None
                        or verification['id'] != op['verification_id']
                        or verification['payload'].get('generation', 1) != req['generation']):
                    raise RuntimeError('acceptance must bind the current verification and generation')
                db.execute("UPDATE requirements SET status='accepted' WHERE id=?", (rid,))
                self._add_event(db, 'requirement.accepted', {
                    'requirement_id': rid, 'change_id': cid, 'generation': req['generation'],
                    'verification_event_id': verification['id'],
                    'evidence': verification['payload']['evidence'],
                    'confirmation_reference': reference}, acknowledged=True)
                control_changed = True
            elif kind == 'grant':
                db.execute('INSERT INTO grants VALUES(?,?,?,?,?,?,?,NULL)',
                           (uuid4().hex, rid, self._text(op['action']), self._text(op['target']),
                            self._text(op['scope']), cid, now))
                control_changed = significant = True
            elif kind == 'revoke':
                grant = self._get(db, 'grants', op['grant_id'])
                if grant['requirement_id'] != rid or grant['revoked_change_id'] is not None:
                    raise ValueError('grant is not active on this requirement')
                db.execute('UPDATE grants SET revoked_change_id=? WHERE id=?', (cid, grant['id']))
                control_changed = significant = True
        if content_changed:
            points = self._points(db, rid)
            if not any(p['field'] == 'outcome' for p in points):
                raise ValueError('at least one active outcome required')
            brief = '\n\n'.join(f'{field}:\n' + '\n'.join('- ' + p['body'] for p in points if p['field'] == field)
                                for field in FIELDS if any(p['field'] == field for p in points))
            db.execute('UPDATE requirements SET brief=?,generation=generation+? WHERE id=?',
                       (brief, 0 if creating else 1, rid))
            if req['status'] == 'awaiting_acceptance':
                db.execute("UPDATE requirements SET status='active' WHERE id=?", (rid,))
                control_changed = True
        if title_changed:
            db.execute('UPDATE requirements SET title_version=title_version+1 WHERE id=?', (rid,))
        if control_changed:
            db.execute('UPDATE requirements SET control_version=control_version+1 WHERE id=?', (rid,))
        current = self._get(db, 'requirements', rid)
        return {'requirement_id': rid, 'change_id': cid, 'generation': current['generation'],
                'content_changed': content_changed, 'needs_coordination': content_changed or significant,
                'status': current['status']}

    def _append_fields(self, db, rid, fields, cid, actor, now):
        if not isinstance(fields, dict) or set(fields) - set(FIELDS):
            raise ValueError('fields must contain only outcome, constraint, advice, context')
        for field, bodies in fields.items():
            if not isinstance(bodies, list):
                raise ValueError('each field must be a list of strings')
            for body in bodies:
                self._append_point(db, rid, field, body, cid, actor, now)

    def _append_point(self, db, rid, field, body, cid, actor, now, previous=None):
        if field not in FIELDS:
            raise ValueError('unknown point field')
        self._text(body)
        position = db.execute('SELECT COALESCE(MAX(position),0)+1 FROM point_versions WHERE requirement_id=?',
                              (rid,)).fetchone()[0]
        db.execute('INSERT INTO point_versions VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?)',
                   (uuid4().hex, previous['point_id'] if previous else uuid4().hex, rid, field, body,
                    previous['revision'] + 1 if previous else 1, now, actor, cid,
                    previous['version_id'] if previous else None,
                    previous['position'] if previous else position))

    def create_question(self, rid, *, identifier, body, reason, actor, task_id=None, point_ids=()):
        for value in (identifier, body, reason, actor):
            self._text(value)
        with self._transaction() as db:
            req = self._get(db, 'requirements', rid)
            if req['status'] != 'active' or req['target_status']:
                raise ValueError('question requires stable active requirement')
            existing = db.execute('SELECT * FROM questions WHERE id=?', (identifier,)).fetchone()
            if existing:
                if any(existing[k] != v for k, v in {'requirement_id': rid, 'task_id': task_id,
                       'body': body, 'reason': reason, 'point_ids': encode(list(point_ids))}.items()):
                    raise RuntimeError('question identity already used')
                return dict(existing)
            if task_id and not db.execute('SELECT 1 FROM task_links WHERE requirement_id=? AND task_id=?',
                                         (rid, task_id)).fetchone():
                raise ValueError('question task must be linked')
            active = {p['point_id'] for p in self._points(db, rid)}
            if not set(point_ids) <= active:
                raise ValueError('question references inactive or foreign points')
            db.execute('INSERT INTO questions VALUES(?,?,?,?,?,?,?,1,?,?,NULL,NULL)',
                       (identifier, rid, task_id, body, reason, encode(list(point_ids)), 'open', time.time(), actor))
            db.execute('INSERT INTO question_delivery VALUES(?,0)', (identifier,))
            return self._get(db, 'questions', identifier)

    def questions(self, rid=None):
        with self._transaction() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM questions WHERE (? IS NULL OR requirement_id=?) '
                                              'ORDER BY created_at,id', (rid, rid))]
            for row in rows:
                row['point_ids'] = json.loads(row['point_ids'])
                row['replies'] = [dict(r) for r in db.execute('SELECT * FROM question_replies WHERE question_id=? '
                                                           'ORDER BY created_at,id', (row['id'],))]
            return rows

    def withdraw_question(self, qid, reason, actor):
        self._text(reason)
        with self._transaction() as db:
            q = self._get(db, 'questions', qid)
            if q['state'] == 'open':
                db.execute("UPDATE questions SET state='withdrawn',revision=revision+1,closed_reason=? WHERE id=?",
                           (reason, qid))
                self._add_event(db, 'question.withdrawn', {'question_id': qid, 'requirement_id': q['requirement_id'],
                                                         'reason': reason, 'actor': actor}, acknowledged=True)
            return self._get(db, 'questions', qid)

    def bind_task(self, rid, tid, generation, versions):
        with self._transaction() as db:
            req = self._get(db, 'requirements', rid)
            if generation != req['generation'] or not isinstance(versions, list):
                raise RuntimeError('task basis must use current generation')
            if not db.execute('SELECT 1 FROM task_links WHERE requirement_id=? AND task_id=?', (rid, tid)).fetchone():
                raise ValueError('task must be linked')
            active = {p['version_id'] for p in self._points(db, rid)}
            if not set(versions) <= active or (req['content_mode'] == 'points' and not versions):
                raise ValueError('task basis requires current point versions')
            db.execute('INSERT OR REPLACE INTO task_basis VALUES(?,?,?,?)', (rid, tid, generation, encode(versions)))

    def coordination(self, rid):
        with self._transaction() as db:
            req = self._get(db, 'requirements', rid)
            reviews = [dict(r) for r in db.execute('SELECT * FROM impact_reviews WHERE requirement_id=? '
                       'AND generation=? ORDER BY task_id', (rid, req['generation']))]
            basis = [dict(r) for r in db.execute('SELECT * FROM task_basis WHERE requirement_id=?', (rid,))]
            for row in basis:
                row['point_versions'] = json.loads(row['point_versions'])
            changes = []
            for row in db.execute('SELECT id,result,operations FROM changes WHERE requirement_id=? AND committed_at IS NOT NULL ORDER BY committed_at', (rid,)):
                result = json.loads(row['result'])
                if result.get('content_changed') and result.get('generation', 0) > req['observed_generation']:
                    changes.append({'change_id': row['id'], 'generation': result['generation'],
                                    'operations': [{'op': op['op'], **{k: op[k] for k in ('version_id', 'field') if k in op}}
                                                   for op in json.loads(row['operations'])]})
            return {'changes': envelope(changes[:50], len(changes)),
                    'generation': req['generation'], 'observed_generation': req['observed_generation'],
                    'needs_review': req['observed_generation'] != req['generation'],
                    'reviews': reviews, 'task_basis': basis}

    def review_impact(self, rid, generation, decisions, coverage_reason):
        self._text(coverage_reason)
        with self._transaction() as db:
            req = self._get(db, 'requirements', rid)
            if generation != req['generation'] or req['status'] != 'active' or req['target_status']:
                raise RuntimeError('review requires current stable active generation')
            linked = {r[0] for r in db.execute('SELECT task_id FROM task_links WHERE requirement_id=?', (rid,))}
            if not isinstance(decisions, list) or {d['task_id'] for d in decisions} != linked or len(decisions) != len(linked):
                raise ValueError('review must cover every linked task, including completed work')
            for d in decisions:
                if d['decision'] not in {'unaffected', 'reverify', 'revise', 'retire'}:
                    raise ValueError('invalid impact decision')
                self._text(d['reason'])
                old = db.execute('SELECT * FROM impact_reviews WHERE requirement_id=? AND generation=? AND task_id=?',
                                 (rid, generation, d['task_id'])).fetchone()
                state = 'applied' if d['decision'] == 'unaffected' else 'pending'
                if old and old['decision'] == d['decision'] and old['reason'] == d['reason']:
                    state = old['state']
                db.execute('INSERT OR REPLACE INTO impact_reviews VALUES(?,?,?,?,?,?,?)',
                           (rid, generation, d['task_id'], d['decision'], d['reason'], state, time.time()))
            db.execute('INSERT OR REPLACE INTO coverage_reviews VALUES(?,?,?)', (rid, generation, coverage_reason))
            db.execute('UPDATE requirements SET observed_generation=? WHERE id=?', (generation, rid))
        return self.coordination(rid)

    def finish_impact(self, rid, generation, tid, evidence=None):
        with self._transaction() as db:
            req = self._get(db, 'requirements', rid)
            if generation != req['generation']:
                raise RuntimeError('stale impact review')
            previous = db.execute('SELECT state FROM impact_reviews WHERE requirement_id=? AND generation=? AND task_id=?',
                                  (rid, generation, tid)).fetchone()
            if previous and previous['state'] == 'applied':
                return
            cursor = db.execute("UPDATE impact_reviews SET state='applied',updated_at=? WHERE requirement_id=? "
                                'AND generation=? AND task_id=?', (time.time(), rid, generation, tid))
            if not cursor.rowcount:
                raise KeyError(tid)
            self._add_event(db, 'task.impact_applied', {'requirement_id': rid, 'generation': generation,
                'task_id': tid, 'evidence': evidence}, acknowledged=True)

    def _note_history(self, db, note, actor='ancor-coordinator'):
        db.execute('INSERT INTO note_versions VALUES(?,?,?,?,?,?,0)',
                   (note['id'], note['revision'], note['body'], note['updated_at'], note['deleted_at'], actor))
        req = self._get(db, 'requirements', note['requirement_id'])
        cid = uuid4().hex
        now = time.time()
        kind = 'invalidate' if note['deleted_at'] else 'append' if note['revision'] == 1 else 'replace'
        operation = {'op': kind, 'field': 'notes', 'point_id': note['id'], 'revision': note['revision'], 'body': note['body']}
        db.execute('INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (cid, note['requirement_id'], encode({'notes_version': req['notes_version']}), encode([operation]),
                    encode({'kind': 'coordinator_note'}), actor, now, now, None, encode({'note_id': note['id']})))
        db.execute('UPDATE requirements SET notes_version=notes_version+1 WHERE id=?', (note['requirement_id'],))

    def note_history(self, rid, nid):
        with self._transaction() as db:
            self._get_note(db, rid, nid)
            return envelope([dict(r) for r in db.execute('SELECT * FROM note_versions WHERE id=? ORDER BY revision', (nid,))])

    def _check_verification(self, db, req, evidence):
        data = json.loads(evidence)
        if data.get('generation') != req['generation']:
            raise RuntimeError('verification must bind current generation')
        if not db.execute('SELECT 1 FROM coverage_reviews WHERE requirement_id=? AND generation=?',
                          (req['id'], req['generation'])).fetchone():
            raise RuntimeError('coverage review required')
        if req['observed_generation'] != req['generation']:
            raise RuntimeError('current generation has not been reviewed')
        if db.execute("SELECT 1 FROM impact_reviews WHERE requirement_id=? AND generation=? AND state='pending'",
                      (req['id'], req['generation'])).fetchone():
            raise RuntimeError('impact adjustments are still pending')
        required = {p['version_id'] for p in self._points(db, req['id']) if p['field'] in {'outcome', 'constraint'}}
        coverage = data.get('coverage')
        if (not isinstance(coverage, list) or any(not isinstance(c, dict) for c in coverage)
                or len(coverage) != len(required) or {c.get('version_id') for c in coverage} != required):
            raise ValueError('verification must cover every current outcome and constraint version')
        for item in coverage:
            self._text(item.get('evidence'))

    def sync_questions(self, box):
        """Recover legacy questions and bridge durable delivery intents idempotently.

        Transport writes are outside the business transaction. Repeating enqueue
        with the same ID is safe after a crash before marking an intent forwarded.
        """
        for row in box.list():
            if row['kind'] != 'question' or not row.get('requirement_id'):
                continue
            with self._transaction() as db:
                if not db.execute('SELECT 1 FROM requirements WHERE id=?', (row['requirement_id'],)).fetchone():
                    continue
                exists = db.execute('SELECT * FROM questions WHERE id=?', (row['id'],)).fetchone()
                if exists and exists['created_by'] == 'legacy_import':
                    state = 'resolved' if row.get('answer') is not None else 'withdrawn' if row.get('closed_at') else exists['state']
                    if state != exists['state']:
                        db.execute('UPDATE questions SET state=?,revision=revision+1,closed_reason=? WHERE id=?',
                                   (state, row.get('close_reason'), row['id']))
                        if row.get('answer') is not None:
                            db.execute('INSERT INTO question_replies VALUES(?,?,?,?,?)',
                                       (uuid4().hex, row['id'], 'legacy:' + row['id'], row['answer'], time.time()))
                if not exists:
                    state = 'resolved' if row.get('answer') is not None else 'withdrawn' if row.get('closed_at') else 'open'
                    db.execute('INSERT INTO questions VALUES(?,?,?,?,?,?,?,1,?,?,NULL,?)',
                               (row['id'], row['requirement_id'], row.get('task_id'), row['message'],
                                'Imported legacy notification; answer remains historical input.', '[]', state,
                                row.get('queued_at') or time.time(), 'legacy_import', row.get('close_reason')))
                    if row.get('answer') is not None:
                        db.execute('INSERT INTO question_replies VALUES(?,?,?,?,?)',
                                   (uuid4().hex, row['id'], 'legacy:' + row['id'], row['answer'], time.time()))
        with self._transaction() as db:
            pending = [dict(row) for row in db.execute('SELECT q.* FROM questions q JOIN question_delivery d '
                       'ON q.id=d.question_id WHERE d.forwarded=0')]
        for question in pending:
            if question['state'] != 'open' or box.target is None:
                continue
            box.enqueue(question['id'], 'question', question['body'], actor=box.coordinator_profile,
                        requirement_id=question['requirement_id'], task_id=question['task_id'])
            with self._transaction() as db:
                db.execute('UPDATE question_delivery SET forwarded=1 WHERE question_id=?', (question['id'],))
        rows = {r['id']: r for r in box.list()}
        for question in self.questions():
            req = self.get_requirement(question['requirement_id'])
            if question['state'] == 'open' and (req['status'] in {'accepted', 'cancelled', 'invalidated'}
                    or req['target_status'] in {'cancelled', 'invalidated'}):
                question = self.withdraw_question(question['id'], 'requirement_' + (req['target_status'] or req['status']), 'host')
            row = rows.get(question['id'])
            if not row:
                continue
            if question['state'] != 'open' and row.get('answer') is None and not row.get('closed_at'):
                box.close_question(question['id'], closed_by='requirement_store',
                                   reason=question.get('closed_reason') or 'question_' + question['state'])

    def active_grants(self, rid):
        with self._transaction() as db:
            self._get(db, 'requirements', rid)
            return [dict(r) for r in db.execute('SELECT * FROM grants WHERE requirement_id=? '
                                               'AND revoked_change_id IS NULL ORDER BY created_at,id', (rid,))]

    def pending_coordination(self):
        with self._transaction() as db:
            return [dict(r) for r in db.execute("SELECT id,generation,observed_generation FROM requirements r "
                "WHERE content_mode='points' AND status='active' AND target_status IS NULL AND "
                "(observed_generation<>generation OR EXISTS(SELECT 1 FROM impact_reviews i WHERE "
                "i.requirement_id=r.id AND i.generation=r.generation AND i.state='pending'))")]
