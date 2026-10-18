# ANCOR coordination

Use this skill for substantial user work handled through the ANCOR Kanban board.

## Prepare a requirement

1. Understand the goal, relevant context, constraints, non-goals, and acceptance criteria.
2. Keep lightweight analysis, ordinary questions, status checks, and small reversible work in the current session.
3. For substantial work, write a concise execution brief using the linked `templates/requirement-brief.md` template.
4. Ask the user to confirm the brief before creating executable work.
5. After confirmation, create one root task on the `ancor` board in `triage`; the root represents the confirmed requirement.

Keep only decomposition-relevant context in the root body. Do not copy the full conversation or private memory.

## Durable decisions and resume (all sessions)

Use stable `confirmation_id` and `question_id` values, not conversational position.
Persist the exact proposed brief/criteria, root ID (when known), base/target version,
source session/message, question, response and resolution on the relevant card.
Before a root exists, retain the pending brief and response in the originating
persistent session; use its stable confirmation ID to search the board before
creating the root. Where supported, pass that confirmation ID as native
`--idempotency-key`; still inspect archived roots and conflicting payloads because
the native key only deduplicates non-archived tasks. Read back creation and record its ID in that session before
reporting handoff. On retries, reuse the same root; conflicting duplicates are a
coordination blocker, not permission to execute both. This is cooperative
idempotency, not an atomic uniqueness constraint.

A reply in another session must identify the question/confirmation and root;
resolve ambiguity before acting. Foreground persists the response and its source
on the root/affected task, reads it back, then explicitly submits a resume turn to
the configured background API session with those IDs. Comments alone do not wake.
Report persistence and resume separately; retry a failed resume idempotently.
Background rereads the persisted response and current version before resolving a
question or unblocking. Never infer a fresh authorization from an old answer.

## Coordinate native Kanban

- Let the native decomposer create the initial child graph.
- Treat the root as the coordination anchor; inspect descendants, dependencies, comments, handoffs, and events before declaring the requirement complete.
- Search existing tasks before creating work. When decomposition misses work, create focused supplemental child tasks and safe dependencies rather than duplicating or rewriting completed work.
- Keep the board explicit and use native Kanban tools.

### No-fanout is delegation, not leader implementation

Inspect the native decomposition decision. If it returns no fanout and makes the
root runnable, the owning leader creates one bounded worker execution card,
links execution -> root with the supported owning-run guard, and parks its own
root attempt on that prerequisite. Read back the edge and worker assignment.
Do not implement the artifact or complete the root to unblock the worker. If
ownership prevents safe linking, stop and hand off to the owner. Require every
initial/supplemental child to resolve the authoritative execution-brief checkpoint
contract before writes. Before decomposition, install the complete template at a
durable absolute path readable by the actual worker profile, outside disposable
workspaces; read it back and record its path and SHA-256 in root guidance. Children
carry the root ID and contract reference, not an LLM-produced verbatim copy. Workers
read the root and full referenced file, verify the digest, and apply its checkpoints
alongside task-specific scope/version/admission. A reference on the root is sufficient
when the child reads that root; missing copied prose is not itself a blocker. A
missing/unreadable/mismatched contract or missing admission remains a before-write
blocker. Never replace the contract with a summary or treat dispatch as approval.
Do not mutate a pinned contract during an active run; version any replacement and
use the confirmed-change/stop procedure when its requirements change. Installation
must respect protected instruction-write controls; never bypass SOUL protection.

### Cross-demand resources: cooperative admission

Use one shared background coordinator for admission across all roots, not a
separate resource owner per demand. Declare canonical resource keys (resolved
repository/worktree, service/environment, account or output path), read/write
mode, affected scope and required capacity on each execution brief. Explicitly
state `none` for no shared mutable resources. Before writes, inspect all active
roots/tasks using those resources, persist admission owner/session/run, resource
set and version, and re-read competing claims. A shared profile, comment marker
or capacity setting is not a lock and does not serialize API and task runs.

Prefer native prerequisite ordering between conflicting execution tasks, after
checking for cycles; never gate a change on its unfinished root. Hold work until
admission is clear. If two coordinators race, ownership is ambiguous, or resource
coverage is unknown, both must cease conflicting writes and request the shared
coordinator to reconcile. Do not call this race-proof exclusion. Do not archive a
prerequisite to release a resource: archive may release downstream prematurely.
Release admission only after preserved evidence and observed run/process
quiescence; reassess queued work and stale admissions on recovery. For resources
requiring hard exclusion this soft MVP must defer to an existing external lock or
remain blocked, not invent one.

## Three cards and explicit confirmation

