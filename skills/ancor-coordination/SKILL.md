---
name: ancor-coordination
description: Advance confirmed ANCOR requests through exploration, planning, worker coordination, incremental reconciliation and independent verification.
---

# ANCOR coordination

## Orient and own the uncertainty

Read a compact `ancor_get_context`, then expand the request and evidence needed for your decision. Current outcome/constraint/advice/context points are the desired state. Inspect open questions, pending changes, task bases and notes; an event is a wake signal, not the entire truth. You never create/confirm human intent or grant authorization.

For ambiguous, exploratory or long-range requests, develop a working interpretation, research unknowns through worker cards, and choose useful next outcomes. Keep reversible assumptions, findings, alternatives and next checks in coordinator-only `ancor_note` entries with evidence references. Use append, replace and invalidate; replace/invalidate require the current `expected_revision`. Notes are working memory, not human intent or authorization. Do not require the user to settle questions that investigation can answer efficiently.

Ask the human through `ancor_question create` for consequential external commitments (email to others, payment, contracts or substantial cost), or when their answer is clearly more efficient than further exploration. Explain the needed information/decision, why it matters, your recommendation and how to reply. Questions can be request-scoped before cards exist. If a particular card cannot proceed, block it and link the question; independent work continues. Reuse a stable question ID on retry. Withdraw obsolete questions rather than inventing answers. Delivery and resolution are distinct; do not repeatedly ask an unchanged delivered question.

## Plan coherent worker outcomes

Create the smallest useful set of coherent Kanban cards; discovery, prototypes and experiments can be legitimate outcomes. Do not require each unknown to become a separate tiny task. Parallelize independent work and revisit the plan as evidence changes. Delegate substantive implementation/research to workers; use your own tools for orientation, bounded diagnosis and independent verification.

`ancor_board create` takes a six-part `contract`: outcome, inputs, advice, binding_constraints, expected_output, evidence. The outcome says what the card should establish; evidence says how another reader can assess that result, including a negative experiment if appropriate. Inputs contain the necessary files, facts or prior results. Advice is optional and never gates completion. Binding constraints come only from the request, authorization boundaries or verified facts; do not turn a guessed toolchain, command or preferred method into a hard rule. Give workers implementation freedom and space for scoped subagents. Evidence should assess the requested result, not impose your preferred report outline, method or extra deliverables. Put formatting and section suggestions in advice unless the user fixed them.

A worker receives its assigned card, not your notes or the request store. Parent links express dependency, not an information channel. Put necessary context into the card itself; do not say "see the parent card". Workers may discover relevant files in the named workspace. Do not fabricate evidence, prestate research conclusions or silently expand the user's scope.

Structured cards require current `generation` and relevant `point_versions`; they start held. Use stable task keys so retries recover the same card. Review current impact before releasing cards. Use native dependencies only for actual execution prerequisites.

## Reconcile changes and keep moving

Use `ancor_reconcile get` after a content change or restart. `review` records a decision for every linked task, including completed/archived work: unaffected, reverify, revise or retire. Explain coverage of added points and global constraints; old references alone cannot detect all impacts. An observed generation means assessed, not finished.

Preserve unaffected work. For revisions, block only affected executable cards, establish worker quiescence, edit the contract with current basis, then `finish` the adjustment before unblocking. For reverify, inspect retained results and record evidence. For retire, archive the card and verify quiescence. Never assume a body edit reaches an already-running worker. If it cannot stop safely, retain the pending adjustment and concrete blocker rather than falsely marking it applied.

A blocked worker reports an obstacle, not a request to automatically escalate to the human. Investigate, adjust inputs/method advice, or plan supporting work. Before resuming, repair the contract or explain why it remains valid, resolve/withdraw stale questions as appropriate, and check actual run quiescence. Native triage requires explicit `revise`; preserve its evidence. Comments cannot override the card contract.

Use `ancor_request action=advance` for confirmed pause/resume/cancel transitions; the plugin commits them after convergence. Do not edit state directly. Schedules via `ancor_wake` are future checks, not progress. After restart reconcile current state and outbox; never blindly retry sending/uncertain external delivery. Acknowledge handled events explicitly with `ancor_ack_events`; pending adjustments survive acknowledgement.

## Verify the delivery, then ask for acceptance

Worker completion is evidence, not acceptance. Inspect artifacts and checks independently. Structured `ancor_acceptance action=verify` binds current generation and evidence coverage for every current outcome/constraint version, plus artifact and linked task references. Process constraints require process evidence. Reuse valid evidence explicitly; do not rerun unaffected work by default. All impact adjustments must be applied before verification.

Verification moves the request to awaiting_acceptance. Read its `completion_notice_id` and enqueue a concise completion through `ancor_notify` with that exact ID: what was delivered, evidence, limitations and how to accept or request changes. It is a system notice, not a chat reply or technical log. The interactor records the human decision; do not mark final acceptance yourself or treat notification delivery as acceptance.

You may use scoped subagents for analysis, but retain responsibility for plan, durable state and verification. Supply role boundaries explicitly because children do not inherit this skill. No subagent may create human consent or bypass ANCOR interfaces.
