"""Installer subprocesses and prompt files must target the same Hermes instance."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


@pytest.fixture
def installer(tmp_path, monkeypatch):
    repo = tmp_path / 'release'
    script = repo / 'scripts/install_ancor.py'
    script.parent.mkdir(parents=True)
    shutil.copy2(Path(__file__).parents[1] / 'scripts/install_ancor.py', script)
    role_skills = (('interactor', ('ancor',)),
                   ('coordinator', ('ancor-coordination',)),
                   ('worker', ('ancor-worker',)))
    for role, skills in role_skills:
        relatives = [f'roles/ancor-{role}/SOUL.md']
        for skill in skills:
            relatives.append(f'skills/{skill}/SKILL.md')
            if skill == 'ancor':
                relatives.append(f'skills/{skill}/references/REQUEST_STANDARD.md')
        for relative in relatives:
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'Fixture release file: {relative}\n')
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Installer test',
                    '-c', 'user.email=installer@example.invalid',
                    '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Fixture'], check=True)

    fake_bin = tmp_path / 'bin'
    fake_bin.mkdir()
    fake_python = fake_bin / 'hermes_fixture.py'
    fake_python.write_text('''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
root = Path(os.environ['HERMES_HOME'])
with Path(os.environ['ANCOR_INSTALL_TEST_LOG']).open('a') as output:
    output.write(json.dumps({'args': args, 'root': str(root),
                            'sentinel': os.environ.get('ANCOR_INSTALL_TEST_SENTINEL')}) + '\\n')
if args[2:4] == ['plugins', 'install']:
    profile = args[1]
    target = root if profile == 'default' else root / 'profiles' / profile
    plugin = target / 'plugins/ancor'
    plugin.mkdir(parents=True)
    (plugin / 'installed').write_text('fixture')
''')
    fake_hermes = fake_bin / 'hermes'
    fake_hermes.write_text('#!/bin/sh\nexec ' + shlex.join(
        [sys.executable, str(fake_python)]) + ' "$@"\n')
    fake_hermes.chmod(0o755)
    monkeypatch.setenv('PATH', str(fake_bin) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'wrong-instance'))
    monkeypatch.setenv('ANCOR_INSTALL_TEST_LOG', str(tmp_path / 'calls.jsonl'))
    monkeypatch.setenv('ANCOR_INSTALL_TEST_SENTINEL', 'preserved')
    monkeypatch.chdir(tmp_path)

    spec = importlib.util.spec_from_file_location('installer_under_test', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('owner', ['default', 'operations'])
def test_all_installs_and_restart_use_explicit_root(installer, tmp_path, monkeypatch, owner):
    ambient = os.environ['HERMES_HOME']
    target = tmp_path / 'chosen instance'
    obsolete = target / 'profiles/ancor-interactor/skills/ancor-interactor/SKILL.md'
    obsolete.parent.mkdir(parents=True)
    obsolete.write_text('obsolete managed prompt\n')
    monkeypatch.setattr(sys, 'argv', ['install_ancor.py', '--hermes-home',
                                    'chosen instance', '--owner-profile', owner])

    assert installer.main() == 0

    calls = [json.loads(line) for line in (tmp_path / 'calls.jsonl').read_text().splitlines()]
    assert [call['args'][1] for call in calls] == [
        owner, 'ancor-interactor', 'ancor-coordinator', 'ancor-worker', owner]
    assert all(call['args'][2:4] == ['plugins', 'install'] for call in calls[:4])
    assert calls[-1]['args'][2:] == ['gateway', 'restart']
    assert all(call['root'] == str(target) for call in calls)
    assert all(call['sentinel'] == 'preserved' for call in calls)
    assert os.environ['HERMES_HOME'] == ambient
    assert not Path(ambient).exists()
    for profile in [owner, 'ancor-interactor', 'ancor-coordinator', 'ancor-worker']:
        home = target if profile == 'default' else target / 'profiles' / profile
        assert (home / 'plugins/ancor/installed').is_file()
    repo = Path(installer.__file__).parents[1]
    for role, skill in installer.ROLES.items():
        home = target / 'profiles' / f'ancor-{role}'
        assert (home / 'SOUL.md').read_bytes() == (repo / 'roles' / f'ancor-{role}' / 'SOUL.md').read_bytes()
        assert (home / 'skills' / skill / 'SKILL.md').read_bytes() == (repo / 'skills' / skill / 'SKILL.md').read_bytes()
    command_home = target / 'profiles' / 'ancor-interactor'
    assert not (command_home / 'skills/ancor-interactor').exists()
    backups = list((target / 'backups').glob('ancor-install-*'))
    assert any((path / 'prompts/ancor-interactor/skills/ancor-interactor/SKILL.md').is_file()
               for path in backups)
    for relative in ('SKILL.md', 'references/REQUEST_STANDARD.md'):
        assert ((command_home / 'skills/ancor' / relative).read_bytes()
                == (repo / 'skills/ancor' / relative).read_bytes())
    for profile in ('ancor-coordinator', 'ancor-worker'):
        assert not (target / 'profiles' / profile / 'skills/ancor').exists()


def test_dry_run_shows_root_without_installing(installer, tmp_path, monkeypatch, capsys):
    ambient = os.environ['HERMES_HOME']
    monkeypatch.setattr(sys, 'argv', ['install_ancor.py', '--hermes-home',
                                    'chosen instance', '--dry-run'])

    assert installer.main() == 0

    commands = [shlex.split(line[2:]) for line in capsys.readouterr().out.splitlines()
                if line.startswith('+ ')]
    assert len(commands) == 5
    assert all(command[:2] == ['env', f'HERMES_HOME={tmp_path / "chosen instance"}']
               for command in commands)
    assert not (tmp_path / 'calls.jsonl').exists()
    assert not (tmp_path / 'chosen instance').exists()
    assert os.environ['HERMES_HOME'] == ambient
