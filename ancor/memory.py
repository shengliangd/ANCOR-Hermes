"""Markdown-first cross-backend Memory layer for ANCOR.

Layout under ``~/.ancor/memory/``:
  - ``principles.md``       Human-owned governance, taste & discipline (read-only for all agents)
  - ``environment/*.md``    Objective host/cluster facts (scoped, dated via ``verified_at``)
  - ``playbooks/*.md``      Procedural runbooks & domain conventions (lazy-loaded via INDEX.md)
  - ``episodes/*.md``       Dated troubleshooting summaries (hidden from ``judge``)
  - ``INDEX.md``            Auto-generated one-line table of contents (never hand-edited)

Concurrency & Integrity:
  - All writes take an exclusive ``fcntl.flock`` on ``.memory.lock`` and support optimistic
    concurrency via ``expected_hash`` (SHA-256 of existing file content).
  - Derived SQLite FTS5 index (``.index.db``) is rebuilt lazily from Markdown files and can be
    deleted at any time without data loss.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
import fcntl
import hashlib
import os
from pathlib import Path
import re
import socket
import sqlite3
from typing import Any

ALLOWED_CATEGORIES = frozenset({'principles', 'environment', 'playbooks', 'episodes'})
WRITABLE_AGENT_CATEGORIES = frozenset({'environment', 'playbooks', 'episodes'})
DEFAULT_STALE_DAYS = 30
DEFAULT_PROMPT_CHAR_BUDGET = 6000

_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n?', re.DOTALL)
_SAFE_SEGMENT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse lightweight flat YAML frontmatter without external dependencies."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or ':' not in stripped:
            continue
        key, _, val = stripped.partition(':')
        k = key.strip().lower()
        v = val.strip()
        if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
            v = v[1:-1]
        if k:
            meta[k] = v
    return meta, text[match.end():]


def _render_frontmatter(meta: dict[str, str], body: str) -> str:
    ordered = ['scope', 'owner', 'verified_at', 'summary']
    lines = ['---']
    for key in ordered:
        if key in meta and meta[key]:
            val = str(meta[key]).replace('\n', ' ').strip()
            if any(ch in val for ch in (':', '#', '"', "'")):
                val = '"' + val.replace('"', '\\"') + '"'
            lines.append(f'{key}: {val}')
    for key in sorted(meta):
        if key not in ordered and meta[key]:
            val = str(meta[key]).replace('\n', ' ').strip()
            lines.append(f'{key}: {val}')
    lines.append('---')
    clean_body = body.lstrip('\n')
    return '\n'.join(lines) + '\n\n' + clean_body + ('' if clean_body.endswith('\n') else '\n')


def _is_stale(verified_at: str | None, *, today: date, stale_days: int) -> bool:
    if not verified_at:
        return False
    try:
        parsed = date.fromisoformat(verified_at[:10])
    except ValueError:
        return False
    return (today - parsed).days > stale_days


def _first_summary_line(body: str) -> str:
    for line in body.splitlines():
        s = line.strip().lstrip('#').strip()
        if s:
            return s[:140]
    return ''


class MemoryStore:
    """Concurrency-safe, role-aware Markdown memory store with derived FTS5 search."""

    def __init__(self, root: str | Path, *, stale_days: int = DEFAULT_STALE_DAYS, today_fn=None):
        self.root = Path(root).expanduser().resolve()
        self.stale_days = max(1, int(stale_days))
        self._today_fn = today_fn or date.today
        self._lock_path = self.root / '.memory.lock'
        self._index_db_path = self.root / '.index.db'

    def ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for sub in ('environment', 'playbooks', 'episodes'):
            (self.root / sub).mkdir(exist_ok=True)

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open('a', encoding='utf-8') as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _normalize_rel_path(self, rel_path: str) -> tuple[str, str]:
        """Validate relative memory path and return (category, normalized_rel_path)."""
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise ValueError('memory path must be a non-empty string')
        cleaned = rel_path.strip().replace('\\', '/').lstrip('/')
        if '..' in cleaned.split('/'):
            raise ValueError('memory path must not contain ".."')
        if cleaned == 'principles.md':
            return 'principles', 'principles.md'
        if cleaned == 'INDEX.md':
            raise ValueError('INDEX.md is auto-generated and cannot be written directly')
        parts = cleaned.split('/')
        if len(parts) != 2 or parts[0] not in WRITABLE_AGENT_CATEGORIES:
            raise ValueError(
                'memory path must be "principles.md" or "<environment|playbooks|episodes>/<name>.md"'
            )
        cat, filename = parts
        if not filename.endswith('.md') or not _SAFE_SEGMENT_RE.match(filename):
            raise ValueError(f'invalid memory filename {filename!r}; must match [A-Za-z0-9._-]+.md')
        return cat, f'{cat}/{filename}'

    def _iter_docs(self, *, role: str = 'human') -> list[tuple[str, str, Path]]:
        if not self.root.exists():
            return []
        docs: list[tuple[str, str, Path]] = []
        p_file = self.root / 'principles.md'
        if p_file.is_file():
            docs.append(('principles', 'principles.md', p_file))
        for cat in ('environment', 'playbooks', 'episodes'):
            if role == 'judge' and cat == 'episodes':
                continue
            subdir = self.root / cat
            if not subdir.is_dir():
                continue
            for path in sorted(subdir.glob('*.md')):
                if path.is_file():
                    docs.append((cat, f'{cat}/{path.name}', path))
        return docs

    def _doc_record(self, cat: str, rel: str, path: Path) -> dict[str, Any]:
        raw = path.read_text(encoding='utf-8')
        meta, body = _parse_frontmatter(raw)
        today = self._today_fn()
        verified_at = meta.get('verified_at') or None
        stale = _is_stale(verified_at, today=today, stale_days=self.stale_days)
        return {
            'path': rel,
            'category': cat,
            'scope': meta.get('scope') or ('global' if cat != 'episodes' else 'episode'),
            'owner': meta.get('owner') or ('human' if cat == 'principles' else 'agent'),
            'verified_at': verified_at,
            'stale': stale,
            'summary': meta.get('summary') or _first_summary_line(body),
            'hash': _sha256_text(raw),
            'size_bytes': len(raw.encode('utf-8')),
        }

    def list_index(self, *, role: str = 'human', scope: str | None = None, category: str | None = None) -> list[dict]:
        items: list[dict] = []
        scope_filter = scope.strip().lower() if isinstance(scope, str) and scope.strip() else None
        cat_filter = category.strip().lower() if isinstance(category, str) and category.strip() else None
        for cat, rel, path in self._iter_docs(role=role):
            if cat_filter and cat != cat_filter:
                continue
            rec = self._doc_record(cat, rel, path)
            if scope_filter and scope_filter not in rec['scope'].lower():
                continue
            items.append(rec)
        return items

    def render_index_markdown(self, *, role: str = 'human') -> str:
        items = self.list_index(role=role)
        lines = ['# ANCOR Memory Index (auto-generated)', '']
        by_cat: dict[str, list[dict]] = {}
        for item in items:
            if item['category'] == 'principles':
                continue
            by_cat.setdefault(item['category'], []).append(item)
        for cat in ('environment', 'playbooks', 'episodes'):
            cat_items = by_cat.get(cat)
            if not cat_items:
                continue
            lines.append(f'## {cat}')
            for item in cat_items:
                flags = []
                if item['scope']:
                    flags.append(f"scope={item['scope']}")
                if item['verified_at']:
                    flags.append(f"verified={item['verified_at']}")
                if item['stale']:
                    flags.append('stale?')
                meta_str = f" ({', '.join(flags)})" if flags else ''
                lines.append(f"- `{item['path']}`{meta_str}: {item['summary']}")
            lines.append('')
        return '\n'.join(lines).rstrip() + '\n'

    def _regenerate_index_file(self) -> None:
        self.ensure_layout()
        content = self.render_index_markdown(role='human')
        target = self.root / 'INDEX.md'
        tmp = target.with_suffix('.tmp')
        tmp.write_text(content, encoding='utf-8')
        tmp.replace(target)

    def read(self, rel_path: str, *, role: str = 'human') -> dict[str, Any]:
        cat, norm = self._normalize_rel_path(rel_path)
        if role == 'judge' and cat == 'episodes':
            raise PermissionError('judge role is not permitted to read episodes memory')
        target = self.root / norm
        if not target.is_file():
            raise KeyError(norm)
        raw = target.read_text(encoding='utf-8')
        meta, body = _parse_frontmatter(raw)
        rec = self._doc_record(cat, norm, target)
        return {**rec, 'content': raw, 'body': body, 'meta': meta}

    def write(
        self,
        rel_path: str,
        content: str,
        *,
        role: str = 'human',
        expected_hash: str | None = None,
        scope: str | None = None,
        summary: str | None = None,
        verified_at: str | None = None,
        owner: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(content, str) or not content.strip():
            raise ValueError('memory content must be a non-empty string')
        cat, norm = self._normalize_rel_path(rel_path)
        is_agent = role in {'root', 'goal', 'judge', 'interactor'}

        if role == 'judge':
            raise PermissionError('judge role has read-only access to ANCOR memory')
        if is_agent and cat == 'principles':
            raise PermissionError('principles.md is human-owned and read-only for agents')

        with self._locked():
            self.ensure_layout()
            target = self.root / norm
            existing_text = target.read_text(encoding='utf-8') if target.is_file() else None
            existing_hash = _sha256_text(existing_text) if existing_text is not None else None

            if existing_text is not None and is_agent:
                existing_meta, _ = _parse_frontmatter(existing_text)
                if existing_meta.get('owner', '').lower() == 'human':
                    raise PermissionError(f'{norm} has owner=human and cannot be modified by role {role!r}')

            if expected_hash is not None:
                if existing_hash != expected_hash:
                    raise RuntimeError(
                        f'concurrent write conflict on {norm}: expected_hash={expected_hash}, current={existing_hash}'
                    )

            if cat == 'principles':
                final_text = content if content.endswith('\n') else content + '\n'
            else:
                parsed_meta, body = _parse_frontmatter(content)
                merged_meta = dict(parsed_meta)
                if scope:
                    merged_meta['scope'] = scope
                elif 'scope' not in merged_meta:
                    merged_meta['scope'] = 'global'
                if owner and not is_agent:
                    merged_meta['owner'] = owner
                elif 'owner' not in merged_meta:
                    merged_meta['owner'] = 'agent' if is_agent else 'human'
                merged_meta['verified_at'] = verified_at or merged_meta.get('verified_at') or self._today_fn().isoformat()
                if summary:
                    merged_meta['summary'] = summary
                elif 'summary' not in merged_meta:
                    merged_meta['summary'] = _first_summary_line(body)
                final_text = _render_frontmatter(merged_meta, body)

            tmp = target.with_suffix('.md.tmp')
            tmp.write_text(final_text, encoding='utf-8')
            tmp.replace(target)
            self._regenerate_index_file()
            return self._doc_record(cat, norm, target)

    def _sync_fts_index(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._index_db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS memory_files (
                path TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                scope TEXT NOT NULL,
                verified_at TEXT,
                summary TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                path UNINDEXED,
                category UNINDEXED,
                scope,
                summary,
                body
            );
        ''')
        existing = {
            row['path']: (row['mtime_ns'], row['size_bytes'])
            for row in conn.execute('SELECT path, mtime_ns, size_bytes FROM memory_files').fetchall()
        }
        current_docs = self._iter_docs(role='human')
        current_paths = {rel for _, rel, _ in current_docs}

        changed = False
        for gone in set(existing) - current_paths:
            conn.execute('DELETE FROM memory_files WHERE path=?', (gone,))
            conn.execute('DELETE FROM memory_fts WHERE path=?', (gone,))
            changed = True

        for cat, rel, path in current_docs:
            st = path.stat()
            sig = (st.st_mtime_ns, st.st_size)
            if existing.get(rel) == sig:
                continue
            raw = path.read_text(encoding='utf-8')
            meta, body = _parse_frontmatter(raw)
            scope = meta.get('scope') or 'global'
            verified_at = meta.get('verified_at')
            summary = meta.get('summary') or _first_summary_line(body)
            conn.execute(
                'INSERT OR REPLACE INTO memory_files(path, category, scope, verified_at, summary, mtime_ns, size_bytes) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (rel, cat, scope, verified_at, summary, st.st_mtime_ns, st.st_size),
            )
            conn.execute('DELETE FROM memory_fts WHERE path=?', (rel,))
            conn.execute(
                'INSERT INTO memory_fts(path, category, scope, summary, body) VALUES (?, ?, ?, ?, ?)',
                (rel, cat, scope, summary, body),
            )
            changed = True

        if changed:
            conn.commit()
        return conn

    def search(self, query: str, *, role: str = 'human', limit: int = 10) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError('search query must be non-empty')
        if not self.root.exists():
            return []
        limit = max(1, min(50, int(limit)))
        # Quote tokens for safe FTS5 matching even with punctuation/paths.
        tokens = [t for t in re.findall(r'[A-Za-z0-9_\u4e00-\u9fff.-]+', query) if t]
        if not tokens:
            return []
        fts_query = ' OR '.join('"' + t.replace('"', '""') + '"' for t in tokens)

        with self._locked():
            conn = self._sync_fts_index()
            try:
                rows = conn.execute(
                    "SELECT f.path, f.category, f.scope, f.verified_at, f.summary, "
                    "snippet(memory_fts, 4, '[', ']', '...', 24) AS snippet "
                    'FROM memory_fts JOIN memory_files f ON f.path = memory_fts.path '
                    'WHERE memory_fts MATCH ? ORDER BY bm25(memory_fts) LIMIT ?',
                    (fts_query, limit * 2),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            finally:
                conn.close()

        today = self._today_fn()
        results: list[dict[str, Any]] = []
        for row in rows:
            if role == 'judge' and row['category'] == 'episodes':
                continue
            results.append({
                'path': row['path'],
                'category': row['category'],
                'scope': row['scope'],
                'verified_at': row['verified_at'],
                'stale': _is_stale(row['verified_at'], today=today, stale_days=self.stale_days),
                'summary': row['summary'],
                'snippet': row['snippet'],
            })
            if len(results) >= limit:
                break

        # Substring fallback for CJK phrases that FTS5 unicode61 doesn't bigram-tokenize.
        if not results:
            needle = query.strip().lower()
            for cat, rel, path in self._iter_docs(role=role):
                raw = path.read_text(encoding='utf-8')
                if needle in raw.lower():
                    rec = self._doc_record(cat, rel, path)
                    idx = raw.lower().find(needle)
                    start = max(0, idx - 60)
                    end = min(len(raw), idx + len(needle) + 100)
                    results.append({
                        'path': rec['path'],
                        'category': rec['category'],
                        'scope': rec['scope'],
                        'verified_at': rec['verified_at'],
                        'stale': rec['stale'],
                        'summary': rec['summary'],
                        'snippet': raw[start:end].replace('\n', ' '),
                    })
                    if len(results) >= limit:
                        break
        return results

    def build_prompt_context(
        self,
        *,
        role: str,
        hostname: str | None = None,
        max_chars: int = DEFAULT_PROMPT_CHAR_BUDGET,
    ) -> str:
        """Assemble a deterministic, KV-cache-friendly pinned memory block for a role.

        Fixed order:
          1. ``principles.md`` (human governance, read-only)
          2. ``environment/*.md`` matching ``global`` or ``host:<hostname>``
          3. Auto-generated ``INDEX.md`` (pointer table for lazy-loading playbooks/episodes)
        """
        if not self.root.exists():
            return ''
        host = (hostname or socket.gethostname()).strip().lower()
        sections: list[str] = []

        p_file = self.root / 'principles.md'
        if p_file.is_file():
            text = p_file.read_text(encoding='utf-8').strip()
            if text:
                sections.append('## ANCOR Principles (read-only)\n' + text)

        env_dir = self.root / 'environment'
        if env_dir.is_dir():
            env_blocks: list[str] = []
            for path in sorted(env_dir.glob('*.md')):
                raw = path.read_text(encoding='utf-8')
                meta, body = _parse_frontmatter(raw)
                scope = (meta.get('scope') or 'global').strip().lower()
                if scope == 'global' or scope == f'host:{host}' or host in scope:
                    stale_tag = ' [stale?]' if _is_stale(
                        meta.get('verified_at'), today=self._today_fn(), stale_days=self.stale_days
                    ) else ''
                    env_blocks.append(f"### environment/{path.name}{stale_tag}\n{body.strip()}")
            if env_blocks:
                sections.append('## ANCOR Environment Facts\n' + '\n\n'.join(env_blocks))

        index_md = self.render_index_markdown(role=role).strip()
        if index_md and len(index_md.splitlines()) > 1:
            sections.append(
                '## ANCOR Memory Index (lazy-load via ancor_memory_read / ancor_memory_search)\n' + index_md
            )

        if not sections:
            return ''
        combined = '\n\n'.join(sections)
        if len(combined) > max_chars:
            combined = combined[: max(0, max_chars - 32)].rstrip() + '\n... [memory truncated]\n'
        return combined
