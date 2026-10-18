# ANCOR-Hermes Native Configuration

This repository contains a native-Hermes configuration package for the ANCOR workflow.

The goal is to get the core ANCOR experience from Hermes itself:

```text
User ↔ ancor-leader
          ↓ confirmed requirement
       root / triage task
          ↓ native Kanban decomposer
       child tasks → ancor-worker
          ↓ blocked / done / review events
       wake independent background ancor-leader ONLY
          ↓ inspect and coordinate; no raw event message to user
       leader decides: remain quiet, ask, report material impact, or deliver verified result
```

## Three-card workflow (soft constraints)

| Card | Owner | Contract |
| --- | --- | --- |
| Requirement root | `ancor-leader` | Confirmed version and aggregate acceptance |
| Independent change | `ancor-leader` | Confirmed base/target version and background application |
| Execution child | `ancor-worker` | Bounded versioned work and inspectable evidence |

Discussion is not authorization. Foreground change handling confirms the exact
delta, persists an independent handoff, and returns; background leaders stop,
replan and apply. Change cards reference their root in the body, never
`parents=[root]`. Native execution prerequisites gate root review.

These are instructions/body conventions, not new native APIs. Root coordination
is soft-serial with stale-version review, not lock/CAS protection. Cooperative
stop acknowledgement and verified quiescence are required before conflicting
replacement work. Block is not a kill, comment is not wake, reclaim may redispatch,
and archive is non-recursive, may release dependents and may delete workspaces.
No atomic tree pause or enforced worker assignment is promised. Leader delivery
requires target-version evidence; confirmed, applied and delivered are distinct.

## Current capability boundary

Full autonomous confirmed-change application is **not verified or available
through the inspected model tool surface**: dispatched leader runs can request
stop and verify quiescence, but cannot edit root scope or unlink/archive obsolete
prerequisites. An independent API coordinator has broader routing tools, not
those missing mutations. Keep such changes blocked rather than treating CLI
commands from a model terminal as a workaround. Native operator recovery is a
different acceptance level, not autonomous completion. See the capability gate
in the coordination skill and coordinator acceptance in `SETUP.md`.

## Six workflow concerns

1. Submit: stable confirmation IDs, persisted explicit approval and idempotent root handoff.
2. Decompose/execute: native graph; no-fanout still delegates to a worker with a checkpoint contract.
3. Blocked: stable question IDs, cross-session persisted answers and explicit background resume.
4. Changes: independently confirmed handoff, stop acknowledgement AND observed quiescence.
5. Completion: one integrated leader acceptance stage, including independent review and
   same-version defect repair; bind delivery to exact artifacts and approved version.
6. Cross-demand conflicts: declared resources and shared-coordinator cooperative admission,
   native dependency ordering and safe blocking when ownership is unclear, not false locks.

Restart reconciliation recovers missed handoffs, responses, active runs and subscriptions.
Human final approval is conditional on approved acceptance criteria, not universal.
The mandatory worker contract lives in `templates/execution-brief.md` under the
coordination skill. Install its complete file at a durable worker-readable absolute
path, verify its bytes, and pin path + SHA-256 on the root before decomposition.
Children read the root and full source; no LLM verbatim copying is required. Missing
or mismatched source still blocks writes, as do stale version or unclear admission.
These are soft protocol rules; documentation and static checks do not prove live E2E.

## Design

- `ancor-leader` is the user-facing and coordination profile.
- `ancor-worker` executes assigned Kanban tasks.
- A root Kanban task represents one user-confirmed requirement.
- The native Kanban decomposer creates the initial task graph.
- The leader supplies a complete execution brief in the root task before decomposition.
- Native Kanban owns task state, dependencies, dispatch, retries, heartbeats, review, and recovery.
- Task event subscriptions only wake the independent background `ancor-leader` API session, never the originating user chat. The leader processes the event before deciding whether user input/authorization, material impact, or final verified delivery warrants a message to the dedicated ANCOR channel. Routine events stay quiet.
- Long-lived user facts belong in local Hermes memory/profile state; current work belongs on the Kanban board.

## Contents

- `roles/ancor-leader/SOUL.md` — leader identity and operating rules.
- `skills/ancor-coordination/SKILL.md` — leader's demand and Kanban coordination procedure.
- `roles/ancor-worker/SOUL.md` — worker identity and operating rules.
- `SETUP.md` — installation and verification procedure.
- `skills/ancor-coordination/templates/requirement-brief.md` — root-task body template used before confirmation.

The independent change handoff template is
`skills/ancor-coordination/templates/change-brief.md`. The coordination skill owns
dynamic subscriptions for roots, changes and newly discovered execution children.

## Configuration

1. Keep the existing `default` profile unchanged.
2. Create the `ancor` Kanban board and use it explicitly for this workflow.
3. Disable `kanban.auto_subscribe_on_create` in ANCOR task-creating profile scopes and explicitly reconcile wake-only subscriptions to the independent background leader API session. Do not subscribe user channels to task events; see `SETUP.md` for the separate Gateway slash-create auto-subscription caveat and inherited-subscription cleanup.
4. Keep `kanban.auto_decompose` enabled initially. The decomposer is independently configured under `auxiliary.kanban_decomposer`; `kanban.orchestrator_profile` identifies the profile that owns the root after fan-out, not the model that performs decomposition.
5. Verify background wake and task processing separately from leader-authored messages and replies on the dedicated channel; verify routine task events produce no raw user notification.
