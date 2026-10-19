---
name: ancor-coordination
description: Use when confirming ANCOR requirements or coordinating work.
---

# ANCOR coordination

## User conversation

Do light work yourself. For substantial work, use `ancor_requirement` to draft a
brief and acceptance criteria. Explain scope, constraints and relevant tradeoffs;
present the complete draft in the current conversation, then call native `clarify`
with the exact returned `clarify_args`.
Check `ancor_get_context` to verify activation before saying work was handed off.

For a change, draft the complete replacement with `replacement_of` set to the
current requirement ID. Confirmation invalidates that requirement and creates a
new one. A stale proposal needs a fresh draft and confirmation. Do not create
requirement or change cards in Kanban.

## Background coordination

Start from `ancor_get_context` and the board. Pending events help direct attention;
consider all current requirements, ongoing work and conflicts, not just the batch.
On `host.recovery`, reconcile the current durable snapshot rather than replaying
old decisions: reuse existing task keys, inspect acceptance evidence and the
outbox, and finish missing handoffs. An accepted requirement without a notice
needs a real coordinator-authored completion, not another acceptance. Reuse the exact
stored message for a pending notification. Never resend `sending`/`uncertain`
notifications or replace their IDs; reconcile transport evidence first. Sent
completions are finished. Acknowledge recovery once handled or durably waiting;
waiting alone is not a reason for repeated coordination turns.

- Resolve blockers and execution errors before asking the user. Inspect attempts
  and evidence; clarify task context, unblock safely or create focused support work.
- Plan incrementally through `ancor_board`. The coordinator performs decomposition;
  Kanban only stores dependencies and dispatches execution. Supply `parent_ids`
  during creation to establish dependencies atomically. Use `hold=true` while
  arranging a graph, then unblock when ready; do not expose a ready task before
  adding its prerequisites. For replacement dependencies, hold the child or add
  the new prerequisite before unlinking the obsolete one. Reuse stable task keys.
- Use `edit` for task title/body/priority within confirmed scope, and `link`/`unlink`
  for dependencies. A repeat-blocked task may enter triage: inspect the cause,
  record a remedy and use explicit `revise`, not repeated unblock attempts.
  Abandoned blocked work can be explicitly `retire`d after quiescence with a reason;
  history stays intact and retirement is not fabricated completion.
- Parallelize independent work. Before creation, consider shared files, services,
  accounts and capacity across requirements. Sequence conflicting work and ask
  the user to resolve contradictory requirements.
- On invalidation, stop obsolete execution. A blocked task is not a stopped
  process. Establish quiescence before replacement work can conflict; preserve
  artifacts and never relabel old evidence as new-version acceptance.
- Inspect completed artifacts and run acceptance checks against the exact current
  criteria. Record `ancor_acceptance` only after verification. If a criterion fails,
  reject with evidence and create bounded repair work; the requirement stays open.


Record useful decisions on tasks, then acknowledge only the supplied event IDs
that have been handled. New events remain pending. An unresolved human question
should be durable and explicit, not a reason to spin or repeatedly send it.

## Waiting and liveness

Before ending a turn, inspect `progression` for every active requirement. Identify
its real next event source: native work/dependency, a durable human question, or
an explicit timed recheck. Missing-source warnings require a plan, not repeated
self-wakes. Use `ancor_wake` schedule with requirement_id, a concrete reason, and
future due_at (Unix seconds). List before scheduling; replace by wake_id when the
plan changes, cancel obsolete wakes. Registration itself does not wake the coordinator.
Due wakes enter the ordinary serialized inbox and require explicit acknowledgement.
Invalidated/accepted requirements suppress obsolete wakes. A long wait may be
legitimate: do not move it earlier just to keep the system busy.

`host.liveness` is insurance while any requirement is active, not a work plan.
Inspect current work, repair missing progression sources, then acknowledge it.
The coordinator's first ANCOR tool call records actual coordination and resets
the two-hour deadline once per child session; process creation, queued events and
failed CLI startup do not. Scanner outages, launch failures, or an existing stuck
coordinator can make it overdue: report the infrastructure problem, never start
an overlapping coordinator. No active requirements means no periodic insurance calls.

## Human communication

Use `ancor_notify` with a stable ID for questions and `accept:<requirement_id>`
for accepted completion. On a user's reply, use its `respond` action to preserve
the answer and enqueue coordination. Use the configured native user destination,
never raw task-event subscriptions.
Open each notification with its purpose: “需要你提供信息”, “需要你授权”,
“需要你决定”, or “完成了一项需求”, followed by the requirement's short name.
For requests, state the missing input or concrete action/decision, your recommendation
and how to reply. For completion, state the result and where to find it.
Explain what matters, what is needed and your recommendation. Keep routine
progress quiet; do not dump IDs or logs unless useful. Confirm transport success
before treating a question or completion notice as delivered. If delivery is
unavailable, retain the pending outcome and report the infrastructure issue.

A reply can clarify execution but cannot silently authorize a requirement change
or a new consequential external action. Preserve its requirement context and
wake coordination.
