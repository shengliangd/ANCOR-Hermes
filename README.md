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

The interactor prepares and confirms requirements, then stops. It does not inspect the board, schedule workers, wake coordination, or perform acceptance. The coordinator owns those operations. Worker completion is evidence, not requirement acceptance.

## Implementation

- `ancor/store.py`: immutable drafts, transactional requirements, task links and durable events.
- `ancor/runtime.py`: one awaited coordinator process under a cross-process lock.
- `ancor/host.py`: Gateway-owned event reconciliation and bounded retries.
- `ancor/board.py`: native Kanban adapter; no native SQL writes.
- `ancor/service.py`: requirement and user-response tools.
- `roles/`: profile identities; `skills/ancor-coordination/`: coordinator procedure;
  `skills/ancor-interactor/`: interactor procedure for handing human replies back to ANCOR.

Native Hermes owns worker dispatch and task lifecycle. The plugin owns requirement semantics and coordination admission. Idle polling uses no model tokens. Events are batched; unchanged unprocessed batches have a retry budget rather than a hot model loop.

## Health states

`progression` reports health per active requirement. `active` requires a running
task with a fresh heartbeat, or ready work. `waiting_on_human` requires a blocked
task with a delivered, unanswered question. Everything else is `stalled`: triage,
stale heartbeat, blocked without an open question, blocked after its question was
answered, no work, or acceptance pending. A scheduled wake is a timer, not
progress. The host publishes a `host.stalled` condition event that cannot be
acknowledged and clears only when real work recovers.

## Guarantee boundaries

The coordinator lock serializes plugin-launched coordination, not all user chats. SQLite transitions are atomic; SQLite and native Kanban are not one transaction, so task metadata supports reconciliation after interrupted creation.

Role prompts guide judgment but do not provide the security boundary. Tool registration and runtime profile checks enforce that interactor and coordinator capabilities are separate.

See [SETUP.md](SETUP.md) for installation and validation. Development tests run with:

```sh
env -u PYTHONPATH python -m pytest tests -q
```

Do not deploy until installed-runtime checks and a real model-backed workflow have passed. Unit tests and synthetic coordinator processes are not model E2E.
