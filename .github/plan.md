# Three-Phase Agent Architecture Implementation Plan

## Document Goal

This document is a step-by-step execution plan for implementing a three-phase agent architecture:

1. **Phase 1**: Stateless generation and tool-calling
2. **Phase 2**: Stateful single-agent session system
3. **Phase 3**: Flexible multi-agent collaboration

The plan is written to guide an implementation agent or engineering team through the full delivery sequence, from schema design to deployable services.

---

## Core Principles

These principles apply across all three phases:

- **Evolve, do not rewrite**: later phases should build on the abstractions of earlier phases.
- **Keep protocol business-semantic**: proto should express durable business concepts, not low-level debugging details.
- **Separate concerns**:
  - messages are for conversational exchange,
  - jobs are for execution,
  - artifacts are for outputs,
  - events are for meaningful collaboration state changes.
- **Defer internal logging complexity**: detailed traces and logs remain backend concerns unless they become shared business semantics.
- **Design for migration**: fields introduced in earlier phases should remain usable later.

---

## Target End State

At the end of all three phases, the system should support:

- stateless request-response generation,
- persistent sessions with message history,
- async execution for long-running work,
- agent identity as a first-class concept,
- task decomposition,
- dependency-aware task execution,
- artifact-based collaboration between agents,
- optional review and aggregation steps,
- a stable API surface that can evolve without breaking consumers.

---

## Overall Delivery Strategy

Implement in this order:

1. **Foundation layer**
   - repo structure
   - proto repository layout
   - codegen pipeline
   - transport conventions
   - storage conventions
   - ID strategy
   - timestamp conventions
2. **Phase 1 service**
   - stateless generate API
   - tool abstraction
   - model adapter
3. **Phase 2 service**
   - session persistence
   - message persistence
   - async session jobs
4. **Phase 3 orchestrator**
   - agent registry
   - task graph model
   - artifact store
   - collaboration event model
   - scheduler and execution coordination
5. **Operational hardening**
   - tests
   - migration compatibility
   - auth and ACL
   - observability
   - failure recovery

Do **not** begin with Phase 3 orchestration. The fastest path is to establish a stable Phase 1 and Phase 2 core first.

---

# 0. Pre-Implementation Foundation

## 0.1 Repository Structure

Create a repo structure that prevents later rewrites:

```text
/proto
  /phase1
  /phase2
  /phase3
/services
  /gateway
  /stateless-agent
  /session-agent
  /multi-agent-orchestrator
  /artifact-service
  /agent-registry
/internal
  /modeladapter
  /toolruntime
  /storage
  /scheduler
  /idgen
  /clock
/docs
  architecture.md
  api-guidelines.md
  data-model.md
```

### Agent instruction
- Create directories first.
- Establish language-specific codegen conventions.
- Lock proto package naming early.
- Add CI for proto lint and breaking-change detection.

## 0.2 Shared Conventions

Define these before writing production code:

- IDs are opaque strings.
- All timestamps use RFC3339 UTC strings unless the org already standardizes protobuf timestamps.
- `metadata_json` is allowed only for extensibility, not core semantics.
- Enums should be used for stable state machines.
- New phases must reuse prior message, tool, and usage abstractions when possible.

### Agent instruction
Produce a short `api-guidelines.md` covering:
- enum vs string rules
- when JSON extension fields are allowed
- pagination format
- async job lifecycle semantics
- backward compatibility policy

## 0.3 Storage Strategy

Pick persistence boundaries now:

- **Phase 1** can be mostly stateless, with optional request logging only.
- **Phase 2** requires durable:
  - sessions
  - messages
  - jobs
- **Phase 3** requires durable:
  - agents
  - tasks
  - artifacts
  - reviews
  - events

### Recommended relational tables
- sessions
- session_messages
- session_jobs
- agents
- tasks
- artifacts
- reviews
- session_events

### Agent instruction
Write table sketches before service implementation. Avoid coding the API before persistence shape is at least roughly agreed.

---

# 1. Phase 1 Implementation Plan: Stateless Agent API

## 1.1 Objective

