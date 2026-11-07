# Installation

The repository installer deploys the plugin and role prompts from one reviewed
Git commit. It assumes an existing Hermes installation and configured profiles.

## Prepare the Hermes instance

Use a compatible Hermes installation with native Kanban, `clarify`, plugin
hooks and the Gateway `api_server` lifecycle adapter. ANCOR starts its polling
and notification loops through that adapter on the configured owner profile.
Put `git`, `hermes` and Python 3 on `PATH`.

Choose the Hermes **root**, not a role's profile directory:

```sh
export HERMES_HOME=/absolute/path/to/.hermes
```

Use this environment value for the manual Hermes commands below. The installer
treats `--hermes-home` as authoritative: it resolves that path and passes it as
`HERMES_HOME` to every plugin-install and Gateway-restart subprocess, alongside
using it for prompt and backup paths. It does not change the invoking shell's
environment. The dry-run preview includes this explicit root for each command.

Check existing profiles with `hermes -p default profile list`. Create any missing
role profiles before installing; run only the applicable creation commands:

```sh
hermes -p default profile create ancor-interactor
hermes -p default profile create ancor-coordinator
hermes -p default profile create ancor-worker
```

Configure each role's model, credentials, workspace access and tool availability.
Use this four-profile inventory for installation and verification:

| Profile | ANCOR plugin | Installed role prompts | Runtime responsibility |
| --- | --- | --- | --- |
| Owner (`default`) | Required | None; preserve owner's existing prompts | Gateway loops, connected `api_server`; shared-bot transport in the example below |
| `ancor-interactor` | Required | `roles/ancor-interactor/SOUL.md`, `skills/ancor-interactor/SKILL.md` | User conversation, ANCOR and native `clarify` |
| `ancor-coordinator` | Required | `roles/ancor-coordinator/SOUL.md`, `skills/ancor-coordination/SKILL.md` | ANCOR, investigation and verification |
| `ancor-worker` | Required | `roles/ancor-worker/SOUL.md`, `skills/ancor-worker/SKILL.md` | Native Kanban, execution and delegation |

The installer uses these three fixed role names. The owner is `default` unless
selected with `--owner-profile`; it must be distinct from the role profiles.
Ensure role skills are loaded, for example with `skills.auto_load` in each
role's configuration (`ancor-interactor`, `ancor-coordination`, `ancor-worker`,
respectively). The owner plugin goes into `$HERMES_HOME/plugins/ancor` when the
owner is `default`; role plugins go into
`$HERMES_HOME/profiles/<profile>/plugins/ancor`. Each role's SOUL goes at its
profile root and its skill under `skills/<skill>/SKILL.md`.

Configure the native Kanban board and worker dispatcher in Hermes. The installer
does not create profiles or provision models, tools, dispatcher settings or
messaging channels. Completing the configuration below is part of installation.
Enable ANCOR and `clarify` in the interactor's actual channel toolset (for
example `platform_toolsets.feishu`), not just its CLI toolset. The coordinator
needs ANCOR in CLI turns, and native worker turns need Kanban and execution tools.

## Configure shared ANCOR settings

Merge the following into the owner's `config.yaml`, using real paths and a
Hermes notification destination. Preserve other plugin entries and settings.

```yaml
plugins:
  enabled: [ancor]
  entries:
    ancor:
      enabled: true
      settings:
        store_path: /absolute/path/to/.hermes/ancor/requests.db
        board: ancor
        owner_profile: default
        gateway_owner: true
        interactor_profile: ancor-interactor
        coordinator_profile: ancor-coordinator
        worker_profile: ancor-worker
        human_target: "<platform>:<destination>"
        transport_profile: default
        poll_interval: 30
```

Use the same settings in all role profiles, with `gateway_owner: false` there.
Only the owner runs the ANCOR background loops. All profiles must share the
same absolute `store_path` and board; create the store's parent directory with
appropriate access. Existing installations should keep their actual store
path, even if its filename differs from this example.

`transport_profile` selects the connected messaging adapter used for delivery.
It does not invoke that profile's model. Questions and completion notices are
currently sent directly by the Gateway, without an interactor rewriting step.
Missing destination or transport configuration prevents notification delivery.
See [plugin.yaml](ancor/plugin.yaml) for all declared settings.

## Connect user conversations and notifications

For a shared bot, use `default` as the owner and transport profile. Configure
the bot once on `default`, then route its ANCOR conversation to
`ancor-interactor`. Coordinator and worker do not need their own human-facing
bots. Copying the same bot credentials into multiple profiles can cause Hermes
to park the duplicate adapters instead of serving them.

Use `hermes -p default gateway setup` to configure the messaging platform,
credentials and inbound access policy. For Feishu, complete the app's bot
permissions and event subscription setup as well. Enable and configure the
owner's `api_server` adapter, including its required API key; ANCOR's lifecycle
hook needs that adapter connected even when user messages arrive via Feishu.
Store a generated, non-placeholder `API_SERVER_KEY` of at least 16 characters
in the owner's `.env`; keep the API listener on loopback as in the example.
Follow the installed Hermes version's platform setup for the bot credentials.
With a custom owner, verify that its Gateway actually invokes that profile's
`api_server` lifecycle hook; changing `owner_profile` alone is insufficient.

