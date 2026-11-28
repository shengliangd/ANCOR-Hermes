"""Top-level ``ancor`` CLI entry point.

Subcommands:
  - ``ancor core ...``     Machine-facing control plane (JSON in/out, no confirmation gate)
  - ``ancor daemon ...``   Standalone scheduling loop (run / start / stop / restart / status)
  - ``ancor serve ...``    Standalone Web viewer server (read-only today, extensible later)
  - ``ancor mcp ...``      Per-role MCP stdio server for CLI execution backends
  - ``ancor install ...``  Install/check/uninstall Interactor skill into hermes/codex/agy/dsh
  - ``ancor init ...``     Write a starter ~/.ancor/config.yaml
  - ``ancor doctor``       Check backends, config, store, and daemon health
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from ..backends import BackendRegistry
from ..config import DEFAULT_CONFIG_PATH, default_config_yaml, load_config
from ..daemon import AncorDaemon, daemon_status, start_daemon_background, stop_daemon_background
from ..installer import TARGETS, check_target, install_target, list_targets, uninstall_target
from ..mcp_server import run_mcp_stdio
from ..serve_app import run_serve
from .core_cmd import handle_core, register_core_parser


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='ancor',
        description='ANCOR — AI-Native Coordination Runtime (CLI-neutral multi-backend orchestrator)',
    )
    parser.add_argument('--config', default=None, help='Path to ANCOR config.yaml (default: ~/.ancor/config.yaml)')
    parser.add_argument('--db', default=None, help='Override store_path (SQLite requirements.db)')
    sub = parser.add_subparsers(dest='command', required=True)

    register_core_parser(sub)

    # daemon
    dm = sub.add_parser('daemon', help='Manage the standalone ANCOR scheduling daemon')
    dm_sub = dm.add_subparsers(dest='daemon_action', required=True)
    dm_sub.add_parser('run', help='Run scheduling loop in foreground')
    dm_sub.add_parser('start', help='Start scheduling daemon in background')
    dm_sub.add_parser('stop', help='Stop background scheduling daemon')
    dm_sub.add_parser('restart', help='Restart background scheduling daemon')
    dm_sub.add_parser('status', help='Show daemon status')

    # serve
    sv = sub.add_parser('serve', help='Serve the ANCOR Web viewer')
    sv.add_argument('--host', default='127.0.0.1')
    sv.add_argument('--port', type=int, default=8765)

    # mcp
    mc = sub.add_parser('mcp', help='Run per-role MCP stdio server for execution backends')
    mc.add_argument('--role', required=True, choices=['root', 'goal', 'judge', 'interactor'])
    mc.add_argument('--goal-id', default=None)
    mc.add_argument('--config', dest='sub_config', default=None, help=argparse.SUPPRESS)
    mc.add_argument('--db', dest='sub_db', default=None, help=argparse.SUPPRESS)

    # install
    ins = sub.add_parser('install', help='Install/check/uninstall the ANCOR Interactor skill into CLI platforms')
    ins.add_argument(
        'target',
        choices=[*sorted(TARGETS.keys()), 'all', 'list'],
        help='Target platform (hermes, codex, agy, dsh, all, or list)',
    )
    ins.add_argument('--dry-run', action='store_true')
    ins.add_argument('--check', action='store_true')
    ins.add_argument('--uninstall', action='store_true')
    ins.add_argument('--source', default=None, help='Explicit path to skills/ancor directory')

    # init
    ini = sub.add_parser('init', help='Write a starter ANCOR config.yaml')
    ini.add_argument('--path', default=str(DEFAULT_CONFIG_PATH))
    ini.add_argument('--force', action='store_true')

    # doctor
    sub.add_parser('doctor', help='Check configured backends, store, skills, and daemon health')
    return parser


def _handle_daemon(args: argparse.Namespace) -> int:
    cfg = load_config(args.config, store_path_override=args.db)
    act = args.daemon_action
    if act == 'run':
        daemon = AncorDaemon(cfg)
        asyncio.run(daemon.run_forever())
        return 0
    if act == 'start':
        print(json.dumps(start_daemon_background(cfg), ensure_ascii=False, indent=2))
        return 0
    if act == 'stop':
        print(json.dumps(stop_daemon_background(cfg), ensure_ascii=False, indent=2))
        return 0
    if act == 'restart':
        stop_daemon_background(cfg)
        print(json.dumps(start_daemon_background(cfg), ensure_ascii=False, indent=2))
        return 0
    if act == 'status':
        print(json.dumps(daemon_status(cfg), ensure_ascii=False, indent=2))
        return 0
    return 1


def _handle_install(args: argparse.Namespace) -> int:
    src = Path(args.source).expanduser().resolve() if args.source else None
    if args.target == 'list':
        print(json.dumps(list_targets(source=src), ensure_ascii=False, indent=2))
        return 0
    targets = sorted(TARGETS.keys()) if args.target == 'all' else [args.target]
    results = []
    for name in targets:
        if args.check:
            results.append(check_target(name, source=src))
        elif args.uninstall:
            results.append(uninstall_target(name, dry_run=args.dry_run))
        else:
            results.append(install_target(name, source=src, dry_run=args.dry_run))
    print(json.dumps(results if len(results) > 1 else results[0], ensure_ascii=False, indent=2))
    return 0


def _handle_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.config, store_path_override=args.db)
    registry = BackendRegistry.from_config(cfg)
    backends_report = {}
    for name, be in registry.all().items():
        ok, detail = be.check_available()
        backends_report[name] = {
            'available': ok,
            'detail': detail,
            'capabilities': {
                'supports_mcp': be.capabilities.supports_mcp,
                'supports_resume': be.capabilities.supports_resume,
                'supports_readonly_fence': be.capabilities.supports_readonly_fence,
                'supports_model_telemetry': be.capabilities.supports_model_telemetry,
            },
        }
    report = {
        'config_path': str(cfg.config_path) if cfg.config_path else None,
        'store_path': str(cfg.store_path),
        'store_exists': cfg.store_path.exists(),
        'daemon': daemon_status(cfg),
        'backends': backends_report,
        'categories': {
            name: {
                'backend': c.backend,
                'args': c.args,
                'fallbacks': [{'backend': fb.backend, 'args': fb.args} for fb in c.fallbacks],
            }
            for name, c in cfg.categories.items()
        },
        'notifiers': [{'name': n.name, 'type': n.type, 'kinds': sorted(n.kinds)} for n in cfg.notifiers],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    cmd = args.command

    if cmd == 'core':
        return handle_core(args)
    if cmd == 'daemon':
        return _handle_daemon(args)
    if cmd == 'serve':
        run_serve(host=args.host, port=args.port, config_path=args.config, store_path=args.db)
        return 0
    if cmd == 'mcp':
        asyncio.run(run_mcp_stdio(
            role=args.role,
            goal_id=args.goal_id,
            config_path=getattr(args, 'sub_config', None) or args.config,
            store_path=getattr(args, 'sub_db', None) or args.db,
        ))
        return 0
    if cmd == 'install':
        return _handle_install(args)
    if cmd == 'init':
        target = Path(args.path).expanduser().resolve()
        if target.exists() and not args.force:
            print(json.dumps({'error': f'{target} already exists (use --force to overwrite)', 'code': 'already_exists'}))
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(default_config_yaml(args.db), encoding='utf-8')
        print(json.dumps({'created': str(target)}, ensure_ascii=False))
        return 0
    if cmd == 'doctor':
        return _handle_doctor(args)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
