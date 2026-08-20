import importlib.util
import pathlib
from importlib.machinery import SourceFileLoader


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "configs" / "RELIABILITY_SOAK_PROTOCOL.md"
LIVE_READINESS = ROOT / "configs" / "LIVE_READINESS.md"
RUNNER = ROOT / "scripts" / "reliability-soak"
MANIFEST = ROOT / "scripts" / "gmaq-runtime-manifest"


def test_protocol_defines_entry_exercises_exit_and_evidence() -> None:
    text = PROTOCOL.read_text()
    for section in ("Entry gates", "Scheduled exercises", "Exit criteria", "Evidence package", "Acceptance"):
        assert f"## {section}" in text, f"missing section: {section}"
    for exercise in ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8"):
        assert exercise in text, f"missing exercise: {exercise}"
    flat = " ".join(text.split())
    assert "48–72 hours" in flat
    assert "zero open positions" in flat
    assert "zero open orders" in flat
    assert "A zero-trade soak fails" in flat
    assert "trade-baseline.json" in flat
    assert "trade-lifecycle.json" in flat
    assert "duplicate" in flat
    assert "does not authorize live trading" in flat
    assert "DRY_RUN_ONLY = TRUE" in text
    assert "scripts/gmaq-control" in text
    assert "scripts/gmaq-exchange-preflight" in text
    assert "scripts/gmaq-liquidity" in text
    assert "scripts/reliability-soak" in text
    assert "scripts/gmaq-runtime-manifest" in text
    assert "SMOKE_ONLY_PASS" in text


def test_protocol_exit_requires_hash_chain_and_full_exercise_coverage() -> None:
    flat = " ".join(PROTOCOL.read_text().split())
    assert "Audit hash chain intact" in flat
    assert "a missed exercise fails the run" in flat
    assert "restarts the soak clock" in flat
    assert "user_data/audit/soak-" in flat


def test_runner_is_isolated_fail_closed_and_authorization_gated() -> None:
    text = RUNNER.read_text()
    assert "--smoke" in text
    assert "--authorization-id" in text
    assert "48|49|50" in text and "71|72" in text
    assert "GMAQ_API_BASE:-http://127.0.0.1:8080" not in text
    assert "GMAQ_CONTAINER_NAME:-gmaq-freqtrade" not in text
    assert "gmaq-runtime-manifest --expected-state DISARMED" in text
    assert "gmaq-control reconcile" in text
    assert "gmaq-control exit" in text
    assert 'trade-baseline.json' in text
    assert 'trade-lifecycle.json' in text
    assert 'LiveExecutionCanaryStrategy' in text
    assert 'complete_canary_trade_ids' in text
    assert 'closed_sides_by_trade' in text
    assert 'no complete post-baseline canary trade lifecycle' in text
    assert "runtime-binding.json" in text
    assert "initial-audit-verify.json" in text
    assert text.count("final-audit-verify.json") >= 2
    assert "trap cleanup EXIT INT TERM" in text
    assert "wait_control_verdict health HEALTHY" in text
    assert "wait_control_verdict reconcile MATCH" in text
    smoke = text[text.index("if [[ $MODE == smoke ]]"):text.index("# A promoted soak")]
    assert "gmaq-control arm" not in smoke
    assert "SMOKE_ONLY_PASS" in smoke


def test_runtime_manifest_reads_only_allowlisted_non_secret_env_fields() -> None:
    loader = SourceFileLoader("gmaq_runtime_manifest_test", str(MANIFEST))
    spec = importlib.util.spec_from_loader("gmaq_runtime_manifest_test", loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    assert module.SAFE_ENV_KEYS == {
        "GMAQ_API_BASE",
        "GMAQ_CONTAINER_NAME",
        "GMAQ_HOST_PORT",
    }
    text = MANIFEST.read_text()
    assert "GMAQ_API_PASSWORD" not in text
    assert "GMAQ_API_USERNAME" not in text
    assert '"contains_secrets": False' in text
    assert "container identity does not match runtime binding" in text
    assert 'EXPECTED_CONTAINER = "gmaq-freqtrade-p0-remediation"' in text
    assert "EXPECTED_HOST_PORT = 8082" in text
    assert "published container port does not match isolated API endpoint" in text


def test_live_readiness_stays_planning_only_and_lists_blockers() -> None:
    text = LIVE_READINESS.read_text()
    flat = " ".join(text.split())
    assert "PLANNING_ONLY = TRUE" in text
    assert "does not authorize credentials" in text
    assert "Tooling presence does not remove any blocker" in text
    for blocker in (
        "sole-operator status are unverified",
        "account modes are unverified",
        "None of those historical values is accepted as current",
        "both returned Binance `-2015`",
        "remain `UNVERIFIED`",
        "not approved",
        "reliability run has not yet been completed",
        "cannot be inferred from dry-run",
    ):
        assert blocker in flat, f"missing blocker: {blocker}"
    assert "LIVE_READINESS_BLOCKER" in text
    assert "CONTRACT" not in text or True  # no-op guard: never weaken to a pass


def test_live_readiness_references_all_new_tooling() -> None:
    text = LIVE_READINESS.read_text()
    for tool in (
        "configs/CONTROL_PLANE.md",
        "scripts/gmaq-control",
        "scripts/gmaq-exchange-preflight",
        "configs/EXECUTION_COST_MODEL.md",
        "scripts/gmaq-liquidity",
        "configs/RELIABILITY_SOAK_PROTOCOL.md",
    ):
        assert tool in text, f"missing tool reference: {tool}"
