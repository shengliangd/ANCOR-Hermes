# ANCOR for Hermes

A thin workflow plugin with three explicit roles: `ancor-interactor` handles the user conversation, `ancor-coordinator` performs durable background coordination, and `ancor-worker` executes task-scoped work.

```text
User ↔ interactor → immutable draft → explicit confirmation
                                      ↓
                             requirement + event (SQLite)
                                      ↓
                         background coordinator
                                      ↓
                        execution tasks → workers
                                      ↓
                      events → resolve / replan / verify
                                      ↓
                        useful questions or delivery
```

The interactor prepares and confirms requirements, may read the board to explain progress, and records the human's final acceptance decision. The coordinator owns execution coordination and independent verification. Worker completion is evidence, not requirement acceptance; coordinator verification moves work to `awaiting_acceptance`, and only the human can commit `accepted`. Pause and resume are confirmed, reversible human decisions; pausing suppresses new work and wakes, and resuming asks the coordinator to re-evaluate existing work.

Every requirement has an authoritative, single-line `title`. Confirmation
prompts lead with that title and an optional high-level summary, followed by the
exact requirement text that will be stored in ANCOR.

## ANCOR dashboard

启用插件后，原生 Hermes dashboard 提供只读 `/ancor` 页面，展示需求、任务、待回答问题和事件概览，并可打开单条需求、任务、通知或事件详情。前端使用单文件 IIFE，无构建步骤、CDN 或新增依赖；页面每 5 秒轮询 `/api/plugins/ancor/overview`，不使用 SSE。

后端路由挂载在 `/api/plugins/ancor`：`GET /overview`、`GET /requirements/{id}`、`GET /tasks/{id}`、`GET /notifications/{id}`、`GET /events/{id}` 和 `GET /health`。所有路由均为只读；POST/PUT/PATCH/DELETE 会返回 405。SQLite 使用 `mode=ro` URI 打开，敏感确认引用、传输目标和回执不返回浏览器。

安装副本位于 `~/.hermes/plugins/ancor/dashboard/`，内容应与本仓库 `ancor/dashboard/` 保持一致。安装后重启 `hermes dashboard`，并访问 `/ancor`；非 loopback 部署仍由 Hermes dashboard 的标准认证门控保护。

## Implementation

- `ancor/store.py`: immutable drafts, transactional requirements, task links, coordinator working notes and durable events.
- `ancor/runtime.py`: one awaited coordinator process under a cross-process lock.
- `ancor/host.py`: Gateway-owned event reconciliation and bounded retries.
- `ancor/board.py`: native Kanban adapter; no native SQL writes.
- `ancor/service.py`: requirement and user-response tools.
- `roles/`: profile identities; `skills/ancor-interactor/`: user conversation,
  requirement confirmation and durable reply handoff; `skills/ancor-coordination/`:
  coordinator procedure; `skills/ancor-worker/`: bounded task execution procedure.

Native Hermes owns worker dispatch and task lifecycle. The plugin owns requirement semantics and coordination admission. Idle polling uses no model tokens. Events are batched; unchanged unprocessed batches have a retry budget rather than a hot model loop.

Worker cards carry a contract scoped to their bounded outcome. The full requirement
brief and acceptance criteria stay in the requirement store and are used during
coordination and final acceptance; they are not copied into every execution card.

Human notifications are durably enqueued by the coordinator and delivered by the
Gateway owner through Hermes' generic send interface. Enqueue returns `pending`;
it is not a delivery receipt. The Gateway keeps one background delivery loop, so
coordinator turns do not wait on transport startup. Unanswered questions are
closed, without inventing an answer, when their requirement becomes terminal or
their linked task is no longer blocked; the coordinator can also explicitly
withdraw a question after newer input makes it obsolete.

The interactor is the only human-facing role and can read the board for progress
explanations. The coordinator creates neutral durable questions and completion
outcomes; it does not conduct user dialogue. All three roles receive the normal
Hermes work tools; role-specific exclusions keep the human and scheduling
boundaries intact. The coordinator and interactor may delegate bounded analysis to
native subagents, while workers execute directly with their normal tools and do
not delegate. Workers receive no ANCOR business tools and use their native task
card, heartbeats and Kanban lifecycle tools.

## Model-facing output

Tools default to compact, decision-oriented references. Every collection uses one
envelope: `items`, `count`, `returned`, and `truncated`. Requirement lists contain
only identity, title, status, replacement and health; they never contain briefs or
acceptance criteria. Board lists omit card bodies and history, notifications carry
bounded previews, and pending events carry a payload preview plus stable IDs.

