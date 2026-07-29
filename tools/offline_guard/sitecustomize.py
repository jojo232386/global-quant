from __future__ import annotations

import json
import os
import socket
import traceback
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import NoReturn


class OfflineNetworkViolation(RuntimeError):
    pass


def _deny(call: str) -> NoReturn:
    path = os.environ.get("GQ_NETWORK_VIOLATION_LOG")
    record = {
        "call": call,
        "timestamp": datetime.now(UTC).isoformat(),
        "stack": "".join(traceback.format_stack()),
    }
    if path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    raise OfflineNetworkViolation(f"offline gate blocked network call: {call}")


def _blocked_connect(*_args, **_kwargs) -> NoReturn:
    _deny("socket.connect")


def _blocked_connect_ex(*_args, **_kwargs) -> NoReturn:
    _deny("socket.connect_ex")


def _blocked_create_connection(*_args, **_kwargs) -> NoReturn:
    _deny("socket.create_connection")


def _blocked_dns(*_args, **_kwargs) -> NoReturn:
    _deny("dns")


if os.environ.get("GLOBAL_QUANT_OFFLINE") == "1":
    socket.socket.connect = _blocked_connect
    socket.socket.connect_ex = _blocked_connect_ex
    socket.create_connection = _blocked_create_connection
    socket.getaddrinfo = _blocked_dns
    socket.gethostbyname = _blocked_dns
    socket.gethostbyname_ex = _blocked_dns
    socket.gethostbyaddr = _blocked_dns

