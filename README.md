# ANCOR for Hermes

ANCOR turns substantial work into durable, inspectable requests and coordinates
them through three roles:

- **Interactor** talks with the user, handles small work directly, and records
  substantial requests and human decisions through confirmed ANCOR interfaces.
- **Coordinator** investigates and plans confirmed requests, creates coherent
  Kanban cards, coordinates workers, and verifies their results.
- **Worker** completes one bounded card and returns concrete artifacts and
  evidence.

ANCOR keeps the request's current intent, point-level history, questions,
decisions, notes, task links, notifications and verification evidence in a
durable store. Human intent and authorization always require explicit native
confirmation. The read-only `/ancor` dashboard shows current work without
exposing private coordinator notes or confirmation details.

## Install

Use the repository installer. It installs the version-pinned plugin into the
owner and role profiles, synchronizes the matching SOUL and SKILL files, backs
up replaced prompt files, verifies their hashes, and restarts the Gateway.

```sh
python scripts/install_ancor.py \
  --hermes-home /absolute/path/to/.hermes \
  --ref <40-character-reviewed-commit>
```

Use `--dry-run` to inspect commands first and `--no-restart` when a controlled
restart is handled separately. The installer does not migrate request data;
take a backup and choose imports separately when upgrading an existing store.

The installer replaces the old manual sequence. Do not install only the Hermes
plugin: ANCOR's runtime and its role prompts are one release.

## Development

```sh
env -u PYTHONPATH python -m pytest tests -q
```

The request model is described in [docs/atomic-requests.md](docs/atomic-requests.md).
Use [scripts/isolated_trial.py](scripts/isolated_trial.py) for disposable
real-model trials; do not point a trial at a live Hermes home.
