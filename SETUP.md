# Installation and verification

This repository is a source package, not an authorization to change a running
Hermes installation. Use a disposable installation for validation first. Linux is
required for the coordinator's `flock`/process lifecycle implementation.

## Profiles and package

Create native profiles `ancor-interactor`, `ancor-coordinator` and `ancor-worker`, with explicitly selected
models and separately provisioned credentials. Keep bot credentials with the
Gateway owner; do not copy the default `.env` wholesale.

Install the `ancor` subdirectory using native plugin installation, pinned to the
reviewed commit, into the Gateway owner and every profile that runs an ANCOR role
(installed once when the owner is also a role profile). Inspect the installed
`hermes plugins install --help` before provisioning. The source form is:

```text
file:///absolute/path/to/ANCOR-Hermes-native-config#ancor
```

Install each role's `SOUL.md` in its profile home, the complete
`skills/ancor-coordination` directory in the coordinator profile, the complete
`skills/ancor-interactor` directory in the interactor profile, and the complete
`skills/ancor-worker` directory in the worker profile. Preserve unrelated
configuration, memory and routes. Workers receive no ANCOR business tools; their
native task card is the complete execution contract for that bounded task, not a
copy of the whole requirement. This is cooperative behavior,
not process isolation.

Load each role skill on every session, including one-shot coordinator runs:

```sh
hermes -p ancor-coordinator config set skills.auto_load '["ancor-coordination"]'
hermes -p ancor-worker config set skills.auto_load '["ancor-worker"]'
```

The coordinator is spawned as a one-shot CLI process, so relying on the model to
call `skill_view` on demand is not sufficient. Read the setting back after
installation.

## Sync role files after a source update

Installing the plugin does not update the profile copies of `SOUL.md` or the
role skills. Treat those copies as a separate deployment artifact. Updating the
source checkout without syncing them leaves the running roles on old
instructions even when the plugin code is current.

Plugin code and role files are also separate copies. Updating the source
checkout does not update `~/.hermes/plugins/ancor`. After a reviewed change,
update or reinstall the plugin as well (`hermes plugins update ancor` for a
tracked install, or reinstall from the pinned source URL), then confirm
`hermes plugins show ancor` and run `hermes plugins doctor ancor`. A tool added
to the plugin is unavailable until the serving Gateway reloads the plugin.
The ANCOR inbox scanner is Gateway-lifetime state, so it survives a plugin
reload; scanner code changes themselves still take effect only after a Gateway
restart.

After checking out a reviewed commit, sync only the role files; do not replace
the whole profile:

```sh
ROOT=/absolute/path/to/ANCOR-Hermes
BACKUP="$HOME/.hermes/profiles/.ancor-backup-$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP/ancor-coordinator" "$BACKUP/ancor-worker"

cp -a "$HOME/.hermes/profiles/ancor-coordinator/SOUL.md" "$BACKUP/ancor-coordinator/SOUL.md"
cp -a "$HOME/.hermes/profiles/ancor-coordinator/skills/ancor-coordination/SKILL.md" "$BACKUP/ancor-coordinator/SKILL.md"
cp -a "$HOME/.hermes/profiles/ancor-worker/SOUL.md" "$BACKUP/ancor-worker/SOUL.md"
cp -a "$HOME/.hermes/profiles/ancor-worker/skills/ancor-worker/SKILL.md" "$BACKUP/ancor-worker/SKILL.md"

mkdir -p "$HOME/.hermes/profiles/ancor-coordinator/skills/ancor-coordination"
mkdir -p "$HOME/.hermes/profiles/ancor-worker/skills/ancor-worker"
cp "$ROOT/roles/ancor-coordinator/SOUL.md" "$HOME/.hermes/profiles/ancor-coordinator/SOUL.md"
cp "$ROOT/skills/ancor-coordination/SKILL.md" "$HOME/.hermes/profiles/ancor-coordinator/skills/ancor-coordination/SKILL.md"
cp "$ROOT/roles/ancor-worker/SOUL.md" "$HOME/.hermes/profiles/ancor-worker/SOUL.md"
cp "$ROOT/skills/ancor-worker/SKILL.md" "$HOME/.hermes/profiles/ancor-worker/skills/ancor-worker/SKILL.md"
```

Verify the installed copies and the auto-load setting before restarting any
session:

