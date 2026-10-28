import ipaddress
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
from typing import Any

import httpx
import httpx._utils as httpx_utils  # pyright: ignore[reportPrivateUsage]
import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_calsync_debug(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """The type-only log assertions hold whatever the developer's shell sets; a test that
    wants ``CALSYNC_DEBUG`` sets it itself."""
    monkeypatch.delenv("CALSYNC_DEBUG", raising=False)


# --- The sealed test environment -------------------------------------------------------
# No test may reach the developer's own config or state, the real keychain or the network.
# Every guard fails closed: a test that needs something real fakes it itself.

PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
KEYRING_REFUSED = "a test reached the real keyring"
LAUNCHCTL_REFUSED = "a test reached the real launchctl"


class Seal:
    """What the guards refused during one test. A guard raises ``AssertionError``, but code
    under test may catch broad exceptions (per-rule isolation, ``ContainErrors``), so every
    refusal is also recorded here and fails the test at teardown. A test of a guard itself
    clears ``refused`` once it has seen the error."""

    def __init__(self) -> None:
        self.refused: list[str] = []

    def refuse(self, message: str) -> AssertionError:
        self.refused.append(message)
        return AssertionError(message)


class SealedKeyring:
    """Stands in for the ``keyring`` module: every call fails. A test with a keychain fake
    patches ``credentials._keyring`` itself, which replaces this one."""

    def __init__(self, seal: Seal) -> None:
        self._seal = seal

    def get_password(self, *args: object, **kwargs: object) -> str | None:
        raise self._seal.refuse(KEYRING_REFUSED)

    def set_password(self, *args: object, **kwargs: object) -> None:
        raise self._seal.refuse(KEYRING_REFUSED)

    def delete_password(self, *args: object, **kwargs: object) -> None:
        raise self._seal.refuse(KEYRING_REFUSED)


def is_local_host(host: object) -> bool:
    """Loopback (``127.0.0.0/8``, ``::1``), ``localhost``, or no host at all (binding)."""
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    name = host.strip().strip("[]").split("%", 1)[0].rstrip(".").lower()
    if name in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _seal_network(monkeypatch: pytest.MonkeyPatch, seal: Seal) -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    real_gethostbyname = socket.gethostbyname
    real_gethostbyname_ex = socket.gethostbyname_ex

    def check(sock: socket.socket | None, address: Any) -> None:
        if sock is not None and sock.family == getattr(socket, "AF_UNIX", None):
            return
        host = address[0] if isinstance(address, tuple) and address else address
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        if not is_local_host(host):
            raise seal.refuse(f"a test reached the network: {host}")

    def connect(self: socket.socket, address: Any) -> None:
        check(self, address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        check(self, address)
        return real_connect_ex(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        check(None, host)
        return real_getaddrinfo(host, *args, **kwargs)

    def gethostbyname(host: str) -> str:
        check(None, host)
        return real_gethostbyname(host)

    def gethostbyname_ex(host: str) -> Any:
        check(None, host)
        return real_gethostbyname_ex(host)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", gethostbyname)
    monkeypatch.setattr(socket, "gethostbyname_ex", gethostbyname_ex)


@pytest.fixture(autouse=True)
def sealed(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Seal]:
    """Every test runs sealed:

    - ``HOME``, ``CALSYNC_CONFIG`` and ``CALSYNC_STATE_DIR`` point into a temporary directory,
      so neither a command run without ``--config`` / ``--state-dir`` nor a ``~`` in a test
      config reaches the developer's own files (``trigger`` without a daemon runs a cycle);
    - the keychain fails closed: ``credentials._keyring`` refuses every call, and
      ``PYTHON_KEYRING_BACKEND`` gives a child process keyring's ``fail`` backend;
    - ``launchctl`` is never run: ``service._run`` refuses (a test passes a fake launchctl);
    - no connection leaves the machine: a connect or name lookup for anything but loopback or
      a unix socket is refused, and no proxy is used (the developer's proxy listens on
      loopback and would carry the request out). ``@pytest.mark.network`` lifts the network
      guard (nothing needs it).
    """
    seal = Seal()
    home = tmp_path_factory.mktemp("no-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CALSYNC_CONFIG", str(home / "config.toml"))
    monkeypatch.setenv("CALSYNC_STATE_DIR", str(home / "state"))

    from calsync import credentials

    monkeypatch.setattr(credentials, "_keyring", lambda: SealedKeyring(seal))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")

    from calsync import service

    def no_launchctl(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise seal.refuse(LAUNCHCTL_REFUSED)

    # The developer's machine runs real launchd jobs: `calsync service` only ever gets a fake.
    monkeypatch.setattr(service, "_run", no_launchctl)

    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    # With NO_PROXY set, urllib's getproxies() never falls back to the macOS system proxy
    # settings, in this process or in a child; httpx then sees "*" and uses no proxy.
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(httpx_utils, "getproxies", lambda: {})

    if request.node.get_closest_marker("network") is None:
        _seal_network(monkeypatch, seal)
    yield seal
    if seal.refused:
        pytest.fail("the sealed test environment refused: " + "; ".join(seal.refused), pytrace=False)


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
        env={**os.environ, "HOME": str(storage)},  # no ~/.config/radicale of the developer's
    )
    url = f"http://127.0.0.1:{port}/"
    deadline = time.monotonic() + 15
    while True:
        try:
            httpx.get(url, timeout=0.5, trust_env=False)  # never through the developer's proxy
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
