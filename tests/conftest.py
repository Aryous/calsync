import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_calsync_debug(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """The type-only log assertions hold whatever the developer's shell sets; a test that
    wants ``CALSYNC_DEBUG`` sets it itself."""
    monkeypatch.delenv("CALSYNC_DEBUG", raising=False)


@pytest.fixture(autouse=True)
def _no_real_config(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A command a test runs without ``--config`` / ``--state-dir`` never reaches the
    developer's own ``~/.config/calsync`` or state (``trigger`` without a daemon runs a cycle)."""
    home = tmp_path_factory.mktemp("no-home")
    monkeypatch.setenv("CALSYNC_CONFIG", str(home / "config.toml"))
    monkeypatch.setenv("CALSYNC_STATE_DIR", str(home / "state"))


@pytest.fixture
def local_zone() -> Iterator[Callable[[str], None]]:
    """``local_zone("Asia/Shanghai")`` makes that the process's local zone (``TZ`` plus
    ``time.tzset``) for the rest of the test; the previous zone comes back afterwards."""
    saved = os.environ.get("TZ")

    def set_zone(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    try:
        yield set_zone
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A scratch directory under /tmp: macOS limits a unix socket path to 104 bytes, and
    ``tmp_path`` alone is longer than that there."""
    path = Path(tempfile.mkdtemp(prefix="calsync-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


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
