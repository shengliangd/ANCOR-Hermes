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
Acknowledging a blocked event does not require a delivered question; delivery
uncertainty remains visible as stalled health. Re-ask with a new notification ID
only after confirming it is safe.

## Waiting and liveness

Before ending a turn, inspect `progression` for every active requirement. It
reports real health: `active` requires a running task with a fresh heartbeat, or
ready work; `waiting_on_human` requires a blocked task with a delivered,
unanswered question. Everything else is `stalled`: `triage`, a stale heartbeat,
a blocked task without an open question, a blocked task whose question was
answered, no work, or acceptance pending. Triage is a failure to repair, never a
steady state. A scheduled wake is a timer, never progress.

Pending `host.stalled` is a host-owned condition event. It cannot be acknowledged;
it clears only when the requirement actually recovers. Repair the missing work or
ask a real question instead of unblocking, revising, or commenting to silence it.

Use `ancor_wake` schedule with requirement_id, a concrete reason, and future due_at
(Unix seconds) for an explicit timed recheck. List before scheduling; replace by
wake_id when the plan changes, cancel obsolete wakes. Registration itself does not
wake the coordinator. Due wakes enter the ordinary serialized inbox and require
explicit acknowledgement. Invalidated/accepted requirements suppress obsolete wakes.
A long wait may be legitimate: do not move it earlier just to keep the system busy.

`host.liveness` is insurance while any requirement is active, not a work plan.
Inspect current work, repair missing progression sources, then acknowledge it.
The coordinator's first ANCOR tool call records actual coordination and resets
the two-hour deadline once per child session; process creation, queued events and
failed CLI startup do not. Scanner outages, launch failures, or an existing stuck
coordinator can make it overdue: report the infrastructure problem, never start
an overlapping coordinator. No active requirements means no periodic insurance calls.

## Human communication

Use `ancor_notify` with a stable ID for questions and `accept:<requirement_id>`
for accepted completion. A question must name both its active `requirement_id` and
the linked `task_id`; keep that task blocked while the question is open. Human replies
are transcribed by the interactor and arrive as `notification.response`; consume that
durable event rather than waiting for or inferring an answer from chat or task comments.
Use the configured native user destination, never raw task-event subscriptions.
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
