# Orca Agent Fleet — anomalia-platform
**Design doc for humans.** Orca agents do not load this file directly. The operational instructions they execute live in `AGENTS.md` / `CLAUDE.md` at the workspace root and in individual service repos.

---

## 1. Principles

1. **Procedural contracts over personas.** Each Orca extension role has explicit inputs, steps, outputs, and an automated quality gate. No conversational roleplay or vague personas.
2. **Deterministic tools before LLMs.** Anything a linter, formatter, type checker, schema validator, or test runner can decide is blocked from using LLM tokens.
3. **Author ≠ reviewer.** The Reviewer runs in an isolated, stateless Orca worker thread. It only inspects the spec and the diff—executing on a separate model instance from the author to eliminate confirmation bias.
4. **Tiers mapped to Orca agent instances.** Concrete model strings are managed directly within the Orca workspace configuration (`--agent` and `--model`), keeping raw prompts clean.
5. **One agent extension = one repo workspace task.** Parallel tasks run in independent Git worktrees (`task/T0NN-<slug>`) to prevent context contamination and file merge collisions.
6. **Strict loop circuit breakers.** Autonomous repair loops are capped at a maximum of two iterations. If deterministic gates or reviewer checks fail twice consecutively, the worker halts and escalates to the human operator.
7. **Environment isolation & safety boundaries.** All worker runs must enforce sandboxed environment overrides (`TEMP_DIR`, mock databases, temp app data) and are strictly barred from touching the `main` branch or production configurations.

---

## 2. Model Tier Mapping

| Tier | Orca Agent Mapping | Target Models | Best Suited Tasks |
| :--- | :--- | :--- | :--- |
| **Tier A — Frontier Reasoning** | Premium Cloud Layer (`--agent claude` / `--agent codex`) | **Claude 3.5 Sonnet / Claude Pro / GPT-4o** | Architecture, task decomposition, contract ownership, complex cross-repo analysis |
| **Tier B — Coding** | Premium Cloud Layer (`--agent claude` / `--agent opencode`) | **Claude 3.5 Sonnet / GPT-4o** | Feature implementation, refactoring, and satisfying failing test suites |
| **Tier C — Fast / Cheap / Local** | Local Ollama / Zero-Cost Layer (`--agent opencode --model ollama/...`) | **Ollama (`qwen2.5-coder:14b`, `deepseek-coder`)** | Structural unit/integration test authoring, deterministic linting, style & diff audits |

---

## 3. Orca Agent Configurations & Roles

```text
[Orca Coordinator Agent] (Runs on root workspace / spec-kit branch)
       │
       ├─── (Task Triage & DAG) ────► 1. Planner [Tier A - Cloud Frontier]
       │
       ├─── (Failing Tests Pass) ──► 2. Test Author [Tier C - Local Ollama]
       │
       ├─── (Implementation) ──────► 3. Developer [Tier B - Cloud Coding]
       │
       ├─── (Cross-Repo Schemas) ──► 4. Contract Owner [Tier A - Frontier]
       │
       └─── (Diff & Security Audit) ► 5. Reviewer [Tier C - Local Ollama]
```

### 3.1 Planner (Tier A)
* **Orca Execution Slot:** Workspace Root / Primary Terminal Agent (`claude` or `opencode`).
* **Input:** Raw user objective, existing `specs/`, repo architectural map.
* **Steps:** Decomposes requirements into structured task groups in `specs/NNN-feature/{spec,plan,tasks}.md`.
* **Output:** Standard spec-kit tasks with explicit input/output boundaries and binary pass/fail criteria.
* **Gate:** Every generated task must specify its target worktree, dependencies, and test commands. Cross-repo dependencies automatically trigger a hold for the Contract Owner.

### 3.2 Test Author (Tier C — Cheap / Local Ollama)
* **Orca Execution Slot:** Target Repository Child Worktree (`task/T0NN-<slug>`).
* **Agent Dispatch:** `orca orchestration worker-start --task <taskId> --worktree new-child --name test-<slug> --agent opencode --model ollama/qwen2.5-coder:14b --json`
* **Input:** `tasks.md` from the spec-kit, schema specifications in `schemas/`.
* **Steps:** Generates structural unit, integration, or contract test suites mapping strictly to the acceptance criteria. Executes tests locally to ensure they fail cleanly for expected structural reasons.
* **Output:** Failing test files committed to the task worktree branch prior to production code changes.
* **Constraints:** **Strictly forbidden from writing or altering functional application logic.** Cannot soften assertions to force passes.

### 3.3 Developer (Tier B)
* **Orca Execution Slot:** Current Task Worktree (`task/T0NN-<slug>`).
* **Agent Dispatch:** `orca orchestration worker-start --task <taskId> --worktree current --agent claude --json`
* **Input:** `tasks.md`, failing test suite files.
* **Steps:** Implements minimal functional code to satisfy the failing tests. Runs local deterministic verification scripts (§4) prior to requesting review.
* **Constraints:** Cannot edit test files written by the Test Author without raising an integration flag in the handoff document; cannot edit shared schemas.
* **Gate:** Must successfully execute all local validation pipelines detailed in §4.

### 3.4 Contract Owner (Tier A)
* **Orca Execution Slot:** Workspace Root / Shared Schemas Directory.
* **Input:** Structural modifications to messaging schemas, CloudEvents envelopes, service bus infrastructure layouts, or global configuration blocks.
* **Steps:** Verifies producer/consumer compatibility layouts. Restructures schema version markers, manages migrations, and updates cross-service validation files.
* **Gate:** Blocks all breaking changes lacking backward-compatible fallbacks or consumer-side test coverage.