Deliver a single request-response interface for model generation and tool calling with no persisted session semantics.

This phase establishes:

- message schema
- tool call schema
- model invocation adapter
- response usage reporting

## 1.2 Scope

### In scope
- `GenerateRequest`
- `GenerateResponse`
- `Message`
- `ContentPart`
- `ToolSpec`
- `ToolCall`
- `ToolResult`
- `Usage`
- synchronous generation

### Out of scope
- sessions
- async jobs
- multi-turn persistence
- task graph
- multi-agent routing

## 1.3 Implementation Steps

### Step 1: Define Phase 1 proto
Create:
- roles enum
- content part union
- tool spec
- usage
- finish reason
- stateless service RPC

### Step 2: Implement model adapter
Create an adapter boundary between service API and actual model providers.

The model adapter should:
- accept normalized messages,
- normalize tool specs,
- return normalized output,
- isolate provider-specific behavior.

### Step 3: Implement tool runtime contract
Do not overbuild a full tool framework yet.
Only support:
- tool schema registration,
- tool call emission,
- tool result round-tripping.

### Step 4: Implement request validation
Validate:
- supported roles
- content part correctness
- duplicate call IDs
- malformed JSON arguments
- model parameter bounds

### Step 5: Add basic tests
Required:
- message serialization tests
- tool call round-trip tests
- finish reason tests
- bad request validation tests

## 1.4 Deliverables

- `phase1.proto`
- generated server and client stubs
- stateless agent service implementation
- model adapter interface
- API tests
- sample client

## 1.5 Exit Criteria

Phase 1 is complete when:
- a client can send a stateless request and get a valid response,
- tool call and tool result messages can round-trip,
- no session persistence is required to use the service.

## 1.6 Risks to Avoid

- adding session fields too early,
- encoding provider-specific response formats into proto,
- adding workflow concepts into stateless API,
- exposing internal tracing or logging fields in the public contract.

---

# 2. Phase 2 Implementation Plan: Stateful Single-Agent Session

## 2.1 Objective

Upgrade from stateless generation to persistent conversational sessions managed by a single primary agent.

This phase introduces:

- session lifecycle,
- durable message history,
- single-agent session ownership,
- async session jobs.

## 2.2 Scope

### In scope
- session create and get
- list session messages
- send user message into session
- sync assistant reply
- async response via job
- session status model
- single primary `AgentRef`

### Out of scope
- task graph
- subtask dependencies
- multi-agent orchestration
- artifact graph
- review routing

## 2.3 Why this phase exists separately

Many real products only need this phase:
- a coding assistant,
- a research assistant,
- a support bot,
- an internal productivity assistant.

Do not jump to orchestration before this layer is solid.

## 2.4 Implementation Steps

### Step 1: Define Phase 2 proto
Add:
- `Session`
- `SessionMessage`
- `Attachment`
- `SessionJob`
- `CreateSession`
- `GetSession`
- `ListSessionMessages`
- `SendMessage`
- `GetSessionJob`
- `CancelSessionJob`

Reuse where possible from Phase 1:
- message parts
- tool calls
- tool results
- usage

### Step 2: Implement session storage
Persist:
- session metadata
- primary agent ID
- created and updated timestamps
- status

### Step 3: Implement message persistence
Persist each message with:
- session ID
- role
- parts
- attachments
- metadata
- created_at

### Step 4: Implement single-agent execution path
`SendMessage` should:
1. persist user message,
2. construct model input from session history,
3. invoke single configured agent,
4. persist assistant message,
5. return sync response or async job.

### Step 5: Implement async session job runner
Introduce a lightweight job system for:
- delayed generation,
- long-running tool workflows,
- background summarization.

This is **not** Phase 3 task orchestration. It is simply a session-scoped async execution mechanism.

### Step 6: Add pagination and message history limits
Implement:
- page size
- page token
- optional truncation strategy for overly long history

### Step 7: Add tests
Required:
- create and get session tests
- message ordering tests
- sync send message tests
- async job lifecycle tests
- job cancellation tests
- persistence recovery tests

