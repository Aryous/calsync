import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def radicale_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A throwaway Radicale CalDAV server (auth disabled: any username works)."""
    pytest.importorskip("radicale")
    port = _free_port()
    storage: Path = tmp_path_factory.mktemp("radicale")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "radicale",
            "--storage-filesystem-folder",
            str(storage),
            "--auth-type",
            "none",
            "--server-hosts",
            f"127.0.0.1:{port}",
            "--logging-level",
            "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}/"
    deadline = time.monotonic() + 15
    while True:
        try:
            httpx.get(url, timeout=0.5)
            break
        except httpx.TransportError:
            if proc.poll() is not None or time.monotonic() > deadline:
                proc.kill()
                _, stderr = proc.communicate()  # reaps the process and closes the pipe
                pytest.fail(f"radicale did not start: {stderr.decode()}")
            time.sleep(0.1)
    try:
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.stderr:
            proc.stderr.close()


@pytest.fixture
def dav_user() -> str:
    """A fresh username per test, so every test gets an empty calendar home."""
    return f"u{uuid.uuid4().hex[:10]}"
