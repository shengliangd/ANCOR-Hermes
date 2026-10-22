from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ancor.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'store.sqlite'
        self.store = Store(self.path)

    def advance(self, requirement):
        return self.store.finalize_transition(requirement['id'])['requirement']

    def advance_all(self, requirements):
        return [self.advance(requirement) for requirement in requirements]

    def test_legacy_schema_requires_explicit_title_migration(self):
        database = Path(self.directory.name) / 'old-store.sqlite'
        db = sqlite3.connect(database)
        try:
            db.execute(
                'CREATE TABLE drafts ('
                'id TEXT PRIMARY KEY, brief TEXT NOT NULL, '
                'acceptance_criteria TEXT NOT NULL, replacement_of TEXT)')
            db.execute('INSERT INTO drafts VALUES (?, ?, ?, ?)',
                       ('old', 'previous brief', 'previous criteria', None))
            db.execute(
                'CREATE TABLE requirements ('
                'id TEXT PRIMARY KEY, draft_id TEXT UNIQUE NOT NULL, '
                'brief TEXT NOT NULL, acceptance_criteria TEXT NOT NULL, '
                'replacement_of TEXT, confirmation_reference TEXT NOT NULL, '
                'status TEXT NOT NULL)')
            db.commit()
        finally:
            db.close()

        with self.assertRaisesRegex(RuntimeError, 'migrate_titles'):
            Store(database)
        db = sqlite3.connect(database)
        try:
            columns = {row[1] for row in db.execute('PRAGMA table_info(drafts)')}
            rows = list(db.execute('SELECT id, brief FROM drafts'))
        finally:
            db.close()
        self.assertNotIn('title', columns)
        self.assertEqual(rows, [('old', 'previous brief')])

    def test_concurrent_confirmation_creates_one_requirement_and_event(self):
        draft = self.store.create_draft('brief', 'criteria')
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: Store(self.path).confirm(draft['id'], 'ref'), range(8)))
        self.assertEqual(len({item['id'] for item in results}), 1)
        self.assertEqual(len(self.store.snapshot()['requirements']), 1)
        self.assertEqual(len(self.store.pending_events()), 1)

    def test_event_write_failure_rolls_back_replacement(self):
        old = self.store.confirm(self.store.create_draft('old', 'criteria')['id'], 'ref')
        draft = self.store.create_draft('new', 'criteria', old['id'])
        db = sqlite3.connect(self.path)
        try:
            db.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'disk simulation'); END")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.confirm(draft['id'], 'ref2')
        self.assertEqual(self.store.get_requirement(old['id'])['status'], 'active')
        self.assertEqual(len(self.store.snapshot()['requirements']), 1)
        self.assertEqual(len(self.store.pending_events()), 1)

    def test_snapshot_links_native_tasks_without_copying_board_state(self):
        draft = self.store.create_draft('brief', 'criteria')
        requirement = self.store.confirm(draft['id'], 'ref')
        self.store.link_task(requirement['id'], 'native-42')
        self.store.link_task(requirement['id'], 'native-42')
        snapshot = Store(self.path).snapshot()
        self.assertEqual(snapshot['drafts'], [draft])
        self.assertEqual(snapshot['requirements'], [requirement])
        self.assertEqual(snapshot['task_links'],
                         [{'requirement_id': requirement['id'], 'task_id': 'native-42'}])
        self.assertEqual(snapshot['pending_events'], self.store.pending_events())
        with self.assertRaises(KeyError):
            self.store.link_task('unknown', 'native-43')

    def test_requirement_lifecycle_emits_events_and_acceptance_needs_evidence(self):
        draft = self.store.create_draft('brief', 'criteria')
        requirement = self.store.confirm(draft['id'], 'ref')
        self.assertEqual([e['kind'] for e in self.store.pending_events()],
                         ['requirement.activated'])
        self.store.confirm(draft['id'], 'retry')
        self.assertEqual(len(self.store.pending_events()), 1)
        with self.assertRaises(ValueError):
            self.store.accept(requirement['id'], ' ')
        self.store.reject(requirement['id'], 'not yet')
        self.assertEqual(self.store.get_requirement(requirement['id'])['status'], 'active')
        verified = self.store.submit_acceptance(requirement['id'], 'test report URL')
        self.assertEqual(verified['status'], 'awaiting_acceptance')
        self.store.accept(requirement['id'], 'human:accept')
        self.assertEqual(self.store.get_requirement(requirement['id'])['status'], 'accepted')
        self.assertEqual(self.store.acceptances()[-1]['payload']['evidence'], 'test report URL')
        with self.assertRaises(ValueError):
            self.store.accept(requirement['id'], 'human:again')
        with self.assertRaises(ValueError):
            self.store.reject(requirement['id'], 'late')
        self.assertEqual([e['kind'] for e in self.store.pending_events()],
                         ['requirement.activated', 'requirement.rejected'])

    def test_replacement_emits_invalidation_and_blocks_old_acceptance(self):
        old = self.store.confirm(self.store.create_draft('old', 'criteria')['id'], 'ref')
        new = self.store.confirm(self.store.create_draft('new', 'criteria', old['id'])['id'], 'ref2')
        self.assertEqual([e['kind'] for e in self.store.pending_events()],
                         ['requirement.activated', 'requirement.transition_requested',
                          'requirement.staged'])
        self.assertEqual(self.store.get_requirement(old['id'])['target_status'], 'invalidated')
        self.assertEqual(new['status'], 'pending')
        with self.assertRaises(ValueError):
            self.store.accept(old['id'], 'obsolete evidence')

    def test_events_are_durable_deduplicated_and_acknowledged_by_exact_id(self):
        first = self.store.add_event('native.finished', {'task_id': 't1'}, 'native:1')
        self.assertEqual(self.store.add_event('ignored', {}, 'native:1'), first)
        observed = self.store.pending_events()
        later = self.store.add_event('native.finished', {'task_id': 't2'}, 'native:2')
        Store(self.path).ack_events([event['id'] for event in observed])
        self.assertEqual(self.store.pending_events(), [later])
        self.assertEqual(self.store.add_event('retry', {}, 'native:1')['id'], first['id'])
        self.store.ack_events([first['id']])
        self.assertEqual(Store(self.path).pending_events(), [later])

    def test_replacement_is_atomic_and_stale_draft_cannot_branch(self):
        original = self.store.confirm(self.store.create_draft('old', 'criteria')['id'], 'ref')
        first = self.store.create_draft('new', 'criteria', original['id'])
        stale = self.store.create_draft('stale', 'criteria', original['id'])
        replacement = self.store.confirm(first['id'], 'ref2')
        self.assertEqual(self.store.get_requirement(original['id'])['target_status'], 'invalidated')
        self.assertEqual(replacement['status'], 'pending')
        with self.assertRaises(ValueError):
            self.store.confirm(stale['id'], 'ref3')
        self.assertEqual(self.store.confirm(first['id'], 'retry'), replacement)
        with self.assertRaisesRegex(ValueError, 'does not reference an existing requirement'):
            self.store.create_draft('missing', 'criteria', 'nonexistent')

    def test_replacement_requires_confirmed_active_or_paused_requirement(self):
        provisional = self.store.create_draft('provisional', 'criteria')
        before = self.store.snapshot()['drafts']
        with self.assertRaisesRegex(ValueError, 'is a draft, not a confirmed requirement'):
            self.store.create_draft('invalid', 'criteria', [provisional['id']])
        self.assertEqual(self.store.snapshot()['drafts'], before)

        cancelled = self.store.confirm(
            self.store.create_draft('cancelled', 'criteria')['id'], 'ref')
        self.store.cancel([cancelled['id']], 'withdrawn')
        with self.assertRaisesRegex(ValueError, 'replacement bases must be stable'):
            self.store.create_draft('invalid', 'criteria', cancelled['id'])
        self.advance(cancelled)
        with self.assertRaisesRegex(ValueError, 'replacement bases must be stable'):
            self.store.create_draft('invalid', 'criteria', cancelled['id'])

    def test_legacy_draft_replacement_fails_confirmation_clearly(self):
        base = self.store.create_draft('draft only', 'criteria')
        draft_id = 'legacy-draft-replacement'
        db = sqlite3.connect(self.path)
        try:
            db.execute('INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?)',
                       (draft_id, 'Legacy', 'legacy brief', 'legacy criteria',
                        json.dumps([base['id']]), None))
            db.commit()
        finally:
            db.close()

        before = self.store.snapshot()
        with self.assertRaisesRegex(ValueError, 'is a draft, not a confirmed requirement'):
            self.store.confirm(draft_id, 'ref')
        after = self.store.snapshot()
        self.assertEqual(after['requirements'], before['requirements'])
        self.assertEqual(after['drafts'], before['drafts'])
        self.assertEqual(after['pending_events'], before['pending_events'])

    def test_draft_confirmation_is_durable_exact_and_idempotent(self):
        draft = self.store.create_draft(' brief\n', ' exact criteria\n')
        draft['brief'] = 'external mutation'
        requirement = self.store.confirm(draft['id'], 'integration:message-1')
        self.assertEqual(requirement['brief'], ' brief\n')
        self.assertEqual(requirement['acceptance_criteria'], ' exact criteria\n')
        self.assertEqual(requirement['status'], 'active')
        reopened = Store(self.path)
        self.assertEqual(reopened.confirm(draft['id'], 'retry'), requirement)
        self.assertEqual(reopened.get_draft(draft['id'])['brief'], ' brief\n')
        self.assertEqual(reopened.get_requirement(requirement['id']), requirement)
        with self.assertRaises(ValueError):
            reopened.confirm(draft['id'], ' ')

    def test_optional_replacement_id_rejects_blank_values(self):
        with self.assertRaises(ValueError):
            self.store.create_draft('brief', 'criteria', '')
        with self.assertRaises(ValueError):
            self.store.create_draft('brief', 'criteria', '  \n')
        draft = self.store.create_draft('brief', 'criteria', None)
        self.assertIsNone(draft['replacement_of'])

    def test_cancel_is_durable_and_only_active_requirements_can_cancel(self):
        requirement = self.store.confirm(self.store.create_draft('brief', 'criteria')['id'], 'ref')
        requested = self.store.cancel([requirement['id']], 'user withdrew the request')
        self.assertEqual(requested[0]['target_status'], 'cancelled')
        result = self.advance_all(requested)
        self.assertEqual(result[0]['status'], 'cancelled')
        self.assertEqual(self.store.pending_events()[-1]['kind'], 'requirement.cancelled')
        self.assertEqual(self.store.pending_events()[-1]['payload']['reason'], 'user withdrew the request')
        with self.assertRaises(ValueError):
            self.store.cancel([requirement['id']], 'duplicate cancellation')

    def test_cancel_accepts_one_id_and_returns_one_requirement(self):
        requirement = self.store.confirm(self.store.create_draft('brief', 'criteria')['id'], 'ref')
        result = self.advance_all(self.store.cancel([requirement['id']], 'withdrawn'))
        self.assertEqual(result[0]['id'], requirement['id'])
        self.assertEqual(result[0]['status'], 'cancelled')

    def test_batch_cancel_and_replacement(self):
        first = self.store.confirm(self.store.create_draft('one', 'criteria')['id'], 'ref1')
        second = self.store.confirm(self.store.create_draft('two', 'criteria')['id'], 'ref2')
        cancelled = self.advance_all(
            self.store.cancel([first['id'], second['id']], 'withdrawn together'))
        self.assertEqual([r['status'] for r in cancelled], ['cancelled', 'cancelled'])
        a = self.store.confirm(self.store.create_draft('a', 'criteria')['id'], 'ref3')
        b = self.store.confirm(self.store.create_draft('b', 'criteria')['id'], 'ref4')
        draft = self.store.create_draft('combined', 'combined criteria', [a['id'], b['id']])
        combined = self.store.confirm(draft['id'], 'ref5')
        self.assertEqual(combined['status'], 'pending')
        self.advance(a)
        self.assertEqual(self.advance(b)['status'], 'invalidated')
        self.assertEqual(self.store.get_requirement(combined['id'])['status'], 'active')

    def test_confirmed_cancellation_is_atomic_and_idempotent(self):
        first = self.store.confirm(self.store.create_draft('one', 'criteria')['id'], 'ref1')
        second = self.store.confirm(self.store.create_draft('two', 'criteria')['id'], 'ref2')
        proposal = self.store.create_cancellation([first['id'], second['id']], 'withdrawn together')
        self.assertEqual([r['status'] for r in (self.store.get_requirement(first['id']),
                                                self.store.get_requirement(second['id']))],
                         ['active', 'active'])
        result = self.store.confirm_cancellation(proposal['id'], 'confirmation-1')
        self.assertEqual([r['target_status'] for r in result], ['cancelled', 'cancelled'])
        result = self.advance_all(result)
        self.assertEqual([r['status'] for r in result], ['cancelled', 'cancelled'])
        retry = self.store.confirm_cancellation(proposal['id'], 'confirmation-1')
        self.assertEqual([r['status'] for r in retry], ['cancelled', 'cancelled'])
        cancelled = [e for e in self.store.pending_events()
                     if e['kind'] == 'requirement.cancelled']
        self.assertEqual(len(cancelled), 2)

    def test_cancellation_proposal_requires_all_active_requirements(self):
        first = self.store.confirm(self.store.create_draft('one', 'criteria')['id'], 'ref1')
        second = self.store.confirm(self.store.create_draft('two', 'criteria')['id'], 'ref2')
        proposal = self.store.create_cancellation([first['id'], second['id']], None)
        self.store.cancel([second['id']], 'already withdrawn')
        with self.assertRaises(ValueError):
            self.store.confirm_cancellation(proposal['id'], 'confirmation-2')
        self.assertEqual(self.store.get_requirement(first['id'])['status'], 'active')
    def test_pause_and_resume_commands_are_durable_and_idempotent(self):
        requirement = self.store.confirm(
            self.store.create_draft('brief', 'criteria')['id'], 'ref')
        proposal = self.store.create_requirement_command(
            'pause', requirement['id'], 'user asked', 'Stop briefly')
        self.assertEqual(proposal, self.store.create_requirement_command(
            'pause', requirement['id'], 'user asked', 'Stop briefly'))
        self.assertEqual(self.store.get_requirement(requirement['id'])['status'], 'active')

        requested = self.store.confirm_requirement_command(proposal['id'], 'confirmation-1')
        self.assertEqual(requested['target_status'], 'paused')
        paused = self.advance(requested)
        self.assertEqual(paused['status'], 'paused')
        retry = self.store.confirm_requirement_command(proposal['id'], 'confirmation-1')
        self.assertEqual(retry['status'], 'paused')
        events = [e for e in self.store.pending_events()
                  if e['kind'] == 'requirement.paused']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['payload']['reason'], 'user asked')
        with self.assertRaises(ValueError):
            self.store.create_requirement_command(
                'pause', requirement['id'], 'again', 'Already paused')

        resume = self.store.create_requirement_command(
            'resume', requirement['id'], 'user ready', 'Continue the work')
        requested = self.store.confirm_requirement_command(resume['id'], 'confirmation-2')
        self.assertEqual(requested['target_status'], 'active')
        active = self.advance(requested)
        self.assertEqual(active['status'], 'active')
        self.assertEqual(len([e for e in self.store.pending_events()
                              if e['kind'] == 'requirement.resumed']), 1)
        with self.assertRaises(ValueError):
            self.store.create_requirement_command(
                'resume', requirement['id'], 'again', 'Already active')

    def test_paused_requirement_can_be_cancelled_or_replaced(self):
        requirement = self.store.confirm(
            self.store.create_draft('brief', 'criteria')['id'], 'ref')
        pause = self.store.create_requirement_command(
            'pause', requirement['id'], 'user asked', 'Stop briefly')
        self.advance(self.store.confirm_requirement_command(pause['id'], 'pause-confirmation'))
        self.assertEqual(self.store.get_requirement(requirement['id'])['status'], 'paused')

        replacement = self.store.confirm(self.store.create_draft(
            'changed', 'new criteria', requirement['id'])['id'], 'replacement-confirmation')
        self.assertEqual(replacement['status'], 'pending')
        self.advance(requirement)
        self.assertEqual(self.store.get_requirement(requirement['id'])['status'], 'invalidated')
        self.assertEqual(self.store.get_requirement(replacement['id'])['status'], 'active')

        paused = self.store.confirm(
            self.store.create_draft('cancel me', 'criteria')['id'], 'ref2')
        command = self.store.create_requirement_command(
            'pause', paused['id'], 'user asked', 'Stop briefly')
        self.advance(self.store.confirm_requirement_command(command['id'], 'pause-confirmation-2'))
        cancellation = self.store.create_cancellation([paused['id']], 'withdrawn while paused')
        cancelled = self.advance_all(
            self.store.confirm_cancellation(cancellation['id'], 'cancel-confirmation'))
        self.assertEqual(cancelled[0]['status'], 'cancelled')

    def test_pause_event_failure_rolls_back_status(self):
        requirement = self.store.confirm(
            self.store.create_draft('brief', 'criteria')['id'], 'ref')
        proposal = self.store.create_requirement_command(
            'pause', requirement['id'], 'user asked', 'Stop briefly')
        db = sqlite3.connect(self.path)
        try:
            db.execute("CREATE TRIGGER fail_pause BEFORE INSERT ON events "
                       "BEGIN SELECT RAISE(ABORT, 'disk simulation'); END")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.confirm_requirement_command(proposal['id'], 'confirmation-1')
        self.assertEqual(self.store.get_requirement(requirement['id'])['status'], 'active')
