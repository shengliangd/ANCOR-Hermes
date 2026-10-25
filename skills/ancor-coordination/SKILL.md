---
name: ancor-coordination
description: Advance confirmed requests through exploration, worker coordination, incremental reconciliation and independent verification.
---

# ANCOR coordination

Read `ancor_get_context`, expanding current points, questions, changes and task evidence as needed. Events signal changes; current state governs decisions. Keep findings, reversible assumptions and next steps in `ancor_note`; replace/invalidate require expected_revision. Notes never create human intent or authority.

## Decide and delegate

Investigate uncertainty before escalating. Ask through `ancor_question create` only for consequential commitments or information whose human answer is clearly more efficient. Ask the smallest actionable question with essential context and your recommendation. An unanswered question blocks only actions needing that answer; continue reversible work. State a fallback without asking for permission or a reply to proceed. Own retrieval and recordkeeping; use formal authorization only within its confirmed scope. Do not presume the answer is effortless.

Questions may be request-scoped; create no placeholder card. Creation already requests delivery. Use a stable ID, avoid duplicate questions or separate notify sends, and withdraw obsolete questions. Block only affected cards; continue independent work.

Create coherent worker outcomes through `ancor_board`. Its contract contains outcome, inputs, advice, binding_constraints, expected_output and evidence. Include necessary workspace/context because workers cannot read your notes or request store. Methods remain optional unless genuinely constrained; evidence assesses the result without adding preferred formats or extra scope. Use dependencies only for actual prerequisites. Children may work within their parent's authority.

Cards start held and bind current generation and relevant point_versions. Use stable task keys, review impact, then release. Persist progress, acknowledge handled events and end the turn when no useful decision remains; dispatch and completion events drive further work, not model polling.

## Reconcile and recover

After changes or restart use `ancor_reconcile get/review` for every linked card, including completed/archived ones: unaffected, reverify, revise or retire. Cover new points and global constraints explicitly. For revision, hold affected work, establish worker quiescence, edit the current contract, finish the adjustment and release. Retire by archiving with quiescence; reverify retained evidence. Never equate observed generation or event acknowledgement with completed adjustments.

Diagnose worker blockers before asking humans. Preserve evidence; repair inputs or contracts and use explicit revise for native triage. Comments cannot override contracts. Confirmed pause/resume/cancel converges through `ancor_request advance`. Use `ancor_wake` for future checks and `ancor_ack_events` for handled events. Reconcile uncertain external delivery rather than blindly retrying it.

## Verify and deliver

Inspect artifacts independently and reuse valid prior evidence. Finish impact adjustments and save notes before `ancor_acceptance verify`. Evidence binds current generation, artifacts, linked tasks, checks, and exactly every current outcome/constraint version; process obligations need process evidence. Awaiting_acceptance freezes notes.

Send one short completion paragraph through `ancor_notify` with the returned completion_notice_id: result, artifact, essential limitation and acceptance/rework action. Keep measurements and implementation details in evidence; suggest no unrequested extensions. Verification and notification are not human acceptance.
