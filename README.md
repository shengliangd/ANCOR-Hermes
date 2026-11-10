**English** | [简体中文](README.zh-CN.md)

# ANCOR

**ANCOR (AI-Native Coordination Runtime) turns long-running, multi-step work from a one-off chat into a formal request that is explicitly confirmed, traceable, reviewable, and automatically scheduled.**

## What is ANCOR?

Chat is a good fit for quick questions. It becomes fragile when work spans hours, multiple tools, and external systems:

- **Intent becomes ambiguous**: user requirements, execution methods, and temporary decisions blur together; scope drifts, and general agreement can be mistaken for specific authorization.
- **Coordination depends on the user**: isolated sessions do not know which machines, directories, GPUs, or external quotas the others are using, so the user has to discover conflicts, while agents may interrupt the user for trivial questions.
- **Delivery lacks a trusted state**: an agent says it is done, but no independent verification exists; progress, questions, and final acceptance never form one stable, inspectable state.

ANCOR solves these problems with a **formal request**. A request is a stable commitment boundary containing:

- **Outcome**: what the user ultimately needs;
- **Constraint**: boundaries that must not be violated;
- **Advice**: optional method suggestions;
- **Context**: exact paths, inventories, formats, environment facts, and other details needed to remove ambiguity.

Creating, revising, and accepting a request all require explicit human confirmation. Execution plans, task breakdowns, process logs, and verification evidence stay out of the request body and live in coordinator task cards and verification records.

> [!WARNING]
> **ANCOR coordinates AI agents that can execute commands, read and modify files, access external services, and change shared task state.**
> Role separation and human confirmation are workflow safeguards, not an OS sandbox. They cannot guarantee protection against prompt injection, model errors, credential exposure, or irreversible actions.
>
> Run ANCOR only in environments and with credentials you are prepared to entrust to an AI agent. For untrusted repositories, documents, messages, or users, use a separate OS user, container, or virtual machine with least-privilege credentials. Keep backups and require independent review for production changes, financial actions, access-control changes, and destructive operations.

## How it works

ANCOR divides the work across three roles.

### Interactor

The interactor is the user-facing role. It:

- handles light work directly;
- starts formal intake after the user invokes `/ancor create`;
- performs necessary intake and clarification;
- records human answers, authorization, pause/resume/cancel requests, and final acceptance;
- explains current state concisely.

The interactor does not automatically turn ordinary conversation into a formal request. Creating one always requires an explicit user command.

### Coordinator

The coordinator owns execution:

- investigates and validates uncertain parts of the request;
- turns the work into Kanban cards with explicit contracts;
- coordinates multiple workers;
- proposes confirmed request revisions when new information changes the commitment;
- independently verifies delivery and sends completion notices.

### Worker

A worker handles one bounded task card at a time. It uses tools within the authorized scope, produces artifacts and evidence, and returns a clear result when it completes, becomes blocked, or needs input.

This separation keeps human intent, execution freedom, and acceptance responsibility distinct:

```text
User confirms request
      ↓
Coordinator plans and coordinates
      ↓
Workers execute bounded tasks
      ↓
Coordinator verifies delivery
      ↓
User accepts the result
```

### Reducing interruptions

ANCOR does not escalate every retry, task switch, worker heartbeat, or intermediate check to the user. The interactor asks only about intake questions that change the outcome, scope, or authorization. During execution, the coordinator handles reversible technical choices, failure recovery, task ordering, and routine resource allocation. It asks a focused question only when continuing requires new authority, changes a tradeoff the user already established, or exposes a conflict that existing constraints cannot resolve.

The user does not need to watch every worker. `/ancor list` shows all open requests, their current state, and the actions that actually require the user; everything else can continue.

### Coordinating resource conflicts

When multiple requests run concurrently, the coordinator sees one Kanban and one set of authorized resources. It can reorder tasks, hold conflicting cards, and prevent incompatible work from simultaneously taking the same GPU, machine, mount, upload path, or external quota.

