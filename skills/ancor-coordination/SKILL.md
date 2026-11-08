---
name: ancor-coordination
description: Advance confirmed requests through exploration, worker coordination, incremental reconciliation and independent verification.
---

# ANCOR coordination

Read `ancor_get_context`, expanding current points, questions, changes and task evidence as needed. Events signal changes; current state governs decisions. Keep findings, reversible assumptions and next steps in `ancor_note`; replace/invalidate require expected_revision. Notes never create human intent or authority. Keep one global view, but treat `waiting_on_human` requests as context rather than wake triggers; revisit one only for a new event, dependency or global constraint affecting it, never to re-verify it merely because a wake occurred.

Handle `interactor.signal` through the ordinary event inbox. Read its full text with `ancor_get_context(event_id=...)` when the preview is truncated. Adjust work accordingly if necessary. Signals do not change requirements or grant authority. Acknowledge with `ancor_ack_events` after handling, including when no action is needed.

## Decide and delegate

Investigate uncertainty before escalating. Ask through `ancor_question create` only for consequential commitments or information whose human answer is clearly more efficient. Ask the smallest actionable question with essential context and your recommendation. An unanswered question blocks only actions needing that answer; continue reversible work. State a fallback without asking for permission or a reply to proceed. Own retrieval and recordkeeping; use formal authorization only within its confirmed scope. Do not presume the answer is effortless.

Questions may be request-scoped; create no placeholder card. Creation already requests delivery. Use a stable ID, avoid duplicate questions or separate notify sends, and withdraw obsolete questions. Block only affected cards; continue independent work.

When every executable card is finished but progress needs human information or an external event, use the same request-scoped question path. Ask once, create no placeholder card, and leave it open until the answer arrives.

Prefer suitable, established solutions; custom approaches need a concrete advantage in meeting current requirements. When uncertainty affects the route or feasibility, arrange short experiments at low cost that tests the complete path and evaluates relevant quality, cost and throughput. Reuse valid evidence. Once sufficiently validated, move to formal execution, allocate authorized resources around actual bottlenecks and parallelize independent work. Keep remaining uncertainty local. Add components, task boundaries or process steps only when their benefit justifies the coordination and maintenance cost; avoid speculative generalization or extra stage gates. Judge completion against confirmed requirements and accepted quality.

Create coherent worker outcomes through `ancor_board`. Its contract contains outcome, inputs, advice, binding_constraints, expected_output and evidence. Include necessary workspace/context because workers cannot read your notes or request store. Methods remain optional unless genuinely constrained; evidence assesses the result without adding preferred formats or extra scope. Use dependencies only for actual prerequisites. Children may work within their parent's authority.

Cards start held and bind current generation and relevant point_versions. Use stable task keys, review impact, then release. Persist progress, acknowledge handled events and end the turn when no useful decision remains; dispatch and completion events drive further work, not model polling.

## Reconcile and recover

After changes or restart use `ancor_reconcile get/review` for every linked card, including completed/archived ones: unaffected, reverify, revise or retire. Cover new points and global constraints explicitly. For revision, hold affected work, establish worker quiescence, edit the current contract, finish the adjustment and release. Retire by archiving with quiescence; reverify retained evidence. Never equate observed generation or event acknowledgement with completed adjustments.

Diagnose worker blockers before asking humans. Preserve evidence; repair inputs or contracts and use explicit revise for native triage. Comments cannot override contracts. Confirmed pause/resume/cancel converges through `ancor_request advance`. Use `ancor_wake` for future checks and `ancor_ack_events` for handled events. Reconcile uncertain external delivery rather than blindly retrying it.

## Verify and deliver

Inspect artifacts independently and reuse valid prior evidence. Finish impact adjustments and save notes before `ancor_acceptance verify`. Evidence binds current generation, artifacts, linked tasks, checks, and exactly every current outcome/constraint version; process obligations need process evidence. Awaiting_acceptance freezes notes.

Mark each coverage item `satisfied=true` only when that outcome or constraint is actually met. If any current item is pending, blocked or failed, verify must be rejected; keep the request active and report the unmet items.

Send one short completion paragraph through `ancor_notify` with the returned completion_notice_id: result, artifact, essential limitation and acceptance/rework action. Keep measurements and implementation details in evidence; suggest no unrequested extensions. Verification and notification are not human acceptance.
