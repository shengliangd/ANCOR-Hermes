"""The interactor supports both natural-language creation and explicit commands."""
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_ancor_is_the_only_interactor_role_skill():
    skill = (ROOT / 'skills/ancor/SKILL.md').read_text()
    standard = (ROOT / 'skills/ancor/references/REQUEST_STANDARD.md').read_text()
    assert not (ROOT / 'skills/ancor-interactor/SKILL.md').exists()
    assert 'name: ancor' in skill
    assert 'If the user explicitly asks you to do substantial work yourself, do it directly' in skill
    assert 'otherwise turn substantial or sustained work into a formal ANCOR request' in skill
    assert '`/ancor create` is an optional explicit shortcut, not a gate' in skill
    assert "skill_view(name='ancor', file_path='references/REQUEST_STANDARD.md')" in skill
    assert 'stable commitment boundary' in standard
    assert 'exact source, destination, mount, workspace, and artifact paths' in standard
    assert 'Context must not become a chronological progress report' in standard


def test_interactor_command_surface_is_intentionally_small():
    skill = (ROOT / 'skills/ancor/SKILL.md').read_text()
    for command in ('/ancor create', '/ancor revise', '/ancor list', '`/ancor`',
                    '/ancor show', '/ancor answer', '/ancor accept', '/ancor help'):
        assert command in skill
    assert 'no flat `/ancor-create` command' in skill
    assert 'no `/ancor changes` command' in skill


def test_bare_ancor_is_a_priority_ordered_user_action_inbox():
    skill = (ROOT / 'skills/ancor/SKILL.md').read_text()
    assert '## /ancor\n' in skill
    assert 'use it as an action inbox, not a project inventory' in skill
    assert 'Do not show requests that are running normally' in skill
    assert '`⏳` Open question or decision blocking active work' in skill
    assert '`🟠` Request blocked because the user must supply input' in skill
    assert '`🔵` Delivery awaiting final acceptance' in skill
    assert '`📝` Unconfirmed request draft' in skill
    assert 'For items with the same priority, show the oldest first.' in skill
    assert '`/ancor list` remains the exhaustive view' in skill
    assert '/ancor\n/ancor list' in skill


def test_create_requires_intake_and_material_clarification_before_drafting():
    skill = (ROOT / 'skills/ancor/SKILL.md').read_text()
    assert 'Request creation may start from an explicit `/ancor create` invocation or directly from clear everyday language' in skill
    assert 'Do not require the slash command' in skill
    assert '### Intake gate' in skill
    assert 'Ask the user concise, high-value questions before drafting' in skill
    assert 'Do not draft or call `ancor_change_propose` while a material ambiguity remains' in skill
    assert '`ancor_request_accept_propose`' in skill
    assert '`ancor_signal_propose`' in skill
    assert '`ancor_get_context' not in skill
    assert 'Native `clarify` remains reserved for the final formal proposal' not in skill


def test_confirmation_delivery_is_surface_specific():
    skill = (ROOT / 'skills/ancor/SKILL.md').read_text()
    assert 'On Desktop or Webapp, first send one ordinary assistant message containing the returned `confirmation_media` directive exactly' in skill
    assert 'The resulting Markdown file card is the complete confirmation' in skill
    assert 'Never replace the file card with a `file://` link or paste the full presentation into `clarify`' in skill
    assert 'On messaging surfaces such as Feishu, do not send `MEDIA:`' in skill
    assert 'Submit the returned full `clarify_args` unchanged' in skill


def test_interactor_soul_preserves_role_boundaries():
    soul = (ROOT / 'roles/ancor-interactor/SOUL.md').read_text()
    assert 'If the user explicitly asks you to do substantial work yourself, do it directly' in soul
    assert 'otherwise turn substantial or sustained work into an ANCOR request automatically' in soul
    assert '`/ancor create` is an optional explicit shortcut' in soul
    assert 'exact native confirmation' in soul
    assert 'the coordinator owns execution' in soul
    assert "Protect the user's attention" in soul
    assert 'cannot mutate Kanban' in soul
