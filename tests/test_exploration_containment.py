"""Containment gate for exploration-layer Markdown records.

Mechanizes the protocol rules that were previously enforced only by
discipline (review rounds found them leaking four times):

1. No performance figures with 3+ decimal places in exploration .md files
   (grid parameters and rule thresholds use at most 2 decimals; results
   live only in the JSON artifacts).
2. Banned overclaim phrases ("cross-market confirmation" etc.) — an N=2
   diagnostic may at most be "direction consistent" with another market.
3. Screening-order section must quote each card's CURRENT verdict token;
   stale verdicts (the round-2 finding) fail loudly.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXPLORATION_DIR = ROOT / "research" / "exploration"
VERDICT_TOKENS = (
    "EXPLORATION_PASS",
    "FAIL",
    "KEPT_PRIMARY_SELECTED",
    "KEPT_THIN",
    "KEPT_N2_ONLY",
    "DROPPED_COST_FRAGILE",
    "DROPPED",
    "BLOCKED_ON_DATA",
    "DATA_ERROR_STOP",
    "PROCESS_DEFECT_STOP",
    "NOT_SELECTED",
)
BANNED_PHRASES = ("cross-market confirmation", "跨市场互证")

CARD_HEADER_RE = re.compile(r"^### (EXPL-\d+)[^\n]*·\s*([A-Z_]+)\s*$", re.M)
HIGH_PRECISION_RE = re.compile(r"\d\.\d{3,}")


def markdown_files() -> list[pathlib.Path]:
    return sorted(EXPLORATION_DIR.glob("*.md"))


def test_no_performance_figures_in_markdown_records():
    for path in markdown_files():
        text = path.read_text(encoding="utf-8")
        match = HIGH_PRECISION_RE.search(text)
        assert match is None, (
            f"{path.name} contains a 3+ decimal figure ({match.group(0) if match else ''}); "
            "performance numbers belong only in the JSON artifacts"
        )


def test_no_banned_overclaim_phrases():
    for path in markdown_files():
        text = path.read_text(encoding="utf-8")
        for phrase in BANNED_PHRASES:
            assert phrase not in text, (
                f"{path.name} contains banned overclaim phrase: {phrase}"
            )


def test_screening_order_matches_card_verdicts():
    backlog = EXPLORATION_DIR / "hypothesis-backlog.md"
    text = backlog.read_text(encoding="utf-8")
    header_verdicts = {
        card: verdict for card, verdict in CARD_HEADER_RE.findall(text)
    }
    order_match = re.search(r"## Screening order\n(.*?)(?=\n## |\Z)", text, re.S)
    assert order_match, "screening order section missing"
    order_section = order_match.group(1)
    problems = []
    for card, header_verdict in header_verdicts.items():
        mentions = [
            line for line in order_section.splitlines() if card in line
        ]
        if not mentions:
            continue  # cards absent from the order section are not checked
        line = mentions[0]
        verdict_in_order = [token for token in VERDICT_TOKENS if token in line]
        if verdict_in_order:
            # a split-status line may name several tokens; the CARD verdict
            # is the one that appears first in the line
            first = min(verdict_in_order, key=lambda t: line.index(t))
            if first != header_verdict:
                problems.append(
                    f"{card}: screening order says {first}, "
                    f"card header says {header_verdict}"
                )
        elif "Queue" not in line or header_verdict != "DRAFT":
            # mentioning a card with a conclusion requires classification
            # first; unclassified mentions are only allowed on explicitly
            # queued lines of still-DRAFT cards
            problems.append(
                f"{card}: mentioned in screening order without a verdict "
                f"(header {header_verdict}); classify the card or move it "
                f"to a Queue line"
            )
    assert not problems, "stale screening-order verdicts: " + "; ".join(problems)
