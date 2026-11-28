"""Tests for AncorCore machine API, `ancor core` CLI, per-role MCP server, multi-notifier, serve, and install."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient

from ancor.cli.main import main as cli_main
from ancor.config import NotifierConfig, load_config
from ancor.core_api import AncorCore
from ancor.installer import check_target, install_target, list_targets, uninstall_target
from ancor.mcp_server import build_role_tools, execute_role_tool
from ancor.notifiers import MultiNotificationDispatcher, build_notifiers
from ancor.serve_app import create_serve_app


def _run_cli(argv: list[str], capsys) -> tuple[int, dict]:
    rc = cli_main(argv)
    out = capsys.readouterr().out.strip()
    return rc, json.loads(out)


def test_ancor_core_cli_confirmation_free_lifecycle(tmp_path, capsys):
    db = str(tmp_path / 'requests.db')

    # 1. Create request directly (no clarify confirmation needed)
    rc, created = _run_cli(
        [
            '--db', db, 'core', 'request', 'create',
            '--title', 'Build pipeline',
            '--outcome', 'Clean CSVs and produce report',
            '--constraint', 'Do not modify raw files',
        ],
        capsys,
    )
    assert rc == 0
    req_id = created['request']['id']
    assert created['request']['status'] == 'active'

    # 2. Overview & request get
    rc, ov = _run_cli(['--db', db, 'core', 'overview'], capsys)
    assert rc == 0
    assert ov['requests']['count'] == 1

    # 3. Spawn goal & report result via CLI
    goal_spec = json.dumps({
        'request_ref': {'id': req_id, 'generation': 1},
        'objective': 'Clean data',
        'done_criteria': 'CSV cleaned',
    })
    rc, goal = _run_cli(['--db', db, 'core', 'goal', 'spawn', '--json', goal_spec], capsys)
    assert rc == 0
    gid = goal['goal_id']

    report_payload = json.dumps({
        'request_generation': 1,
        'status': 'completed',
        'summary': 'Cleaned all files',
        'artifacts': [{'path': '/tmp/out.csv', 'desc': 'cleaned csv'}],
    })
    rc, rep = _run_cli(['--db', db, 'core', 'goal', 'report', gid, '--json', report_payload], capsys)
    assert rc == 0
    assert rep['status'] == 'completed'

    # 4. Question create & direct answer
    q_payload = json.dumps({
        'request_id': req_id,
        'id': 'q-fmt',
        'body': 'Which timestamp format?',
        'reason': 'Multiple formats found',
    })
    rc, q = _run_cli(['--db', db, 'core', 'question', 'create', '--json', q_payload], capsys)
    assert rc == 0
    assert q['state'] == 'open'

    rc, ans = _run_cli(['--db', db, 'core', 'question', 'answer', 'q-fmt', '--body', 'Use ISO-8601'], capsys)
    assert rc == 0
    assert ans['request']['id'] == req_id

    # 5. Pause & resume
    rc, paused = _run_cli(['--db', db, 'core', 'request', 'pause', req_id, '--reason', 'maintenance'], capsys)
    assert rc == 0
    assert paused['request']['target_status'] == 'paused'


def test_mcp_server_role_isolation_and_execution(tmp_path):
    cfg = load_config(store_path_override=tmp_path / 'requests.db')
    core = AncorCore(cfg)
    req_id = core.request_create(title='MCP Test', fields={'outcome': ['verify mcp']})['request']['id']

    root_defs, root_handlers = build_role_tools(core, role='root')
    assert 'ancor_goal_spawn' in root_defs
    assert 'ancor_judge_verdict' not in root_defs

    spawn_raw = execute_role_tool(root_defs, root_handlers, 'ancor_goal_spawn', {
        'request_ref': {'id': req_id, 'generation': 1},
        'objective': 'Work in MCP',
        'done_criteria': 'Reported',
    })
    spawned = json.loads(spawn_raw)
    gid = spawned['goal_id']

    # Goal role sees only goal tools, cannot call root-only tools
    goal_defs, goal_handlers = build_role_tools(core, role='goal', goal_id=gid)
    assert 'ancor_goal_self' in goal_defs
    assert 'ancor_event_ack' not in goal_defs
    forbidden = json.loads(execute_role_tool(goal_defs, goal_handlers, 'ancor_event_ack', {'event_ids': [1]}))
    assert forbidden['code'] == 'forbidden'

    self_info = json.loads(execute_role_tool(goal_defs, goal_handlers, 'ancor_goal_self', {}))
    assert self_info['goal']['goal_id'] == gid

    # Judge role sees only judge tools + read-only memory tools (no memory_write)
    judge_defs, _ = build_role_tools(core, role='judge')
    assert set(judge_defs.keys()) == {
        'ancor_request_read',
        'ancor_judge_verdict',
        'ancor_memory_index',
        'ancor_memory_read',
        'ancor_memory_search',
    }


def test_multi_notifier_fan_out_and_isolation(tmp_path):
    cfg = load_config(store_path_override=tmp_path / 'requests.db')
    core = AncorCore(cfg)
    req_id = core.request_create(title='Notify Test', fields={'outcome': ['notify']})['request']['id']

    sink1_out = tmp_path / 'sink1.jsonl'
    sink2_out = tmp_path / 'sink2.txt'

    sinks = build_notifiers([
        NotifierConfig(
            name='json-sink',
            type='command',
            command=sys.executable,
            args=['-c', f'import sys; open({str(sink1_out)!r}, "a").write(sys.stdin.read() + "\\n")'],
            input_mode='stdin_json',
            kinds=frozenset({'question', 'completion'}),
        ),
        NotifierConfig(
            name='template-sink',
            type='command',
            command=sys.executable,
            args=['-c', f'import sys; open({str(sink2_out)!r}, "a").write(sys.argv[1] + "\\n")', '{kind}:{message}'],
            input_mode='template_args',
            kinds=frozenset({'question'}),
        ),
        NotifierConfig(
            name='broken-sink',
            type='command',
            command=sys.executable,
            args=['-c', 'import sys; sys.exit(2)'],
            input_mode='stdin_json',
            kinds=frozenset({'question'}),
        ),
    ])

    box = core.outbox(actor='root')
    box.enqueue('q-1', 'question', 'Need approval?', actor='root', request_id=req_id)

    dispatcher = MultiNotificationDispatcher(box, sinks, resolve_request=core.store.get_request)
    # Deliver all 3 sink entries
    for _ in range(4):
        progressed = asyncio.run(dispatcher.deliver_once())
        if not progressed:
            break

    assert sink1_out.exists()
    data = json.loads(sink1_out.read_text(encoding='utf-8').strip())
    assert data['id'] == 'q-1' and data['message'] == 'Need approval?'
    assert sink2_out.read_text(encoding='utf-8').strip() == 'question:Need approval?'

    records = {r['notifier_name']: r['state'] for r in dispatcher.delivery_records('q-1')}
    assert records == {
        'json-sink': 'sent',
        'template-sink': 'sent',
        'broken-sink': 'uncertain',
    }


def test_serve_app_and_installer_and_doctor(tmp_path, capsys):
    db = tmp_path / 'requests.db'
    cfg = load_config(store_path_override=db)
    core = AncorCore(cfg)
    core.request_create(title='Serve Test', fields={'outcome': ['view in web']})

    app = create_serve_app(cfg)
    client = TestClient(app)
    assert client.get('/health').status_code == 200
    ov = client.get('/api/overview')
    assert ov.status_code == 200
    assert ov.json()['requests']['count'] == 1
    assert client.get('/').status_code == 200

    # Installer across hermes / codex / agy / dsh in isolated fake home
    fake_home = tmp_path / 'home'
    dry = install_target('codex', home=fake_home, dry_run=True)
    assert dry['dry_run'] is True
    assert not (fake_home / '.codex' / 'skills' / 'ancor' / 'SKILL.md').exists()

    res = install_target('codex', home=fake_home)
    assert 'SKILL.md' in res['files']
    chk = check_target('codex', home=fake_home)
    assert chk['installed'] is True and chk['up_to_date'] is True

    listing = list_targets(home=fake_home)
    assert {item['target'] for item in listing} == {'hermes', 'codex', 'agy', 'dsh'}

    un = uninstall_target('codex', home=fake_home)
    assert un['removed'] is True
    assert check_target('codex', home=fake_home)['installed'] is False

    # Doctor CLI
    rc, doc = _run_cli(['--db', str(db), 'doctor'], capsys)
    assert rc == 0
    assert doc['store_exists'] is True
    assert 'hermes' in doc['backends'] and 'codex' in doc['backends']