Merge this routing example into the owner's `config.yaml`, preserving existing
routes. Replace the chat ID with the exact ID reported by the receiving bot:

```yaml
platforms:
  api_server:
    enabled: true
    extra:
      host: 127.0.0.1
gateway:
  multiplex_profiles: true
  profile_routes:
    - name: ancor-user-chat
      platform: feishu
      chat_id: "oc_REPLACE_WITH_CHAT_ID"
      profile: ancor-interactor
```

Then set these ANCOR settings consistently in all four profiles:

```yaml
human_target: "feishu:oc_REPLACE_WITH_CHAT_ID"
transport_profile: default
```

These two keys belong under `plugins.entries.ancor.settings`, not at the YAML
root. `human_target` is the outbound destination; `gateway.profile_routes`
selects who handles inbound messages. Set both, so a reply to a notification
reaches the interactor. Platform IDs and destination syntax differ for other
channels; use Hermes' platform-specific target format. The bot must be able to
send to the chosen destination, and the receiving bot's access policy must
admit the user's reply. In groups, check mention requirements as well.
For Feishu DMs, an outbound `feishu:ou_<open_id>` target is also supported;
inbound routes must use the actual DM `chat_id` or matching `user_id`, not an
open ID in the `chat_id` field. Open IDs belong to the sending bot's app scope.

The example routes messages received by the default bot. A dedicated bot on
another profile needs its own credentials and a connected adapter; select that
profile as `transport_profile` and configure its inbound routing separately
(`bot_profile` selects a non-default receiving bot in shared-bot routes).
Setting `transport_profile: ancor-interactor` alone does not create an adapter
for a profile that only receives routed conversations. Outbound notifications
also do not guarantee an entry in the interactor's conversation history; the
durable ANCOR question and notification records remain available to it.

## Install a reviewed version

Run from the repository checkout whose role prompt files match the selected
commit. `--ref` accepts a full 40-character commit ID; omitting it uses `HEAD`.
The plugin is installed from Git, while prompt files are copied from the working
tree after checking that they match that commit.

```sh
ANCOR_REF="$(git rev-parse HEAD)"
python3 scripts/install_ancor.py \
  --hermes-home "$HERMES_HOME" --ref "$ANCOR_REF" --dry-run
```

After reviewing the preview, install:

```sh
python3 scripts/install_ancor.py \
  --hermes-home "$HERMES_HOME" --ref "$ANCOR_REF"
```

Pass `--owner-profile <name>` to both commands when using a different owner.
The installer installs/enables the plugin in all four profiles, backs up and
replaces the six role SOUL/SKILL files, verifies the installed prompt hashes,
and requests a Gateway restart. It prints the backup location under
`backups/ancor-install-<timestamp>/`, including `release.json` with the commit
and expected prompt hashes. Installation is sequential; a failure can leave
some profiles updated, so inspect the failed step before retrying.

## Upgrades and activation

Before replacing an existing installation, compare its plugin and prompt files
with the selected release. The installer force-replaces plugin files and
replaces prompts verbatim; it does not detect or merge live customizations.
Reconcile those differences first. Preserve a separate backup of installed
code, configuration and data; the automatic backup contains role prompts and
the release manifest only. For a consistent snapshot of request/notification
databases and native Kanban, stop their writers first.

`--no-restart` skips only the final Gateway restart command. Hermes plugin
installation may still reload or activate plugins in the running Gateway;
this flag is not an offline staging mode. Inspect Hermes' activation output:
Python tools may be deferred to a new session, and existing conversations keep
their loaded instructions. Start fresh role sessions to use updated prompts.
ANCOR's existing background scanner survives plugin reloads; changes to its
running host/scanner logic require a Gateway restart.

## Verify the complete installation

Check every profile, not just the one used to run the installer. Substitute a
custom owner for `default` if applicable:

```sh
for ANCOR_PROFILE in default ancor-interactor ancor-coordinator ancor-worker; do
  hermes -p "$ANCOR_PROFILE" plugins list
done
hermes -p default gateway status
```

All four must show ANCOR installed and enabled from the selected release, and
the installer must finish its six prompt hash checks. Check role skill loading
and shared settings against the inventory above. Gateway status/logs should
show the configured owner, interactor and transport profile served, the
messaging and `api_server` adapters connected, and no ANCOR startup or delivery
errors.

Then exercise a small, deliberately authorized request in the intended chat:

1. A user message reaches the interactor, which presents native confirmation.
   Confirming records exactly one request in the shared store.
2. The owner wakes the coordinator; the native dispatcher runs a card under
   `ancor-worker` on the configured board.
3. A question or verified-completion notification arrives in the intended chat
   through the configured bot. Check the delivery receipt, not merely `pending`.
4. The user's reply reaches the interactor and the confirmed answer or acceptance
   updates the correct question/request. Delivery alone is not acceptance.

This checks inbound routing, execution, outbound delivery and the return path.
The [isolated trial runner](scripts/isolated_trial.py) checks models and workflow
with simulated confirmation callbacks and notification delivery, driving the
host directly. It cannot verify real channel routing, native confirmation UI
or the owner's Gateway lifecycle hook. Real-model checks can incur provider costs.
