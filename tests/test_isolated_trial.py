"""The trial provisioner cannot accidentally provision over a user instance."""
import importlib.util
import json
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def trial_module():
    path = Path(__file__).parents[1] / 'scripts/isolated_trial.py'
    spec = importlib.util.spec_from_file_location('ancor_trial_provisioner', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_trial_refuses_existing_root_without_changing_it(tmp_path, trial_module):
    root = tmp_path / 'existing'
    root.mkdir()
    config = root / 'config.yaml'
    config.write_text('preserve this instance')
    with pytest.raises(ValueError, match='must be new'):
        trial_module.prepare(root, tmp_path / 'native', tmp_path / 'credentials')
    assert config.read_text() == 'preserve this instance'
    assert list(root.iterdir()) == [config]


def test_trial_refuses_provisioning_inside_credential_source(tmp_path, trial_module):
    source = tmp_path / 'existing-hermes'
    source.mkdir()
    destination = source / 'trial'
    with pytest.raises(ValueError, match='outside'):
        trial_module.prepare(destination, tmp_path / 'native', source)
    assert not destination.exists()


def test_trial_interactor_installs_command_skill_with_shared_standard(trial_module):
    assert trial_module.role_skills('ancor-interactor') == ('ancor',)
    assert trial_module.role_skills('ancor-coordinator') == ('ancor-coordination',)
    assert trial_module.role_skills('ancor-worker') == ('ancor-worker',)


def test_trial_model_recovery_stays_inside_trial_and_does_not_print_secrets(tmp_path, trial_module, capsys):
    root = tmp_path / 'trial'
    worker = root / 'profiles/ancor-worker'
    coordinator = root / 'profiles/ancor-coordinator'
    worker.mkdir(parents=True)
    coordinator.mkdir(parents=True)
    (root / 'trial.json').write_text(json.dumps({'root': str(root), 'models': {}}))
    for home, model in ((worker, 'old'), (coordinator, 'healthy')):
        (home / 'config.yaml').write_text(yaml.safe_dump({'model': {'default': model, 'provider': 'deepseek'}}))
        (home / '.env').write_text('DEEPSEEK_API_KEY=fixture-secret-only\n')
    trial_module.use_model(root, 'ancor-worker', 'ancor-coordinator')
    assert yaml.safe_load((worker / 'config.yaml').read_text())['model']['default'] == 'healthy'
    assert 'fixture-secret-only' not in capsys.readouterr().out
    original = (worker / 'config.yaml').read_bytes()
    with pytest.raises(ValueError, match='inside isolated'):
        trial_module.use_model(root, 'default', 'ancor-coordinator')
    assert (worker / 'config.yaml').read_bytes() == original
