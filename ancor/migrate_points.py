"""Copy a quiescent legacy store into the versioned schema without guessing intent.

Usage: python -m ancor.migrate_points SOURCE --output NEW_DATABASE
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


def _backup(source, target):
    original = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
    destination = sqlite3.connect(target)
    try:
        original.backup(destination)
    finally:
        destination.close()
        original.close()


def migrate_copy(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_file():
        raise ValueError('source database does not exist')
    suffixes = ['', '.notifications.db', '.confirmations.db', '.host.json', '.drafts']
    if any(Path(str(output) + suffix).exists() for suffix in suffixes):
        raise ValueError('output and its sidecars must not exist')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ancor-migration-', dir=output.parent) as directory:
        staged = Path(directory) / 'requirements.db'
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
    return {'output': str(output), 'requirements': len(snapshot['requirements']),
            'legacy_requirements': sum(r['content_mode'] == 'legacy' for r in snapshot['requirements']),
            'notes_with_unrecoverable_history': missing_history,
            'source_modified': False,
            'next_step': 'Legacy briefs remain authoritative. Convert each through an explicitly confirmed change.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(migrate_copy(args.source, args.output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
