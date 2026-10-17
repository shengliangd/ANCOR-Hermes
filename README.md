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
       wake ancor-leader
          ↓ coordinate, repair, or ask the user
```

## Design

- `ancor-leader` is the user-facing and coordination profile.
- `ancor-worker` executes assigned Kanban tasks.
- A root Kanban task represents one user-confirmed requirement.
- The native Kanban decomposer creates the initial task graph.
- The leader supplies a complete execution brief in the root task before decomposition.
- Native Kanban owns task state, dependencies, dispatch, retries, heartbeats, review, and recovery.
- A blocked task should first wake the originating `ancor-leader` session; the leader tries to resolve it before asking the user.
- Long-lived user facts belong in local Hermes memory/profile state; current work belongs on the Kanban board.

## Contents

- `roles/ancor-leader/SOUL.md` — leader identity and operating rules.
- `skills/ancor-coordination/SKILL.md` — leader's demand and Kanban coordination procedure.
- `roles/ancor-worker/SOUL.md` — worker identity and operating rules.
- `SETUP.md` — installation and verification procedure.
- `skills/ancor-coordination/templates/requirement-brief.md` — root-task body template used before confirmation.

## Configuration

1. Keep the existing `default` profile unchanged.
2. Create the `ancor` Kanban board and use it explicitly for this workflow.
3. Create tasks from the persistent `ancor-leader` Gateway session so native creator-session notification subscriptions can wake the leader.
4. Keep `kanban.auto_decompose` enabled initially. The decomposer is independently configured under `auxiliary.kanban_decomposer`; `kanban.orchestrator_profile` identifies the profile that owns the root after fan-out, not the model that performs decomposition.
5. Verify notification delivery on the actual channel before relying on it.
