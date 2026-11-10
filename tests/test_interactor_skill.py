"""The interactor has one role skill with a narrow explicit command surface."""
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_ancor_is_the_only_interactor_role_skill():
    skill = (ROOT / 'skills/ancor/SKILL.md').read_text()
    standard = (ROOT / 'skills/ancor/references/REQUEST_STANDARD.md').read_text()
    assert not (ROOT / 'skills/ancor-interactor/SKILL.md').exists()
    assert 'name: ancor' in skill
    assert 'Formal request creation is command-gated' in skill
    assert "skill_view(name='ancor', file_path='references/REQUEST_STANDARD.md')" in skill
    assert 'stable commitment boundary' in standard
    assert 'exact source, destination, mount, workspace, and artifact paths' in standard
    assert 'Context must not become a chronological progress report' in standard


def test_interactor_command_surface_is_intentionally_small():
    skill = (ROOT / 'skills/ancor/SKILL.md').read_text()
    for command in ('/ancor create', '/ancor revise', '/ancor list',
                    '/ancor show', '/ancor answer', '/ancor accept', '/ancor help'):
        assert command in skill
    assert 'no flat `/ancor-create` command' in skill
    assert 'no `/ancor changes` command' in skill


def test_create_requires_intake_and_material_clarification_before_drafting():
    skill = (ROOT / 'skills/ancor/SKILL.md').read_text()
    assert '### Intake gate' in skill
    assert 'Ask the user concise, high-value questions before drafting' in skill
    assert 'Do not draft or call `ancor_change propose` while a material ambiguity remains' in skill
    assert 'Native `clarify` remains reserved for the final formal proposal and confirmation' in skill


def test_interactor_soul_preserves_role_boundaries():
    soul = (ROOT / 'roles/ancor-interactor/SOUL.md').read_text()
    assert 'Formal request creation starts only when the user invokes `/ancor create`' in soul
    assert 'exact native confirmation' in soul
    assert 'the coordinator owns execution' in soul
    assert "Protect the user's attention" in soul
    assert 'cannot mutate Kanban' in soul
