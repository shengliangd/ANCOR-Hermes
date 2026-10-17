# Native Hermes setup

This procedure assumes the Hermes CLI and Gateway are already installed.

## 1. Create profiles

Create two named profiles and install the role files into their profile homes using the native profile/setup mechanism. Keep the exact model/provider selection explicit per profile:

```bash
hermes profile create ancor-leader --description "User-facing requirement leader; clarifies goals, coordinates Kanban work, handles blockers and delivery."
hermes profile create ancor-worker --description "Executes assigned Kanban tasks and returns inspectable artifacts, progress, or blockers."
```

Inspect `hermes profile create --help` for the installed version. Copy the role files to the corresponding profile homes. Keep `default` unchanged.

Choose models with the native profile model/configuration commands. Use a stronger model for `ancor-leader`; use a cost-effective execution model for `ancor-worker`.

## 2. Create the board

```bash
hermes kanban boards create ancor --name "ANCOR"
```

Use the board explicitly when creating or inspecting tasks:

```bash
hermes kanban --board ancor create "..." --triage --assignee ancor-leader
hermes kanban --board ancor list
```

Confirm the exact syntax with `hermes kanban --help` and use the `ancor` board explicitly.

## 3. Native settings

Set these through `hermes config set` in the profile/config scope supported by the installed Hermes version:

Set `kanban.auto_decompose` to `true`, `kanban.auto_decompose_per_tick` to `3`,
`kanban.orchestrator_profile` to `ancor-leader`, and enable
`kanban.auto_subscribe_on_create`, `kanban.notify_in_gateway`, and
`kanban.dispatch_in_gateway`.

Configure the independent decomposition model under:

Configure `auxiliary.kanban_decomposer.provider` and
`auxiliary.kanban_decomposer.model` independently from the two profile models.

Use native config commands after checking their help. The decomposer reads profile descriptions; the leader receives the root task after fan-out and reviews or extends the graph.

## 4. Required runtime path

- Run the `ancor-leader` Gateway persistently.
- Create ANCOR root tasks from the leader's persistent Gateway conversation.
- Keep notification polling enabled for the Gateway that owns the leader's adapter.
- Ensure the worker profile has the tools required by its task class.
- Give the leader Kanban/orchestrator tools and `clarify`; it may retain lightweight analysis tools.

Create tasks inside the persistent leader Gateway session so `auto_subscribe_on_create` can establish the leader's originating-session subscription.

## 5. Acceptance smoke test

1. Ask `ancor-leader` a lightweight question; it should answer without creating a task.
2. Submit a substantial request; leader should draft an execution brief and obtain confirmation.
3. After confirmation, leader creates one `ancor` root task in `triage`.
4. Confirm the native decomposer creates children, dependencies, and worker assignees.
5. Make a worker call `kanban_block` with a resolvable issue. Verify the blocked event wakes the leader rather than directly asking the user.
6. Verify leader attempts a support task/context repair/unblock before asking the user.
7. Make a worker or reviewer report a missing work item. Verify leader creates a new child task without duplicating existing work.
8. Restart the Gateway and inspect that active/blocked tasks remain on the board.

Record actual event delivery and task IDs.