- A requirement root belongs to `ancor-leader`; execution children belong to `ancor-worker`; an independent change card belongs to `ancor-leader` and is ordinary, not triage. These are body/comment conventions, not native task types or API fields.
- Record root ID, requirement version and evidence criteria on execution children. Inspect decomposition output and correct assignment before dispatch where possible; model-selected assignment is a soft constraint, not guaranteed routing. Native prerequisites run before their dependents: execution children gate root review, not the reverse. Supplemental execution work must also gate root review.
- Explicit confirmation must cover a specific brief and its acceptance criteria. Record the source/session/message reference when available and date. Proposals, discussion and ambiguous assent must not create executable work or change approved scope. Initial confirmed scope is `v1`.

## Changes: foreground confirmation and durable handoff only

1. Read current root scope/version and pending changes. Draft `templates/change-brief.md` with root ID, `base_version`, `target_version`, delta, consequences and target acceptance criteria. Ask for explicit confirmation of that delta. Until then do not change applied scope/version, stop workers or create executable change/replacement work.
2. After confirmation, search for an existing handoff with the same confirmation reference to avoid duplicate execution. Create an independent ordinary card assigned to `ancor-leader`. Reference the root in its body; **never use `parents=[root]`**, which would gate change processing on old-root completion.
3. Persist the confirmed brief, link the change ID in a root comment, and establish/read back the change's background wake subscription. Creating a runnable change card is the background dispatch handoff; a comment alone is not a wake. Return promptly with “confirmed and handed off,” not “applied.” If persistence or subscription fails, report the incomplete handoff and recover idempotently.
4. Do not reclaim, archive, block workers, rewrite dependencies or dispatch replacements in the foreground. Specific deployment/publication and other consequential external actions still require their own authorization.

## Changes: background leader application

### Capability gate and session ownership

A dispatched `ancor-leader` change run is still a dispatcher-owned task worker,
not the background API coordinator. Its native system instructions prohibit
shelling out to `hermes kanban` for board operations. Do not override that rule
in this skill, unset task/run guards, invoke DB internals, or use raw SQL.
The inspected native tool registry has no edit, unlink or archive tool; enabling
`kanban` for an independent API session adds board routing (`kanban_list` and
`kanban_unblock`), not these missing operations. A shared profile does not confer
additional authority or transfer run ownership.

Before promising full application, verify the actual allowed operation surface
in the intended session. A change run may request stop, collect acknowledgements,
verify quiescence and persist a capability handoff, then block honestly. The
independent API coordinator should reconcile that handoff and escalate missing
capabilities; moving the same prompt to an API session alone is not a verified
fix. Official Kanban guidance reserves CLI operations for humans/automation and
model coordination for tools. Never treat a terminal command that happened to
succeed in a foreground model session as proof of supported API coordination.

With the currently inspected tool surface, root-body replacement and retirement
of an obsolete blocked prerequisite require an authorized native operator or a
separately approved automation surface. This is a boundary of this configuration,
not permission to add a bridge or patch Hermes. Until such a surface is approved
and tested, keep the change blocked and do not claim autonomous change E2E.
Where an authorized native operator performs recovery, preserve the stopped card
and its history; prefer removing only its obsolete dependency after holding the
root and establishing replacement gating. Native `unlink` immediately recomputes
readiness; native `archive` also cleans workspaces. Do not release the last old
prerequisite before safe new gating, and do not mark obsolete work complete.

1. Read root, change, competing changes, execution graph, comments, runs and artifact locations. Verify approval covers this delta. Record a root-scoped soft coordination marker in a comment: change ID, session/run owner and phase. Yield to another active coordinator; inspect its run before declaring a marker stale and record recovery. Re-read ownership and version before each mutation. Shared profile identity is not serialization; this convention is not a lock, lease or CAS.
2. Compare current applied version to `base_version`. If different, stop for stale-base review, compare intervening decisions and seek renewed confirmation when scope or consequences differ. Do not silently rebase or overwrite. Check for already-applied changes and resume idempotently.
3. Inventory affected running and queued descendants. Put stop requests on each affected worker's **own task** with change ID and version. Workers inspect their own comments at safe checkpoints; editing the root does not guarantee steering, and comments do not wake idle sessions.
4. Hold affected queued work using supported native blocking operations where applicable, then re-read for dispatch races. Ask running workers to preserve artifacts outside disposable workspaces, cease writes and acknowledge stopping with task/run/version and evidence. A blocked card is not proof of process termination. Require worker acknowledgement **and** observed run/process quiescence before conflicting replacement work. If either is unknown, block the change and report missing evidence; never fabricate an acknowledgement or dispatch a conflicting substitute.
5. Preserve evidence before cleanup. Reclaim is not a hold: it can immediately redispatch. Archive is non-recursive, can release downstream tasks and can delete workspaces. Inspect/hold the affected downstream set and establish quiescence before retiring individual obsolete prerequisites. Do not archive the root as a tree stop. There is no atomic tree pause; re-check races and escalate when safe ordering cannot be established.
6. Re-read version, marker and confirmation immediately before applying. Persist approved target scope/criteria and `target_version` on the root with change ID/evidence, then read back before calling it `applied`. Old artifacts require explicit target-version revalidation, not automatic relabeling.
7. Create bounded target-version execution tasks assigned to `ancor-worker`, with dependencies gating root review and dynamic subscriptions. Dispatch no conflicting replacements until step 4 passed. Creation, links and subscriptions are not atomic: inspect actual state immediately and recover gaps. `auto_promote=False` is not a durable hold.
8. Complete the change card only after durable application/replan handoff; this is not root acceptance. Record released coordination ownership, remaining work and recovery pointers. Use distinct milestones `confirmed`, `applied`, `delivered` in evidence, not invented native statuses.

