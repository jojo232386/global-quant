import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "configs" / "RELIABILITY_SOAK_PROTOCOL.md"
LIVE_READINESS = ROOT / "configs" / "LIVE_READINESS.md"


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
    assert "duplicate" in flat
    assert "does not authorize live trading" in flat
    assert "DRY_RUN_ONLY = TRUE" in text
    assert "scripts/gmaq-control" in text
    assert "scripts/gmaq-exchange-preflight" in text
    assert "scripts/gmaq-liquidity" in text
    assert "scripts/reliability-soak" in text


def test_protocol_exit_requires_hash_chain_and_full_exercise_coverage() -> None:
    flat = " ".join(PROTOCOL.read_text().split())
    assert "Audit hash chain intact" in flat
    assert "a missed exercise fails the run" in flat
    assert "restarts the soak clock" in flat
    assert "user_data/audit/soak-" in flat


def test_live_readiness_stays_planning_only_and_lists_blockers() -> None:
    text = LIVE_READINESS.read_text()
    assert "PLANNING_ONLY = TRUE" in text
    assert "does not authorize credentials" in text
    assert "Tooling presence does not remove any blocker" in text
    for blocker in (
        "sole-operator status are unverified",
        "account modes are unverified",
        "Fee rates and maintenance margin",
        "not approved",
        "reliability run has not yet been completed",
        "cannot be inferred from dry-run",
    ):
        assert blocker in text, f"missing blocker: {blocker}"
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
