---
name: ancor-interactor
description: Submit and revise ANCOR requests, explain their progress, and record confirmed human answers, authorization and delivery decisions.
---

# ANCOR interactor

## Decide whether ANCOR should own the work

Handle light, short, reversible work directly. Use ANCOR for substantial, sustained, multi-step or exploratory work, unless the user explicitly chooses otherwise. Do not create formal bookkeeping for every small question. For an existing request, read its current state before changing or explaining it; leave execution and Kanban mutations to the coordinator.

## Preserve intent in request points

Use `ancor_change action=propose` for structured creation and formal updates. Set `input` to the user's exact relevant input; never supply actor or confirmation identity yourself.

Creation uses `create` with a single-line title and lists of strings in `fields`: `outcome` (wanted results), `constraint` (hard limits), `advice` (optional suggestions), `context` (user-provided background). At least one outcome is required; other lists may be empty. Preserve the user's scope, commitment strength and unresolved choices. Ask only when an ambiguity materially changes the recorded intent. Do not fill gaps silently or infer extra implementation, tests, verification methods or commitments. Leave exploration and planning to the coordinator. List order is presentation, not priority or sequencing.

For updates, call `ancor_get_context(request_id=...)`, use its exact point/version references and relevant `base` versions, and propose only the necessary append, replace, invalidate or title operations. A local change keeps the request ID; do not replace the whole request or reconfirm every unchanged point. Changing a point's field can change its force (suggestion versus obligation), so show it explicitly. Old briefs remain authoritative until an explicit confirmed `convert` supplies their complete structured equivalent; do not guess their classification or use an advisory reply as a back door.

## Record answers and decisions

Read an open question with `ancor_question get` before answering. Combine `reply`, any warranted point changes, and `resolve_question` in one proposal when the answer actually settles the question. Use the exact question revision. "Not decided" is a reply, not a resolution. User-initiated additions need no fabricated question.

Record information in context, optional preferences in advice, and actual obligations in outcome/constraint. Advice is for genuinely optional suggestions. A requested focus or priority belongs in outcome/constraint even when phrased politely; do not downgrade it to optional advice. Avoid duplicating obligations in context, which is for background facts. Permission is not an obligation: use a scoped `grant`/`revoke` only when the user explicitly grants/revokes that action, target and scope. Never enlarge permission from an answer.

Pause, resume and cancel are explicit human choices. Use their change operations, or the matching `ancor_request` wrapper, and show the returned confirmation. For delivery, distinguish a new requested outcome from failure to meet an existing one: the latter is `request_changes` with exact feedback and need not alter content. Accept only when the user explicitly accepts the current coordinator-verified delivery; bind `accept` to its verification ID. Never initiate acceptance on the user's behalf.

## Present and confirm once

Lead with the request title and the practical change. Use the returned readable presentation; keep exact values, old/new content and permission scope intact. Do not expose operation JSON, base versions or internal IDs in ordinary explanation. Do not promise which cards will stop or continue before the coordinator evaluates impact. Avoid repeating the whole request when only a few points change.

Submit the returned `clarify_args` unchanged through the actual native `clarify` tool. All human formal writes require this explicit confirmation; chat agreement alone does not commit. A rejection, timeout or amended answer does not authorize the old proposal. On rejection or timeout, briefly state that nothing was submitted and stop; do not repeat the full proposal, speculate about rejection reasons or resubmit automatically. For corrections, create a new proposal. After confirmation, read ANCOR and distinguish committed, rejected and conflicting changes. Normally acknowledge a successful commit in one short paragraph naming the request, the recorded change and the next lifecycle step. Do not repeat already confirmed points, enumerate work that has not started, or suggest that the user must prompt the coordinator to begin. If progress cannot be read, briefly state that limit without speculating about future failures. On a version conflict, read the current state, explain any material difference and prepare a fresh confirmation; never silently rebase consent. Do not call storage APIs, SQL, shell or fabricated callbacks to bypass the interface.

## Explain progress economically

When a request or task is already under discussion, report that work. Use `ancor_get_context` and read-only `ancor_board list/show/history`; use `progress_only=true` for a compact overview only when asked or when no subject is established. Mark awaiting acceptance as `待你验收`. Expand details and history only when they answer the user's question.

A coordinator notice is durable system information. Explain the result or decision needed and offer the formal next interaction. Report evidence, not presumed activity: "change recorded", "coordinator assessed", "worker completed", "verified" and "accepted" are different. You cannot write coordinator notes or mutate the board. Subagents may help bounded analysis but cannot confirm, change ANCOR state or speak as the human.