A resource conflict does not need to interrupt the user when it can be resolved through queuing, splitting work, reducing concurrency, or changing dependencies. The coordinator asks the user only when resolving the conflict would exceed existing authorization, change priorities, or require a tradeoff between conflicting business goals. ANCOR does not create resources out of nowhere; it uses the resources already granted in a more coordinated and explainable way.

## Current implementation: a Hermes plugin

ANCOR is currently implemented as a plugin for [Hermes](https://github.com/NousResearch/hermes-agent):

- durable storage for requests, questions, notifications, task links, and verification evidence;
- ANCOR tool surfaces for Interactor, Coordinator, and Worker profiles;
- formal confirmation through Hermes native `clarify`;
- task orchestration through native Kanban;
- Gateway polling, event wakes, and notification delivery;
- a read-only `/ancor` dashboard;
- an `/ancor` skill command installed only for the interactor.

The repository also contains the three role SOUL/SKILL files and the installer:

- [`roles/`](roles/)
- [`skills/`](skills/)
- [`ancor/plugin.yaml`](ancor/plugin.yaml)

Using ANCOR therefore requires a configured Hermes Gateway, native Kanban, `clarify`, plugin hooks, and an environment that can run multiple profiles.

## Installation

See **[SETUP.md](SETUP.md)** for the complete installation, profile, model, toolset, messaging-channel, and verification steps.

ANCOR requires several Hermes profiles: owner, interactor, coordinator, and worker. The recommended path is to let your coding agent read and execute `SETUP.md`. Ask it to inspect the existing Hermes configuration first, then create missing profiles, install the plugin, apply the role prompts, and run the verification checklist. For example:

```text
Read SETUP.md in the ANCOR-Hermes repository and install ANCOR for the current Hermes instance.
Inspect the existing configuration first. Do not overwrite profiles, credentials, or message routes that I did not ask you to change.
After installation, verify the setup using the checklist in SETUP.md.
```

For the initial configuration, use **DeepSeek V4.1 Flash high** or **GPT-6.1 Sol medium** consistently across the interactor, coordinator, and worker. Get the complete workflow running first, then tune each role separately based on quality, latency, cost, and concurrency needs.

## Usage

The interactor supports these commands:

| Command | Purpose |
| --- | --- |
| `/ancor create <request>` | Start creating a formal request |
| `/ancor revise <request> <change>` | Revise an existing request |
| `/ancor list` | List all open requests and the next required actions |
| `/ancor show <request>` | Show the current state of one request |
| `/ancor answer <question> <reply>` | Answer an open coordinator question |
| `/ancor accept <request>` | Accept the latest verified delivery |
| `/ancor help` | Show command help |

Pause, resume, cancel, grant, revoke, and general signals remain available through explicit natural-language instructions.

### Complete example

Suppose the user wants to clean a set of raw CSV files into a training-ready dataset and produce a quality report. At the same time, another ANCOR request is using the same processing machine and shared mount.

#### 1. Explicitly create a formal request

```text
/ancor create Clean the 12 CSV files under /data/raw, deduplicate them, and generate a training-ready dataset and quality report. Do not modify the original data.
```

`/ancor create` only starts formal intake. It does not create the request by itself.

#### 2. Interactor performs intake and clarification

The interactor first resolves whatever can be established without asking, then asks only questions that change the result or authorization boundary:

```text
Three points need confirmation:
1. Should the cleaned data be written to /data/clean/v1?
2. Should duplicates be determined by exact row equality, or by user_id + timestamp?
3. Should rejected records be stored separately for auditing?
```

Only after the user answers does the interactor organize the request.

#### 3. Confirm the request

The tool presents content close to the following and asks the user to confirm it through Hermes native `clarify`:

```text
Title: Clean 12 CSV files and generate training data and a quality report

Outcome:
- Clean and deduplicate the 12 CSV files under /data/raw and produce a training-ready dataset.
- Produce a data-quality report covering input counts, retained counts, rejected counts, and rejection reasons.

Constraints:
- Original data is read-only and must not be modified.
- Duplicate detection uses user_id + timestamp.
- Rejected records are stored separately for auditing.

Context:
- Input: /data/raw
- Output: /data/clean/v1
- Rejected records: /data/clean/v1/rejected
```

The request is written to ANCOR storage only after the user confirms it.

#### 4. Coordinator and workers execute

The coordinator splits the work into bounded Kanban cards, for example:

- validate the structure and encoding of all 12 CSV files;
- implement cleaning, deduplication, and rejected-record capture;
- generate the quality report;
- independently verify the outputs and constraint coverage.

Workers stay within their task cards and cannot expand the request authorization on their own.

The coordinator then notices that another request is using the same processing machine and shared mount. It starts the non-conflicting validation and report preparation, queues the heavy cleaning work until the resource is released, and does not interrupt the user for this routine conflict. It would ask a specific priority question only if both requests had hard deadlines and no safe ordering existed.

#### 5. Inspect progress and accept

```text
/ancor list
```

When the coordinator resolves the resource conflict automatically, the user sees:

| | Request | Current state | What you need to do |
|---|---|---|---|
| 🟢 | Clean 12 CSV files (abc12345) | Waiting for the shared processor; queued automatically | No action |

If execution discovers multiple `timestamp` formats that affect the definition of a duplicate, and the request does not cover that new ambiguity, the coordinator stops the affected work and asks only that necessary question:

| | Request | Current state | What you need to do |
|---|---|---|---|
| ⏳ | Clean 12 CSV files (abc12345) | Waiting for the timestamp parsing rule | `/ancor answer <question ID> <reply>` |

After delivery and coordinator verification:

| | Request | Current state | What you need to do |
|---|---|---|---|
| 🔵 | Clean 12 CSV files (abc12345) | Awaiting acceptance | `/ancor accept abc12345` |

The user can inspect the details and accept:

```text
/ancor show abc12345
/ancor accept abc12345
```

Execution completion, coordinator verification, and human acceptance are three distinct stages. Only final acceptance marks the request as accepted.

## Design principles

- **A request is a commitment boundary, not a project diary.** Progress, task decomposition, and evidence belong in execution records.
- **A command is not confirmation.** `/ancor create` only starts the workflow; formal submission still requires native `clarify`.
- **Human authorization must be specific.** General preferences or chat agreement are never silently treated as permission.
- **Execution and acceptance are separate.** Worker completion, coordinator verification, and human acceptance are three different events.
- **Interrupt the user only at consequential points.** Routine retries, queuing, and resource conflicts belong to the coordinator; only new authority, tradeoffs, or material ambiguity require the user.
- **Coordinate resource conflicts before escalating them.** Use ordering, queuing, and throttling within existing authority; ask for a decision only when crossing those boundaries.
- **Expand context on demand.** Default state stays compact; full requirements, history, questions, and task evidence are loaded only when needed.

## Documentation

- [Installation and configuration](SETUP.md)
- [Request model and transaction semantics](docs/atomic-requests.md)
- [Request authoring standard](skills/ancor/references/REQUEST_STANDARD.md)
- [Plugin settings and interfaces](ancor/plugin.yaml)
- [Isolated real-model trial](scripts/isolated_trial.py)

## Development

Run the tests from the repository root using a Python environment with `pytest` installed:

```sh
env -u PYTHONPATH python3 -m pytest tests -q
```

Some native integration tests require a compatible Hermes checkout and its dependencies; they are skipped when those conditions are unavailable. Set `ANCOR_HERMES_SOURCE` to point to the Hermes source checkout.

## License

ANCOR is licensed under the [MIT License](LICENSE).
