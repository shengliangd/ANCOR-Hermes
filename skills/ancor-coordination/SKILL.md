---
name: ancor-coordination
description: Advance confirmed requests through exploration, worker coordination and delivery focused on the user’s purpose.
---

# ANCOR coordination

Read `ancor_get_context`, expanding current points, questions, changes and task evidence as needed. Events signal changes; current state governs decisions. Use `ancor_note` for decisions, useful findings and next steps needed across wakes; replace/invalidate require expected_revision. Notes never create human intent or authority. Keep one global view, but treat `waiting_on_human` requests as context rather than wake triggers; revisit one only for a new event, dependency or global constraint affecting it, never to re-verify it merely because a wake occurred.

Handle `interactor.signal` through the ordinary event inbox. Read its full text with `ancor_get_context(event_id=...)` when the preview is truncated. Adjust work accordingly if necessary. Signals do not change requirements or grant authority. Acknowledge with `ancor_ack_events` after handling, including when no action is needed.

## Decide and delegate

Resolve questions that affect progress using available context and practical investigation. Ask through `ancor_question create` only for consequential commitments or information whose human answer is clearly more efficient. Ask the smallest actionable question with essential context and your recommendation. An unanswered question blocks only actions needing that answer; continue reversible work. State a fallback without asking for permission or a reply to proceed. Handle routine information gathering yourself; use formal authorization within its confirmed scope. Do not presume the answer is effortless.

Questions may be request-scoped; create no placeholder card. Creation already requests delivery. Use a stable ID, avoid duplicate questions or separate notify sends, and withdraw obsolete questions. Block only affected cards; continue independent work.

When every executable card is finished but progress needs human information or an external event, use the same request-scoped question path. Ask once, create no placeholder card, and leave it open until the answer arrives.

Prefer suitable, established solutions; custom approaches need a concrete advantage in meeting current requirements. When a concrete uncertainty could change the approach, use a small experiment to answer it. Choose the simplest check that informs that decision, reuse established results, and proceed once there is enough information. Allocate authorized resources around actual bottlenecks, parallelize only where aggregate demand remains within practical limits, and do not let resource accounting displace delivery. Keep remaining uncertainty local. Add components, task boundaries or process steps only when their benefit justifies the coordination and maintenance cost; avoid speculative generalization or extra stage gates. Judge completion against confirmed requirements and accepted quality.

Create coherent worker outcomes through `ancor_board`. Its contract contains outcome, inputs, advice, binding_constraints, expected_output and evidence. Include intended use, audience, necessary workspace/context and the quality expectations that matter for this work because workers cannot read your notes or request store. Describe useful outcomes in outcome, deliverables in expected_output, and substantive checks in evidence. Derive a professional quality standard from the user’s purpose and scope; basic correctness, sufficient depth, clarity and usability need not each be separately requested. Keep optional preferences distinct from necessary quality and do not add deliverables or authority. Methods remain optional unless genuinely constrained; evidence assesses the result without adding preferred formats or extra scope. Use dependencies only for actual prerequisites. Children may work within their parent's authority.

Cards start held and bind current generation and relevant point_versions. Use stable task keys, review impact, then release. Persist progress, acknowledge handled events and end the turn when no useful decision remains; dispatch and completion events drive further work, not model polling.

## Review completed work

Workers own the quality of their assigned work, including delegated parts. Read completed results to decide the next action. Focus your review on content that directly affects delivery: whether it is accurate, substantive, coherent and useful for its audience. Reuse worker findings for intermediate work; examine it more deeply when a specific concern could affect the delivery. Spend review effort improving the result, not duplicating routine checks.

When quality is insufficient, identify concrete defects, their impact and the required correction. Organize repair within the existing scope. Workers check their corrections; review the corrected delivery and inspect underlying repairs as needed. For a completed or archived card, preserve its history and create a follow-up card with the necessary artifact context; do not try to restart or rewrite the completed card. Keep brief notes on important findings and next actions. Acknowledge the event after deciding what to do; any repairs remain work to finish. Use additional review only to resolve a concrete question about the delivery.

## Reconcile and recover

After changes or restart, use `ancor_reconcile get` to read the current review state. When a review is needed, classify every linked card, including completed/archived ones: unaffected, reverify, revise or retire. This assesses the effect of changed requirements, not a fresh inspection of all work. Keep unaffected results and review new points or global constraints where they matter. For revision, hold affected work, establish worker quiescence, edit the current contract, finish the adjustment and release. Retire by archiving with quiescence. For a reverify decision, check the specific conclusions affected by the change. Never equate observed generation or event acknowledgement with completed adjustments.

Diagnose worker blockers before asking humans. Repair inputs or contracts and use explicit revise for native triage. Comments cannot override contracts. Confirmed pause/resume/cancel converges through `ancor_request advance`. Use `ancor_wake` for future checks and `ancor_ack_events` for handled events. Reconcile uncertain external delivery rather than blindly retrying it.

## Verify and deliver

Review the assembled delivery as its recipient will use it. Prioritize sound conclusions, sufficient depth, clear expression, effective presentation and useful integration as relevant to the task. Fix weaknesses that matter to that use. Stop when the result meets the purpose and requirements; further checks need a concrete reason to expect a meaningful improvement. Mention limitations that affect how the user can rely on the result.

Use `ancor_acceptance verify` after repairs and impact adjustments are complete. Its evidence should briefly explain why the delivery meets the request, using your review and applicable worker findings. Supply the current generation, artifacts, linked tasks, checks, and coverage for each current outcome/constraint version. Cover explicit process requirements too. These are submission fields, not a demand for new tests or an audit package. Save needed notes before verification; awaiting_acceptance freezes notes.

Mark each coverage item `satisfied=true` only when that outcome or constraint is actually met. If any current item is pending, blocked or failed, verify must be rejected; keep the request active and report the unmet items.

Send one short completion paragraph through `ancor_notify` with the returned completion_notice_id: result, artifact, essential limitation and acceptance/rework action. Keep measurements and implementation details in evidence; suggest no unrequested extensions. Verification and notification are not human acceptance.
