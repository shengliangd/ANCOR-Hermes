# Worker execution brief

Use for ordinary execution and supplemental repair: this is the same execution
card convention, not a fourth card type. Put review feedback on the affected
worker's own task as well as the root rejection record.

- Card convention: execution; implementer: `ancor-worker`.
- Root ID / board / current approved requirement version:
- Approved scope, constraints and observable acceptance criteria:
- Artifact locations and reproducible checks:
- Prerequisites; execution -> root dependency read-back:
- Independent background leader API wake-only subscription read-back (raw session
  ID, owner `ancor-leader`, no user-channel subscription; or explicit blocker):
- Events only wake that leader; workers report on the board, never directly notify
  the user. Leader processes the event and decides whether input/authorization,
  material impact, or final verified delivery warrants a user message.

## Mandatory worker checkpoint contract

- Resource keys / read-write modes / capacity / admission owner and evidence
  (or explicit `none`); approved version and cancellation/change IDs:
- Read own task, root version, comments and admission before the first write,
  before each substantial side effect, at heartbeat/checkpoints and before handoff.
- If version is stale, a stop is requested, or admission is missing/conflicting,
  cease new writes and safely stop owned subprocesses; do not silently relabel work.
- Preserve partial artifacts outside disposable workspaces. Post stop acknowledgement
  on this task with task/run/version, stop request ID, checkpoint, artifact paths
  and subprocess disposition. Use supported block/handoff and end the attempt.
- Acknowledgement is not observed quiescence. The coordinator must separately
  verify run/process cessation before releasing resources or dispatching replacements.
  Unknown subprocess state remains blocked; never claim success or kill unrelated work.
- Handoff binds requirement version to exact artifact revision/digest, test commands
  and results, resource release evidence and unresolved limitations.

This authoritative template supplements the installed worker role; never bypass
protected SOUL writes to install it. Before decomposition, the coordinator installs
this complete file at a durable absolute worker-readable path outside disposable
workspaces, verifies its bytes and records path + SHA-256 on the root. Each worker
reads its root and this full file and verifies the digest before writes. A child
may reference the root's contract instead of copying this prose; summaries and
ellipses are not authoritative substitutes. Missing/unreadable/mismatched source
blocks execution, but omission of a verbatim child-body copy does not. Task-specific
scope, current version, resources and admission must still be explicitly resolved;
the template's blank fields do not supply authorization.

## Rejection / repair (when applicable)

- Failed execution task / review run / rejection evidence reference:
- Failed approved criterion, observed result, expected result, reproduction:
- Bounded repair and regression checks; no unconfirmed scope expansion:
- Route: active review `request-changes`, unclaimed review `reopen-review`,
  or supplemental execution for an already done task:
- Actual status / implementer provenance / restored worker assignment:
- Prior attempt and preserved artifacts; do not overwrite failure history:
- Root remains unaccepted and unfinished; repair gates root, not the reverse:
- Worker handoff: versioned artifact/test evidence; request review with
  `ancor-leader` in the review lane, otherwise complete only execution:
- Leader re-review: failed criterion and regression results, version and
  pending-change recheck, remaining blockers (not invented user acceptance):

New goals or changed approved criteria belong in the confirmed change procedure,
not this repair section. A done task is not reopened by `reopen-review`.
