from __future__ import annotations

import json
import os
from pathlib import Path

from global_quant.gate1a.environment import collect_tool_versions


REQUIRED_KEYS = {
    "python",
    "nautilus_trader",
    "pytest",
    "uv",
    "platform",
    "architecture",
}


def test_tool_versions_are_sampled_from_the_running_environment() -> None:
    sampled = collect_tool_versions()

    assert set(sampled) == REQUIRED_KEYS
    for item in sampled.values():
        assert set(item) == {"value", "source"}
        assert isinstance(item["value"], str)
        assert item["value"]
        assert isinstance(item["source"], str)
        assert item["source"]

    output = os.environ.get("GATE_TOOL_VERSION_OUTPUT")
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(sampled, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
