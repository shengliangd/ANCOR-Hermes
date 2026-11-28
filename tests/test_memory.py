"""Tests for ANCOR Markdown-first MemoryStore, role permissions, staleness, concurrency, MCP, and CLI."""
from __future__ import annotations

from datetime import date
import json

import pytest

from ancor.cli.main import main as cli_main
from ancor.config import load_config
from ancor.core_api import AncorCore
from ancor.mcp_server import build_role_tools, execute_role_tool
from ancor.memory import MemoryStore


def test_memory_taxonomy_role_permissions_and_staleness(tmp_path):
    today = [date(2026, 11, 29)]
    mem = MemoryStore(tmp_path / 'memory', stale_days=30, today_fn=lambda: today[0])

    # Human writes principles.md; agents cannot overwrite it
    mem.write('principles.md', 'Focus on real capability gains, not report counts.', role='human')
    with pytest.raises(PermissionError):
        mem.write('principles.md', 'Agent overwrite attempt', role='goal')
    with pytest.raises(PermissionError):
        mem.write('principles.md', 'Root overwrite attempt', role='root')

    # Agent writes host-scoped environment fact + playbook + episode
    env_rec = mem.write(
        'environment/host-dsw-768140.md',
        'Avoid running scripts from /tmp because /tmp/inspect.py shadows stdlib.',
        role='goal',
        scope='host:dsw-768140',
        summary='dsw-768140 /tmp/inspect.py hazard',
        verified_at='2026-10-01',
    )
    assert env_rec['stale'] is True  # 59 days > 30

    mem.write(
        'environment/host-other.md',
        'Other host details.',
        role='goal',
        scope='host:other-box',
        summary='other host',
        verified_at='2026-11-28',
    )
    mem.write(
        'playbooks/external-jobs.md',
        'Submit training jobs via detached launcher and call ancor_goal_wait.',
        role='root',
        scope='global',
        summary='How to run long-running external training jobs',
        verified_at='2026-11-28',
    )
    mem.write(
        'episodes/2026-11-28-ossfs-stall.md',
        'Single-stream ossfs reads stalled at 10 MB/s; 32 threads reached 250 MB/s.',
        role='goal',
        scope='host:dsw-768140',
        summary='ossfs throughput debugging',
        verified_at='2026-11-28',
    )

    # INDEX.md is auto-generated and marks stale entry
    index_text = (tmp_path / 'memory' / 'INDEX.md').read_text(encoding='utf-8')
    assert 'environment/host-dsw-768140.md' in index_text
    assert 'stale?' in index_text
    assert 'playbooks/external-jobs.md' in index_text

    # Judge cannot write, and cannot see episodes in index, read, search, or prompt
    with pytest.raises(PermissionError):
        mem.write('environment/topology.md', 'judge write', role='judge')
    with pytest.raises(PermissionError):
        mem.read('episodes/2026-11-28-ossfs-stall.md', role='judge')
    judge_paths = {item['path'] for item in mem.list_index(role='judge')}
    assert 'episodes/2026-11-28-ossfs-stall.md' not in judge_paths
    assert 'playbooks/external-jobs.md' in judge_paths
    assert mem.search('ossfs', role='judge') == []
    assert len(mem.search('ossfs', role='goal')) == 1

    # Prompt context is deterministic, includes principles + matching host env + INDEX,
    # excludes non-matching host env body and excludes playbook full body (lazy-load).
    prompt = mem.build_prompt_context(role='goal', hostname='dsw-768140')
    assert 'Focus on real capability gains' in prompt
    assert '/tmp/inspect.py' in prompt
    assert 'Other host details.' not in prompt
    assert 'playbooks/external-jobs.md' in prompt
    assert 'Submit training jobs via detached launcher' not in prompt


def test_memory_optimistic_concurrency_and_fts_rebuild(tmp_path):
    mem = MemoryStore(tmp_path / 'memory')
    first = mem.write(
        'environment/topology.md',
        '控制面在阿里云 DSW，生产机是 ds-dev。',
        role='human',
        owner='agent',
        scope='global',
        summary='跨机拓扑',
    )
    # Stale expected_hash raises RuntimeError (conflict)
    with pytest.raises(RuntimeError, match='concurrent write conflict'):
        mem.write('environment/topology.md', 'new body', role='goal', expected_hash='wrong-hash')

    # Matching expected_hash succeeds
    second = mem.write(
        'environment/topology.md',
        '控制面在阿里云 DSW，生产机是 ds-dev，NAS 挂载于 /mnt/nas。',
        role='goal',
        expected_hash=first['hash'],
    )
    assert second['hash'] != first['hash']

    # Human-owned file cannot be modified by agent
    mem.write('playbooks/locked.md', 'Do not edit.', role='human', owner='human')
    with pytest.raises(PermissionError, match='owner=human'):
        mem.write('playbooks/locked.md', 'Agent edit', role='goal')

    # CJK search works and survives deleting derived .index.db
    hits = mem.search('阿里云', role='goal')
    assert len(hits) == 1 and hits[0]['path'] == 'environment/topology.md'
    index_db = tmp_path / 'memory' / '.index.db'
    if index_db.exists():
        index_db.unlink()
    hits_after_rebuild = mem.search('ds-dev', role='goal')
    assert len(hits_after_rebuild) == 1 and hits_after_rebuild[0]['path'] == 'environment/topology.md'


def test_memory_mcp_and_cli_integration(tmp_path, capsys):
    db = str(tmp_path / 'requests.db')
    cfg = load_config(store_path_override=db)
    core = AncorCore(cfg)

    # CLI write + index + read + search + prompt
    rc = cli_main([
        '--db', db, 'core', 'memory', 'write', 'environment/topology.md',
        '--content', 'DSW and ds-dev do not share /root.',
        '--scope', 'global',
        '--summary', 'DSW vs ds-dev topology',
    ])
    assert rc == 0
    written = json.loads(capsys.readouterr().out)
    assert written['path'] == 'environment/topology.md'

    rc = cli_main(['--db', db, 'core', 'memory', 'search', 'ds-dev'])
    assert rc == 0
    found = json.loads(capsys.readouterr().out)
    assert found['count'] == 1

    # MCP root can write, judge cannot write
    root_defs, root_handlers = build_role_tools(core, role='root')
    res = json.loads(execute_role_tool(root_defs, root_handlers, 'ancor_memory_write', {
        'path': 'playbooks/eval.md',
        'content': 'Always verify outputs on full dev set.',
        'summary': 'Evaluation playbook',
    }))
    assert res['path'] == 'playbooks/eval.md'

    judge_defs, judge_handlers = build_role_tools(core, role='judge')
    denied = json.loads(execute_role_tool(judge_defs, judge_handlers, 'ancor_memory_write', {
        'path': 'playbooks/eval.md',
        'content': 'tamper',
    }))
    assert denied['code'] == 'forbidden'
    read_res = json.loads(execute_role_tool(judge_defs, judge_handlers, 'ancor_memory_read', {
        'path': 'playbooks/eval.md',
    }))
    assert 'Always verify outputs' in read_res['body']
