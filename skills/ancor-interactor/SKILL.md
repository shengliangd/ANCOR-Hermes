---
name: ancor-interactor
description: Use when the user replies to an ANCOR question or supplies a decision or information for an active ANCOR requirement.
---

# ANCOR interactor

You own the user-facing conversation. The coordinator sees neither this chat nor
Kanban comments; it sees durable ANCOR events. Keep the conversation natural while
making sure useful human input reaches the durable channel.

## Start from durable context

Before deciding where a reply belongs, inspect `ancor_get_context` and
`ancor_notify list` when available. Match the user's reply to an open question by
its topic, requirement and linked task. The notification ID is an implementation
detail; the user should not need to know it.

Use the exact ID returned by the tools. If several open questions could match and
the difference matters, ask one short disambiguating question rather than guessing.

## Decide whether this is a handoff

A message belongs in ANCOR when it supplies something the coordinator can act on:

- a decision or choice;
- a constraint or correction;
- a requested fact or value;
- an explicit confirmation or rejection.

Continue the conversation normally when the user is:

- asking for an explanation or progress;
- thinking aloud or comparing alternatives;
- asking about consequences or still discussing a tentative answer.

The distinction is about durable meaning, not formality. “保持隔离实例” is a clear
decision. “再想想” is a state of the conversation, not an answer.

## Hand it back

Use `ancor_notify respond` with the matching notification `id` and an `answer` that
faithfully captures the user's decision or information. The conversation may be
informal, so a concise summary is fine; exact wording is not required. Preserve exact
names, values and constraints, and do not turn the reply into a new requirement or
broader authorization. If the reply clearly belongs to an active requirement but there
is no matching notification, use `ancor_requirement respond` with the `requirement_id`
and the same faithful summary in `body`. If the user is asking for a new or changed
requirement, follow the normal draft and confirmation path instead.

Confirmation should be proportional:

- When the user is directly replying to a specific question and the meaning is clear,
  record it and briefly say what you recorded.
- When the reply is ambiguous, refers to several open questions, combines several
  decisions, or could be read as authorizing a consequential change, restate your
  understanding and ask before recording.
- Do not add a confirmation ritual to an already explicit answer.

After recording, say that the answer has been handed back to ANCOR and coordination
can continue. Do not claim the task or requirement is complete unless the coordinator
has produced durable evidence.

## Examples

Positive:

> User replies to a question: “保持隔离实例”

Locate the matching open question and call `ancor_notify respond` with its exact
`id` and a concise answer such as `answer="保持现有隔离实例，不新建实例"`, then
say: “已把‘保持现有隔离实例’交回 ANCOR，coordinator 会继续处理。”

Positive when the meaning needs clarifying:

> User: “就按现在这样”

Reply: “我理解为保持现有隔离实例、不新建实例。确认后我就交回 ANCOR。” After
the user confirms, record that concise meaning rather than repeating the exchange.

Negative — a comment is not the reply channel:

> User: “保持隔离实例”
>
> Bad: only call `ancor_board comment` or `kanban_comment` on the linked task.

Comments do not create a `notification.response` event. The notification's answer
remains empty and the task stays blocked. A comment can add execution history, but
it is not the handoff.

Negative — do not withhold a real answer because it looks informal:

> User: “就按现在这样”
>
> Bad: decide it is not evidence and leave the notification unanswered.

The coordinator cannot infer a human decision from chat. If the meaning is clear,
record it; if not, confirm the interpretation.

Negative — do not record a question as an answer:

> User: “现在到什么程度了？”
>
> Bad: call `ancor_notify respond` with the progress question.

Explain progress in the conversation. Record only the decision or information the
coordinator asked for.
