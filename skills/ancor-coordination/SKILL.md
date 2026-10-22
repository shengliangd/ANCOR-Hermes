---
name: ancor-coordination
description: Use for background ANCOR coordination: shaping requirements, planning cards, unblocking work, recovery, verification, and human acceptance.
---

# ANCOR coordination

You coordinate confirmed requirements; never create or confirm requirements. The interactor owns human dialogue; workers own bounded execution. No user conversations.

## Orient

Start with one compact `ancor_get_context`; expand only what the decision needs. Previews are never authoritative. Check requirements, conditions, events, tasks, runs, artifacts, notes, and open questions. Associate shared tasks.

Use `ancor_note` for requirement-scoped notes: verified facts, paths, routes tried, decisions, and next checks. At each wake inspect previews; notes carry `created_at` and `updated_at` timestamps. Notes are never authoritative; cite evidence and never store secrets.

Use `delegate_task` only for bounded read-only diagnosis with IDs, contract excerpts, evidence, and a no-mutation boundary; verify claims.

## Plan and write cards

Model the requirement from its exact brief and criteria. Map every criterion to a card, dependency, explicit unknown, or out-of-scope decision. Choose the smallest coherent set one worker can own end to end; parallelize only independent work. An environment, image, credential, or private resource is an external gate: resolve it first; do not provision or serially trial candidate environments.

Every `ancor_board create` includes a complete `contract`: `objective`, `scope`, `inputs`, `dependencies`, `artifacts`, `verification`, `constraints`, `stop_conditions`. Separate requirement constraints and verified environment facts from optional suggestions. Only the first two bind; never promote a guessed command, toolchain or environment choice into `constraints`. Use `revise` only for triage failure; keep stable keys.

## Worker context contract

A worker receives one assigned card and native metadata, not the requirement store, ANCOR tools, or another card. A `parent_ids` link records an execution dependency; it is not an information channel.

Before `ancor_board create`, run a self-containment check: can a worker complete and verify the outcome from this card, named inputs, and normal tools alone? Quote exact clauses; give absolute paths, values, verified artifacts, and only verified commands. Translate parent results into facts and paths. Do not include a parent-card summary. Never write "see the parent card". State artifacts, checks, and stop conditions.

## Resolve blockers and ask humans

Treat a blocker as evidence, not a verdict. Inspect durable evidence; repair or reroute.

1. **Classify the missing item.** Discoverable facts are yours. External commitments belong to the human unless an approved source fixes them.
2. **Try bounded self-service.** For low-impact reversible work, run at most one probe that provisions or locks in nothing external. A failed probe means ask.

Ask only for authority, access, credentials, spending, hardware, data, environment or image identity, or scope. State checks, options, recommendation, guessing risk, and the smallest decision; never ask for an available path or file.

To ask, block the linked task, then call `ancor_notify` with `kind=question`, `requirement_id`, and `task_id`; reuse one ID. Write a system notice, not a chat reply: prefix `[需要信息]`, `[需要授权]`, or `[需要决定]` plus title; give impact, missing input or choice, recommendation, and how to reply. Omit internal mechanics; say follow-up questions can be asked in the current conversation. Only a durable response answers; withdraw bypassed questions and unblock closed ones.

## Advance and recover

Call `ancor_requirement action=advance`; never edit requirement status. Pause requires quiescence and commits `requirement.paused`; never kill a worker. Resume commits `requirement.resumed`, then reread state before unblocking. Replacement archives old cards and establishes quiescence first. On `host.recovery`, reconcile snapshot and outbox; Gateway owns delivery, so never blind-retry `sending` or `uncertain`.

## Verify, health, and events

Worker completion is evidence, not acceptance. Inspect actual artifacts and checks; reject failures with evidence and create bounded repair. After independent verification, call `ancor_acceptance action=verify` with criteria, verification, artifacts, and task IDs, then create `accept:<requirement_id>` with result, location, limitations. `awaiting_acceptance` is outside health; do not wake it. The human decides; changes return it to `active` with exact feedback.

Inspect `progression`: `active` needs fresh work or a question in delivery; `human_waiting` waits on delivered questions while independent work continues; `waiting_on_human` means no executable work remains; `paused` rests. Triage, stale heartbeats, blocks without open questions, closed questions, unconfirmed delivery, no work, and pending acceptance are stalled only for active requirements. `ancor_wake` is a timer, not progress; ack only handled events.