```sh
diff -q "$ROOT/roles/ancor-coordinator/SOUL.md" "$HOME/.hermes/profiles/ancor-coordinator/SOUL.md"
diff -q "$ROOT/skills/ancor-coordination/SKILL.md" "$HOME/.hermes/profiles/ancor-coordinator/skills/ancor-coordination/SKILL.md"
diff -q "$ROOT/roles/ancor-worker/SOUL.md" "$HOME/.hermes/profiles/ancor-worker/SOUL.md"
diff -q "$ROOT/skills/ancor-worker/SKILL.md" "$HOME/.hermes/profiles/ancor-worker/skills/ancor-worker/SKILL.md"
hermes -p ancor-coordinator config get skills.auto_load
hermes -p ancor-worker config get skills.auto_load
```

The coordinator reads these files when it spawns a new one-shot session. A
coordinator process already in flight keeps the prompt it started with; the next
spawn uses the synced files. Restart existing role sessions and the serving
Gateway after syncing a changed installation so no cached profile state survives.

## Disable automatic self-improvement

Keep ANCOR role instructions and skills fixed to the reviewed repository version.
Disable automatic post-turn review and skill curation in both role profiles:

```sh
hermes -p ancor-interactor config set auxiliary.background_review.enabled false
hermes -p ancor-interactor config set curator.enabled false
hermes -p ancor-coordinator config set auxiliary.background_review.enabled false
hermes -p ancor-coordinator config set curator.enabled false
hermes -p ancor-worker config set auxiliary.background_review.enabled false
hermes -p ancor-worker config set curator.enabled false
```

These are native profile settings, not plugin settings. Apply them before starting
sessions; restart existing role sessions and the serving Gateway after changing
an existing installation. Read back both profiles' configuration. If automatic
improvement already changed installed skills or instructions, review the diff and
restore the approved repository versions before validation; disabling it does not
undo earlier edits. This disables automatic maintenance, not explicit `/refine`
or all file-writing tools. Re-enabling self-improvement is a separate future
experiment, not part of the current setup.

## Settings

Use `hermes config set` in the appropriate native profile. Plugin settings live
under `plugins.entries.ancor.settings`; confirm the installed configuration reader and
plugin doctor before starting:

| Setting | Scope | Value |
| --- | --- | --- |
| `store_path` | all three | Same absolute durable SQLite path, outside plugin code |
| `board` | all three | Dedicated native board, e.g. `ancor` |
| `interactor_profile` | all three | `ancor-interactor` |
| `coordinator_profile` | all three | `ancor-coordinator` |
| `worker_profile` | all three | `ancor-worker` |
| `owner_profile` | all three | Actual Gateway-owning profile |
| `gateway_owner` | owner only | `true`; keep `false` in role profiles |
| `poll_interval` | owner | At least 30 seconds |
| `human_target` | coordinator | Explicit native `platform:destination` |
| `transport_profile` | owner | Served Gateway profile owning that transport |

The coordinator only durably enqueues notifications. The Gateway owner claims
them in a background loop and sends through Hermes' generic `send_message_tool`
under `transport_profile`; no per-notification CLI process is started. A send
returns `pending`, not `sent`. Failed sends retain safe structured diagnostics
in the outbox `error` field (exception class and reason code), never raw output,
message text or credentials. `sending` and `uncertain` entries are not retried
automatically: reconcile transport evidence before any retry. An item that found
no live transport adapter remains `pending` and is retried with bounded backoff.
Unanswered questions are closed automatically when their requirement is
cancelled, invalidated or accepted, or when their linked task is no longer
blocked. Closing a question is not an answer and cannot be used as human consent.

## Upgrade an existing store to requirement titles

ANCOR 0.2 makes `title` a required, authoritative field on drafts and
confirmed requirements. It is not derived from the brief at runtime. The plugin
fails closed when it opens a legacy store; run the one-time migration before
starting the updated plugin:

```sh
python -m ancor.migrate_titles export --db /absolute/path/requirements.db \
  --out /absolute/path/ancor-titles.json
# Fill every `title` in the exported drafts and requirements entries.
python -m ancor.migrate_titles apply --db /absolute/path/requirements.db \
  --titles /absolute/path/ancor-titles.json
```

The export contains the exact legacy context and an empty title for every record.
An agent or human fills every title; the migration deliberately does not derive
one. `apply` rejects missing, extra, duplicate or invalid titles before writing,
requires every draft and requirement ID exactly once, and creates a timestamped
SQLite backup beside the store before rebuilding the two title-bearing tables.
Requirement IDs, statuses, events, task links and notification state are
preserved. Stop the Gateway or otherwise quiesce ANCOR before applying the
migration, then restart it with the updated plugin.

### Coordinator notification channel