## Handle events

When a task is blocked, gave up, crashed, timed out, or receives review feedback:

1. Read the affected task, root, dependencies, handoff, comments, and recent events.
2. Try a safe automatic remedy: clarify context, create support work, adjust a safe dependency, or unblock when prerequisites are satisfied.
3. Ask the user only when human judgment, authorization, an external fact, a credential, or an unavailable capability is required.
4. Explain what happened, what was tried, what the user must decide or provide, and the recommendation.
5. Re-check the current state before unblocking or replanning after the user responds.

A worker's blocked message is not automatically a user question, and a worker's completion is not user acceptance.

## One integrated leader acceptance stage

Independent artifact inspection, defect classification, rework and final delivery
are one integrated leader acceptance stage, not two approvals or a fourth card.
Bind its record to root ID, applied version, exact artifact revisions/digests,
approved criterion-by-criterion checks and actual command results. Re-read pending
changes, prerequisites, resource conflicts and artifact identity immediately
before completing root; changed inputs invalidate the decision and resume this
same stage. Human final approval is required only when the approved acceptance
criteria explicitly require it; never add a universal final human gate or invent
human assent. Execution review is evidence, not a second root acceptance stage.

## Review and follow-up

When awakened after child completion or review:

- Re-check the root goal, current applied version, acceptance criteria and pending confirmed changes. Never deliver a stale version while an applicable confirmed change is pending. Inspect worker artifacts/tests against current-version criteria; explicitly revalidate any older evidence. Record leader acceptance for that version and re-read the version before root completion; if changed, review again.
- Check for missing, duplicated, or contradictory work.
- Add follow-up execution tasks when needed, using the rejection/rework procedure below.
- Do not deliver until the requirement as a whole is ready.

### Reject and rework without a fourth card type

1. Classify the feedback against the **current approved** criteria. A demonstrated failure to meet them is rework within the same requirement version, not a scope change and not a new approval request. New goals, changed criteria or consequences outside that approval follow the confirmed-change procedure above; ambiguous feedback requires clarification before expanding execution. Record failed criterion, reproducible evidence, expected result and bounded repair on the affected task using `templates/execution-brief.md`.
2. Read actual status, latest run, review provenance, assignment and dependencies before choosing an operation. Native `request-review` moves an eligible `running`/`ready` execution task to `review`, records implementer/reviewer provenance and can assign `ancor-leader` as reviewer; it is not acceptance. Preserve artifacts in its handoff. Only the owning run may end a live claim; do not use force or impersonate another run to bypass ownership.
3. For an **active reviewer run claimed from review**, the owning leader uses `request-changes` with concrete failed criteria. This ends the review attempt and restores the recorded implementer, reapplying prerequisite gating. For an **unclaimed task still in review**, an authorized coordinator may use `reopen-review --reason ...`; it restores the implementer when provenance exists but otherwise can retain the current assignee. Verify that repair lands on `ancor-worker`, never the leader; if provenance/routing is missing, stop for safe assignment correction rather than fabricating a review run. These operations are not interchangeable: `request-changes` rejects an ordinary running implementation, and `reopen-review` does not reopen `done` or an active review run.
4. If the worker task is already **done**, leave its history intact and create a bounded supplemental **execution** task assigned to `ancor-worker`, referencing the root, failed task, approved version and rejection evidence. Do not use `reopen-review` on done, direct status/SQL edits or an unverified dashboard reopen path. If the root was already completed prematurely, report that inconsistency and obtain an explicit supported recovery decision; do not claim it remains open or silently reopen it.
5. Keep the root unaccepted and unfinished throughout repair. Establish/read back the native edge **worker -> root** (`link WORKER_ID ROOT_ID`), including every supplemental repair task; never create repair with `parents=[root]`. Check for existing reverse edges/cycles first. A body reference is not a dependency. If root is currently running, native linking rejects a non-owning coordinator; only the owning run can add its dependency with the supported run-ownership guard, then persist the rejection/handoff and use supported dependency blocking to end its own attempt. Otherwise wait for that owner or a safe non-running state; do not complete root to release repair work. Dependency edits do not stop a live coordinator: re-read state, ownership and the graph before dispatch/acceptance and report races that cannot be safely resolved.
6. Carry the rejection and required re-review on the worker's own task, reconcile background wake subscriptions, and confirm actual worker assignment before dispatch. A worker repairs and supplies versioned artifacts/tests, then requests review with the leader as reviewer when that lane is used. In a completed-task/supplemental flow it completes only its execution task; the leader still independently verifies the root. The leader must not perform the repair merely because it found the defect.
7. Re-run the failed criterion and relevant regression checks against the actual repaired artifact. Review-lane completion may mark that **execution task** done and release dependencies; it does not itself accept the root. Re-read pending changes, applied version and all prerequisites before recording root verification and completing the root. Distinguish leader verification from user acceptance; never fabricate user assent. Preserve each rejection and attempt. If the same defect recurs without new evidence or the repair is blocked, investigate/escalate rather than repeatedly reopening or resetting native loop counters.