## 2.5 Service Boundaries

Recommended service split:

- `session-agent` service
  - owns sessions, messages, and jobs API
- `stateless-agent` service
  - still provides low-level generation primitive
- `gateway`
  - optional routing layer

The Phase 2 service may internally call the Phase 1 stateless generator.

## 2.6 Data Model Notes

### Session
Represents the long-lived user conversation container.

### SessionMessage
Represents conversational records, not orchestration tasks.

### SessionJob
Represents async execution for the session, but still under a single-agent model.

## 2.7 Exit Criteria

Phase 2 is complete when:
- sessions can be created and resumed,
- messages are durably persisted,
- one primary agent can serve the session over time,
- long-running work can be returned as an async job.

## 2.8 Risks to Avoid

- turning `SessionJob` into a premature workflow engine,
- introducing task dependencies here,
- allowing multiple competing agent owners without protocol support,
- mixing message history with internal execution logs.

---

# 3. Phase 3 Implementation Plan: Flexible Multi-Agent Collaboration

## 3.1 Objective

Introduce explicit collaboration semantics for multiple agents working inside one session.

This phase adds:

- multiple participants,
- explicit task model,
- dependency graph,
- artifacts as exchange objects,
- optional review,
- collaboration events.

## 3.2 Core Design Rule

In Phase 3, **Task becomes the orchestration backbone**.

Messages still exist, but they are no longer the main execution structure.

Use these layers clearly:
- **Session** = collaboration boundary
- **Message** = human-readable exchange
- **Task** = execution unit
- **Artifact** = produced or consumed output
- **Review** = quality gate
- **Event** = meaningful collaboration state change

## 3.3 Scope

### In scope
- multiple participant agents
- task creation
- parent-child relationships
- dependency edges
- task status machine
- artifact production and consumption
- review submission
- event emission for meaningful workflow changes

### Out of scope
- full internal trace streaming
- arbitrary low-level worker log protocol
- distributed transaction semantics across every subsystem

## 3.4 Recommended Rollout Order

Do not implement all Phase 3 features at once.

### Phase 3A: Minimal collaboration
Implement first:
- agent registry
- session participants
- task
- assigned agent
- depends_on_task_ids
- artifact

### Phase 3B: Controlled orchestration
Implement next:
- scheduler logic
- ready vs blocked task transitions
- parent and child hierarchy
- retry semantics
- task output propagation

### Phase 3C: Quality and collaboration hardening
Implement last:
- review
- event feed
- aggregation semantics
- richer task types
- lineage analysis

## 3.5 Implementation Steps

### Step 1: Define Phase 3 proto
Add:
- `AgentRef`
- session participants
- `Task`
- `Artifact`
- `Review`
- `SessionEvent`

Ensure reuse of:
- session structure from Phase 2
- session message from Phase 2, with task and artifact references added

### Step 2: Implement agent registry
The registry should answer:
- what agents exist,
- what type each agent is,
- what capabilities each has,
- which versions are active.

Do not hardcode agent identity in orchestration logic.

### Minimal registry fields
- agent_id
- kind
- name
- version
- labels and capabilities

### Step 3: Implement task storage and state machine
Task states should at minimum support:
- pending
- ready
- running
- blocked
- waiting_review
- succeeded
- failed
- cancelled
- retrying

### Required invariants
- a task cannot move to running if dependencies are unsatisfied,
- a task cannot move to succeeded without terminal output,
- blocked means explicit unmet dependency or gate,
- retrying increments attempt count.

### Step 4: Implement dependency resolution
Build scheduler logic that:
- checks dependency completion,
- moves tasks from pending to ready,
- dispatches ready tasks to the assigned agent,
- records status changes.

This scheduler can start simple.
Do not optimize for distributed scale first.

### Step 5: Implement artifact creation and consumption
Artifacts are the medium of collaboration.

Examples:
- plan
- evidence set
- draft
- code patch
- execution result
- review note
- final response

### Rules
- artifacts should record producer task and producer agent,
- tasks should explicitly reference input and output artifact IDs,
- artifacts should be durable and independently addressable.

