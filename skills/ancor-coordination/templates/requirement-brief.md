# Root requirement execution brief

Use this structure as the body of a confirmed root/triage Kanban task. Keep only context relevant to decomposition and execution; do not dump the entire conversation or private memory into the board.

## Version and ownership

- Card convention: requirement root; assignee: `ancor-leader`.
- Current applied requirement version: `v1` on initial confirmation.
- Confirmation reference/date and exact approved brief:
- Pending/applied change IDs and coordination marker (owner/run/phase):
- Execution children must carry root ID, version, scope and evidence criteria; assign them to `ancor-worker` (inspect actual decomposition output).
- Children gate root review; root completion requires leader verification of this version and review of pending confirmed changes.
- Rejection record: failed approved criterion / evidence / affected execution ID / repair ID / re-review result. Keep root unaccepted and unfinished until verified.
- Rework within approved scope keeps this version; changed scope/criteria require explicit confirmation via a change brief. Repair uses an existing or supplemental worker execution card, not a fourth card type.
- Supplemental repair dependencies: `link WORKER_ID ROOT_ID`; never `parents=[root]`. Preserve completed execution history; do not use `reopen-review` on done.

## Durable handoff and resources

- Stable confirmation_id / exact brief / source session-message / root ID read-back:
- Independent background API coordinator raw session ID / resume receipt /
  wake-only subscription read-back (owner `ancor-leader`; no user-channel subscription):
- Leader message decision after processing: quiet, input/authorization, material
  impact, or final verified delivery; dedicated channel is not an event subscriber.
- question_id / question / response-source / resolution / resume pending:
- Canonical resource keys, modes, capacity and admission owner (or explicit none):
- Authoritative execution-brief contract: durable absolute path / SHA-256 /
  worker-profile readability and byte-verification evidence (install before decomposition):
- Decomposer: children reference this root and its authoritative contract. Workers
  read and verify the full source before writes; no verbatim copying requirement.
  No-fanout delegates one worker; leader never implements.
- Single integrated leader acceptance: version + exact artifact revision/digest +
  criterion checks; human final approval only if explicitly required by criteria.

## Goal

<!-- What outcome does the user want? -->

## User-confirmed scope

<!-- What did the user explicitly approve? -->

## Non-goals

<!-- What must not be done in this requirement? -->

## Background

<!-- Relevant existing systems, decisions, files, and context. -->

## Constraints

<!-- Technical, resource, permission, timing, deployment, or safety constraints. -->

## Existing work and dependencies

<!-- Existing Kanban tasks, artifacts, systems, or external inputs. -->

## Acceptance criteria

<!-- Observable conditions for considering the requirement complete. -->

## Decomposition guidance

<!-- Work that must not be omitted; ordering and review requirements. -->

## Open questions

<!-- Questions that block or may affect later planning. -->

## Confirmation

<!-- Record a concise human-readable confirmation summary and date. -->
