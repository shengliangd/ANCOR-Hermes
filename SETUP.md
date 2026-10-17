# Native Hermes setup

This procedure assumes the Hermes CLI and Gateway are already installed.

## 1. Create profiles

Create two named profiles and install the role files into their profile homes using the native profile/setup mechanism. Keep the exact model/provider selection explicit per profile:

```bash
hermes profile create ancor-leader --description "User-facing requirement leader; clarifies goals, coordinates Kanban work, handles blockers and delivery."
hermes profile create ancor-worker --description "Executes assigned Kanban tasks and returns inspectable artifacts, progress, or blockers."
```

Inspect `hermes profile create --help` for the installed version. Copy the role files to the corresponding profile homes. Install `skills/ancor-coordination/` and its `templates/` directory into the `ancor-leader` profile's native skills directory. Keep `default` unchanged; do not install the coordination skill into `ancor-worker`.

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

## 4. Create a background leader session

Create one long-lived `ancor-leader` API session for Kanban coordination, separate from the user's normal conversation. Create it through the running Hermes Gateway API under the `ancor-leader` profile, not with a CLI chat session. Keep the leader Gateway running so it can receive runtime wake events.

```text
Worker blocked / completed
  → Kanban event
  → wake the background ancor-leader session
  → leader inspects and coordinates
  → leader asks the user only when necessary
```

Use the installed Hermes notification/session commands to create the session and subscription; inspect their current help first. Verify that the target is the background leader session, not the user's originating chat, and that a test event causes a real leader turn rather than only a displayed notification.

Tasks created from the user's leader conversation may need an explicit subscription to this background session; do not rely on `auto_subscribe_on_create` to retarget events automatically. Keep `notify_in_gateway` enabled for the Gateway that owns the leader session.

## 5. Configure the user escalation target

Set a Hermes home channel for the platform through which the user should receive leader escalations. The home channel is the user-facing destination; it is separate from the background leader session that receives Kanban wake events.

Keep the target in Hermes' native platform configuration rather than adding a platform-specific ANCOR destination. The leader's coordination skill instructs it to use the relevant native platform target/home-channel routing when user input is required, so the setup works with any messaging platform Hermes supports.

Verify the configured home channel with the native status/configuration commands and send a harmless test message before relying on it for escalations.

## 6. Required runtime path

- Run the `ancor-leader` Gateway persistently.
- Keep the background leader session available for wake events.
- Ensure the worker profile has the tools required by its task class.
- Give the leader Kanban/orchestrator tools and `clarify`; it may retain lightweight analysis tools.
- Create the ANCOR root task from the leader workflow; runtime event subscriptions are handled by the coordination skill.


