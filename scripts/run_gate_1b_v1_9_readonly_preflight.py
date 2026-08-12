#!/usr/bin/env python3
"""Run the Gate 1B v1.9 authenticated read-only Demo preflight."""

import sys

from global_quant.gate1b.read_only_preflight_cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
