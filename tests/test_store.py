from concurrent.futures import ThreadPoolExecutor
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
                         ['requirement.confirmed'])
        self.store.confirm(draft['id'], 'retry')
        self.assertEqual(len(self.store.pending_events()), 1)
        with self.assertRaises(ValueError):
            self.store.accept(requirement['id'], ' ')
        self.store.reject(requirement['id'], 'not yet')
        self.assertEqual(self.store.get_requirement(requirement['id'])['status'], 'active')
        self.store.accept(requirement['id'], 'test report URL')
        self.assertEqual(self.store.get_requirement(requirement['id'])['status'], 'accepted')
        self.assertEqual(self.store.pending_events()[-1]['payload']['evidence'], 'test report URL')
        with self.assertRaises(ValueError):
            self.store.accept(requirement['id'], 'again')
        with self.assertRaises(ValueError):
            self.store.reject(requirement['id'], 'late')
        self.assertEqual([e['kind'] for e in self.store.pending_events()],
                         ['requirement.confirmed', 'requirement.rejected', 'requirement.accepted'])

    def test_replacement_emits_invalidation_and_blocks_old_acceptance(self):
        old = self.store.confirm(self.store.create_draft('old', 'criteria')['id'], 'ref')
        self.store.confirm(self.store.create_draft('new', 'criteria', old['id'])['id'], 'ref2')
        self.assertEqual([e['kind'] for e in self.store.pending_events()],
                         ['requirement.confirmed', 'requirement.invalidated', 'requirement.confirmed'])
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
        self.assertEqual(self.store.get_requirement(original['id'])['status'], 'invalidated')
        with self.assertRaises(ValueError):
            self.store.confirm(stale['id'], 'ref3')
        self.assertEqual(self.store.confirm(first['id'], 'retry'), replacement)
        missing = self.store.create_draft('missing', 'criteria', 'nonexistent')
        with self.assertRaises(KeyError):
            self.store.confirm(missing['id'], 'ref4')

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
        result = self.store.cancel([requirement['id']], 'user withdrew the request')
        self.assertEqual(result[0]['status'], 'cancelled')
        self.assertEqual(self.store.pending_events()[-1]['kind'], 'requirement.cancelled')
        self.assertEqual(self.store.pending_events()[-1]['payload']['reason'], 'user withdrew the request')
        with self.assertRaises(ValueError):
            self.store.cancel([requirement['id']], 'duplicate cancellation')

    def test_cancel_accepts_one_id_and_returns_one_requirement(self):
        requirement = self.store.confirm(self.store.create_draft('brief', 'criteria')['id'], 'ref')
        result = self.store.cancel([requirement['id']], 'withdrawn')
        self.assertEqual(result[0]['id'], requirement['id'])
        self.assertEqual(result[0]['status'], 'cancelled')

    def test_batch_cancel_and_replacement(self):
        first = self.store.confirm(self.store.create_draft('one', 'criteria')['id'], 'ref1')
        second = self.store.confirm(self.store.create_draft('two', 'criteria')['id'], 'ref2')
        cancelled = self.store.cancel([first['id'], second['id']], 'withdrawn together')
        self.assertEqual([r['status'] for r in cancelled], ['cancelled', 'cancelled'])
        a = self.store.confirm(self.store.create_draft('a', 'criteria')['id'], 'ref3')
        b = self.store.confirm(self.store.create_draft('b', 'criteria')['id'], 'ref4')
        draft = self.store.create_draft('combined', 'combined criteria', [a['id'], b['id']])
        combined = self.store.confirm(draft['id'], 'ref5')
        self.assertEqual(combined['status'], 'active')
        self.assertEqual(self.store.get_requirement(a['id'])['status'], 'invalidated')
        self.assertEqual(self.store.get_requirement(b['id'])['status'], 'invalidated')
