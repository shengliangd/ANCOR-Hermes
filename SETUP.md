# Native Hermes setup

This procedure assumes the Hermes CLI and Gateway are already installed.

## 1. Create profiles

Create two named profiles and install the role files into their profile homes using the native profile/setup mechanism. Keep the exact model/provider selection explicit per profile:

```bash
hermes profile create ancor-leader --description "User-facing requirement leader; clarifies goals, coordinates Kanban work, handles blockers and delivery."
hermes profile create ancor-worker --description "Executes assigned Kanban tasks and returns inspectable artifacts, progress, or blockers."
```

Inspect `hermes profile create --help` for the installed version. Copy the role files to the corresponding profile homes. Install `skills/ancor-coordination/` and its `templates/` directory into the `ancor-leader` profile's native skills directory. Keep the existing `default` profile's role, memory, and ordinary conversation behavior unchanged. The native Gateway configuration may still need the shared `gateway.multiplex_profiles` setting and one dedicated-channel `gateway.profile_routes` entry described below; these are routing infrastructure, not a change to the default agent's role.

Before allowing decomposition, install the complete
`skills/ancor-coordination/templates/execution-brief.md` at a durable absolute path
accessible to the actual dispatched worker profile, outside disposable task
workspaces. Read back the installed file, verify byte identity/SHA-256, and record
that path and digest in each root's decomposition guidance. Workers read the root
and verify the full source before writes; children need only reference their root,
not reproduce long contract text. Check accessibility in the worker's execution
environment, not just the installer's shell. Keep pinned files unchanged for active
runs. This template delivery does not require changing protected SOUL files or
installing leader coordination procedures as a worker role.

Choose models with the native profile model/configuration commands. Use a stronger model for `ancor-leader`; use a cost-effective execution model for `ancor-worker`.

Configure provider credentials separately in each named profile using native setup. Cross-profile dispatch does not inherit the dispatcher's/default profile's environment credentials. Provision only the necessary provider credentials; do not copy the entire default `.env` or duplicate its messaging bot credentials. Verify an actual model request in both profiles; a successful decomposer request does not verify worker or leader authentication.

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
`kanban.orchestrator_profile` to `ancor-leader`, enable
`kanban.notify_in_gateway` and `kanban.dispatch_in_gateway`, and set
`kanban.auto_subscribe_on_create` to `false` in every ANCOR task-creating
profile's effective configuration (leader and worker). Do not change unrelated
default-profile behavior merely to set an ANCOR policy.

This setting disables the model tool's creator-session auto-subscription, not
event processing. In the inspected native source, `tools/kanban_tools.py`
(`_resolve_notify_target`, `_maybe_auto_subscribe`) otherwise creates a
`notify+wake` subscription for a non-TUI creator session: a user chat can receive
a raw event ping before leader judgment. Explicit subscriptions must instead use
`delivery_mode=wake` and the independent background API session, never a user
channel. Keep `notify_in_gateway` enabled; turning off the notifier would also
disable the desired wake path.

**Separate native paths need auditing:** `gateway/slash_commands.py`
(`_handle_kanban_command`, `_kanban_auto_subscribe`) auto-subscribes ordinary
Gateway `/kanban create` output with `notify+wake` without consulting that config
gate. Do not use that plain slash-create path for ANCOR; the inspected native
`/kanban --board ancor create ... --json` path skips this output-based subscription,
or use the model tool with the config gate disabled. These are creation-path
choices, not permission for a model to shell out around its tool restrictions.
Existing and inherited subscriptions are not removed by the setting; workers and
decomposition can copy durable subscriptions. Before rollout, an authorized
operator must inspect and remove ANCOR user-channel subscriptions using native
subscription commands, then read back wake-only targets/modes/ownership. Preserve
unrelated boards and subscriptions. This documentation does not perform that
migration. See the [native Kanban reference](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban)
and re-check installed source/help when versions change.

Configure the independent decomposition model under:

Configure `auxiliary.kanban_decomposer.provider` and
`auxiliary.kanban_decomposer.model` independently from the two profile models.

Use native config commands after checking their help. The decomposer reads profile descriptions; the leader receives the root task after fan-out and reviews or extends the graph.

## 4. Create a background leader session

