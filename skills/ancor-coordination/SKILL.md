---
name: ancor-coordination
description: Use when confirming ANCOR requirements or coordinating work.
---

# ANCOR coordination

## User conversation

Do light work yourself. For substantial work, use `ancor_requirement` to draft a
brief and acceptance criteria. Explain scope, constraints and relevant tradeoffs;
present the complete draft in the current conversation, then call native `clarify`
with the exact returned `clarify_args`. No separate destination or delivery receipt
is needed. Do not manufacture confirmation or substitute a summary.
Check `ancor_get_context` to verify activation before saying work was handed off.

For a change, draft the complete replacement with `replacement_of` set to the
current requirement ID. Confirmation invalidates that requirement and creates a
new one. A stale proposal needs a fresh draft and confirmation. Do not create
requirement or change cards in Kanban, or replan execution in the user turn.

## Background coordination

Start from `ancor_get_context` and the board. Pending events help direct attention;
consider all current requirements, ongoing work and conflicts, not just the batch.

- Resolve blockers and execution errors before asking the user. Inspect attempts
  and evidence; clarify task context, unblock safely or create focused support work.
- Plan incrementally through `ancor_board`. Create only bounded work that is ready
  to start; reuse stable task keys on retries. Workers execute, the leader coordinates.
- Parallelize independent work. Before creation, consider shared files, services,
  accounts and capacity across requirements. Sequence conflicting work; do not
  invent a resource lock or silently choose between contradictory requirements.
- On invalidation, stop obsolete execution. A blocked task is not a stopped
  process. Establish quiescence before replacement work can conflict; preserve
  artifacts and never relabel old evidence as new-version acceptance.
- Inspect completed artifacts and run acceptance checks against the exact current
  criteria. Record `ancor_acceptance` only after verification. If a criterion fails,
  reject with evidence and create bounded repair work; the requirement stays open.
- Notify the user after successful requirement acceptance. Ask for human help only
  when judgment, authorization or an unavailable fact is genuinely needed.

Record useful decisions on tasks, then acknowledge only the supplied event IDs
that have been handled. New events remain pending. An unresolved human question
should be durable and explicit, not a reason to spin or repeatedly send it.

## Human communication

Use `ancor_notify` with a stable ID for questions and `accept:<requirement_id>`
for accepted completion. On a user's reply, use its `respond` action to preserve
the answer and enqueue coordination. Use the configured native user destination,
never raw task-event subscriptions.
Explain what matters, what is needed and your recommendation. Keep routine
progress quiet; do not dump IDs or logs unless useful. Confirm transport success
before treating a question or completion notice as delivered. If delivery is
unavailable, retain the pending outcome and report the infrastructure issue.

A reply can clarify execution but cannot silently authorize a requirement change
or a new consequential external action. Preserve its requirement context and
wake coordination; do not directly resume work from the foreground.