### Step 6: Implement session messages as human-facing context
Messages remain useful for:
- user prompts,
- agent summaries,
- visible explanations,
- final answer delivery.

Do not use messages as the only place where task outputs live.

### Step 7: Implement optional review system
Review should be introduced only after the task model is stable.

Review supports:
- approve,
- reject,
- needs revision.

A task may enter `WAITING_REVIEW` before final aggregation.

### Step 8: Implement event stream for business-level state changes
Emit events only for meaningful state changes:
- task created
- task ready
- task started
- task blocked
- task completed
- task failed
- artifact created
- review submitted
- handoff
- finalized

Do not dump raw worker logs into this API.

### Step 9: Implement aggregation flow
Create a standard pattern for final answer assembly:

1. root task created from user request
2. planner creates subtasks
3. workers produce artifacts
4. reviewer optionally approves outputs
5. aggregator creates final artifact
6. assistant message delivers final answer

This should be a documented orchestration template, not just code behavior.

## 3.6 Orchestration Template to Implement First

Use one standard multi-agent flow as the first supported collaboration pattern:

### Template: Planner -> Workers -> Reviewer -> Aggregator

- user creates root request
- planner breaks work into tasks
- researchers, coders, or executors complete subtasks
- reviewer checks output quality
- aggregator composes final result
- final message is posted to session

### Why this template first
Because it covers:
- decomposition
- parallelism
- dependencies
- artifacts
- review
- final aggregation

without requiring a fully general orchestration DSL.

## 3.7 Exit Criteria

Phase 3 is complete when:
- multiple agents can participate in the same session,
- tasks can depend on other tasks,
- agents can exchange work through artifacts,
- the system can run at least one end-to-end collaboration template,
- final output can be traced to task and artifact lineage at the business level.

## 3.8 Risks to Avoid

- using messages as the only collaboration primitive,
- keeping agent identity implicit,
- putting every internal detail into events,
- overcomplicating the scheduler before basic correctness,
- adding too many custom task types too early,
- building a generic workflow language before validating one strong orchestration template.

---

# 4. Cross-Phase Compatibility Rules

## 4.1 Field Evolution

Across phases:
- keep message and tool abstractions stable,
- do not rename core concepts casually,
- prefer additive change over destructive change,
- reserve field numbers if removing fields.

## 4.2 Service Reuse

Recommended dependency graph:

- Phase 2 service may call Phase 1 generator
- Phase 3 orchestrator may call Phase 2 session APIs and Phase 1 generator
- artifact store and agent registry should be reusable shared services

## 4.3 Migration Strategy

### From Phase 1 to Phase 2
- existing stateless clients still work unchanged,
- new clients opt into session APIs.

### From Phase 2 to Phase 3
- existing single-agent sessions still work,
- multi-agent sessions use new participant, task, and artifact features,
- not every Phase 2 session must migrate to Phase 3 orchestration.

---

# 5. Testing Strategy

## 5.1 Phase 1 tests
- proto serialization
- request validation
- tool-call flow
- model adapter normalization
- sync success and error cases

## 5.2 Phase 2 tests
- session lifecycle
- message ordering
- async job lifecycle
- persistence recovery
- history reconstruction
- cancel behavior

## 5.3 Phase 3 tests
- task creation
- dependency satisfaction
- blocked to ready transition
- retry transitions
- artifact lineage
- review gating
- aggregation flow
- multi-agent end-to-end scenario

## 5.4 Contract Tests
Across all phases:
- backward-compatible proto evolution checks
- client and server wire compatibility
- enum transition safety
- pagination correctness

---

# 6. Operationalization Plan

## 6.1 Authentication and Authorization

Introduce before external rollout:
- session ownership checks
- agent execution permissions
- artifact access rules
- admin-only orchestration controls if needed

## 6.2 Observability

Keep detailed logs internal, but instrument all phases with:
- request counts
- latency
- failure rates
- async job duration
- task duration
- retry counts
- artifact creation counts

## 6.3 Failure Recovery

