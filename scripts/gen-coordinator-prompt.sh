#!/usr/bin/env bash
# ==============================================================================
# gen-coordinator-prompt.sh
# Dynamic Prompt Generator for Orca Coordinator (anomalia project)
# ==============================================================================
# Usage:
#   ./scripts/gen-coordinator-prompt.sh [options]
#
# Examples:
#   ./scripts/gen-coordinator-prompt.sh -f specs/001-alert-email-import -t "Phase 3: T010-T012" -b spec-kit
#   ./scripts/gen-coordinator-prompt.sh -f specs/002-telemetry-sync -t "T001" -b develop -m "ollama/deepseek-coder:14b"
#   ./scripts/gen-coordinator-prompt.sh --interactive
# ==============================================================================

set -euo pipefail

# Defaults
PROJECT_NAME="anomalia-platform"
FEATURE_DIR=""
TASK_SCOPE=""
TARGET_BRANCH="spec-kit"
TIER_C_MODEL="ollama/qwen2.5-coder:14b"
STACK_GATES="ruff check . && pytest"
INTERACTIVE=false

# Helper: print usage
usage() {
  cat <<HELP
Usage: $(basename "$0") [OPTIONS]

Options:
  -f, --feature-dir <path>     Path to spec directory (e.g. specs/001-alert-email-import)
  -t, --tasks <scope>          Task scope or group (e.g. "Phase 3: T010-T012" or "T010")
  -b, --branch <name>          Base target branch (default: "spec-kit")
  -p, --project <name>         Project name (default: "anomalia-platform")
  -m, --model-tier-c <model>   Tier C Ollama model (default: "ollama/qwen2.5-coder:14b")
  -g, --gates <commands>       Deterministic test gates (default: "ruff check . && pytest")
  -i, --interactive            Prompt interactively for inputs
  -h, --help                   Show this help message

HELP
  exit 0
}

# Parse CLI arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--feature-dir)
      FEATURE_DIR="$2"
      shift 2
      ;;
    -t|--tasks)
      TASK_SCOPE="$2"
      shift 2
      ;;
    -b|--branch)
      TARGET_BRANCH="$2"
      shift 2
      ;;
    -p|--project)
      PROJECT_NAME="$2"
      shift 2
      ;;
    -m|--model-tier-c)
      TIER_C_MODEL="$2"
      shift 2
      ;;
    -g|--gates)
      STACK_GATES="$2"
      shift 2
      ;;
    -i|--interactive)
      INTERACTIVE=true
      shift
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "Unknown option: $1"
      usage
      ;;
  esac
done

# Interactive mode if requested or if feature-dir is missing
if [ "$INTERACTIVE" = true ] || [ -z "$FEATURE_DIR" ]; then
  echo "--- Interactive Coordinator Prompt Configuration ($PROJECT_NAME) ---"
  
  read -r -p "Feature directory [e.g., specs/001-alert-email-import]: " INPUT_FEATURE_DIR
  FEATURE_DIR="${INPUT_FEATURE_DIR:-specs/001-alert-email-import}"

  read -r -p "Task scope [e.g., Phase 3 (T010-T012)]: " INPUT_TASK_SCOPE
  TASK_SCOPE="${INPUT_TASK_SCOPE:-Phase 3}"

  read -r -p "Target branch [default: spec-kit]: " INPUT_BRANCH
  TARGET_BRANCH="${INPUT_BRANCH:-spec-kit}"

  read -r -p "Tier C local model [default: ollama/qwen2.5-coder:14b]: " INPUT_TIER_C
  TIER_C_MODEL="${INPUT_TIER_C:-ollama/qwen2.5-coder:14b}"

  read -r -p "Deterministic test gate [default: ruff check . && pytest]: " INPUT_GATES
  STACK_GATES="${INPUT_GATES:-ruff check . && pytest}"
fi

# Derive Feature Title from spec.md if available
FEATURE_TITLE=""
if [ -f "$FEATURE_DIR/spec.md" ]; then
  FEATURE_TITLE=$(grep -m 1 "^# " "$FEATURE_DIR/spec.md" 2>/dev/null | sed 's/^# //' || true)
