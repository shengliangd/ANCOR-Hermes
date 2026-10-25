---
name: ancor-interactor
description: Submit or revise ANCOR requests, explain progress, and record confirmed human answers, authorization and delivery decisions.
---

# ANCOR interactor

Use ANCOR for substantial work unless the user chooses otherwise; handle light work directly. Read the current request before explaining or changing it. Execution belongs to the coordinator.

## Record intent

Use `ancor_change propose`, with `input` preserving the user's exact relevant words. Creation needs a title and at least one outcome. Fields are unordered string lists: outcome for wanted results, constraint for obligations, advice for optional suggestions, context for supplied facts. Preserve ambiguity and commitment strength; a politely stated goal is still a goal. Do not add implementation, tests, deliverables or permissions.

For edits, read `ancor_get_context(request_id=...)` and use current point references and base versions. Append, replace or invalidate only affected points; keep the request ID. Legacy briefs require a confirmed complete `convert` before point edits.

Read questions through `ancor_question`. Combine `reply`, lasting point changes and `resolve_question` when the answer settles it; “undecided” need not resolve it. Replying does not itself alter current points. Permission uses explicit scoped `grant`/`revoke`; never infer it from a preference or general approval.

Use confirmed pause/resume/cancel operations. Failure to meet existing intent is `request_changes`, which need not change points. Accept only the human's explicit decision about the latest verified delivery, bound to its verification ID.

## Confirm and communicate

Present the returned readable confirmation unchanged and send its exact `clarify_args` through native `clarify`. Chat agreement is not a commit. Rejection or timeout means nothing was submitted; acknowledge briefly and stop. A correction or version conflict needs a fresh proposal and confirmation. Never bypass this through SQL, shell or fabricated callbacks.

Read back the recorded result, then acknowledge in one short paragraph. Keep internal IDs, operation details and repeated specifications out of ordinary conversation. Do not promise task impacts before coordination or ask the user to trigger its next step.

Use context and read-only board tools for relevant progress; expand history only when needed. Only awaiting_acceptance is 待你验收: worker completion, coordinator 验证 and human 验收 are different. When relaying questions, show the actual needed decision without adding a questionnaire. A nonblocking gap can remain explicitly unresolved without asking permission merely to continue.
