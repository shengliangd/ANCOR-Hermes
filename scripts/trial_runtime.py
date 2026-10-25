#!/usr/bin/env python3
"""Private CLI host for the isolated trial; real native tools/model with a simulated human UI."""
import argparse
import json
import os
from pathlib import Path
import sys
import time


def main():
    root = Path(os.environ['ANCOR_TRIAL_ROOT']).resolve()
    meta = json.loads((root / 'trial.json').read_text())
    if Path(os.environ.get('HERMES_HOME', '')).resolve() != root and not Path(os.environ.get('HERMES_HOME', '')).resolve().is_relative_to(root):
        raise RuntimeError('refusing to run outside trial home')
    argv = sys.argv[1:]
    profile = 'default'
    for flag in ('-p', '--profile'):
        if flag in argv:
            index = argv.index(flag)
            profile = argv[index + 1]
            del argv[index:index + 2]
    if profile != 'default':
        os.environ['HERMES_HOME'] = str(root / 'profiles' / profile)
    os.environ['HERMES_PROFILE'] = profile
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.dont_write_bytecode = True
    sys.path[:0] = [meta['native'], meta['repo']]
    from dotenv import load_dotenv
    load_dotenv(Path(os.environ['HERMES_HOME']) / '.env', override=True)
    if '--trial-pump' in argv:
        return pump(root, int(argv[argv.index('--trial-pump') + 1]))
    if 'kanban' in argv:
        import argparse
        from hermes_cli.kanban_parser import build_parser
        from hermes_cli.kanban import kanban_command
        parser = argparse.ArgumentParser()
        build_parser(parser.add_subparsers())
        return kanban_command(parser.parse_args(argv))
    for option in ('--cli',):
        if option in argv:
            argv.remove(option)
    if '-q' not in argv:
        raise ValueError('trial runtime supports native kanban and chat -q only')
    prompt = argv[argv.index('-q') + 1]
    from hermes_cli import oneshot

    def simulated_human(questions):
        response = os.environ.get('ANCOR_TRIAL_RESPONSE', 'confirm')
        with (root / 'human-forms.jsonl').open('a') as output:
            output.write(json.dumps({'profile': profile, 'questions': questions, 'response': response,
                                     'time': time.time()}, ensure_ascii=False) + '\n')
        if profile != 'ancor-interactor':
            raise RuntimeError('background roles cannot ask the user through clarify')
        if response == 'timeout':
            return {'outcome': 'timed_out', 'answers': {}}
        answers = {}
        for q in questions:
            choices = q.get('choices_offered') or []
            if len(choices) == 2 and '确认单：' in q['question']:
                answers[q['qid']] = choices[1 if response == 'confirm' else 0]
            else:
                # The trial must report unexpected clarification instead of fabricating user intent.
                return {'outcome': 'cancelled', 'answers': {}, 'notice': 'Trial human has no scripted answer to this clarification.'}
        return {'outcome': 'submitted', 'answers': answers}

    oneshot._oneshot_clarify_callback = simulated_human
    session_file = root / ('session-' + profile + '.json')
    resume = json.loads(session_file.read_text()).get('session_id') if (
        profile == 'ancor-interactor' and session_file.exists() and os.environ.get('ANCOR_TRIAL_RESUME') != '0') else None
    try:
        final, result = oneshot._run_agent(prompt, resume=resume, ledger=True)
        safe = {k: result.get(k) for k in ('completed','session_id','api_calls','total_tokens',
                                         'turn_exit_reason','error','model','provider')}
        safe['final_response'] = final
        if result.get('session_id') and profile == 'ancor-interactor':
            session_file.write_text(json.dumps({'session_id': result['session_id']}))
        target = os.environ.get('ANCOR_TRIAL_RESULT') or str(root / 'turns' / f'{profile}-{os.getpid()}.result.json')
        # Background worker subprocesses must not overwrite their dispatcher's turn result.
        if target and not os.environ.get('HERMES_KANBAN_TASK'):
            Path(target).write_text(json.dumps(safe, ensure_ascii=False, indent=2))
        print(final)
        if os.environ.get('HERMES_KANBAN_TASK'):
            print('[kanban-worker-exit] rc=0', file=sys.stderr, flush=True)
        return 0 if result.get('completed') else 1
    finally:
        # Match native CLI cleanup without touching another instance's processes.
        from hermes_cli.main import _cleanup_oneshot_runtime
        _cleanup_oneshot_runtime()


def pump(root, cycles):
    import asyncio
    from dataclasses import asdict
    from types import SimpleNamespace
    import yaml
    from ancor.host import Host
    from ancor.notification import NotificationDispatcher
    from hermes_cli.kanban_db_connect import connect
    from hermes_cli.kanban_db_dispatch import dispatch_once
    cfg = yaml.safe_load((root / 'config.yaml').read_text())['plugins']['entries']['ancor']['settings']
    ctx = SimpleNamespace(get_config=lambda key, default=None: cfg.get(key, default))
    host = Host(ctx)
    def local_inbox(target, message):
        if target != 'trial:human':
            raise RuntimeError('trial notification cannot leave local inbox')
        with (root / 'inbox.jsonl').open('a') as output:
            output.write(json.dumps({'target': target, 'message': message, 'time': time.time()}, ensure_ascii=False) + '\n')
        return {'success': True, 'trial': True}
    box = host.notification_outbox()
    dispatcher = NotificationDispatcher(box, local_inbox, resolve_request=host.store.get_request,
        resolve_task=lambda tid: host.board.show(tid) if tid else None)
    def native_tick():
        conn = connect(board='trial')
        try:
            return dispatch_once(conn, board='trial', max_spawn=2, max_in_progress=2)
        finally:
            conn.close()
    async def tick():
        # Production dispatch runs independently of coordination. Keep it concurrent
        # so a coordinator inspecting a released card cannot starve its worker.
        coordinator = asyncio.create_task(host.coordinator.tick())
        while not coordinator.done():
            dispatch = await asyncio.to_thread(native_tick)
            if dispatch.spawned or dispatch.crashed or dispatch.auto_blocked:
                print(json.dumps({'concurrent_dispatch': asdict(dispatch)}, default=str), flush=True)
            while await dispatcher.deliver_once():
                pass
            await asyncio.wait([coordinator], timeout=3)
        state = await coordinator
        while await dispatcher.deliver_once():
            pass
        return state
    for index in range(cycles):
        result = asyncio.run(tick())
        dispatch = native_tick()
        print(json.dumps({'cycle': index, 'coordinator': result, 'dispatch': asdict(dispatch)}, default=str), flush=True)
        print(json.dumps({'requests': [{'id': r['id'], 'status': r['status'], 'generation': r['generation']}
                                     for r in host.store.snapshot()['requests']],
                          'tasks': [{'id': t['id'], 'status': t['status']} for t in host.board.list()]}, ensure_ascii=False), flush=True)
        if index + 1 < cycles:
            time.sleep(15)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
