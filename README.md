# ANCOR for Hermes

ANCOR coordinates substantial, exploratory and long-running requests through three roles:

- **Interactor** handles light work directly and submits substantial work unless the user explicitly chooses otherwise. It preserves intent, records human decisions through confirmed interfaces, and explains progress economically.
- **Coordinator** owns planning, exploration, scheduling, adaptation and independent verification. It gives workers coherent outcomes and asks humans only for important commitments or when their answer is much more efficient than investigation.
- **Worker** executes one bounded Kanban card, chooses methods within real constraints, may delegate scoped subtasks, and reports inspectable results or blockers.

```text
User ↔ interactor → exact change proposal → explicit native confirmation
                                                   ↓
                                  current request + durable change history
                                                   ↓
                                      background coordinator
                                                   ↓
                                   Kanban cards → workers → scoped subagents
                                                   ↓
                                   evidence → reconcile / repair / verify
                                                   ↓
                                  human question or delivery for acceptance
```

## Request model

A request has a stable ID, a presentation title and unordered point groups: `outcome`, `constraint`, `advice`, `context`. Each point has a stable identity, immutable versions, timestamps and source references. Append, invalidate and replace operate locally; replacing a point invalidates its old version and appends a successor atomically. Title and coordinator notes have separate versions and do not invalidate content verification.

A confirmed change can update points, record a reply, resolve a question and request rework together. Optimistic version checks prevent silent overwrites. All human intent, authorization and final decisions require exact native `clarify` confirmation through ANCOR; proposal creation or a conversational yes does not commit. Coordinator notes, scheduling, task lifecycle and verification are role-owned internal writes, not new human consent.

Questions are durable objects separate from notification delivery. A reply does not necessarily resolve a question. Its lasting information or obligations enter the current request points; its original wording remains as source evidence. Grants record action, target and scope; permission is not an obligation or a universal tool sandbox.

The coordinator reads current desired state and incremental differences, evaluates every linked task, and preserves unaffected work. Task contracts bind relevant point versions. Changed contracts require safe worker handoff. Added global constraints require semantic impact assessment, not just searching old references. Acknowledging events cannot erase unfinished reconciliation.

Worker completion is evidence. Coordinator verification binds the current generation and evidence for all active outcomes/constraints. Only the human accepts the specific verified delivery. Rework can leave request content unchanged. Completion notices identify the verification attempt so old notices cannot approve later results.

## Interfaces

| Interface | Purpose |
| --- | --- |
| `ancor_change` | Propose formal changes, inspect one, page history |
| `ancor_request` | Confirmed lifecycle/acceptance convenience operations; coordinator transition convergence |
| `ancor_get_context` | Compact overview or exact current request context |
| `ancor_question` | Read questions; coordinator create/withdraw |
| `ancor_board` | Interactor read-only progress; coordinator card management |
| `ancor_reconcile` | Persist impact decisions and unfinished adjustments |
| `ancor_note` | Coordinator-only append/replace/invalidate/history |
| `ancor_acceptance` | Independent coordinator verification |
| `ancor_notify` | Durable local outbox for human questions/completion notices |
| `ancor_wake`, `ancor_ack_events` | Future checks and handled event acknowledgement |

Workers receive native task lifecycle tools and normal work tools, including bounded delegation, but no ANCOR business tools. The worker card is its execution contract; it may investigate relevant resources in its assigned workspace.

## Runtime and dashboard

Native Hermes owns dispatch and task lifecycle. ANCOR owns request semantics, confirmation, durable coordination and human delivery decisions. One coordinator runs under a cross-process lock. SQLite mutations are transactional; SQLite and native Kanban are not one transaction, so held card creation and idempotent keys support recovery. Idle polling uses no model tokens; unchanged event batches have bounded retries.

The read-only `/ancor` dashboard polls `/api/plugins/ancor/overview` every five seconds. Detail routes cover `/requests/{id}`, `/tasks/{id}`, `/questions/{id}`, `/notifications/{id}`, `/events/{id}` and `/changes/{id}`. SQLite reads use `mode=ro`; browser projections redact confirmation references, transport receipts and secrets. Coordinator notes remain private.

## Migration and validation

Version 0.7 is a breaking vocabulary change. Runtime tables, fields, tools, APIs, events and documentation use **request** exclusively. The old term appears only in the offline migration boundary and historical fixtures. There are no old public tool/argument aliases. A runtime opening an old schema fails before creating parallel tables.

Use `python -m ancor.migrate_requests SOURCE --output NEW_DATABASE` on quiescent copies. It preserves identity and history, translates machine-owned schema and JSON references, retains human-authored text verbatim, and retires unconfirmed proposals for fresh confirmation. Optional `--native-source` migrates an offline Kanban copy's ANCOR bindings and idempotency keys. The original instance is untouched. Legacy prose briefs require an explicit confirmed conversion to structured points.

See [SETUP.md](SETUP.md) for isolated installation and validation, and [the request model](docs/atomic-requests.md) for detailed semantics. Development checks:

```sh
env -u PYTHONPATH python -m pytest tests -q
```

Real model trials use `scripts/isolated_trial.py` to provision separate homes, role configs, skills, databases, Kanban and notification targets. The model and native tools are real; only the simulated human confirmation callback and local notification endpoint are test fixtures. Test reports must distinguish deterministic tests, native lifecycle probes and real model runs. Do not update a running installation until its isolated workflow has passed.
