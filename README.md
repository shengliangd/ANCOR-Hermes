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

The interactor prepares and confirms requirements, may read the board to explain progress, then stops at the coordination boundary. It does not mutate the board, schedule workers, wake coordination, or perform acceptance. The coordinator owns those operations. Worker completion is evidence, not requirement acceptance. Pause and resume are confirmed, reversible human decisions; pausing suppresses new work and wakes, and resuming asks the coordinator to re-evaluate existing work.

Every requirement has an authoritative, single-line `title`. Confirmation
prompts lead with that title and an optional high-level summary, followed by the
exact requirement text that will be stored in ANCOR.

## Implementation

- `ancor/store.py`: immutable drafts, transactional requirements, task links and durable events.
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

Expansion is explicit and bounded:

- `ancor_get_context(requirement_id=...)` returns one requirement's exact brief and acceptance criteria, plus scoped and bounded linked collections.
- `ancor_get_context(event_id=...)` returns one complete pending event payload.
- `ancor_board show(task_id=...)` returns one full card with contract, comments, runs and events.
- `ancor_board history(task_id=..., kind=...)` pages one card's comments, events or runs.
- `ancor_notify list(requirement_id=...)` scopes the notification history; adding `id` returns one full notification.

Previews are never authoritative for confirmation, acceptance, or transcription.
List results report total and returned counts so truncation is visible rather than silent.
Unanswered questions stay visible ahead of recent completion notices, and live or recent
tasks sort ahead of archived history.

The wake packet follows the same rule: requirements are title references, tasks are
card references, invalidated work carries a bounded reason preview, and no full brief,
task body or notification message is copied into the prompt.

## Health states

`progression` reports health per active or paused requirement. `active` requires a running
task with a fresh heartbeat, ready work, or a question still inside its short delivery
window. `waiting_on_human` requires a blocked task with a delivered, unanswered, open
question. `paused` is a deliberate resting state, not stalled and not liveness. For active
requirements, everything else is `stalled`: triage, stale heartbeat, blocked without an
open question, blocked after its question was answered or withdrawn, unconfirmed delivery,
no work, or acceptance pending. A scheduled wake is a timer, not progress. The host
publishes a `host.stalled` condition event that cannot be acknowledged and clears only when
real work recovers.

## Guarantee boundaries

The coordinator lock serializes plugin-launched coordination, not all user chats. SQLite transitions are atomic; SQLite and native Kanban are not one transaction, so task metadata supports reconciliation after interrupted creation.

Role prompts guide judgment but do not provide the security boundary. Tool registration and runtime profile checks enforce that interactor and coordinator capabilities are separate.

See [SETUP.md](SETUP.md) for installation and validation. Development tests run with:

```sh
env -u PYTHONPATH python -m pytest tests -q
```

Do not deploy until installed-runtime checks and a real model-backed workflow have passed. Unit tests and synthetic coordinator processes are not model E2E.
