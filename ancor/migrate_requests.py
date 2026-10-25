"""Copy a quiescent legacy store into the versioned schema without guessing intent.

Usage: python -m ancor.migrate_requests SOURCE --output NEW_DATABASE
The source is read-only. Stop its writers before taking the multi-file snapshot.
The output must not exist; publish/switch the configured store separately.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .store import Store
from .legacy import migrate_schema, retire_pending, migrate_native_copy, translate_json


def _backup(source, target):
    original = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
    destination = sqlite3.connect(target)
    try:
        original.backup(destination)
    finally:
        destination.close()
        original.close()


def migrate_copy(source, output, *, native_source=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_file():
        raise ValueError('source database does not exist')
    suffixes = ['', '.notifications.db', '.confirmations.db', '.host.json', '.drafts', '.kanban.db']
    if any(Path(str(output) + suffix).exists() for suffix in suffixes):
        raise ValueError('output and its sidecars must not exist')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ancor-migration-', dir=output.parent) as directory:
        staged = Path(directory) / 'requests.db'
        _backup(source, staged)
        for suffix in ('.notifications.db', '.confirmations.db'):
            sibling = Path(str(source) + suffix)
            if sibling.is_file():
                _backup(sibling, Path(str(staged) + suffix))
        for suffix in ('.host.json', '.drafts'):
            sibling = Path(str(source) + suffix)
            if sibling.is_dir():
                shutil.copytree(sibling, Path(str(staged) + suffix))
            elif sibling.is_file():
                shutil.copy2(sibling, Path(str(staged) + suffix))
        native_links = 0
        if native_source is not None:
            native_copy = Path(str(staged) + '.kanban.db')
            _backup(Path(native_source), native_copy)
            native_links = migrate_native_copy(native_copy)
        host_state = Path(str(staged) + '.host.json')
        if host_state.exists():
            host_state.write_text(json.dumps(translate_json(json.loads(host_state.read_text())), ensure_ascii=False))
        migrate_schema(staged)
        retired = retire_pending(staged)
        for suffix in ('.notifications.db', '.confirmations.db'):
            sidecar = Path(str(staged) + suffix)
            if sidecar.exists():
                migrate_schema(sidecar)
                retired += retire_pending(sidecar)
        store = Store(staged)
        snapshot = store.snapshot()
        with store._transaction() as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('migrated database failed integrity check')
            missing_history = db.execute('SELECT COUNT(*) FROM note_versions WHERE history_missing=1').fetchone()[0]
        # Destination is an offline copy. Publish the main file last, never replace an existing database.
        for suffix in [*suffixes[1:], '']:
            item = Path(str(staged) + suffix)
            if item.exists():
                target = Path(str(output) + suffix)
                if target.exists():
                    raise FileExistsError(target)
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    os.link(item, target)  # atomic no-clobber publication on the same filesystem
    return {'output': str(output), 'requests': len(snapshot['requests']),
            'legacy_requests': sum(r['content_mode'] == 'legacy' for r in snapshot['requests']),
            'notes_with_unrecoverable_history': missing_history,
            'source_modified': False, 'retired_pending_proposals': retired, 'translated_native_links': native_links,
            'next_step': 'Legacy briefs remain authoritative. Convert each through an explicitly confirmed change.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--native-source', type=Path, help='Optional quiescent native Kanban DB; migrate a copy beside the new request DB')
    args = parser.parse_args()
    print(json.dumps(migrate_copy(args.source, args.output, native_source=args.native_source), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
