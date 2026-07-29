#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PROFILE='(version 1)(allow default)(deny network*)'
PYTHON_GUARD="$ROOT/tools/offline_guard"

if [ "${1:-}" = "--without-python-guard" ]; then
    shift
    exec /usr/bin/sandbox-exec -p "$PROFILE" /usr/bin/env \
        -u GLOBAL_QUANT_OFFLINE \
        -u PYTHONPATH \
        "$@"
fi

if [ -n "${PYTHONPATH:-}" ]; then
    GUARDED_PYTHONPATH="$PYTHON_GUARD:$ROOT/src:$PYTHONPATH"
else
    GUARDED_PYTHONPATH="$PYTHON_GUARD:$ROOT/src"
fi

exec /usr/bin/sandbox-exec -p "$PROFILE" /usr/bin/env \
    GLOBAL_QUANT_OFFLINE=1 \
    PYTHONPATH="$GUARDED_PYTHONPATH" \
    "$@"
