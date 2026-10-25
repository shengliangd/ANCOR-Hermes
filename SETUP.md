# Installation and verification

This repository is source, not permission to modify an existing Hermes instance. Provision an isolated data root first. Linux is required for the coordinator's cross-process lock and process lifecycle.

## Isolated real model trial

The trial provisioner reads model selections and only their required API credentials from existing role profiles. It does not copy bot credentials, external routes, schedules, sessions or business state. It refuses an existing destination or a destination inside the credential source. It creates a private virtual environment that reads the already installed library files and disables lazy dependency installation; no package or original instance state is modified.

```sh
python scripts/isolated_trial.py prepare \
  --root /absolute/path/to/new-trial \
  --native /absolute/path/to/hermes-agent \
  --credentials-from /absolute/path/to/existing-hermes-home
python scripts/isolated_trial.py turn \
  --root /absolute/path/to/new-trial \
  --message '17×19 等于多少？'
```

The root contains private owner/role configs, plugin snapshots, SOUL/skills, workspaces, request and Kanban databases, local notification records, forms and turn logs. The native models and tool execution are real; the trial callback simulates a human selecting an exact confirmation. It never fabricates an answer to an unexpected clarification. Use `--response reject` or `--response timeout` for negative confirmation cases. No model outputs or task completion results are scripted.

The interactor resumes its trial conversation across turns. Coordinator wakes and workers use their own sessions. Runtime inputs and logs stay in the isolated root; do not commit credentials or raw configuration. Use a local inbox for background notifications, never a real email/chat destination during the trial.

## Profiles and plugin

Create `ancor-interactor`, `ancor-coordinator` and `ancor-worker` under the isolated Hermes root, with deliberately selected models and per-profile model credentials. The owner is a separate profile or the isolated default root. Set `HERMES_HOME` explicitly for every launcher so `-p` resolves inside that root. Use a dedicated native board without changing another instance's current board.

Install the `ancor` package into the owner and role profiles through native plugin installation pinned to the reviewed commit. Inspect `hermes plugins install --help`; a source URL has the form `file:///absolute/path/to/ANCOR-Hermes#ancor`. Run plugin show/doctor after installation. The owner hosts lifecycle infrastructure; interactor and coordinator expose different business tools; workers expose none.

Install each role's `roles/<role>/SOUL.md` in its profile home and its complete skill directory in `skills/`. The coordinator skill directory is `ancor-coordination`; the other names match the role. Plugin installation does not sync role files. Treat plugin code and role instructions as one reviewed release, deployed as separate artifacts.

Auto-load each role skill, including one-shot turns. Disable automatic background review/curation so tested role instructions remain pinned:

```sh
hermes -p ancor-interactor config set skills.auto_load '["ancor-interactor"]'
hermes -p ancor-coordinator config set skills.auto_load '["ancor-coordination"]'
hermes -p ancor-worker config set skills.auto_load '["ancor-worker"]'
# Apply in each isolated role profile:
hermes -p ancor-worker config set auxiliary.background_review.enabled false
hermes -p ancor-worker config set curator.enabled false
```

Verify installed files and read back settings before starting fresh sessions. In-flight sessions retain their original instructions. Reload plugin registrations after code changes; restart an isolated Gateway after scanner changes. Do not restart the existing user instance as part of a trial.

## Tools and authority

All roles receive normal execution tools appropriate to their work. Use explicit toolset names; `all` is not a valid saved toolset sentinel.

| Role | Additional tools | Exclusions |
| --- | --- | --- |
| interactor | `ancor`, `clarify`, `delegation` | Native Kanban mutations; use read-only `ancor_board` |
| coordinator | `ancor`, `delegation` | `clarify`, direct human/external messaging, cron; use ANCOR questions/outbox/wakes |
| worker | `kanban`, `delegation` | ANCOR business tools, `clarify`, direct human messaging, cron |

Apply the matrix to each platform used by that role. Workers may delegate bounded parts of their card. Native children do not inherit role SOUL/skills, so pass their outcome, inputs/workspace, constraints, authorization limits and return expectations. Children cannot gain wider permissions; the parent integrates and verifies results. Native child Kanban restrictions and lineage fences remain in effect.

The interactor does light, short work directly, submits substantial work unless the user explicitly chooses otherwise, and does not take over an existing ANCOR request's execution. The coordinator plans and advances uncertain work without asking the human to provide a complete specification. Human questions are for consequential commitments or clear efficiency gains. Workers may explore relevant files in their assigned workspace; a card is an outcome contract, not a file allowlist or mandatory implementation plan.

Human formal writes must go through ANCOR's immutable proposal and actual native `clarify` confirmation. A chat reply, fabricated confirmation flag or advisory response cannot commit intent or permission. Coordinator notes, scheduling and verification use role-checked internal interfaces and never create human consent. This is cooperative role separation, not an OS sandbox or universal authorization enforcement layer.