### 3.5 Reviewer (Tier C — Cheap / Local Ollama)
* **Orca Execution Slot:** Isolated Read-Only Thread Workspace (`opencode-cheap-review`).
* **Agent Dispatch:** `orca orchestration worker-start --task <taskId> --worktree current --agent opencode --model ollama/qwen2.5-coder:14b --json`
* **Input:** `git diff`, `tasks.md`, and execution summaries of the deterministic pipeline gates (§4).
* **Steps:** Evaluates the diff against acceptance criteria. Inspects changes for edge-case errors, idempotency failures, security vulnerabilities (OWASP top 10, credential leaks), and observability compliance (§5).
* **Constraints:** **Forbidden from outputting code adjustments.** Bypasses style reviews (handled natively by linters).
* **Output:** Generates `specs/NNN-*/review.md` with an explicit binary pass/fail status and line-item notes. If failed, routes execution back to the Developer.

---

## 4. Deterministic Pipeline Gates & PR Packets

Orca worker threads must run these local checks before passing data to the next agent role. If these checks fail, the agent task loop halts instantly to save LLM tokens.

| Stack Component | Validation Commands |
| :--- | :--- |
| **Python (`ingestion-func`)** | `ruff check .` · `pytest` (from target root) |
| **.NET (`processing-func`)** | `dotnet format --verify-no-changes` · `dotnet build` · `dotnet test` |
| **Data Schemas** | Native JSON Schema structural validation of `schemas/*.json` |

### Automated PR Packet Requirements
Before code is proposed for merge into the working branch (`spec-kit`), the Developer agent must assemble a structured **PR Packet**:
1. **Change Summary & Intent:** Explaining what was built and referencing the exact task ID in `tasks.md`.
2. **Files Touched & Seams:** Explicit file boundaries ensuring no shared or out-of-scope files were edited.
3. **Deterministic Verification Proof:** Raw terminal logs showing passing linter, type-check, and test suite execution.
4. **Single-PR Boundary:** Exactly one agent/task owns one branch/PR to eliminate merge conflicts.

---

## 5. Observability Verification Rules

The Reviewer agent ensures compliance with these core standards:
* **OpenTelemetry** via the Azure Monitor OpenTelemetry framework routed to Application Insights.
* **Explicit tracking IDs** attached to every processing run, passed transparently through Service Bus message attributes and CloudEvents layouts.
* **Data metrics recorded per execution loop:** rows processed, rows rejected, and quarantined inputs mapped to specific error codes.

---

## 6. Orca Fleet Workflow & Orchestration State Machine

Agents pass context exclusively by writing files to the `specs/NNN-*/` path. **Agents do not read each other's live Orca chat screen histories.**

### Native Orca Orchestration Protocol (The State Machine)

```bash
# 1. Initialize Durable Run Namespace
orca orchestration run-create --objective "Implement Feature NNN" --json

# 2. Register Spec-Kit Tasks
TASK_ID=$(orca orchestration task-create --spec "Task specification text" --task-title "Task Title" --json | jq -r '.taskId')

# 3. Spawn Test Author in Child Worktree
orca orchestration worker-start --task "$TASK_ID" --worktree new-child --name "task-T0NN-slug" --agent opencode --model ollama/qwen2.5-coder:14b --setup run --json

# 4. Wait for Completion / Questions
orca orchestration check --wait --types worker_done,escalation,question --timeout-ms 900000 --json

# 5. Hand Off to Developer Agent (Same Worktree)
orca orchestration worker-start --task "$TASK_ID" --worktree current --agent claude --json
orca orchestration check --wait --types worker_done,escalation,question --timeout-ms 900000 --json

# 6. Audit Diff via Read-Only Reviewer
orca orchestration worker-start --task "$TASK_ID" --worktree current --agent opencode --model ollama/qwen2.5-coder:14b --json
orca orchestration check --wait --types worker_done,escalation,question --timeout-ms 900000 --json

# 7. Decision Gate & Merge Approval (Human-in-the-Loop)
orca orchestration gate-create --task "$TASK_ID" --question "Task passed tests and review. Merge worktree into spec-kit?" --options '["yes","no"]' --json

# 8. Terminal Cleanup & Archive
orca orchestration worker-release --dispatch <dispatchId> --json
```

### Worker Messaging Contract
* Every worker must report completion via `worker_done` with `--outcome succeeded|failed`, a summary `--body`, and `--files-modified`.
* Workers use `orca orchestration ask` for mid-task clarifications instead of interactive terminal prompts.

### Circuit Breakers & Retry Limits
* **Maximum 2 repair iterations per task group.** If a deterministic check or reviewer check fails twice consecutively, the Coordinator halts immediately and escalates to the human operator using `orca orchestration ask`.
* **Zero Main Touching:** All work runs in `task/T0NN-<slug>` worktrees and merges into `spec-kit`. Never push or commit directly to `main`.
* **Isolated Environment:** Every worker session must override `TEMP_DIR`, databases (`CASA_DB`), and application configs (`CASA_APPDATA`) to dedicated temporary folders.

---

## 7. Revised Rollout Phases

1. **Phase 1 (Hybrid Baseline):** Bind the Orca coordinator workspace to your premium Claude subscription for the **Planner** and **Developer** steps, while configuring a local **Ollama** model tier exclusively for the **Reviewer** diff audits.
2. **Phase 2 (Automated Test Insertion):** Introduce the local/cheap **Test Author** agent step to write structural test files before the cloud-tier Developer writes the implementation code.
3. **Phase 3 (Multi-Repo Federation):** Scale up to cross-project execution loops using independent workspace checkouts under the management of the **Contract Owner** agent.