Background questions and completion notices fail closed unless the coordinator
has `human_target` and the Gateway owner has `transport_profile`. The coordinator
is the only role that enqueues them; the owner delivers from the durable outbox,
and the interactor reads it to relay answers.

```sh
hermes -p ancor-coordinator config set plugins.entries.ancor.settings.human_target 'telegram:-1001234567890'
hermes -p default config set plugins.entries.ancor.settings.transport_profile default
```

Use the exact destination syntax accepted by the selected native transport, for
example `telegram:<chat_id>`, `discord:#ops`,
`slack:C0123ABCD` or `feishu:oc_<chat_id>`. The destination must include a
non-empty platform and destination separated by `:`.

`transport_profile` must name a profile served by the same Gateway whose native
`.env`/`config.yaml` contains that channel's credentials and whose platform
adapter is connected. If the bot credentials live only with the Gateway owner,
set that owner's profile name here; do not copy the default `.env` into role
profiles. A CLI-only send test is insufficient: the production path uses the
Gateway's live adapter. Pointing at a profile without a connected adapter leaves
questions pending instead of silently falling back to a cold CLI process.

Verify the exact transport path used by the outbox before enabling the
coordinator:

```sh
printf 'ANCOR channel test' | hermes --profile default send --to 'telegram:-1001234567890' --file - --json
```

Replace the profile and destination with the configured values. This checks the
underlying native transport, but production uses the live Gateway adapter through
the generic send API. Verify the same platform is connected for
`transport_profile`, then submit one real question and confirm its outbox state
becomes `sent`. `sent` means the native transport accepted the message, not that
a human read or answered it.

Enable the plugin/toolset in the selected profiles. The owner registers lifecycle
infrastructure, not business tools. All three role profiles receive the normal
Hermes work surface. Replace the invalid `all` sentinel in every saved
`platform_toolsets` list with explicit toolset names; Hermes silently ignores an
unknown toolset, which would leave the role with only its named ANCOR tools.

Baseline: `web browser terminal file code_execution vision image_gen tts skills
todo memory session_search connections computer_use`.

| Role | Additional toolsets | Must not enable |
| --- | --- | --- |
| interactor | `ancor`, `clarify`, `delegation`, `cronjob` | native `kanban`; use `ancor_board list/show` for read-only progress |
| coordinator | `ancor`, `kanban`, `delegation` | `clarify`, `cronjob`; use `ancor_notify` and `ancor_wake` |
| worker | `kanban` | `ancor`, `clarify`, `cronjob`, `delegation`; the native task card is its entire bounded-task contract |

Apply the same matrix to every platform that routes to a role; at minimum `cli`
for coordinator/worker and both `cli` and `feishu` for interactor.

```sh
BASE_TOOLSETS="web browser terminal file code_execution vision image_gen tts skills todo memory session_search connections computer_use"
hermes -p ancor-coordinator tools enable $BASE_TOOLSETS ancor kanban delegation --platform cli
hermes -p ancor-coordinator tools disable clarify cronjob --platform cli
hermes -p ancor-worker tools enable $BASE_TOOLSETS kanban --platform cli
hermes -p ancor-worker tools disable clarify cronjob delegation --platform cli
hermes -p ancor-interactor tools enable $BASE_TOOLSETS ancor clarify delegation cronjob --platform feishu
hermes -p ancor-interactor tools disable kanban --platform feishu
hermes -p ancor-interactor tools enable $BASE_TOOLSETS ancor clarify delegation cronjob --platform cli
hermes -p ancor-interactor tools disable kanban --platform cli
```

The worker does not register the `ancor` toolset: it must not read the requirement
store or use ANCOR business tools. `tools list` for the worker should show `kanban`
and no `ancor` entry.

### Unattended approvals

The Gateway starts coordinator and worker runs with `-q`, so no human is present to
answer a dangerous-command approval prompt. Make that policy explicit for these two
profiles; otherwise a verification command can wait for the approval timeout and
fail closed:

```sh
hermes -p ancor-coordinator config set approvals.single_query_mode approve
hermes -p ancor-worker config set approvals.single_query_mode approve
```

This changes only command-approval behavior for unattended `-q` turns. It does not
grant requirement authorization, widen a task contract, or bypass hardline denies.
If local policy requires `deny`, add the specific verification commands to the
top-level `command_allowlist` instead, and test the actual worker flow end to end.
Do not use `approvals.mode off` or `--yolo` as a substitute for a bounded profile.

