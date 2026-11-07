# ANCOR for Hermes

ANCOR turns substantial work into durable, inspectable requests and coordinates
them through three roles:

- **Interactor** talks with the user, handles small work directly, and records
  substantial requests and human decisions through confirmed ANCOR interfaces.
  It resolves ambiguous references before drafting and maintains self-contained
  request versions.
- **Coordinator** investigates confirmed requests, validates uncertain approaches,
  organizes native Kanban cards, coordinates workers, and verifies their results.
- **Worker** completes one bounded card and returns concrete artifacts and
  evidence, reusing suitable tools and keeping implementation within the card.

ANCOR keeps the request's current intent, point-level history, questions,
decisions, notes, task links, notifications and verification evidence in a
durable store. Human intent and authorization always require explicit native
confirmation. The read-only `/ancor` dashboard shows current work without
exposing private coordinator notes or confirmation details.

Worker completion, coordinator verification and human acceptance are separate
steps. Questions and verified completion notices currently go from the
coordinator through a durable outbox to the Gateway for delivery; they do not
pass through an interactor model turn before reaching the user.

## Install

Follow [SETUP.md](SETUP.md) for profile configuration, installation, upgrades and
activation checks. The repository installer deploys a fixed Git commit of the
plugin and its matching role prompts to an existing Hermes instance.

From a checkout of the reviewed commit, preview the installation:

```sh
export HERMES_HOME=/absolute/path/to/.hermes
ANCOR_REF="$(git rev-parse HEAD)"
python3 scripts/install_ancor.py \
  --hermes-home "$HERMES_HOME" --ref "$ANCOR_REF" --dry-run
```

Installation replaces plugin files and role prompts; it does not merge local
customizations or configure models, channels and shared ANCOR settings. Its
automatic backup covers replaced prompts, not the complete running instance.
Review existing installation differences before upgrading.

## Development

Use a Python environment with `pytest` installed, from the repository root:

```sh
env -u PYTHONPATH python3 -m pytest tests -q
```

Native integration checks additionally need a compatible Hermes checkout and
its dependencies; some are skipped when those are unavailable. Set
`ANCOR_HERMES_SOURCE` for checks that support an alternate source path.

- [Request model](docs/atomic-requests.md)
- [Plugin settings and declared interfaces](ancor/plugin.yaml)
- [Isolated real-model trial runner](scripts/isolated_trial.py): use a new,
  disposable Hermes home; model calls use the configured provider and credentials.
