# Versioned requirements (0.6)

A requirement keeps its identity while its points change. New structured requirements contain a title and four unordered groups: outcome, constraint, advice and context. At least one active outcome is required. The title is presentation metadata. Each point has a stable identity and immutable content versions. Replacement invalidates an exact old version and appends its successor in one transaction. Withdrawal does not undo external actions.

Coordinator notes have a separate version sequence. Their current projection retains the existing note interface; every new append, replace and invalidate also preserves a historical version and a change-ledger entry. Human-facing change history and the dashboard exclude coordinator notes. Imported notes explicitly mark missing older history: pre-0.6 updates overwrote their bodies, so that history cannot be reconstructed.

## Changes and confirmation

`ancor_change propose` accepts original `input`, a requirement ID (omitted for creation), `base` versions, and `operations`. The returned immutable proposal must pass through the existing native clarify hooks. New proposals store a frozen, human-readable Chinese confirmation in the same transaction: requirement title, exact before/after text, question and answer, lifecycle effects, or delivery evidence. Native choices are “暂不提交” and “确认提交”; a short confirmation reference binds the presentation to the proposal without exposing operation JSON or internal object IDs. Existing pending proposals retain their original confirmation protocol. Neither a model-provided confirmation flag nor proposal creation commits anything. Commit validates the relevant versions again. Repeated commits return the original result. Conflicts require a new proposal and confirmation.

Read base versions from `ancor_get_context(requirement_id=...)` and pass only the keys needed by the change. Content uses `generation` and `control_version`; title uses `title_version` and `control_version`. Notes do not change these versions. The adapter supplies the actor profile, and the confirmation hook records its session/tool-call identity. Human identity is unknown unless a trusted integration supplies it; profile identity must not be represented as a named human.

Operations:

- `create {title, fields}` or `convert {fields}` (the latter for a legacy brief).
- `append {field, body}`, `replace {version_id, body, field?}`, `invalidate {version_id}`.
- `title {title}`.
- `reply {question_id, revision, body}` and `resolve_question {question_id, revision, reason}`.
- `pause/resume/cancel {reason}`, `request_changes {feedback}`, `accept {verification_id}`.
- `grant {action, target, scope}`, `revoke {grant_id}`.

A change may combine a reply, point updates, question resolution and a rework request. At most one lifecycle action is allowed. Acceptance cannot be combined with changes to the requirements being accepted. A reply by itself neither resolves its question nor changes the content. Grants record scoped permission, not an obligation, and do not provision credentials or enforce a universal tool sandbox.

`ancor_change history` is a compact, cursor-paged index; `get` expands a specific record. `ancor_note append/replace/invalidate/history` manages coordinator notes; replace/invalidate require `expected_revision`. Legacy add/update/delete aliases remain available and now preserve history.

## Questions and delivery

`ancor_question create` records a coordinator question and a durable delivery intent in the requirement database. A task is optional; if supplied it must belong to the requirement and be blocked. Host polling forwards the intent idempotently to the existing notification outbox. Missing transport configuration leaves the intent pending. Sent, answered, resolved and withdrawn have distinct meanings.

Reply/content/resolution commit together in the requirement database. Notification closure follows asynchronously; a crash does not lose the authoritative decision. Legacy questions are imported with their original answers as historical input, never silently promoted to new requirements or authorization. Existing structured notification wrappers also create/withdraw the main question record.

Requirement-level questions can put otherwise idle work into waiting-on-human after delivery. Independent ready/running tasks remain executable. Superseded questions are withdrawn, not answered on the user's behalf.

## Incremental coordination

`ancor_get_context` returns current points regardless of event acknowledgement. `ancor_reconcile get` returns the current generation, last reviewed generation, change references, task bases and unfinished adjustments. The host reconstructs an outstanding reconciliation wake from this durable state after acknowledgement or restart, subject to its existing retry budget.

`ancor_reconcile review` must cover every linked task, including completed work. Decisions are unaffected, reverify, revise or retire. `coverage_reason` records how additions and global constraints were considered; old point references alone are insufficient to find all impacts. `finish` records that an individual adjustment has been applied. An observed generation means assessed, not finished. Impact classification and evidence quality remain coordinator judgments.

New structured cards supply `generation` and `point_versions`. They are initially held until reviewed and explicitly unblocked. Existing unrelated cards are not automatically stopped when the requirement changes. Contract edits require an affected card to be stopped and its retained runs to be quiescent; editing the card body cannot update an already-running worker. Current basis or an explicit unaffected decision is required before release. SQLite and native Kanban are not one transaction: held creation and idempotent task keys support recovery. A reused key with a different contract is rejected.

This is asynchronous reconciliation, not an instantaneous cross-process revocation fence. A new prohibition requires the coordinator to stop affected work; actions already performed may require separate remediation. SOUL/SKILL files have not been updated in this change, so a deployment must align those instructions before relying on autonomous adoption of the new workflow.

## Verification and acceptance

Structured evidence binds `generation` and a `coverage` entry for every current outcome/constraint version, plus verification descriptions, artifact references and linked task IDs. Evidence for process constraints must address the process, not just inspect the final artifact. Pending impact adjustments block verification. Content changes invalidate the applicability of the old verification; unchanged evidence may be explicitly reused in the new verification.

Acceptance binds the exact verification attempt. Completion notices use `accept:<requirement_id>:<verification_id>` for structured requirements, obtained from requirement detail. A later verification, including rework without a content change, gets a new notice identity. Old acceptance proposals and old completion notices cannot apply to a newer delivery. Title and note changes do not invalidate content verification.

Legacy requirements and their original APIs remain compatible. For structured requirements, the old pause/resume/cancel/accept/request_changes entrypoints route to confirmed changes; advisory `respond` cannot bypass the new content model.

## Offline migration and rollout

Stop source writers before making a multi-database snapshot, then run:

```sh
python -m ancor.migrate_points /path/requirements.db --output /path/migrated/requirements.db
```

The command copies the source and available notification/confirmation sidecars, host state and immutable presentation files, upgrades the copy, and checks SQLite integrity. It never overwrites the source or an existing output. Old briefs remain verbatim and authoritative until an explicit confirmed conversion. Requirement IDs, task links and original drafts are retained. Existing pending legacy acceptance proposals without version bindings must be prepared again.

The dashboard remains read-only and now displays structured content, questions, current task adjustments and recent change history. Confirmation references and transport secrets remain redacted.

Local validation covers transactional rollback, optimistic conflicts, retries, native confirmation, role boundaries, question/outbox recovery, scoped native card adjustment, current-generation verification, migration and dashboard reads. The isolated native probe uses `ANCOR_HERMES_SOURCE` (or the default installed source path) and a disposable database; it does not launch models or modify a live board. A real model-backed workflow and installation checks remain release gates before deployment. No runtime installation is performed by this repository change.
