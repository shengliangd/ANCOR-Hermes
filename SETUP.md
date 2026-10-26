# Installation

The supported installation path is the repository installer:

```sh
python scripts/install_ancor.py \
  --hermes-home /absolute/path/to/.hermes \
  --ref <40-character-reviewed-commit>
```

It installs the plugin into the configured owner profile and the three role
profiles (`ancor-interactor`, `ancor-coordinator`, `ancor-worker`). It then
copies the matching role SOUL/SKILL files, records a release manifest and
backup, verifies every copied file, and restarts the Gateway. `--dry-run` shows
the commands without changing the Hermes home; `--no-restart` leaves the final
restart to the operator.

The shared ANCOR settings remain under `plugins.entries.ancor.settings` in the
owner and role profile configuration. The installer preserves those settings;
the configured owner profile hosts Gateway lifecycle work.

The installer updates code and role instructions. It does not migrate or
replace request databases or native Kanban boards. Back up the existing
instance before an upgrade, then import selected historical requests through a
separate reviewed procedure.

For isolated model trials, use `scripts/isolated_trial.py` with a disposable
Hermes home and notification target. Existing conversations retain their
original system instructions; start a fresh session after installing a new
role release.