The coordinator and interactor may use native `delegate_task` for bounded
analysis; the worker may not delegate. A child inherits the parent's tools and
permissions, does not inherit the role SOUL/skill, and cannot use `clarify`,
`send_message`, or native Kanban. Put the investigation boundary and return
contract in every delegation context; the parent verifies the result and makes
all durable changes. The interactor's task-board path stays read-only through
`ancor_board list` / `ancor_board show`; its normal tools are for analysis, not
for bypassing requirement confirmation or recording unconfirmed human input.

### Gateway owner and API server

The current lifecycle host is the owner's native `api_server` platform connection.
Configure it in the profile that actually serves the Gateway; named-profile
request routing alone does not start owner lifecycle handlers. For a conventional
installation the owner is `default`; substitute the real owner name everywhere.

```sh
OWNER=default
hermes -p "$OWNER" config set plugins.entries.ancor.settings.owner_profile "$OWNER"
hermes -p "$OWNER" config set plugins.entries.ancor.settings.gateway_owner true
hermes -p "$OWNER" config set plugins.entries.ancor.settings.poll_interval 30
hermes -p "$OWNER" config set platforms.api_server.enabled true
```

Set a strong `API_SERVER_KEY` (at least 16 characters, not a placeholder) in the
owner profile's environment; `hermes -p "$OWNER" config env-path` prints the file.
The adapter refuses to start without it even on a loopback bind. Optional listener
overrides are `platforms.api_server.extra.host` and
`platforms.api_server.extra.port` (defaults `127.0.0.1:8642`).

Restart the serving Gateway, then check that the owner's `api_server` listener is
present:

```sh
hermes -p "$OWNER" gateway restart
hermes -p "$OWNER" gateway status
```

In a multiplex Gateway, per-profile URLs appear as `/p/<profile>/v1`; an absent
`api_server` entry means the host cannot invoke ANCOR, usually because the key was
rejected or the adapter is disabled. No extra standalone scheduler or foreground
coordinator session is required.

Before calling the Gateway ready, verify all of the following:

- `owner_profile` names the profile that serves the Gateway, and only that profile
  has `gateway_owner=true`.
- That profile has `api_server` enabled and connected, with `poll_interval >= 30`.
- The `ancor` plugin and the correct role toolsets are enabled in the owner and in
  all three role profiles.
- Each role's `hermes -p <role> tools list` matches the matrix: coordinator and
  interactor have `delegation`; worker does not. Coordinator and worker lack
  `clarify`/`cronjob`; interactor lacks native `kanban`.
- Native Kanban dispatch is running: either `kanban.dispatch_in_gateway true` in
  the owner, or one separate `hermes kanban daemon`. With review dispatch disabled,
  it must still be able to spawn `ancor-worker` for ordinary tasks.
- The selected user conversation routes to `ancor-interactor`, so native `clarify`
  reaches the user and the interactor can read the board for progress reporting.

Create the dedicated board through native board commands; do not change the
installation-wide current board. Turn off creator auto-subscription in the ANCOR
profiles. Do not add user-facing task subscriptions: questions and delivery are
coordinator decisions, not direct worker event notifications. Audit existing ANCOR
subscriptions before reusing an old board. This setup flow does not copy business
state automatically. An existing requirements database is reused in place only
after the explicit title migration above. On first open, ANCOR 0.4 also upgrades
requirements that the old coordinator-only flow marked `accepted`: it preserves
the original `requirement.accepted` event, records the retained evidence as
`requirement.verified`, and moves the requirement to `awaiting_acceptance` for
the human final gate. The upgrade is idempotent and leaves current human-confirmed
acceptances unchanged.

Disable automatic root decomposition for this workflow: the coordinator creates only
execution tasks, progressively, through the plugin board tool. Native dispatch
still owns execution. Preserve unrelated boards' decomposition settings.

In the Gateway owner profile, run `hermes config set kanban.review_dispatch false`.
This is an owner-wide dispatcher setting, not a per-board switch: use a dedicated
owner if unrelated boards need native review dispatch. Workers finish execution
with `kanban_complete`, not `kanban_request_review`. The coordinator independently
verifies completed artifacts through `ancor_acceptance`, moving the requirement to
`awaiting_acceptance`; the human-facing role then makes the final decision. No native
review stage or coordinator task-completion tool is needed. Disabling review dispatch does not
repair existing review/failed tasks; preserve their evidence for operator review.

Route the selected user conversation to `ancor-interactor` using native profile
routing. Present and confirm the draft in that current conversation using native
`clarify`. Confirmation needs no separate message destination, delivery receipt or
plugin-imposed length limit. `human_target` and `transport_profile` are only for
coordinator-selected background questions and completion notices; they are not
prerequisites for requirement confirmation.