fi
if [ -z "$FEATURE_TITLE" ]; then
  FEATURE_TITLE=$(basename "$FEATURE_DIR")
fi

# Print prompt
cat <<EOF
============================= COPY TO ORCA COORDINATOR =============================
You are the Orca Coordinator for ${PROJECT_NAME}. Your mission is to implement and verify:
Feature: ${FEATURE_TITLE}
Directory: ${FEATURE_DIR}
Scope: ${TASK_SCOPE}. Work from this workspace root on branch '${TARGET_BRANCH}'.

Read first, in this order:
1. AGENTS.md
2. .specify/memory/constitution.md
3. docs/agent-fleet.md
4. ${FEATURE_DIR}/tasks.md

### Core Loop Protocol
For each task group in ${FEATURE_DIR}/tasks.md within scope (${TASK_SCOPE}), execute and supervise the following state machine:

1. INITIALIZE RUN & TASK
   - Call: orca orchestration run-create --objective "Implement ${FEATURE_TITLE}" --json
   - Register the task group:
     orca orchestration task-create --spec "<task_specification_from_tasks.md>" --task-title "<task_title>" --json
   - Save the returned taskId.

2. STAGE 1: TEST AUTHOR (Tier C — Local Ollama)
   - Spawn a Test Author in a fresh child worktree:
     orca orchestration worker-start --task <taskId> --worktree new-child --name task-T0NN-<slug> --agent opencode --model ${TIER_C_MODEL} --setup run --json
   - Role boundaries: Strictly forbidden from writing or modifying application logic. Must write structural unit/integration tests mapping directly to acceptance criteria.
   - Run: orca orchestration check --wait --types worker_done,escalation,question --timeout-ms 900000 --json
   - Verify that all newly created tests fail cleanly for structural reasons before advancing to Stage 2.

3. STAGE 2: DEVELOPER (Tier B — Cloud Coding)
   - Hand off the existing child worktree to the Developer:
     orca orchestration worker-start --task <taskId> --worktree current --agent claude --json
   - Implement minimal application logic until local deterministic gates pass:
     ${STACK_GATES}
   - Monitor via: orca orchestration check --wait
   - Developer prepares a PR packet: change intent, files touched, and passing test logs.

4. STAGE 3: REVIEWER (Tier C — Local Ollama / Author ≠ Reviewer)
   - Dispatch an isolated, read-only Reviewer in the worktree:
     orca orchestration worker-start --task <taskId> --worktree current --agent opencode --model ${TIER_C_MODEL} --json
   - Evaluate git diff against acceptance criteria for edge cases, security flaws (OWASP top 10), and idempotency. Forbidden from modifying code.
   - Reviewer outputs ${FEATURE_DIR}/review.md with an explicit binary status: [PASS] or [FAIL].

5. REPAIR LOOP & CIRCUIT BREAKER
   - If Reviewer status is [FAIL]: Feed review.md back to the Developer in the worktree and repeat Stages 2–4.
   - Circuit Breaker: If deterministic gates or reviewer audits fail TWICE consecutively, halt the loop immediately and escalate using:
     orca orchestration ask

6. DECISION GATE & MERGE
   - On [PASS], open an approval gate:
     orca orchestration gate-create --task <taskId> --question "Task <task_title> passed tests and code review. Merge worktree into ${TARGET_BRANCH}?" --options '["yes","no"]' --json
   - Upon receiving approval, merge task-T0NN-<slug> locally into '${TARGET_BRANCH}', release the worker pane (orca orchestration worker-release), and check off the item in ${FEATURE_DIR}/tasks.md.

### Hard Safety Limits
- Never push, never commit directly to main, and never touch production credentials or live databases.
- Enforce sandboxed environment variables (TEMP_DIR, temporary mock databases, and temp app data) in every worker session.
- Pilot rule: Complete exactly ONE task group end to end, then STOP and send me a summary report (gates passed, loop iterations used, files touched). Wait for my go-ahead before starting the next group.
====================================================================================
EOF
