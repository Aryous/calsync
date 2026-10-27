"""The sealed test environment (``tests/conftest.py``) holds: no test reaches the developer's
config or state, the real keychain, or the network."""

import contextlib
import os
import pwd
import re
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from calsync import credentials
from calsync.config import CredentialRef, default_config_path, default_state_dir, load_config
from calsync.errors import ConfigError
from calsync.http import RequestLog, make_client
from tests.conftest import KEYRING_REFUSED, Seal

REAL_GETADDRINFO = socket.getaddrinfo  # taken at import, before any test patches it
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


def test_home_config_and_state_are_temporary() -> None:
    home = Path.home()
    assert home == Path(os.environ["HOME"])
    assert not home.is_relative_to(REAL_HOME)
    assert default_config_path() == home / "config.toml"
    assert default_state_dir() == home / "state"
    assert Path("~/.config/calsync/config.toml").expanduser().is_relative_to(home)


def test_the_default_config_without_calsync_config_is_under_the_temporary_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CALSYNC_CONFIG")
    monkeypatch.delenv("CALSYNC_STATE_DIR")
    path = default_config_path()
    assert path == Path.home() / ".config" / "calsync" / "config.toml"
    assert not path.is_relative_to(REAL_HOME)
    assert not default_state_dir().is_relative_to(REAL_HOME)
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(default_config_path())


def test_the_real_keyring_is_refused(sealed: Seal) -> None:
    with pytest.raises(AssertionError, match=KEYRING_REFUSED):
        credentials._keyring().get_password("calsync-test", credentials.KEYRING_USER)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(AssertionError, match=KEYRING_REFUSED):
        credentials.read_secret(CredentialRef("keychain", "calsync-test"))
    with pytest.raises(AssertionError, match=KEYRING_REFUSED):
        credentials.write_secret(CredentialRef("keychain", "calsync-test"), "x")
    assert len(sealed.refused) == 3
    sealed.refused.clear()


def test_a_child_process_gets_the_fail_keyring() -> None:
    probe = "import keyring; print(type(keyring.get_keyring()).__module__)"
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "keyring.backends.fail"


def test_httpx_cannot_reach_the_network(sealed: Seal) -> None:
    with pytest.raises(AssertionError, match=re.escape("a test reached the network: example.com")):
        httpx.Client().get("https://example.com")
    sealed.refused.clear()


@pytest.mark.anyio
async def test_calsyncs_client_cannot_reach_the_network(sealed: Seal) -> None:
    async with make_client(RequestLog()) as client:  # trust_env: the proxy must not carry it out
        with pytest.raises(AssertionError, match=re.escape("a test reached the network: example.com")):
            await client.get("https://example.com/")
    sealed.refused.clear()


def test_a_raw_socket_cannot_reach_the_network(sealed: Seal) -> None:
    with pytest.raises(AssertionError, match=re.escape("a test reached the network: 192.0.2.1")):
        socket.create_connection(("192.0.2.1", 443), timeout=1)
    with socket.socket() as sock, pytest.raises(AssertionError, match=re.escape("192.0.2.1")):
        sock.connect_ex(("192.0.2.1", 443))
    sealed.refused.clear()


def test_a_refusal_the_code_swallows_is_still_recorded(sealed: Seal) -> None:
    with contextlib.suppress(Exception):  # what per-rule isolation or ContainErrors would do
        httpx.get("http://example.com/")
    assert sealed.refused == ["a test reached the network: example.com"]
    sealed.refused.clear()


def test_loopback_is_allowed() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        with socket.create_connection(server.getsockname(), timeout=1):
            pass
        with socket.create_connection(("localhost", server.getsockname()[1]), timeout=1):
            pass


def test_unix_sockets_are_allowed(short_dir: Path) -> None:
    path = str(short_dir / "s.sock")
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(path)
        server.listen(1)
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(path)


def test_no_proxy_is_used() -> None:
    assert httpx.Client()._mounts == {}  # pyright: ignore[reportPrivateUsage]
    assert os.environ["NO_PROXY"] == "*"


@pytest.mark.network
def test_the_network_marker_lifts_the_network_guard() -> None:
    assert socket.getaddrinfo is REAL_GETADDRINFO  # nothing is resolved or connected here