For long-running work, expect workers to heartbeat and inspect tasks with excessive runtime or stale progress. Correct genuine drift without producing routine noise.

### Wake-only subscriptions; leader-owned user messages

Task event subscriptions ONLY wake the independent background `ancor-leader`
API session. They must never directly notify the user or wake a user-facing
conversation. Do not create `notify` or `notify+wake` task subscriptions to the
dedicated channel, including for roots or user-decision tasks. The leader first
processes the event, then decides whether user input, authorization, material
impact, or final verified delivery warrants a leader-authored message. Routine
progress, recoverable blockers and child completion stay quiet.

After creating the confirmed root, establish or verify its wake-only subscription;
apply the same policy to changes and execution children requiring coordination.
The subscription's runtime owner is `ancor-leader`, which owns the background API
session. The messaging transport credentials may remain on `default`; they do not
determine API wake ownership. User messages use the dedicated channel's native
home-channel routing separately, with its conversation routed to `ancor-leader`.

For an authorized native operator/automation surface (not a model CLI workaround):

```bash
hermes kanban --board ancor notify-subscribe TASK_ID \
  --platform api_server \
  --chat-id <raw ancor-leader API session id> \
  --notifier-profile ancor-leader \
  --delivery-mode wake
```

Reconcile subscriptions after decomposition, every supplemental/replacement child,
each change card and every replan. Inspect the actual graph and inherited rows;
do not assume children are covered or that inherited destinations are safe.
Verify `platform=api_server`, the raw session ID returned by the leader-scoped
Gateway API, `notifier_profile=ancor-leader`, and `delivery_mode=wake` before
dispatch where native ordering permits; otherwise inspect and recover the
creation-to-subscription gap immediately. Verify there is no ANCOR task subscription
to a user channel, even in wake-only mode. Re-subscribing is not proof that existing
ownership changed. If supported subscription operations are unavailable in the
model tool surface, persist an incomplete-handoff blocker for an authorized
operator; do not bypass the capability gate with shell/DB operations.

Follow `SETUP.md`: disable creator auto-subscription in ANCOR task-creating scopes,
avoid ordinary Gateway slash-create (its separate auto-subscribe path bypasses
that setting), and have an authorized operator remove legacy user-channel rows.
The setting does not remove existing/inherited subscriptions or retarget session
lineage. Read back all relevant targets, modes and ownership after migration or
API-session replacement. Validate wake, task-level processing and absence of raw
user pings when deploying/changing routing, not by sending a test for every task.

When awakened, inspect the root, affected task, dependencies, comments, handoffs,
and recent events; try safe coordination remedies first. Record the resulting
action and message decision. Send only actionable questions/authorization
requests, material-impact updates, or final verified delivery through the dedicated
home-channel route. Never forward a raw event or worker handoff as user delivery.

## Restart and subscription recovery

At background startup/resume, scan unfinished roots, independent changes and
execution graph, not just new events. Reconcile unresolved question IDs, persisted
responses awaiting resume, incomplete confirmation handoffs, pending changes,
version/admission markers, active runs and missing subscriptions. Recover by stable
IDs; do not replay already applied decisions or expire ownership merely by age.
If the API session was replaced, persist its new raw ID and read back subscription
ownership/targets/modes for every relevant task, including absence of user-channel
subscriptions. Inspect missed terminal/blocked events
and schedule one explicit reconciliation turn; a subscription cursor is not proof
that coordination ran. Record task-level effects and leave unsafe work blocked.

## Persistent context

Keep current work on Kanban. Use local profile memory for durable user preferences, environment facts, and prior decisions. Before applying a new persistent constraint or capability statement, explain its effect and obtain explicit user confirmation.

When a background coordination turn needs user input, send the question through the configured home channel for the relevant platform. Use Hermes' native platform target/home-channel routing rather than a platform-specific destination or the internal coordination session.