Create one shared, long-lived `ancor-leader` API session for Kanban coordination, separate from the user's normal conversation. Create it through the running Hermes Gateway API under the `ancor-leader` profile, not with a CLI chat session. Keep the leader Gateway running so it can receive runtime wake events.

```text
Worker blocked / completed
  → Kanban event
  → wake the background ancor-leader session
  → leader inspects and coordinates
  → leader decides whether input/authorization, material impact,
    or final verified delivery warrants a user message; otherwise stays quiet
```

Use the installed Hermes notification/session commands to create the session and subscription; inspect their current help first. Verify that the target is the background leader session, not the user's originating chat, and that a test event causes a real leader turn rather than only a displayed notification.

All coordination-relevant tasks need an explicitly established or verified inherited wake-only subscription to this background session. Creator identity/session lineage is not a delivery policy. Never add a parallel task subscription to the dedicated user channel. Keep `notify_in_gateway` enabled for the Gateway that owns the leader session. Verify a routine event produces a background leader turn and task-level effect without a raw user ping; separately verify a leader-selected escalation or verified delivery and its reply route.

### Coordinator capability acceptance

Enable the native `kanban` toolset for the leader's actual API platform scope
using supported configuration commands, preserving its other tools. Verify the
resolved schemas in that API session. This is separate from dispatch: a dispatched
leader card receives worker lifecycle rules and cannot act as an unrestricted
board administrator. The inspected API/orchestrator toolset adds list/unblock,
not edit/unlink/archive. A background session plus wake subscriptions therefore
does not by itself enable full confirmed-change application. Keep unsupported
changes blocked; do not introduce CLI-through-terminal, DB writes or a custom
bridge as an unannounced native-configuration fix. See the capability gate in
the coordination skill. Verify this boundary before another provider-backed
change test; normal/rework/conflict fixtures do not establish this capability.

## 5. Configure a dedicated ANCOR channel

Create or designate a dedicated channel/chat for ANCOR user interaction during setup. Do not reuse the user's ordinary Hermes conversation when the user must reply to a blocker, clarification, authorization request, or delivery decision. Record this channel as the native Hermes home-channel target for ANCOR.

The dedicated channel should be reachable by the platform adapter, but its conversation should be routed to `ancor-leader` at runtime. In a multiplexed gateway, the adapter credentials may remain owned by `default` while the conversation is executed by `ancor-leader`. Configure the native `gateway.profile_routes` entry for the dedicated channel with:

```yaml
gateway:
  multiplex_profiles: true
  profile_routes:
    - name: ancor-dedicated-channel
      platform: <platform>
      chat_id: <dedicated-channel-id>
      profile: ancor-leader
      bot_profile: default
      enabled: true
```

Use the installed Hermes routing/configuration commands where available; do not hand-edit live configuration without checking their help. The exact platform and target remain deployment-specific. Do not add a platform-specific ANCOR destination or put credentials in this repository.

The route's `profile` is the runtime owner; `bot_profile` is the transport owner. The dedicated channel carries leader-authored messages and user replies, not Kanban task subscriptions. Background API wake subscriptions use `--notifier-profile ancor-leader` because that profile owns the target session, even when messaging bot credentials remain on `default`. See the coordination skill for runtime subscription commands. Preserve unrelated routes when adding this entry, and reload the host Gateway through its supported lifecycle mechanism before verifying the route.

Verify that:

- an ordinary message in the dedicated channel is handled by `ancor-leader`;
- a reply to a test escalation remains in the same `ancor-leader` conversation;
- the platform adapter still uses the Profile that owns its credentials;
- the dedicated channel is not accidentally the user's ordinary Hermes conversation.

Send a harmless test message before relying on the channel for real escalations.

## 6. Required runtime path

Keep the host Gateway persistently running with multiplexing enabled and verify that `ancor-leader` is in its served profile set. Do not start a separate `ancor-leader` Gateway when it is served by the host multiplexer.
- Keep the background leader session available for wake events.
- Ensure the worker profile has the tools required by its task class.
- Give the leader Kanban/orchestrator tools and `clarify`; it may retain lightweight analysis tools.
- Create the ANCOR root task from the leader workflow; runtime event subscriptions are handled by the coordination skill.


