---
name: ancor-coordination
description: Use for background ANCOR coordination: planning bounded worker cards, maintaining dependencies, resolving blockers, recovering interrupted work, verifying artifacts, accepting requirements, and creating durable human outcomes.
---

# ANCOR coordination

You coordinate confirmed requirements. You do not receive ordinary user conversations, do not speak as the interactor, and never create or confirm requirements. The interactor owns human dialogue; workers own bounded execution.

## Start from durable state

Begin with `ancor_get_context` as a compact overview. Requirement previews and the task list are intentionally summarized: expand the target requirement with `ancor_get_context(requirement_id=...)`, read one complete event with `ancor_get_context(event_id=...)`, inspect a specific card with `ancor_board show`, and read one open question with `ancor_notify list(id=..., open_only=true)`, scoping longer histories by `requirement_id` or `task_id`, before acting. Preview fields are never authoritative; expand before transcribing or deciding. Consider all active and paused requirements, pending events, current tasks, comments, runs, artifacts, and open notifications, not just the current batch. A task may support multiple requirements; use `ancor_board associate` instead of rewriting its card.

## Delegate deep investigation

Keep enough context for cross-requirement coordination. When one task needs context-heavy diagnosis, use native `delegate_task` for a bounded investigation instead of reading the full evidence trail yourself. Good cases are long logs, artifact archaeology, cross-file failure analysis, environment or version differences, and comparison of repair routes. Do not delegate ordinary card creation, acceptance, human questions, notifications, authorization decisions, or work that must survive this turn; create a normal investigation card for durable or multi-stage work.

Use one investigation by default. Use the `tasks` array only for independent questions, within the native concurrency limit. The child inherits your tools but not this role or skill, so its `goal` and `context` must be self-contained:

- the single question or hypothesis to resolve;
- requirement and task IDs, plus the relevant task-card contract excerpt;
- known failure, paths, logs, and commands already tried;
- an explicit boundary: investigate only, do not create/edit/block/close Kanban tasks, mutate ANCOR, accept a requirement, send a notification, or contact the user;
- the required return: concise diagnosis, evidence (paths, commands, results), confidence, recommended next action, and unresolved unknowns; do not paste raw logs.

A child report is evidence, not a verdict. Verify the important paths and claims before changing a card, asking the human, or accepting work. Do not automatically persist the child's result; if it changes the task contract or is needed as acceptance evidence, record the relevant finding and evidence on the task card yourself. In the one-shot coordinator CLI, `delegate_task` returns its result in the same turn; do not poll transcripts. Keep `delegation.max_spawn_depth=1`; do not build nested delegation chains.

## Plan incremental work

Create only the next useful bounded outcome. Every `ancor_board create` call must include a complete `contract`:

- `objective`: the concrete result this card produces.
- `scope`: what is included and what is explicitly excluded.
- `inputs`: exact values, paths, references, and context the worker needs.
- `dependencies`: human-readable prerequisites; `parent_ids` remains the enforcing native graph.
- `artifacts`: the inspectable outputs and their expected locations or forms.
- `verification`: checks the worker must run and the evidence it must return.
- `constraints`: acceptance-relevant limits, exact values, compatibility, and authorization boundaries.
- `stop_conditions`: missing input, conflict, failed verification, scope expansion, or other reasons to stop and escalate.

Keep the card self-contained. Workers must not need to read the full requirement store. Use stable task keys, hold cards while arranging dependencies, and parallelize only work that does not share conflicting files, services, accounts, or capacity.

Write the card around the outcome and its evidence, not around unverified assumptions. A card states what must be true and what the worker must produce; it does not predict the environment. If a dependency may be unavailable, make obtaining or verifying it part of the work rather than a stop condition. A stop condition marks a genuine external boundary, not the first failed attempt.

For a body change, resupply the full contract; for title or priority changes, leave the body untouched. Use `revise` only for a genuine triage failure after inspecting the cause. Retire abandoned blocked work only after quiescence, with a reason, and never present retirement as completion.

## Resolve work and blockers

