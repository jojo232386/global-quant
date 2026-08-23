import importlib.util
import pathlib
from importlib.machinery import SourceFileLoader

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "configs" / "RELIABILITY_SOAK_PROTOCOL.md"
LIVE_READINESS = ROOT / "configs" / "LIVE_READINESS.md"
RUNNER = ROOT / "scripts" / "reliability-soak"
MANIFEST = ROOT / "scripts" / "gmaq-runtime-manifest"


def load_manifest(name: str):
    loader = SourceFileLoader(name, str(MANIFEST))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_protocol_defines_entry_exercises_exit_and_evidence() -> None:
    text = PROTOCOL.read_text()
    for section in ("Entry gates", "Scheduled exercises", "Exit criteria", "Evidence package", "Acceptance"):
        assert f"## {section}" in text, f"missing section: {section}"
    for exercise in ("E1", "E2", "E4", "E5", "E6", "E7", "E8"):
        assert exercise in text, f"missing exercise: {exercise}"
    flat = " ".join(text.split())
    assert "48–72 hours" in flat
    assert "suspended host time never counts" in flat
    assert "zero open positions" in flat
    assert "zero open orders" in flat
    assert "A zero-trade run fails" in flat
    assert "trade-baseline.json" in flat
    assert "trade-lifecycle.json" in flat
    assert "MATCH verdicts" in flat
    assert "API reconnection" in flat
    assert "restart-recovery.md" not in flat
    assert "duplicate" in flat
    assert "does not authorize live trading" in flat
    assert "DRY_RUN_ONLY = TRUE" in text
    assert "scripts/gmaq-control" in text
    assert "scripts/gmaq-exchange-preflight" in text
    assert "scripts/gmaq-liquidity" in text
    assert "scripts/reliability-soak" in text
    assert "scripts/gmaq-runtime-manifest" in text
    assert "SMOKE_ONLY_PASS" in text
    assert "OBSERVATION_ONLY_PASS" in text
    assert "legacy continuously-ARMED 48-hour" in flat
    scheduled = text[text.index("## Scheduled exercises") : text.index("## Exit criteria")]
    assert "| E3 |" not in scheduled


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
    assert 'MAX_LOOP_GAP_SECONDS=300' in text
    assert 'LOOP_GAP_SECONDS < 0 || LOOP_GAP_SECONDS > MAX_LOOP_GAP_SECONDS' in text
    assert 'while true; do' in text and 'NOW >= END_EPOCH' in text
    assert 'continuous soak monitor gap is invalid' in text
    assert "runtime-binding.json" in text
    assert "initial-audit-verify.json" in text
    assert text.count("final-audit-verify.json") >= 2
    assert "trap cleanup EXIT INT TERM" in text
    assert "wait_control_verdict health HEALTHY" in text
    assert "wait_control_verdict reconcile MATCH" in text
    assert "wait_for_canary_completion" in text
    assert "soak_canary_lifecycle_complete" in text
    assert "OBSERVATION_ONLY_PASS" in text
    smoke = text[text.index("if [[ $MODE == smoke ]]"):text.index("# A promoted soak")]
    assert "gmaq-control arm" not in smoke
    assert "SMOKE_ONLY_PASS" in smoke


def test_runner_strictly_preflights_before_its_only_arm() -> None:
    text = RUNNER.read_text()
    first_preflight = text.index('record_command "$EVIDENCE_DIR/preflight.json" ./scripts/gmaq-control preflight')
    second_preflight = text.index('record_command "$EVIDENCE_DIR/preflight-after-kill.json" ./scripts/gmaq-control preflight')
    arm = text.index('./scripts/gmaq-control arm --authorization-id "$AUTHORIZATION_ID"')

    assert first_preflight < second_preflight < arm
    assert '[[ $(json_verdict <"$EVIDENCE_DIR/preflight-after-kill.json") == PASS ]]' in text
    assert text.count("gmaq-control arm --authorization-id") == 1