## Shared settings

Plugin settings live under `plugins.entries.ancor.settings`. All participating isolated profiles use the same absolute request database and board.

| Setting | Scope | Value |
| --- | --- | --- |
| `store_path` | owner/roles | Shared absolute `requests.db` outside plugin code |
| `board` | owner/roles | Dedicated isolated native board |
| `interactor_profile` | owner/roles | `ancor-interactor` |
| `coordinator_profile` | owner/roles | `ancor-coordinator` |
| `worker_profile` | owner/roles | `ancor-worker` |
| `owner_profile` | owner/roles | Gateway-owning profile |
| `gateway_owner` | owner only | `true`; `false` in other profiles |
| `poll_interval` | owner | At least 30 seconds |
| `human_target` | owner/coordinator | Explicit isolated native `platform:destination` |
| `transport_profile` | owner | Profile actually serving that transport |
| `block_unblock_cycles` | coordinator/worker | Default 3; limits repair loops before native triage |

For unattended isolated CLI turns, set the native `approvals.single_query_mode` policy explicitly (for example `approve` for a controlled local fixture). This does not grant request authorization, external commitments or broader task scope. Preserve native hardline denials; do not use `--yolo` to hide missing workflow authorization.

Native dispatch owns worker spawning. Disable automatic root decomposition: the coordinator creates coherent execution cards progressively. Keep `kanban.review_dispatch=false` in a dedicated owner; workers call `kanban_complete`, coordinator verifies, and the human accepts. This setting is owner-wide, so do not change an owner serving unrelated workflows.

## Gateway lifecycle and notifications

The plugin's production lifecycle starts from the owner's connected `api_server` adapter. Enable that adapter only in the isolated owner, on an unused loopback port, with a non-placeholder `API_SERVER_KEY`. The coordinator runs one awaited process under an inherited file lock. The Gateway also runs the durable notification delivery loop; coordinator turns never wait for transport startup.

A queued notification is `pending`, not delivered. `sent` means the transport accepted it, not that a human read or accepted it. Missing adapter configuration leaves delivery pending. Never automatically retry `sending` or `uncertain` outcomes. Questions and replies are authoritative in the request store; delivery is a separately recoverable outbox projection. Completion notice IDs bind the verification attempt and are read from request detail.

Questions can be request-scoped before tasks exist. A task-linked question must link a blocked card. Delivered questions can account for waiting-on-human, while independent ready/running work continues. Terminal requests or obsolete questions are closed/withdrawn without fabricating answers.

The dashboard remains read-only. `/ancor` polls `/api/plugins/ancor/overview` every five seconds; request detail is `/api/plugins/ancor/requests/{id}`. Verify browser projections do not expose confirmations, transport receipts or coordinator notes. Non-loopback use retains Hermes' standard authentication.

## Upgrade boundary

Version 0.7 uses request consistently across schema, objects, tools, APIs, event payloads and configuration examples. The runtime deliberately has no old tool/argument aliases. It refuses an old schema before creating any parallel tables.

Stop writers to the source snapshots and migrate into a new location:

```sh
python -m ancor.migrate_requests /path/to/old.db \
  --output /path/to/migrated/requests.db \
  --native-source /path/to/offline-native-board.db
```

The source database and sidecars are read-only. The command copies request state, notifications, confirmations, host state and immutable presentation files; translates machine-owned identifiers; and retains human-authored text verbatim. Request IDs, task links, committed events and note history remain. Pending proposals are archived under `retired_confirmations` and require fresh confirmation. Historical consent is not silently transferred across renamed protocols.

Optional native migration writes only `/path/to/migrated/requests.db.kanban.db`: ANCOR link metadata and its idempotency keys are translated; unmanaged tasks and original card contracts are preserved. Neither native source nor live board is modified. Select the migrated request store and matching native board copy together when provisioning the new instance.

Prose-only briefs remain authoritative until the user explicitly confirms a `convert` operation. Do not automatically classify or strengthen their content. Older schemas missing titles require the explicit title-mapping migration; never derive missing titles silently. Existing notes whose older bodies were overwritten can only retain their current version with a missing-history marker.

## Verification gates

Run unit/integration tests, skill validation, native lifecycle probes, then real model journeys in the isolated instance. Cover direct light work, vague sustained requests, rejection/timeout, creation, point edits, questions/answers, pause/resume, rework, final acceptance, restart recovery and task-scoped delegation. Inspect both recorded state and actual artifacts, not just final chat text.

A deterministic coordinator or mocked model is not model E2E. A local inbox is an explicitly simulated transport, not proof of external chat delivery. Document observed friction, fixes, remaining limitations and exact trial artifacts. Only after this passes should a separate reviewed deployment update any user instance.
