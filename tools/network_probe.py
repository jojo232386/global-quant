from __future__ import annotations

import asyncio
import errno
import os
import socket
import subprocess
import sys
import urllib.request


def socket_family(mode: str) -> tuple[int, tuple]:
    if mode.endswith("ipv6"):
        return socket.AF_INET6, ("2606:4700:4700::1111", 443, 0, 0)
    return socket.AF_INET, ("1.1.1.1", 443)


def raw_os_probe(mode: str) -> int:
    family, address = socket_family(mode)
    connection = socket.socket(family, socket.SOCK_STREAM)
    result = connection.connect_ex(address)
    connection.close()
    if result not in {errno.EPERM, errno.EACCES}:
        print(f"os_network_denied=FAIL errno={result}")
        return 2
    print(f"os_network_denied=PASS errno={result}")
    return 0


def main() -> int:
    mode = sys.argv[1]
    if mode.startswith("os-"):
        return raw_os_probe(mode)
    if mode in {"connect", "ipv4", "ipv6"}:
        family, address = socket_family(mode)
        connection = socket.socket(family, socket.SOCK_STREAM)
        connection.connect(address)
    elif mode == "connect_ex":
        connection = socket.socket()
        connection.connect_ex(("1.1.1.1", 443))
    elif mode == "dns":
        socket.getaddrinfo("example.com", 443)
    elif mode == "http":
        urllib.request.urlopen("http://example.com", timeout=1)
    elif mode == "https":
        urllib.request.urlopen("https://example.com", timeout=1)
    elif mode == "websocket":
        asyncio.run(asyncio.open_connection("example.com", 443))
    elif mode == "child":
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "import socket; socket.create_connection(('1.1.1.1', 443))",
            ],
            env=os.environ,
            check=False,
        )
        return child.returncode
    else:
        raise ValueError(mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
