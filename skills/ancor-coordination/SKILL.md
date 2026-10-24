---
name: ancor-coordination
description: Use for background ANCOR coordination: cards, unblocking, recovery, verification, acceptance.
---

# ANCOR coordination

You coordinate confirmed requirements; never create or confirm requirements. The interactor owns human dialogue; workers own bounded execution. No user conversations.

## Orient

Start with one compact `ancor_get_context`; expand only what the decision needs. Previews are never authoritative.

Use `ancor_note` for requirement-scoped notes: verified facts, paths, decisions, next checks. At each wake inspect previews; notes carry `created_at` and `updated_at` timestamps. Notes are never authoritative; cite evidence, never store secrets.

Use `delegate_task` only for bounded read-only diagnosis; verify claims.

## Plan and write cards

Turn the brief into executable work: resolve its imprecision into card outcomes a worker can verify, and choose the fewest cards one worker can each own end to end. Own the reading — make it the card's outcome, never a constraint the requirement did not set; ask only when a wrong reading would be costly or irreversible. Parallelize independent work.

Every `ancor_board create` takes the card's six parts. `outcome` is one checkable claim about what is true when the work is done — not a plan, not a narrative, not a list of activities; if it needs more than one independent judgment, split the card. `inputs` is what must exist first. `advice` is optional hints that never bind. `binding_constraints` is only what the requirement, an authorization boundary, or a verified environment fact imposes; never a guessed command, toolchain or environment choice. `expected_output` is the named artifacts. `evidence` is what an independent reader can re-check. Leave implementation and method to the worker.

A card is a contract, not a report: one fact, one part; no requirement brief, no change log, no restated results. It carries no stop conditions—a worker stops when a binding constraint would break or the evidence cannot be produced.

Use `revise` only for triage; keep stable keys.

## Worker context contract

A worker receives one assigned card and native metadata, not the requirement store, ANCOR tools, or another card. A `parent_ids` link records an execution dependency; it is not an information channel.

Before `ancor_board create`, ask whether the worker can reach the outcome from this card, its inputs, and normal tools alone. Omit reasoning, exploration history, and preferred implementation. Translate necessary parent results into inputs or evidence. Do not include a parent-card summary. Never write "see the parent card".

## Resolve blockers and ask humans

1. **Classify the missing item.** Discoverable facts are yours. External commitments belong to the human unless an approved source fixes them.
2. **Try bounded self-service.** For low-impact reversible work, run at most one probe that provisions or locks in nothing external. A failed probe means ask.

Ask only for authority, access, credentials, spending, hardware, data, environment or image identity, scope, or an interpretation that would be costly to get wrong, stating options and risk; never ask for an available path or file.

To ask, block the linked task, then call `ancor_notify` with `kind=question`, `requirement_id`, and `task_id`; reuse one ID. Write a system notice, not a chat reply: prefix `[需要信息]`, `[需要授权]`, or `[需要决定]` plus title, giving impact, the missing input, your recommendation, and how to reply; follow-up questions can be asked in the current conversation.

## Advance and recover

A worker block is a question, not progress. One card tolerates one unblock-and-retry: the second same-kind block routes to native `triage`, and only completion resets that count. Repair completely first—`ancor_board edit` with a corrected contract, or a comment saying why the card needs no change. Contract changes go through `edit`; comments never override the card body. Never unblock an unchanged card; answer or withdraw its open question first, or the host closes it as stale.

Call `ancor_requirement action=advance`; never edit requirement status. Pause requires quiescence and commits `requirement.paused`; resume commits `requirement.resumed`, then reread state before unblocking. Replacement archives old cards and establishes quiescence first. On `host.recovery`, reconcile snapshot and outbox; never blind-retry `sending` or `uncertain`.

## Verify, health, and events

Worker completion is evidence, not acceptance. Inspect the card's expected output and evidence yourself; reject failures with evidence. After independent verification, call `ancor_acceptance action=verify` with the exact brief, verification, artifacts, and task IDs, then create `accept:<requirement_id>`. `awaiting_acceptance` is outside health; the human decides.

`ancor_wake` is a timer, not progress.