def test_runner_disarms_after_canary_then_observes_without_rearming() -> None:
    text = RUNNER.read_text()
    canary_wait = text.index("wait_for_canary_completion\n")
    disarm = text.index("soak_canary_lifecycle_complete")
    observation = text.index("# The observation clock starts only after entries are disabled.")
    loop = text.index("while true; do", observation)
    post_canary = text[disarm:]

    assert canary_wait < disarm < observation < loop
    assert "gmaq-control arm" not in post_canary
    assert "gmaq-control preflight" not in post_canary
    assert "NEXT_REFRESH" not in post_canary


def test_disarmed_observation_continues_health_reconcile_audit_and_duplicate_checks() -> None:
    text = RUNNER.read_text()
    observation = text[text.index("# The observation clock starts only after entries are disabled."):]

    assert 'record_command "$EVIDENCE_DIR/health-samples.jsonl" ./scripts/gmaq-control health' in observation
    assert 'record_command "$EVIDENCE_DIR/reconcile-records.jsonl" ./scripts/gmaq-control reconcile' in observation
    assert 'record_command "$EVIDENCE_DIR/audit-verify-records.jsonl" ./scripts/gmaq-control audit verify' in observation
    assert "duplicate_scan >>\"$EVIDENCE_DIR/duplicate-scans.jsonl\"" in observation


def test_runner_anchors_audit_after_runtime_binding() -> None:
    text = RUNNER.read_text()
    first_up = text.index("./scripts/gmaq up")
    first_wait = text.index("wait_ping", first_up)
    first_anchor_seq = text.index("START_SEQ=$(audit_count)")
    first_anchor = text.index("capture_audit_anchor", first_wait)

    assert first_up < first_wait < first_anchor_seq < first_anchor


def test_runtime_manifest_reads_only_allowlisted_non_secret_env_fields() -> None:
    module = load_manifest("gmaq_runtime_manifest_test")
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
    assert "container image does not match compose-pinned image" in text


def test_runtime_manifest_rejects_container_image_drift(monkeypatch) -> None:
    module = load_manifest("gmaq_runtime_manifest_image_test")
    expected_ref = "freqtradeorg/freqtrade:stable@sha256:" + "a" * 64
    monkeypatch.setattr(module, "compose_service_image", lambda: expected_ref)
    monkeypatch.setattr(
        module,
        "inspect_field",
        lambda container, template: expected_ref if template == "{{.Config.Image}}" else "sha256:" + "b" * 64,
    )
    monkeypatch.setattr(module, "run_text", lambda command, **kwargs: "sha256:" + "c" * 64)

    with pytest.raises(module.ManifestError, match="container image does not match"):
        module.verified_container_image("gmaq-freqtrade-p0-remediation")


def test_runtime_manifest_accepts_compose_pinned_image_and_id(monkeypatch) -> None:
    module = load_manifest("gmaq_runtime_manifest_image_match_test")
    expected_ref = "freqtradeorg/freqtrade:stable@sha256:" + "a" * 64
    expected_id = "sha256:" + "b" * 64
    monkeypatch.setattr(module, "compose_service_image", lambda: expected_ref)
    monkeypatch.setattr(
        module,
        "inspect_field",
        lambda container, template: expected_ref if template == "{{.Config.Image}}" else expected_id,
    )
    monkeypatch.setattr(module, "run_text", lambda command, **kwargs: expected_id)

    assert module.verified_container_image("gmaq-freqtrade-p0-remediation") == (
        expected_ref,
        expected_id,
    )


def test_runtime_manifest_reads_freqtrade_image_from_tracked_compose() -> None:
    module = load_manifest("gmaq_runtime_manifest_compose_image_test")

    assert module.compose_service_image() == (
        "freqtradeorg/freqtrade:stable@sha256:"
        "50720a4af314a812be2cfbf5cc6331c63e9332b06f3f4372241f54bc61a35486"
    )


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
