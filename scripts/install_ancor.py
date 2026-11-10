#!/usr/bin/env python3
"""Install one complete, version-pinned ANCOR release.

The Hermes plugin and role prompts are separate installation surfaces.  This
command treats them as one release, backs up the old prompt files, installs
the plugin into every participating profile, and verifies the resulting files.
It does not migrate or modify ANCOR databases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone


ROLES = {
    "interactor": "ancor",
    "coordinator": "ancor-coordination",
    "worker": "ancor-worker",
}
OBSOLETE_SKILLS = {
    "interactor": ("ancor-interactor",),
}


def run(command: list[str], *, hermes_home: Path, dry_run: bool = False) -> None:
    print("+", shlex.join(["env", f"HERMES_HOME={hermes_home}", *command]))
    if not dry_run:
        environment = os.environ.copy()
        environment["HERMES_HOME"] = str(hermes_home)
        subprocess.run(command, check=True, env=environment)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def profile_home(hermes_home: Path, profile: str) -> Path:
    return hermes_home if profile == "default" else hermes_home / "profiles" / profile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--ref", help="full git commit; defaults to HEAD")
    parser.add_argument("--owner-profile", default="default")
    parser.add_argument("--no-restart", action="store_true",
                        help="install and verify without restarting the Gateway")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    hermes_home = args.hermes_home.expanduser().resolve()
    git = shutil.which("git")
    hermes = shutil.which("hermes")
    if not git or not hermes:
        raise SystemExit("git and hermes must be available on PATH")
    ref = args.ref or subprocess.check_output(
        [git, "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    if len(ref) != 40:
        raise SystemExit("--ref must resolve to a full 40-character commit")
    resolved = subprocess.check_output(
        [git, "-C", str(repo), "rev-parse", f"{ref}^{{commit}}"], text=True).strip()
    if resolved != ref:
        raise SystemExit(f"ref is not an exact commit: {ref}")

    source = f"file://{repo}#ancor"
    profiles = [args.owner_profile, "ancor-interactor", "ancor-coordinator", "ancor-worker"]
    if len(set(profiles)) != len(profiles):
        raise SystemExit("owner profile must be distinct from role profiles")
    prompt_sources: dict[str, Path] = {}
    for role, skill in ROLES.items():
        prompt_sources[f"ancor-{role}/SOUL.md"] = repo / "roles" / f"ancor-{role}" / "SOUL.md"
        skill_dir = repo / "skills" / skill
        for source_path in sorted(skill_dir.rglob("*")):
            if not source_path.is_file() or "__pycache__" in source_path.parts:
                continue
            relative = source_path.relative_to(repo / "skills").as_posix()
            prompt_sources[f"ancor-{role}/skills/{relative}"] = source_path
    missing = [str(path) for path in prompt_sources.values() if not path.is_file()]
    if missing:
        raise SystemExit("missing release files: " + ", ".join(missing))
    drifted = []
    for name, path in prompt_sources.items():
        relative = path.relative_to(repo).as_posix()
        expected = subprocess.check_output(
            [git, "-C", str(repo), "rev-parse", f"{ref}:{relative}"], text=True).strip()
        actual = subprocess.check_output(
            [git, "-C", str(repo), "hash-object", str(path)], text=True).strip()
        if expected != actual:
            drifted.append(name)
    if drifted:
        raise SystemExit(
            "working-tree prompt files do not match --ref: " + ", ".join(drifted))

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = hermes_home / "backups" / f"ancor-install-{timestamp}"
    if not args.dry_run:
        backup.mkdir(parents=True, exist_ok=False)
        manifest = {
            "ref": ref,
            "source": source,
            "profiles": profiles,
            "files": {name: sha256(path) for name, path in prompt_sources.items()},
        }
        (backup / "release.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for profile in profiles:
        run([hermes, "-p", profile, "plugins", "install", source,
             "--ref", ref, "--force", "--yes-deps", "--enable"],
            hermes_home=hermes_home, dry_run=args.dry_run)

    if not args.dry_run:
        staged = Path(tempfile.mkdtemp(prefix=".ancor-prompts-", dir=hermes_home))
        try:
            obsolete_paths = []
            for role, skills in OBSOLETE_SKILLS.items():
                for skill in skills:
                    obsolete = profile_home(hermes_home, f"ancor-{role}") / "skills" / skill
                    if not obsolete.exists():
                        continue
                    backup_path = backup / "prompts" / f"ancor-{role}" / "skills" / skill
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(obsolete, backup_path)
                    obsolete_paths.append(obsolete)
            for name, source_path in prompt_sources.items():
                role, relative = name.split("/", 1)
                destination = profile_home(hermes_home, role) / relative
                staged_path = staged / name
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, staged_path)
                if destination.exists():
                    backup_path = backup / "prompts" / name
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination, backup_path)
            for name in prompt_sources:
                role, relative = name.split("/", 1)
                destination = profile_home(hermes_home, role) / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged / name, destination)
            for obsolete in obsolete_paths:
                shutil.rmtree(obsolete)
        finally:
            shutil.rmtree(staged, ignore_errors=True)

    for name, source_path in prompt_sources.items():
        role, relative = name.split("/", 1)
        installed = profile_home(hermes_home, role) / relative
        if not args.dry_run and sha256(installed) != sha256(source_path):
            raise SystemExit(f"prompt verification failed: {installed}")

    if not args.no_restart:
        run([hermes, "-p", args.owner_profile, "gateway", "restart"],
            hermes_home=hermes_home, dry_run=args.dry_run)
    print(f"ANCOR {ref} installed into {', '.join(profiles)}")
    if not args.dry_run:
        print(f"Backup and release manifest: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
