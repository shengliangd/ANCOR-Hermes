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
native task card is the complete execution contract. This is cooperative behavior,
not process isolation.

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
| `poll_interval` | owner | At least 15 seconds |
| `human_target` | coordinator | Explicit native `platform:destination` |
| `transport_profile` | coordinator | Profile owning that configured transport |
| `sender_executable` | coordinator | Optional executable, default `hermes` (native transport) |

For an isolated transport test only, set `sender_executable` to the absolute path
of the approved test executable through native config, e.g.
`hermes -p ancor-coordinator config set plugins.entries.ancor.settings.sender_executable /absolute/isolated/bin/hermes`.
It must implement the native `send --file - --json` contract. A PATH-only override
is insufficient when Hermes bootstrap prepends its own launcher. Do not configure
the test executable in a live profile; leave `hermes` for normal native delivery.
Failed sends retain safe structured diagnostics in the outbox `error` field
(exception class, reason code, exit code, resolved executable), never raw output
or exception text. Legacy errors remain readable. `uncertain`/`sending` entries
are not retried automatically: reconcile transport evidence before any retry.

### Coordinator notification channel

Background questions and completion notices fail closed unless the coordinator
profile has both values below. The coordinator is the only role that sends them;
the interactor reads the resulting outbox and relays answers.

```sh
hermes -p ancor-coordinator config set plugins.entries.ancor.settings.human_target 'telegram:-1001234567890'
hermes -p ancor-coordinator config set plugins.entries.ancor.settings.transport_profile default
```

Use the exact destination syntax accepted by `hermes send --to` for the selected
platform, for example `telegram:<chat_id>`, `discord:#ops`,
`slack:C0123ABCD` or `feishu:oc_<chat_id>`. The destination must include a
non-empty platform and destination separated by `:`.

`transport_profile` must name the profile whose native `.env`/`config.yaml`
actually contains that channel's credentials and for which `hermes send` works.
If the bot credentials live only with the Gateway owner, set that owner's profile
name here; do not copy the default `.env` into role profiles. Pointing at a
profile without credentials, or omitting either setting, leaves notification
delivery unavailable.

Verify the exact transport path used by the outbox before enabling the
coordinator:

```sh
printf 'ANCOR channel test' | hermes --profile default send --to 'telegram:-1001234567890' --file - --json
```

Replace the profile and destination with the configured values. The JSON receipt
must contain `"success": true` and no `error` or `skipped` field. The plugin invokes
exactly `hermes --profile <transport_profile> send --to <human_target> --file - --json`;
`sent` means the native transport accepted the message, not that a human read or
answered it.

Enable the plugin/toolset in the selected profiles. The owner registers lifecycle
infrastructure, not business tools. The interactor exclusively receives requirement
draft/confirm/cancel/respond, read-only board/progress context and notification
reply tools. Its Kanban read path is `ancor_board list` / `ancor_board show`; do not
grant that profile native Kanban write tools. The coordinator receives full context,
board, wake, acceptance and notification send tools; it has no requirement-lifecycle
tool. The worker receives no ANCOR business tools.

### Gateway owner and API server

The current lifecycle host is the owner's native `api_server` platform connection.
Configure it in the profile that actually serves the Gateway; named-profile
request routing alone does not start owner lifecycle handlers. For a conventional
installation the owner is `default`; substitute the real owner name everywhere.

```sh
OWNER=default
hermes -p "$OWNER" config set plugins.entries.ancor.settings.owner_profile "$OWNER"
hermes -p "$OWNER" config set plugins.entries.ancor.settings.gateway_owner true
hermes -p "$OWNER" config set plugins.entries.ancor.settings.poll_interval 15
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
- That profile has `api_server` enabled and connected, with `poll_interval >= 15`.
- The `ancor` plugin and the correct role toolsets are enabled in the owner and in
  all three role profiles.
- Native Kanban dispatch is running: either `kanban.dispatch_in_gateway true` in
  the owner, or one separate `hermes kanban daemon`. With review dispatch disabled,
  it must still be able to spawn `ancor-worker` for ordinary tasks.
- The selected user conversation routes to `ancor-interactor`, so native `clarify`
  reaches the user and the interactor can read the board for progress reporting.

Create the dedicated board through native board commands; do not change the
installation-wide current board. Turn off creator auto-subscription in the ANCOR
profiles. Do not add user-facing task subscriptions: questions and delivery are
coordinator decisions, not direct worker event notifications. Audit existing ANCOR
subscriptions before reusing an old board. Existing business state is not migrated.

Disable automatic root decomposition for this workflow: the coordinator creates only
execution tasks, progressively, through the plugin board tool. Native dispatch
still owns execution. Preserve unrelated boards' decomposition settings.

In the Gateway owner profile, run `hermes config set kanban.review_dispatch false`.
This is an owner-wide dispatcher setting, not a per-board switch: use a dedicated
owner if unrelated boards need native review dispatch. Workers finish execution
with `kanban_complete`, not `kanban_request_review`. The coordinator independently
verifies completed artifacts through `ancor_acceptance`; no native review stage
or coordinator task-completion tool is needed. Disabling review dispatch does not
repair existing review/failed tasks; preserve their evidence for operator review.

Route the selected user conversation to `ancor-interactor` using native profile
routing. Present and confirm the draft in that current conversation using native
`clarify`. Confirmation needs no separate message destination, delivery receipt or
plugin-imposed length limit. `human_target` and `transport_profile` are only for
coordinator-selected background questions and completion notices; they are not
prerequisites for requirement confirmation.

Invalidation records an event. The triggered coordinator decides how to stop, hold or
replan affected work; event ingestion itself does not block tasks. New work cannot
be created for an invalidated requirement. There is no global requirement that all
old workers stop before unrelated work can proceed.

Pause and resume use the same exact native `clarify` confirmation path as drafts
and cancellations. A paused requirement is not active: it suppresses scheduled
wakes, is reported as `paused` rather than `stalled`, and cannot create new work.
Its coordinator blocks linked running or ready tasks and waits for quiescence;
resume reactivates the requirement and requires the coordinator to re-evaluate
existing work before unblocking it. No additional configuration is required.

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
  manually acknowledged, and clears only when the requirement recovers.

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
