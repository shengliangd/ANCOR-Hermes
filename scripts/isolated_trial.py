#!/usr/bin/env python3
"""Prepare and exercise a private ANCOR/Hermes instance with real configured models.

Only the human clarify callback and notification destination are simulated. Model
responses, tool calls, confirmations, coordinator/worker execution and Kanban are
real. Never start this against an existing Hermes home.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
ROLES = ('ancor-interactor', 'ancor-coordinator', 'ancor-worker')


def role_skills(role):
    if role == 'ancor-interactor':
        primary = 'ancor'
    elif role == 'ancor-coordinator':
        primary = 'ancor-coordination'
    else:
        primary = role
    return (primary,)


def prepare(root, native, credentials):
    import yaml
    from dotenv import dotenv_values
    root, native, credentials = root.resolve(), native.resolve(), credentials.resolve()
    if root.exists():
        raise ValueError('trial root must be new; refusing to change an existing instance')
    if root == credentials or root.is_relative_to(credentials):
        raise ValueError('trial root must be outside the existing Hermes instance')
    root.mkdir(mode=0o700, parents=True)
    # Read-only dependency resolution; do not launch/update the source instance.
    sys.path.insert(0, str(native))
    prior_home = os.environ.get('HERMES_HOME')
    os.environ['HERMES_HOME'] = str(credentials)
    from pm.environments import selected_venv
    dependency_env = selected_venv(native)
    if prior_home is None:
        os.environ.pop('HERMES_HOME', None)
    else:
        os.environ['HERMES_HOME'] = prior_home
    python = dependency_env / 'bin/python'
    if not python.exists():
        raise ValueError('configured native Python environment is missing')
    # Reuse vetted library files read-only through a private venv; disable lazy installs.
    from pm.environments import install_key
    state = root / 'installs' / install_key(native)
    copied = state / 'environments' / 'trial' / 'venv'
    copied.mkdir(parents=True)
    shutil.copy2(dependency_env / 'pyvenv.cfg', copied / 'pyvenv.cfg')
    source_site = next((dependency_env / 'lib').glob('python*/site-packages'))
    private_site = copied / 'lib' / source_site.parent.name / 'site-packages'
    private_site.mkdir(parents=True)
    (private_site / 'borrowed-dependencies.pth').write_text(str(source_site) + '\n')
    (copied / 'bin').mkdir()
    for name in ('python', 'python3', 'python3.14'):
        (copied / 'bin' / name).symlink_to((dependency_env / 'bin/python').resolve())
    fact = json.loads((credentials / 'installs' / install_key(native) / 'facts.json').read_text())
    fact['packages']['venv']['environment'] = str(copied)
    fact['packages']['venv'].pop('resolved_lock', None)
    (state / 'facts.json').write_text(json.dumps(fact))
    python = copied / 'bin/python'
    workspace = root / 'workspace'
    workspace.mkdir()
    settings = {'store_path': str(root / 'ancor/requests.db'), 'board': 'trial',
                'owner_profile': 'default', 'gateway_owner': False,
                'interactor_profile': 'ancor-interactor', 'coordinator_profile': 'ancor-coordinator',
                'worker_profile': 'ancor-worker', 'human_target': 'trial:human',
                'transport_profile': 'default', 'poll_interval': 30}
    (root / 'ancor').mkdir()
    identities = {}
    for role in ('default', *ROLES):
        home = root if role == 'default' else root / 'profiles' / role
        home.mkdir(parents=True, exist_ok=True)
        source_role = 'ancor-coordinator' if role == 'default' else role
        source = credentials / 'profiles' / source_role
        original = yaml.safe_load((source / 'config.yaml').read_text()) or {}
        model = copy.deepcopy(original['model'])
        providers = copy.deepcopy(original.get('providers', {}))
        # Model credentials only. No bot routes, OAuth logins, schedules or secrets copied wholesale.
        names = {'DEEPSEEK_API_KEY'} if model.get('provider') == 'deepseek' else set()
        if str(model.get('provider', '')).startswith('custom:'):
            provider_name = model['provider'].split(':', 1)[1]
            providers = {provider_name: providers[provider_name]}
            names.add(providers[provider_name]['key_env'])
        else:
            providers = {}
        values = dotenv_values(source / '.env')
        secret_values = {name: values.get(name) or os.environ.get(name) for name in names}
        if any(not v for v in secret_values.values()):
            raise ValueError(f'model credential missing for {role}; no fallback to bot credentials')
        fd = os.open(home / '.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            for name, value in secret_values.items():
                output.write(name + '=' + json.dumps(value) + '\n')
        toolsets = ['terminal', 'file', 'skills', 'delegation']
        if role == 'ancor-interactor':
            toolsets += ['ancor', 'clarify']
        elif role == 'ancor-coordinator':
            toolsets += ['ancor']
        elif role == 'ancor-worker':
            toolsets += ['kanban']
        config = {
            '_config_version': original.get('_config_version', 1),
            'model': model, 'providers': providers, 'auth': {'adopt_external_logins': False},
            'platform_toolsets': {'cli': toolsets},
            'agent': {'max_turns': 24},
            'approvals': {'single_query_mode': 'approve'},
            'auxiliary': {'background_review': {'enabled': False}}, 'curator': {'enabled': False},
            'skills': {'auto_load': [{'ancor-interactor': 'ancor',
                                    'ancor-coordinator': 'ancor-coordination', 'ancor-worker': 'ancor-worker'}[role]] if role in ROLES else []},
            'plugins': {'enabled': ['ancor'], 'entries': {'ancor': {'enabled': True, 'settings': settings}}},
            'kanban': {'review_dispatch': False, 'dispatch_in_gateway': False, 'auto_subscribe': False},
            'telemetry': {'enabled': False}, 'terminal': {'cwd': str(workspace)},
        }
        (home / 'config.yaml').write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
        (home / '.no-bundled-skills').touch()
        (home / 'plugins').mkdir(exist_ok=True)
        shutil.copytree(REPO / 'ancor', home / 'plugins/ancor', ignore=shutil.ignore_patterns('__pycache__'))
        (home / 'skills').mkdir(exist_ok=True)
        if role in ROLES:
            shutil.copy2(REPO / 'roles' / role / 'SOUL.md', home / 'SOUL.md')
            for skill in role_skills(role):
                shutil.copytree(REPO / 'skills' / skill, home / 'skills' / skill)
        identities[role] = {'model': model.get('default'), 'provider': model.get('provider')}
    (root / 'trial.json').write_text(json.dumps({'root': str(root), 'native': str(native),
           'python': str(python), 'repo': str(REPO), 'models': identities}, indent=2))
    (root / 'bin').mkdir()
    wrapper = '#!/bin/sh\nexec ' + shlex.quote(str(python)) + ' -B ' + shlex.quote(str(REPO / 'scripts/trial_runtime.py')) + ' "$@"\n'
    (root / 'bin/hermes').write_text(wrapper)
    (root / 'bin/hermes').chmod(0o700)
    subprocess.run([str(root / 'bin/hermes'), 'kanban', 'boards', 'create', 'trial',
                    '--default-workdir', str(workspace)], env=environment(root), cwd=workspace, check=True,
                   stdout=subprocess.DEVNULL)
    print(json.dumps({'trial_root': str(root), 'models': identities, 'live_instance_modified': False}, indent=2))


def sync_trial(root):
    meta = json.loads((root / 'trial.json').read_text())
    if Path(meta['root']).resolve() != root.resolve() or root == Path.home() / '.hermes':
        raise ValueError('not the isolated trial root')
    for role in ('default', *ROLES):
        home = root if role == 'default' else root / 'profiles' / role
        shutil.copytree(REPO / 'ancor', home / 'plugins/ancor', dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__'))
        if role in ROLES:
            shutil.copy2(REPO / 'roles' / role / 'SOUL.md', home / 'SOUL.md')
            for skill in role_skills(role):
                shutil.copytree(REPO / 'skills' / skill, home / 'skills' / skill, dirs_exist_ok=True)


def environment(root):
    env = dict(os.environ)
    # HERMES_HOME is a real isolated runtime setting, not a reused shell variable.
    env.update(HERMES_HOME=str(root), ANCOR_TRIAL_ROOT=str(root),
               HERMES_KANBAN_HOME=str(root / 'kanban'), TERMINAL_CWD=str(root / 'workspace'),
               PYTHONDONTWRITEBYTECODE='1', HERMES_DISABLE_LAZY_INSTALLS='1', PATH=str(root / 'bin') + os.pathsep + env['PATH'])
    return env


def run_turn(root, message, role='ancor-interactor', *, response='confirm', resume=True):
    meta = json.loads((root / 'trial.json').read_text())
    count = len(list((root / 'turns').glob('*.input.txt'))) if (root / 'turns').exists() else 0
    (root / 'turns').mkdir(exist_ok=True)
    name = f'{count:03d}-{role}'
    input_path = root / 'turns' / (name + '.input.txt')
    input_path.write_text(message)
    env = environment(root)
    env['ANCOR_TRIAL_RESPONSE'] = response
    env['ANCOR_TRIAL_RESULT'] = str(root / 'turns' / (name + '.result.json'))
    env['ANCOR_TRIAL_RESUME'] = '1' if resume else '0'
    with (root / 'turns' / (name + '.log')).open('w') as log:
        result = subprocess.run([str(root / 'bin/hermes'), '-p', role, 'chat', '-q', message],
                                cwd=root / 'workspace', env=env, stdout=log, stderr=subprocess.STDOUT, timeout=900)
    output = Path(env['ANCOR_TRIAL_RESULT'])
    data = json.loads(output.read_text()) if output.exists() else {'error': 'turn produced no result'}
    print(json.dumps({'turn': name, 'exit_code': result.returncode, **{k: data.get(k) for k in
                     ('completed','session_id','final_response','api_calls','total_tokens','error')}}, ensure_ascii=False))
    if result.returncode:
        raise RuntimeError(f'trial turn failed; inspect {name}.log')
    return data


def pump(root, cycles=1):
    env = environment(root)
    count = len(list((root / 'turns').glob('pump-*.log')))
    logpath = root / 'turns' / f'pump-{count:03d}.log'
    with logpath.open('w') as output:
        result = subprocess.run([str(root / 'bin/hermes'), '--trial-pump', str(cycles)], cwd=root / 'workspace',
                                env=env, stdout=output, stderr=subprocess.STDOUT, timeout=1500)
    print(json.dumps({'pump_log': str(logpath), 'exit_code': result.returncode}))
    if result.returncode:
        raise RuntimeError('trial pump failed; inspect its isolated log')


def use_model(root, role, source_role, reclaim=False):
    import yaml
    if role not in ROLES or source_role not in ROLES:
        raise ValueError('model selection stays inside isolated role profiles')
    meta = json.loads((root / 'trial.json').read_text())
    if Path(meta['root']).resolve() != root.resolve():
        raise ValueError('not the trial instance')
    home, source = root / 'profiles' / role, root / 'profiles' / source_role
    cfg = yaml.safe_load((home / 'config.yaml').read_text())
    other = yaml.safe_load((source / 'config.yaml').read_text())
    previous = cfg['model']
    cfg['model'], cfg['providers'] = copy.deepcopy(other['model']), copy.deepcopy(other.get('providers', {}))
    (home / 'config.yaml').write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
    if home != source:
        shutil.copy2(source / '.env', home / '.env')
    meta.setdefault('model_changes', []).append({'role': role, 'from': previous, 'to': cfg['model'],
                                                'source_role': source_role, 'time': time.time()})
    meta['models'][role] = {'model': cfg['model']['default'], 'provider': cfg['model']['provider']}
    (root / 'trial.json').write_text(json.dumps(meta, indent=2))
    if reclaim:
        result = subprocess.run([str(root / 'bin/hermes'), 'kanban', '--board', 'trial', 'list', '--json'],
                                env=environment(root), capture_output=True, text=True, check=True, timeout=90)
        for task in json.loads(result.stdout):
            if task.get('assignee') == role and task['status'] == 'running':
                subprocess.run([str(root / 'bin/hermes'), 'kanban', '--board', 'trial', 'reclaim', task['id'],
                                '--reason', 'Isolated trial model transport recovery; preserve task and retry'],
                               env=environment(root), check=True, timeout=90)
    print(json.dumps({'role': role, 'model': meta['models'][role], 'live_instance_modified': False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'turn', 'sync', 'pump', 'use-model'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--native', type=Path)
    parser.add_argument('--credentials-from', type=Path)
    parser.add_argument('--role', default='ancor-interactor', choices=ROLES)
    parser.add_argument('--message')
    parser.add_argument('--model-from-role', choices=ROLES)
    parser.add_argument('--reclaim', action='store_true')
    parser.add_argument('--cycles', type=int, default=1)
    parser.add_argument('--fresh', action='store_true', help='Start a new simulated user conversation after updating role instructions')
    parser.add_argument('--response', default='confirm', choices=['confirm','reject','timeout'])
    args = parser.parse_args()
    if args.action == 'prepare':
        if not args.native or not args.credentials_from:
            parser.error('prepare requires --native and --credentials-from')
        prepare(args.root, args.native, args.credentials_from)
    elif args.action == 'use-model':
        if not args.model_from_role:
            parser.error('use-model requires --model-from-role')
        use_model(args.root.resolve(), args.role, args.model_from_role, args.reclaim)
    elif args.action == 'pump':
        pump(args.root.resolve(), args.cycles)
    elif args.action == 'sync':
        sync_trial(args.root.resolve())
    else:
        if not args.message:
            parser.error('turn requires --message')
        run_turn(args.root.resolve(), args.message, args.role, response=args.response, resume=not args.fresh)


if __name__ == '__main__':
    main()
