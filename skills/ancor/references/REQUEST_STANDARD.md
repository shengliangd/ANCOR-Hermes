# ANCOR request standard

An ANCOR request is a stable commitment boundary. It records what the user needs, the non-negotiable boundaries around that result, and the facts required to identify the work unambiguously. It is not a project history, execution plan, task board, command log, or evidence package.

## Reading model

The user should be able to understand the commitment from the title, outcome, constraint, and advice. Context is the detailed execution-disambiguation layer: it carries exact facts that a coordinator or worker needs to identify the right objects and locations.

Keep the commitment layer concise and stable. Make context exact and complete enough to remove material ambiguity.

## Field responsibilities

### Title

State the final result or decision in one short label. Prefer roughly 12 to 40 Chinese characters, or an equally concise English phrase. The hard limit is 80 characters.

The title must still describe the request after implementation details and progress are removed.

### Outcome

Write observable results the user wants to receive. Normally use one to three outcome points. Split a request when outcomes can be accepted independently. Merge points that describe the same result from different angles.

Each outcome should:

- identify a result, not a process step;
- preserve explicit user requirements and uncertainty;
- avoid implementation methods unless the user made the method non-negotiable;
- avoid adding favorable tests, deliverables, or permissions that the user did not request.

### Constraint

Write only durable, non-negotiable rules that constrain the result or execution boundary. Typical constraints are:

- scope inclusions and exclusions;
- prohibitions and preservation rules;
- compatibility, quality, security, or authority boundaries;
- hard deadlines;
- externally fixed methods or destinations.

Each constraint must be necessary, current, and meaningfully different from the outcomes. Keep the list short enough to review. A large constraint list is usually a sign that project history, execution planning, or discovered operational detail has leaked into the commitment layer.

Do not turn ordinary good practice into constraints. Do not duplicate a requirement across outcome and constraint merely to emphasize it.

### Advice

Use advice only for genuinely optional suggestions that preserve useful user intent. Omit it by default. Do not place mandatory behavior, task decomposition, or hidden authorization in advice.

### Context

Context carries the exact facts needed to interpret the commitment without guessing. It is expected to be more detailed than the user-facing fields.

Include, when relevant:

- exact source, destination, mount, workspace, and artifact paths;
- exact dataset, file, table, field, account, resource, repository, or object names;
- necessary inventories, especially when an incomplete list would leave scope ambiguous;
- target schemas, format references, examples, URLs, and documentation locations;
- known relationships between inputs, such as shared media or duplicate sources;
- current-state facts that determine the correct action, such as where source data already exists or how many records a target table currently contains;
- environment and access facts needed to understand feasibility;
- prior confirmed decisions that remain necessary to interpret the request;
- disambiguating identifiers that are genuinely needed to locate the right object.

Context should be structured as distinct facts, preferably one fact per point. Exact paths and inventories are not noise when they prevent the executor from guessing.

Context must not become a chronological progress report. Separate durable state from transient activity:

- Acceptable: `The source data is under /source and the target mount is /target; the registry currently has 24 rows.`
- Not acceptable: `Yesterday the copy failed, then we changed the target, and today the worker is checking the registry.`

Do not include command transcripts, tool output, run IDs, routine worker status, task decomposition, implementation stages, debugging history, or verification evidence unless a specific identifier is itself required to locate the correct object.

## Request boundaries

Create one request when all outcomes share one stable user purpose and one final acceptance decision.

Split a request when:

- deliverables can be accepted independently;
- separate decisions or authorization boundaries are involved;
- different owners or timelines should be scheduled independently;
- keeping them together would require unrelated context or constraints.

Merge points when they are alternative descriptions of the same outcome or consequences of the same constraint.

Do not use a request as a container for an entire project. Subtasks, dependencies, sequencing, and progress belong to coordinator cards.

## Prohibited content

Remove the following from the commitment layer unless the user explicitly made the item a non-negotiable result or boundary:

- implementation recipes, scripts, commands, tool choices, ports, process IDs, worker counts, concurrency settings, and code structure;
- task cards, dependencies, phases, waves, and scheduling plans;
- logs, outputs, test transcripts, and completed checks;
- progress history, run history, event IDs, and statements about what the coordinator previously did;
- duplicate requirements across fields;
- exhaustive inventories of irrelevant files;
- authorization implied by preference or general approval;
- acceptance criteria invented to make the request look rigorous.

Necessary operational details belong in context. Execution method and evidence belong in task cards and verification records.

## Quality checks

Before proposing a request, verify:

1. Can a new coordinator understand the request without the chat transcript?
2. Is there one stable user purpose and one final acceptance boundary?
3. Does every outcome describe a result rather than a process?
4. Is every constraint a real non-negotiable boundary?
5. Does context identify every object and location whose ambiguity would affect execution?
6. Is each context item a durable fact rather than progress history?
7. Can any point move to a task card, comment, run record, or verification evidence without losing user intent?
8. Is every requirement stated once, in its correct field?
9. Does the title remain accurate after removing the implementation plan?
10. Would splitting or merging produce a cleaner acceptance boundary?

If these checks fail, revise the draft before calling `ancor_change propose`.

## Revision rule

A revision is not complete merely because the edited points look correct. Apply the change in principle, then re-read the entire resulting request and run every quality check again. Remove stale or conflicting points in the same change.
