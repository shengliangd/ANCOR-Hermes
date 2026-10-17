# ANCOR coordination

Use this skill for substantial user work handled through the ANCOR Kanban board.

## Prepare a requirement

1. Understand the goal, relevant context, constraints, non-goals, and acceptance criteria.
2. Keep lightweight analysis, ordinary questions, status checks, and small reversible work in the current session.
3. For substantial work, write a concise execution brief using the linked `templates/requirement-brief.md` template.
4. Ask the user to confirm the brief before creating executable work.
5. After confirmation, create one root task on the `ancor` board in `triage`; the root represents the confirmed requirement.

Keep only decomposition-relevant context in the root body. Do not copy the full conversation or private memory.

## Coordinate native Kanban

- Let the native decomposer create the initial child graph.
- Treat the root as the coordination anchor; inspect descendants, dependencies, comments, handoffs, and events before declaring the requirement complete.
- Search existing tasks before creating work. When decomposition misses work, create focused supplemental child tasks and safe dependencies rather than duplicating or rewriting completed work.
- Keep the board explicit and use native Kanban tools.

## Handle events

When a task is blocked, gave up, crashed, timed out, or receives review feedback:

1. Read the affected task, root, dependencies, handoff, comments, and recent events.
2. Try a safe automatic remedy: clarify context, create support work, adjust a safe dependency, or unblock when prerequisites are satisfied.
3. Ask the user only when human judgment, authorization, an external fact, a credential, or an unavailable capability is required.
4. Explain what happened, what was tried, what the user must decide or provide, and the recommendation.
5. Re-check the current state before unblocking or replanning after the user responds.

A worker's blocked message is not automatically a user question, and a worker's completion is not user acceptance.

## Review and follow-up

When awakened after child completion or review:

- Re-check the root goal and acceptance criteria.
- Check for missing, duplicated, or contradictory work.
- Add follow-up tasks when needed.
- Do not deliver until the requirement as a whole is ready.

For long-running work, expect workers to heartbeat and inspect tasks with excessive runtime or stale progress. Correct genuine drift without producing routine noise.

The home-channel notification subscription must target the dedicated ANCOR channel and be owned by the Profile that owns the actual platform adapter and credentials. In the standard multiplexed setup, the adapter may remain on `default`, while the dedicated channel's conversation route points to `ancor-leader`. Therefore use the platform owner for notification delivery, and use `ancor-leader` for the API wake subscription. Do not assume `notifier_profile` should always equal `ancor-leader`.

For the standard setup:

```bash
hermes kanban --board ancor notify-subscribe TASK_ID \
  --platform <dedicated-channel platform> \
  --chat-id <dedicated-channel id> \
  --notifier-profile <profile owning the platform adapter> \
  --delivery-mode notify

hermes kanban --board ancor notify-subscribe TASK_ID \
  --platform api_server \
  --chat-id <raw ancor-leader API session id> \
  --notifier-profile ancor-leader \
  --delivery-mode wake
```

Verify both subscriptions are recorded and send a harmless end-to-end test event before dispatching real child work. The notification adapter owner may be `default` even though replies in the dedicated channel are routed to `ancor-leader` by `gateway.profile_routes`.

When awakened, inspect the root, affected task, dependencies, comments, handoffs, and recent events. Try safe coordination remedies first. Ask the user through the configured home-channel routing only when human input or authorization is genuinely required.

## Persistent context

Keep current work on Kanban. Use local profile memory for durable user preferences, environment facts, and prior decisions. Before applying a new persistent constraint or capability statement, explain its effect and obtain explicit user confirmation.

When a background coordination turn needs user input, send the question through the configured home channel for the relevant platform. Use Hermes' native platform target/home-channel routing rather than a platform-specific destination or the internal coordination session.