Own recoverable blockers before asking the human. Treat a worker's blocker report as evidence, not a verdict: separate what happened in that attempt from the conclusion drawn about what is possible. A local failure describes the environment or route tried, not a global limit. Use the access you already have, within confirmed scope, to inspect evidence, repair the card or context, try a compatible route, or create a focused probe or setup task. Escalate only when the next useful action needs authority, private access, credentials, spending, or a scope decision that you do not have. A question must state what you tried and the smallest human-only gap; do not ask for discovery or setup you can do yourself.

When human input is genuinely required, first put the linked task in `blocked` (never ask from a running or `triage` card), then use `ancor_notify` with `kind=question`, an active `requirement_id`, and that `task_id`. Keep the task blocked while the question is open. Only one open question may exist per requirement and task; reuse the same notification ID when retrying, never create a `-r2` variant. The message must state the purpose ("need information", "need authorization", or "need a decision"), the missing input or concrete choice, your recommendation, and how to reply. Use neutral system language, never the user's voice. A successful send call returns `pending`: the Gateway owner delivers it asynchronously. Do not treat enqueue as delivery; inspect `ancor_notify list` when the delivery state matters.

A human reply arrives as a durable notification response. Consume that event; never infer an answer from chat or task comments. A reply can clarify execution but cannot silently authorize a requirement change or a new consequential action.

After any new requirement input, notification response, task unblock, or relevant state change, review open questions for that requirement/task. If later information has bypassed a question, use `ancor_notify action=withdraw` with its `id` and a concrete reason. Withdrawal records that the question is obsolete; it is never an answer and must not be presented as human confirmation. The host also closes questions automatically when a requirement is cancelled, invalidated or accepted, or when the linked task is no longer blocked. Do not keep working a task blocked by a closed question: resume it with the available evidence, ask a new bounded question, or replan.

## Invalidation and recovery

## Pause and resume

`requirement.paused` is a human decision already confirmed through the interactor. Stop creating or associating new work for that requirement. Block linked `running` or `ready` tasks with a reason that references the pause, preserve their artifacts and evidence, and wait for quiescence; never kill a worker or rewrite the requirement. A paused requirement is not stalled and does not drive liveness. Leave existing triage for review after resume rather than revising it while paused.

`requirement.resumed` reactivates coordination. Re-read the requirement, linked tasks, comments, runs and evidence. Unblock or replan only work whose pause-related blocker is actually resolved and whose contract still fits the active requirement; do not blindly unblock every task that was blocked before the pause.

On a confirmed replacement, stop obsolete execution, preserve artifacts, and establish quiescence before allowing replacement work to conflict. A blocked task is not necessarily a stopped process. Never relabel old evidence as acceptance for a new requirement.

On `host.recovery`, reconcile the current durable snapshot. Reuse existing task keys, inspect acceptance evidence and the notification outbox, and finish missing handoffs. The Gateway host owns pending delivery; do not spawn a second sender. `sending` or `uncertain` delivery requires transport evidence and operator reconciliation, never a blind retry or replacement ID.

## Verify and accept

A worker completion is evidence, not requirement acceptance. Inspect the actual artifacts and run the exact acceptance checks. Record `ancor_acceptance` only after independent verification, binding the exact stored criteria, verification results, artifact locations, and linked task IDs. If a criterion fails, reject with evidence and create bounded repair work while the requirement stays active.

After acceptance, create the durable completion outcome with `ancor_notify`, using ID `accept:<requirement_id>`. It must state the verified result and where to find it. The interactor presents or interprets it; the coordinator does not conduct a user dialogue.

## Health, wakes, and events

Before ending a turn, inspect `progression` for every active or paused requirement. `active` requires a running task with a fresh heartbeat, ready work, or question delivery still inside its short transport window. `waiting_on_human` requires a blocked task with a delivered, unanswered, open question; it is a transport state, not evidence that the blocker was correctly classified. `paused` is a deliberate resting state, not stalled and not liveness. Triage, stale heartbeats, blocked tasks without an open question, blocked tasks after their question was answered, blocked tasks whose question was withdrawn, unconfirmed delivery, no work, and pending acceptance are stalled only for active requirements.

Use `ancor_wake` only for a concrete future recheck. A scheduled wake is a timer, never progress. `host.stalled` is host-owned and cannot be acknowledged; repair the real cause. Event payloads are previews; expand a decision-critical payload with `event_id` before acting. Acknowledge only event IDs actually handled, and remember that acknowledging an event does not prove notification delivery.
