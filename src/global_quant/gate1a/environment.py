from __future__ import annotations

import platform
import shutil
import subprocess
from importlib import metadata


def collect_tool_versions() -> dict[str, dict[str, str]]:
    uv_path = shutil.which("uv")
    if uv_path is None:
        raise RuntimeError("uv executable is unavailable")
    uv_output = subprocess.check_output(
        [uv_path, "--version"],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()
    if not uv_output.startswith("uv "):
        raise RuntimeError(f"unexpected uv version output: {uv_output}")

    return {
        "python": {
            "value": platform.python_version(),
            "source": "platform.python_version()",
        },
        "nautilus_trader": {
            "value": metadata.version("nautilus-trader"),
            "source": "importlib.metadata.version('nautilus-trader')",
        },
        "pytest": {
            "value": metadata.version("pytest"),
            "source": "importlib.metadata.version('pytest')",
        },
        "uv": {
            "value": uv_output.removeprefix("uv "),
            "source": f"{uv_path} --version",
        },
        "platform": {
            "value": platform.platform(),
            "source": "platform.platform()",
        },
        "architecture": {
            "value": platform.machine(),
            "source": "platform.machine()",
        },
    }
