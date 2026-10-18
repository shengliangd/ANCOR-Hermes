# Independent confirmed requirement change

This is an ordinary non-triage task assigned to `ancor-leader`, not a child gated
by the root. Fields below are body conventions, not native API fields.

- Stable confirmation_id / question_id (if applicable):
- Persisted response/source / explicit background resume receipt:
- Cross-root resource conflicts / admission owner / required ordering:
- Root ID / board:
- Base version (current applied version observed before confirmation):
- Target version:
- Exact proposed delta and consequences:
- Target acceptance criteria:
- Explicit user confirmation reference/date and approved text:
- Idempotency reference (same confirmation must not create duplicate work):
- Affected running/queued tasks and dependencies (background inspection required):
- Preserved artifact locations and reuse/revalidation plan:
- Independent background leader API handoff / wake-only subscription read-back
  (raw session ID, owner `ancor-leader`, no user-channel subscription):
- Leader's post-processing user-message decision: input/authorization, material
  impact, final verified delivery, or quiet; never a raw task-event notification.
- Milestone: confirmed; application and delivery are separate.

## Background application record

- Root-scoped soft coordination owner/run/phase and competing changes:
- Base-version recheck; stale-base review and any renewed confirmation:
- Stop requests on affected tasks, acknowledgements and observed quiescence:
- Queued downstream holds and dispatch-race rechecks:
- Root target-version update/read-back and applied change reference:
- New execution task IDs, dependency/assignment checks and background wake-only subscriptions:
- Remaining blockers / recovery pointer / released ownership:

Do not claim applied until root read-back succeeds. Do not dispatch conflicting
replacements without stop acknowledgement and verified quiescence. Change-card
completion records application/handoff, not root or user acceptance.
