#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

COMMIT=$(git rev-parse --verify HEAD)
BRANCH=$(git branch --show-current)
STATUS=$(git status --porcelain=v1 --untracked-files=all)
if [ -n "$STATUS" ]; then
    printf '%s\n' "Gate evidence requires a clean worktree." >&2
    exit 2
fi

EVIDENCE_ROOT=${1:-"$ROOT/evidence/runtime/gate1a-$COMMIT"}
case "$EVIDENCE_ROOT" in
    "$ROOT"/evidence/runtime/*) ;;
    *)
        printf '%s\n' "Evidence must stay under evidence/runtime/." >&2
        exit 2
        ;;
esac
if [ -e "$EVIDENCE_ROOT/commands.jsonl" ]; then
    printf '%s\n' "Evidence directory already contains a command log." >&2
    exit 2
fi

mkdir -p "$EVIDENCE_ROOT/determinism"
printf '%s\n' "$COMMIT" > "$EVIDENCE_ROOT/tested_commit.txt"
printf '%s\n' "$BRANCH" > "$EVIDENCE_ROOT/tested_branch.txt"
: > "$EVIDENCE_ROOT/preflight_status.txt"

PYTHON="$ROOT/.venv/bin/python"
LOGGER="$ROOT/scripts/run_logged.py"
OFFLINE="$ROOT/scripts/run_offline.sh"
COMMAND_LOG="$EVIDENCE_ROOT/commands.jsonl"

run_pytest() {
    name=$1
    seed=$2
    shift 2
    PYTHONHASHSEED=$seed "$PYTHON" "$LOGGER" \
        --name "$name" \
        --command-log "$COMMAND_LOG" \
        --output-log "$EVIDENCE_ROOT/$name.log" \
        -- "$OFFLINE" "$PYTHON" -m pytest -q \
        --junitxml "$EVIDENCE_ROOT/$name.xml" "$@"
}

for repetition in 1 2 3; do
    run_pytest "full_seed_1_rep_$repetition" 1
    run_pytest "full_seed_20260730_rep_$repetition" 20260730
done

run_pytest network_matrix 20260730 tests/integration/test_network_isolation.py
run_pytest crash_matrix 20260730 tests/integration/test_crash_recovery.py
run_pytest nautilus_backtest 20260730 tests/integration/test_nautilus_backtest.py
run_pytest scenario_matrix_test 20260730 \
    tests/integration/test_scenario_matrix.py \
    tests/unit/test_scenario_oracle.py
run_pytest determinism_matrix_test 20260730 \
    tests/integration/test_determinism_matrix.py

PYTHONHASHSEED=20260730 "$PYTHON" "$LOGGER" \
    --name scenario_evidence \
    --command-log "$COMMAND_LOG" \
    --output-log "$EVIDENCE_ROOT/scenario_evidence.log" \
    -- "$OFFLINE" "$PYTHON" "$ROOT/scripts/run_scenario_matrix.py" \
    --scenario-root "$EVIDENCE_ROOT/scenario-work" \
    --output "$EVIDENCE_ROOT/scenario_results.json" \
    --repetition 1

PYTHONHASHSEED=20260730 "$PYTHON" "$LOGGER" \
    --name determinism_evidence \
    --command-log "$COMMAND_LOG" \
    --output-log "$EVIDENCE_ROOT/determinism_evidence.log" \
    -- "$OFFLINE" "$PYTHON" "$ROOT/scripts/run_determinism_evidence.py" \
    --output-root "$EVIDENCE_ROOT/determinism"

"$OFFLINE" "$PYTHON" "$ROOT/scripts/build_gate_manifest.py" \
    --evidence-root "$EVIDENCE_ROOT" \
    --tested-commit "$COMMIT" \
    --output "$EVIDENCE_ROOT/candidate_manifest.json"

"$OFFLINE" "$PYTHON" "$ROOT/scripts/decide_gate.py" \
    --manifest "$EVIDENCE_ROOT/candidate_manifest.json" \
    --output "$EVIDENCE_ROOT/machine_candidate_verdict.json"

printf '%s\n' "candidate_evidence=$EVIDENCE_ROOT"