The default overview also suppresses history rather than merely truncating it:
terminal tasks are excluded unless `include_done` or `include_archived` is set,
and notifications default to unresolved questions unless
`include_closed_notifications` is set. `counts` still accounts for every source row,
`omitted` names each excluded history bucket with an executable follow-up query, and
`attention` is a short priority-ordered discovery list. Host-owned stalled work is
returned under `conditions`, never duplicated as an ordinary pending event.
`awaiting_acceptance` separately lists coordinator-verified requirements waiting for
the human's final decision. They are open work and remain outside coordinator health,
attention and routine wakes until the human accepts or requests changes.

Expansion is explicit and bounded:

- `ancor_get_context(requirement_id=...)` returns one requirement's exact brief and acceptance criteria, its acceptance status, and all linked tasks by default, including done and archived work needed for verification.
- `ancor_get_context(event_id=...)` returns one complete pending or already acknowledged event payload without changing its acknowledgement state.
- `ancor_ack_events(event_ids=[...])` atomically acknowledges handled event IDs. Any missing, host-owned or triage-blocked ID rejects the whole batch; repeated IDs are idempotent.
- `ancor_board show(task_id=...)` returns one full card with contract, comments, runs and events.
- `ancor_board history(task_id=..., kind=...)` pages one card's comments, events or runs.
- `ancor_notify list(requirement_id=...)` scopes the notification history; adding `id` returns one full notification.
- `ancor_requirement accept(requirement_id=...)` prepares final human acceptance; `request_changes(requirement_id=..., feedback=...)` prepares an exact change request. Both require native confirmation. Acceptance closes the completion notice; change requests return the requirement to active coordination.
- `ancor_note list/get/add/update/delete` maintains coordinator-only working notes on one requirement. Every note carries `created_at` and `updated_at` Unix timestamps. Notes are non-authoritative, soft-deleted on removal, and never substitute for requirement text, acceptance evidence or a task contract.

Previews are never authoritative for confirmation, acceptance, or transcription.
List results report total and returned counts so truncation is visible rather than silent.
Unanswered questions stay visible ahead of recent completion notices, live or recent
tasks sort ahead of archived history, and verified work remains in
`awaiting_acceptance` until the human makes the final decision.

The wake packet follows the same rule: requirements are title references, tasks are
non-terminal card references, invalidated work carries a bounded reason preview, and
no full brief, task body or notification message is copied into the prompt. Stalled
requirements appear as host conditions with `reason`, `since`, `generation` and
`last_woken_at`; an unchanged condition is not repeated in a wake more than once per
hour. Other new events do not reset that backoff.

## Health states

`progression` reports health per active, paused or awaiting-acceptance requirement. `active` requires a running
task with a fresh heartbeat, ready work, or a question still inside its short delivery
window. `waiting_on_human` requires a blocked task with a delivered, unanswered, open
question. A `human_waiting` list can accompany any of these states: it identifies blocked
cards whose questions are waiting for a human without claiming the whole requirement is
blocked. One card waiting on a human must not stop independently executable linked work.
`paused` is a deliberate resting state, not stalled and not liveness.
`awaiting_acceptance` is the human gate: it is open work but not a coordinator stall.
For active
requirements, everything else is `stalled`: triage, stale heartbeat, blocked without an
open question, blocked after its question was answered or withdrawn, unconfirmed delivery,
no work, or acceptance pending. A scheduled wake is a timer, not progress. The host
retains one `host.stalled` condition per requirement in `requirements.db.host.json` and
surfaces it through `conditions`, not as an ordinary event. It cannot be acknowledged,
clears only when real work recovers, and increments `generation` when the reason changes.

## Guarantee boundaries

The coordinator lock serializes plugin-launched coordination, not all user chats. SQLite transitions are atomic; SQLite and native Kanban are not one transaction, so task metadata supports reconciliation after interrupted creation.

Role prompts guide judgment but do not provide the security boundary. Tool registration and runtime profile checks enforce that interactor and coordinator capabilities are separate.

See [SETUP.md](SETUP.md) for installation and validation. Development tests run with:

```sh
env -u PYTHONPATH python -m pytest tests -q
```

Do not deploy until installed-runtime checks and a real model-backed workflow have passed. Unit tests and synthetic coordinator processes are not model E2E.
