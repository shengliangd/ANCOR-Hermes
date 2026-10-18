# Installation and verification

This repository is a source package, not an authorization to change a running
Hermes installation. Use a disposable installation for validation first. Linux is
required for the coordinator's `flock`/process lifecycle implementation.

## Profiles and package

Create native profiles `ancor-leader` and `ancor-worker`, with explicitly selected
models and separately provisioned credentials. Keep bot credentials with the
Gateway owner; do not copy the default `.env` wholesale.

Install the `ancor` subdirectory using native plugin installation, pinned to the
reviewed commit, into the Gateway owner and both role profiles. Inspect the
installed `hermes plugins install --help` before provisioning. The source form is:

```text
file:///absolute/path/to/ANCOR-Hermes-native-config#ancor
```

Install each role's `SOUL.md` in its profile home and the complete
`skills/ancor-coordination` directory in the leader profile. Preserve unrelated
configuration, memory and routes. The worker instructions use read-only requirement
context and safe checkpoints to stop obsolete work; this is cooperative behavior,
not process isolation.

## Disable automatic self-improvement

Keep ANCOR role instructions and skills fixed to the reviewed repository version.
Disable automatic post-turn review and skill curation in both role profiles:

```sh
hermes -p ancor-leader config set auxiliary.background_review.enabled false
hermes -p ancor-leader config set curator.enabled false
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
| `leader_profile` | all three | `ancor-leader` |
| `worker_profile` | all three | `ancor-worker` |
| `owner_profile` | all three | Actual Gateway-owning profile |
| `gateway_owner` | owner only | `true`; keep `false` in role profiles |
| `poll_interval` | owner | At least 15 seconds |
| `human_target` | leader | Explicit native `platform:destination` |
| `transport_profile` | leader | Profile owning that configured transport |
| `sender_executable` | leader | Optional executable, default `hermes` (native transport) |

For an isolated transport test only, set `sender_executable` to the absolute path
of the approved test executable through native config, e.g.
`hermes -p ancor-leader config set plugins.entries.ancor.settings.sender_executable /absolute/isolated/bin/hermes`.
It must implement the native `send --file - --json` contract. A PATH-only override
is insufficient when Hermes bootstrap prepends its own launcher. Do not configure
the test executable in a live profile; leave `hermes` for normal native delivery.
Failed sends retain safe structured diagnostics in the outbox `error` field
(exception class, reason code, exit code, resolved executable), never raw output
or exception text. Legacy errors remain readable. `uncertain`/`sending` entries
are not retried automatically: reconcile transport evidence before any retry.

Enable the plugin/toolset in the selected profiles. The owner registers lifecycle
infrastructure, not business tools. The leader receives requirement, context,
board and acceptance tools; the worker receives read-only requirement context.

The current lifecycle host is the owner's native `api_server` platform connection.
Configure that listener through native Hermes settings and keep the Gateway
running. Named-profile request routing alone does not start owner lifecycle
handlers. No extra standalone scheduler or foreground coordinator session is
required. Native Kanban dispatch must be enabled and able to run `ancor-worker`.

Create the dedicated board through native board commands; do not change the
installation-wide current board. Turn off creator auto-subscription in the ANCOR
profiles. Do not add user-facing task subscriptions: questions and delivery are
leader decisions, not direct worker event notifications. Audit existing ANCOR
subscriptions before reusing an old board. Existing business state is not migrated.

Disable automatic root decomposition for this workflow: the leader creates only
execution tasks, progressively, through the plugin board tool. Native dispatch
still owns execution. Preserve unrelated boards' decomposition settings.

In the Gateway owner profile, run `hermes config set kanban.review_dispatch false`.
This is an owner-wide dispatcher setting, not a per-board switch: use a dedicated
owner if unrelated boards need native review dispatch. Workers finish execution
with `kanban_complete`, not `kanban_request_review`. The leader independently
verifies completed artifacts through `ancor_acceptance`; no native review stage
or leader task-completion tool is needed. Disabling review dispatch does not
repair existing review/failed tasks; preserve their evidence for operator review.

Route the selected user conversation to `ancor-leader` using native profile
routing. Present and confirm the draft in that current conversation using native
`clarify`. Confirmation needs no separate message destination, delivery receipt or
plugin-imposed length limit. `human_target` and `transport_profile` are only for
leader-selected background questions and completion notices; they are not
prerequisites for requirement confirmation.

Invalidation records an event. The triggered leader decides how to stop, hold or
replan affected work; event ingestion itself does not block tasks. New work cannot
be created for an invalidated requirement. There is no global requirement that all
old workers stop before unrelated work can proceed.

## Restart recovery contract

On the first scan that acquires the existing coordinator flock, the host reads
current requirements, native tasks/events, acceptance evidence and the existing
notification outbox. Pending events already trigger reconciliation. If the inbox
is empty but active requirements, undelivered acceptances or unresolved notices
remain, it records one durable `host.recovery` event. Acknowledging that event
makes subsequent idle polls quiet; it is not recreated on every poll. A later
host startup may reconcile still-unresolved work again. Drafts alone do not wake
a model. A sent completion is neither recreated nor resent.

Acceptance criteria for recovery:
- Confirmed work resumes without human resubmission, including an interrupted
  coordinator that acknowledged its original event before finishing coordination.
- A surviving coordinator retains its inherited flock; a restarted host waits.
  Existing native task keys/claims remain authoritative; recovery does not dispatch
  workers itself or replace native scheduling.
- Durable worker outcome events from downtime are ingested using per-task cursors
  and deduplicated source keys.
- Acceptance before notification enqueue wakes the leader with original evidence;
  only the leader decides the real completion text and enqueues the stable ID.
- Pending notices remain pending; `sending`/`uncertain` require transport evidence
  and operator reconciliation, never blind retries or replacement IDs.
- No continuous idle model calls. Unhandled identical batches retain the existing
  three-attempt/hour cooldown; the newest event gets a reserved batch slot so new
  input cannot hide behind a full cooling head batch.

These are durable wake/reconciliation guarantees, not guarantees that an arbitrary
model successfully completes work. Recovery does not infer acceptance or delivery.
The host reads the outbox without requiring transport credentials/configuration.
Crash tests use real OS processes and locks with scripted leader decisions; native
history reads use the installed read-only API. They are not actual-model E2E.

## Verification gates

1. Run `env -u PYTHONPATH python -m pytest tests -q` in the source checkout.
2. Verify native plugin discovery/registration under disposable profile homes.
3. Verify exact-draft confirmation via native tool execution; cancellation,
   timeout, changed questions and replay must not activate a requirement.
4. Verify real native Kanban creation, idempotent retry, blocking and readback.
5. Verify one coordinator across processes, cancellation/restart ownership,
   event replay and no idle model calls.
6. Exercise an actual model-backed workflow: confirmation → rolling tasks →
   blocker remedy → confirmed replacement → artifact verification → user delivery.
   Include failed acceptance, competing work, restart and reply recovery.

Synthetic tests do not establish model behavior or real transport delivery. Do not
call the installation ready until the selected surface passes the final gate.
