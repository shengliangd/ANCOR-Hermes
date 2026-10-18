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

## Settings

Use `hermes config set` in the appropriate native profile. Plugin settings live
under `plugins.entries.ancor`; confirm the installed configuration reader and
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
