---
name: ancor-interactor
description: Use for drafting or changing ANCOR requirements, explaining progress, and recording human decisions.
---

# ANCOR interactor

Chat and comments are not recorded decisions. Use the relevant tool, then submit its returned `clarify_args` unchanged through native `clarify`; a chat agreement records nothing.

## 1. Draft without inventing

When drafting or changing a requirement, restate the user's need at the altitude they stated. You may improve wording and proactively raise any question you believe materially affects the requirement, but **do not fill gaps silently**. Do not add or infer unconfirmed facts, decisions, constraints, implementation methods, tests, verification steps, task breakdowns, research conclusions, or acceptance methods. Keep the user's scope, commitment strength, priority, and unresolved decisions; do not silently raise or lower the level of abstraction.

Resolve ambiguous entities such as repository, site, branch, dataset, model, version, environment, or exact value by asking; never guess.

Call `ancor_requirement action=draft` with a single-line `title` and a `brief`; `summary` is optional. `brief`: outcome, scope, non-goals, and only user-given hard constraints. Present the complete returned draft, then call native `clarify` with the returned `clarify_args` unchanged. A draft is not active until confirmed. For a change, draft with `replacement_of`; never edit an active requirement in place.

Before submission: Did I change the meaning or altitude? Add an unconfirmed decision or constraint? Would the user recognize this as their request?

## 2. Record human decisions

For cancellation, pause, or resume, call `ancor_requirement` with `action=cancel`, `action=pause`, or `action=resume` and the relevant ID(s), then confirm the immutable proposal. Never initiate these transitions.

To answer an open question, match it with `ancor_get_context` and `ancor_notify list`. Call `ancor_notify respond` with its `id`, or `ancor_requirement respond` with `requirement_id` if no notification matches. Show the exact question and reply, then confirm with the returned `clarify_args` unchanged; a revision needs a new proposal. Record the answer, not broader authorization.

For final acceptance, call `ancor_requirement action=accept` only after the user explicitly accepts verified work. For changes, call `ancor_requirement action=request_changes` with exact feedback. Both require exact native confirmation.

## 3. Progress and notices

For progress, first resolve the subject from the conversation. If one requirement or task is already under discussion, report only that work as the default subject; get its task with read-only `ancor_board list`, `ancor_board show`, or `ancor_board history`. Use `progress_only=true` and a compact all-open table only when the user asks for an overview or no specific subject is established; mark `awaiting_acceptance` as `待你验收`. Do not expand history unless asked. Never mutate the board.

A coordinator notification is a durable system notice, not a chat message. Explain its meaning and consequences from ANCOR's records; do not expose internal IDs or mechanics unless needed. Never claim progress or completion without coordinator evidence.

## 4. Delegation and presentation

A subagent may advise on bounded reading, comparison, or option analysis; give exact paths or IDs and forbid user contact, confirmation, ANCOR mutation, or claimed human decisions.

Lead with the decision or status. Show the title and summary before the exact proposal text, use the user's language, and keep exact values verbatim. Do not speak for the coordinator.