By the end of Phase 3, support:
- idempotent task status updates
- task retry
- safe resume after process restart
- task dispatch recovery for stuck tasks

## 6.4 Backfill and Repair Utilities

Implement internal admin tools for:
- replaying failed jobs and tasks
- repairing orphaned tasks
- listing dangling artifacts
- recomputing task readiness

---

# 7. Concrete Build Order

This is the exact recommended execution order for the implementation agent.

## Milestone 0: Foundation
1. Create repo layout
2. Define API conventions
3. Add proto lint and codegen CI
4. Sketch DB tables
5. Create shared ID and time helpers

## Milestone 1: Phase 1 API
1. Author `phase1.proto`
2. Generate stubs
3. Implement stateless generate endpoint
4. Implement model adapter
5. Implement request validation
6. Add tests and example client

## Milestone 2: Phase 2 Session Core
1. Author `phase2.proto`
2. Implement session storage
3. Implement message storage
4. Implement create, get, and list APIs
5. Implement sync `SendMessage`
6. Reuse Phase 1 generator internally
7. Add tests

## Milestone 3: Phase 2 Async Jobs
1. Implement `SessionJob` persistence
2. Add async `SendMessage`
3. Add job polling APIs
4. Add cancellation
5. Add recovery tests

## Milestone 4: Phase 3 Minimal Collaboration
1. Author `phase3.proto`
2. Implement agent registry
3. Implement session participants
4. Implement task storage
5. Implement artifact storage
6. Add task create, get, and list APIs
7. Add artifact create and list APIs

## Milestone 5: Phase 3 Scheduling
1. Implement dependency checks
2. Implement task state machine
3. Implement ready queue
4. Dispatch tasks to assigned agents
5. Persist task outputs
6. Add retry mechanics

## Milestone 6: Phase 3 Collaboration Flow
1. Implement standard orchestration template
2. Add root-task creation on user request
3. Add planner-produced subtasks
4. Add worker artifact production
5. Add aggregator finalization
6. Post final session message

## Milestone 7: Review and Eventing
1. Implement review records
2. Add waiting-review transitions
3. Implement business-level event feed
4. Add audit-friendly lineage queries

## Milestone 8: Hardening
1. Add auth
2. Add metrics and dashboarding
3. Add failure recovery tools
4. Run compatibility tests
5. Write architecture docs and examples

---

# 8. Definition of Done by Phase

## Phase 1 Done
- stateless generate API works
- tool call schema is stable
- provider-specific logic is hidden behind adapter
- contract tests pass

## Phase 2 Done
- sessions persist durably
- one primary agent can serve a session over time
- sync and async message handling both work
- job lifecycle is stable

## Phase 3 Done
- multi-agent participation is explicit
- task dependencies are enforced
- artifacts mediate collaboration
- at least one end-to-end orchestration template works reliably
- final answers are explainable via task and artifact lineage

---

# 9. Final Guidance for the Implementing Agent

1. **Do not start with the most general model.**
   Validate one good orchestration template before trying to support arbitrary workflows.

2. **Do not overload messages.**
   Messages are for conversation. Tasks and artifacts should carry collaboration semantics.

3. **Do not expose internal debug details in proto.**
   Keep protocol clean and business-oriented.

4. **Do not skip Phase 2.**
   A strong single-agent session layer makes Phase 3 dramatically easier.

5. **Prefer additive schema evolution.**
   Every phase should feel like a natural extension of the previous one.

6. **Keep scheduling correctness ahead of sophistication.**
   A simple correct scheduler beats an ambitious fragile one.

7. **Treat artifacts as durable first-class outputs.**
   This is one of the most important design decisions for Phase 3.

---

# 10. Suggested Immediate Next Actions

The next action sequence should be:

1. Write `phase1.proto`
2. Write `phase2.proto`
3. Write `phase3.proto`
4. Review field naming for cross-phase consistency
5. Sketch DB tables
6. Implement Phase 1 service
7. Implement Phase 2 service
8. Implement minimal Phase 3 task and artifact model
9. Add scheduler
10. Add review and event features only after the collaboration flow works

---

End of plan.