Confirmation stages a transition instead of committing it immediately. A
requirement with non-empty `target_status` is frozen: it cannot create, edit,
unblock or associate cards, or schedule wakes. The coordinator calls
`ancor_requirement action=advance`; cancellation and replacement use native
archive on linked cards, and replacement activates the new requirement only
after every old card is archived. A shared card still used by another stable
active or paused requirement is reported as a blocker rather than archived.

Pause and resume use the same exact native `clarify` confirmation path as drafts
and cancellations. Confirmation requests the pause; `advance` blocks linked
running or ready tasks, verifies executor quiescence, then commits `paused`. A
paused requirement suppresses scheduled wakes, is reported as `paused` rather
than `stalled`, and cannot create new work. Resume is staged too: after
`advance`, the coordinator re-evaluates existing work before unblocking it. No
additional configuration is required.

Final human acceptance uses the same exact native `clarify` path.
`ancor_requirement action=accept` commits `accepted`; `action=request_changes`
requires exact feedback, returns the requirement to `active`, closes the obsolete
completion notice and wakes the coordinator for bounded repair. While a requirement
is `awaiting_acceptance`, it is open work for progress questions but does not create
coordinator health alerts, liveness pressure or habitual wakes.

## Restart recovery contract

On the first scan that acquires the existing coordinator flock, the host reads
current requirements, native tasks/events, acceptance evidence and the existing
notification outbox. Pending events already trigger reconciliation. If the inbox
is empty but active or paused requirements, undelivered acceptances or unresolved notices
remain, it records one durable `host.recovery` event. The derived `host.stalled`
condition does not suppress this startup recovery. Acknowledging recovery makes
subsequent idle polls quiet; it is not recreated on every poll. A later host
startup may reconcile still-unresolved work again. Drafts alone do not wake a
model. A sent completion is neither recreated nor resent.

Acceptance criteria for recovery:
- Confirmed work resumes without human resubmission, including an interrupted
  coordinator that acknowledged its original event before finishing coordination.
- A surviving coordinator retains its inherited flock; a restarted host waits.
  Existing native task keys/claims remain authoritative; recovery does not dispatch
  workers itself or replace native scheduling.
- Durable worker outcome events from downtime are ingested using per-task cursors
  and deduplicated source keys.
- Acceptance before notification enqueue wakes the coordinator with original evidence;
  only the coordinator decides the real completion text and enqueues the stable ID.
- Pending notices remain pending; `sending`/`uncertain` require transport evidence
  and operator reconciliation, never blind retries or replacement IDs.
- No continuous idle model calls. Unhandled identical batches retain the existing
  three-attempt/hour cooldown; the newest event gets a reserved batch slot so new
  input cannot hide behind a full cooling head batch.
- Progress is judged from real work: running with a fresh heartbeat, or a blocked
  task with a delivered unanswered human question. Triage, stale heartbeat,
  blocked without an open question, and blocked after its question was answered
  are stalled. Scheduled wakes are timers. `host.stalled` is host-owned, is never
  manually acknowledged, and clears only when the requirement recovers. The host
  keeps its condition ledger (`reason`, `since`, `generation`, `last_woken_at`) in
  `requirements.db.host.json`; an unchanged condition wakes at most once per hour,
  while a reason change resets the backoff. Ordinary new events do not reset it.

These are durable wake/reconciliation guarantees, not guarantees that an arbitrary
model successfully completes work. Recovery does not infer acceptance or delivery.
The host reads the outbox without requiring transport credentials/configuration.
Crash tests use real OS processes and locks with scripted coordinator decisions; native
history reads use the installed read-only API. They are not actual-model E2E.

## Verification gates

1. Run `env -u PYTHONPATH python -m pytest tests -q` in the source checkout.
2. Verify native plugin discovery/registration under disposable profile homes.
3. Verify exact-draft and exact-cancellation confirmation via native tool execution;
   cancellation, timeout, changed questions and replay must not activate or cancel a
   requirement.
4. Verify real native Kanban creation, idempotent retry, blocking and readback.
5. Verify one coordinator across processes, cancellation/restart ownership,
   event replay and no idle model calls.
6. Exercise an actual model-backed workflow: confirmation → rolling tasks →
   blocker remedy → confirmed replacement → artifact verification → user delivery.
   Include failed acceptance, competing work, restart and reply recovery.

Synthetic tests do not establish model behavior or real transport delivery. Do not
call the installation ready until the selected surface passes the final gate.
