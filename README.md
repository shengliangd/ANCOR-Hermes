# ANCOR for Hermes

A thin workflow plugin: user-facing `ancor-leader`, native Kanban execution by
`ancor-worker`, and durable requirements outside Kanban.

```text
User ↔ leader → immutable draft → explicit confirmation
                                      ↓
                             requirement + event (SQLite)
                                      ↓
                      serialized background leader
                                      ↓
                        execution tasks → workers
                                      ↓
                      events → resolve / replan / verify
                                      ↓
                        useful questions or delivery
```

The leader does light work directly. Substantial work needs a confirmed brief and
acceptance criteria. A confirmed replacement atomically invalidates the old
requirement and creates a new one. Requirements are not Kanban cards.

Coordination considers all current requirements and the board, not only the
supplied pending events. Planning can be incremental. Worker blockers go to the
leader first; worker completion is evidence, not requirement acceptance.

## Implementation

- `ancor/store.py`: immutable drafts, transactional requirements, task links and durable events.
- `ancor/runtime.py`: one awaited leader process under a cross-process lock.
- `ancor/host.py`: Gateway-owned event reconciliation and bounded retries.
- `ancor/board.py`: native Kanban adapter; no native SQL writes.
- `ancor/service.py`: requirement and acceptance tools.
- `roles/`: profile identities; `skills/ancor-coordination/`: operational procedure.

Native Hermes owns worker dispatch and task lifecycle. The plugin owns requirement
semantics and coordination admission. Idle polling uses no model tokens. Events
are batched; unchanged unprocessed batches have a retry budget rather than a hot
model loop. The native event reader is a small, explicit version coupling to
`kanban_db.list_events`; the other integration surfaces are public plugin APIs and
native CLI automation.

## Guarantee boundaries

The coordinator lock serializes plugin-launched coordination, not all user chats.
SQLite transitions are atomic; SQLite and native Kanban are not one transaction,
so task metadata supports reconciliation after interrupted creation.

Blocking a native task does not prove its process has stopped. Invalidation guards
are best-effort, not OS isolation or atomic dispatch fencing. The leader must verify
old execution has quiesced before conflicting replacement work. Role prompts guide
judgment; acceptance records do not magically establish artifact correctness.

See [SETUP.md](SETUP.md) for installation and validation. Development tests run with:

```sh
env -u PYTHONPATH python -m pytest tests -q
```

Do not deploy until the installed-runtime checks and a real model-backed workflow
have passed. Unit tests and synthetic coordinator processes are not model E2E.
