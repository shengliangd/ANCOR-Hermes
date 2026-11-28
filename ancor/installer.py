"""Skill installer for external CLI platforms (``ancor install <hermes|codex|agy|dsh|all>``).

Installing the ``ancor`` package itself never touches third-party tool directories.
This module explicitly installs/checks/uninstalls the Interactor skill into selected targets.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil


@dataclass(frozen=True)
class TargetSpec:
    name: str
    description: str
    env_override: str
    default_rel_path: str

    def skills_dir(self, *, home: Path | None = None) -> Path:
        override = os.environ.get(self.env_override)
        if override:
            return Path(override).expanduser().resolve()
        base = (home or Path.home()).expanduser().resolve()
        return base / self.default_rel_path


TARGETS: dict[str, TargetSpec] = {
    'hermes': TargetSpec(
        name='hermes',
        description='Hermes Agent (~/.hermes/skills/ancor)',
        env_override='ANCOR_HERMES_SKILLS_DIR',
        default_rel_path='.hermes/skills/ancor',
    ),
    'codex': TargetSpec(
        name='codex',
        description='OpenAI Codex CLI (~/.codex/skills/ancor)',
        env_override='ANCOR_CODEX_SKILLS_DIR',
        default_rel_path='.codex/skills/ancor',
    ),
    'agy': TargetSpec(
        name='agy',
        description='Antigravity CLI (~/.gemini/antigravity/skills/ancor)',
        env_override='ANCOR_AGY_SKILLS_DIR',
        default_rel_path='.gemini/antigravity/skills/ancor',
    ),
    'dsh': TargetSpec(
        name='dsh',
        description='DeepSeek Harness (~/.dsh/skills/ancor)',
        env_override='ANCOR_DSH_SKILLS_DIR',
        default_rel_path='.dsh/skills/ancor',
    ),
}


def find_skill_source(explicit: str | Path | None = None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if (p / 'SKILL.md').exists():
            return p
        raise FileNotFoundError(f'SKILL.md not found under {p}')
    candidates = [
        Path(__file__).resolve().parent.parent / 'skills' / 'ancor',
        Path(__file__).resolve().parent / 'assets' / 'skills' / 'ancor',
    ]
    for candidate in candidates:
        if (candidate / 'SKILL.md').exists():
            return candidate
    raise FileNotFoundError('ANCOR interactor skill source directory (skills/ancor/SKILL.md) not found')


def _tree_digest(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    digests: dict[str, str] = {}
    for path in sorted(root.rglob('*')):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            digests[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def check_target(target_name: str, *, source: Path | None = None, home: Path | None = None) -> dict:
    if target_name not in TARGETS:
        raise ValueError(f'unknown install target {target_name!r}; expected one of {sorted(TARGETS)}')
    spec = TARGETS[target_name]
    dest = spec.skills_dir(home=home)
    src = find_skill_source(source)
    src_map = _tree_digest(src)
    dst_map = _tree_digest(dest)
    installed = bool(dst_map) and (dest / 'SKILL.md').exists()
    up_to_date = installed and all(dst_map.get(k) == v for k, v in src_map.items())
    return {
        'target': target_name,
        'description': spec.description,
        'destination': str(dest),
        'installed': installed,
        'up_to_date': up_to_date,
        'files': sorted(src_map.keys()),
    }


def install_target(
    target_name: str,
    *,
    source: Path | None = None,
    home: Path | None = None,
    dry_run: bool = False,
) -> dict:
    if target_name not in TARGETS:
        raise ValueError(f'unknown install target {target_name!r}; expected one of {sorted(TARGETS)}')
    spec = TARGETS[target_name]
    dest = spec.skills_dir(home=home)
    src = find_skill_source(source)
    copied: list[str] = []
    for path in sorted(src.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        out = dest / rel
        copied.append(rel.as_posix())
        if not dry_run:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, out)
    return {
        'target': target_name,
        'action': 'install',
        'dry_run': dry_run,
        'source': str(src),
        'destination': str(dest),
        'files': copied,
    }


def uninstall_target(
    target_name: str,
    *,
    home: Path | None = None,
    dry_run: bool = False,
) -> dict:
    if target_name not in TARGETS:
        raise ValueError(f'unknown install target {target_name!r}; expected one of {sorted(TARGETS)}')
    spec = TARGETS[target_name]
    dest = spec.skills_dir(home=home)
    existed = dest.exists()
    if existed and not dry_run:
        shutil.rmtree(dest)
    return {
        'target': target_name,
        'action': 'uninstall',
        'dry_run': dry_run,
        'destination': str(dest),
        'removed': existed,
    }


def list_targets(*, source: Path | None = None, home: Path | None = None) -> list[dict]:
    return [check_target(name, source=source, home=home) for name in TARGETS]
