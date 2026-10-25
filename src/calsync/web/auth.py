"""The pairing key and the session cookie derived from it. Standard library only, so
``calsync web pair`` starts fast.

The key is ``<state_dir>/web.key`` (mode 0600): whoever has it, in a pair link
``http://<host>:<port>/?k=<key>`` or typed into the pair page, gets the cookie, whose value is
an HMAC of a fixed message under the key. Replacing the key (``reset_key``) invalidates every
cookie at once; the server notices by the file's identity, without a restart.
"""

import contextlib
import hashlib
import hmac
import ipaddress
import os
import secrets
import stat
from collections.abc import Iterable
from pathlib import Path

from calsync.errors import CalsyncError

KEY_FILE = "web.key"
COOKIE = "calsync_session"
COOKIE_MAX_AGE = 34_560_000  # 400 days, the longest a browser keeps a cookie
WILDCARDS = frozenset({"0.0.0.0", "::"})


def key_path(state_dir: Path) -> Path:
    return state_dir / KEY_FILE


def session_token(key: str) -> str:
    """The cookie value for ``key``."""
    return hmac.new(key.encode(), b"calsync-session-v1", hashlib.sha256).hexdigest()


class InsecureKeyFile(CalsyncError):
    """``web.key`` is open to other users (any group or other permission bit): not used."""

    def __init__(self, path: Path, mode: int) -> None:
        super().__init__(
            f"other users can access {path} (mode {mode:04o}); run `chmod 600 {path}`, "
            "or replace the key with `calsync web reset-key`"
        )


def read_key(state_dir: Path) -> str | None:
    """The key, or None if there is none (no file, or an empty one). ``InsecureKeyFile`` if
    other users could read or replace it."""
    path = key_path(state_dir)
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, encoding="utf-8") as fh:  # the mode checked is the file's that is read
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode & 0o077:
            raise InsecureKeyFile(path, mode)
        text = fh.read().strip()
    return text or None


def _write_new(state_dir: Path) -> str:
    """A fresh key, written to a new private file that then replaces ``web.key`` at once."""
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = secrets.token_urlsafe(32)
    tmp = state_dir / f".{KEY_FILE}.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(key + "\n")
        os.replace(tmp, key_path(state_dir))
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    return key


def load_or_create_key(state_dir: Path) -> str:
    """The key, created first if there is none."""
    return read_key(state_dir) or _write_new(state_dir)


def reset_key(state_dir: Path) -> str:
    """Replace the key: every browser paired so far has to pair again."""
    return _write_new(state_dir)


class Keys:
    """The key as the server sees it: read from disk for every pairing attempt, and the cookie
    token cached for as long as the file stays the same (inode, mtime and size)."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._cached: tuple[tuple[int, int, int], str] | None = None

    def matches(self, candidate: str) -> bool:
        """``candidate`` is the key (compared in constant time)."""
        key = read_key(self._state_dir)
        return key is not None and hmac.compare_digest(candidate.encode(), key.encode())

    def token(self) -> str | None:
        """The current cookie value, or None while there is no key."""
        try:
            st = os.stat(key_path(self._state_dir))
        except FileNotFoundError:
            self._cached = None
            return None
        stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
        if self._cached is not None and self._cached[0] == stamp:
            return self._cached[1]
        key = read_key(self._state_dir)
        if key is None:
            return None
        self._cached = (stamp, session_token(key))
        return self._cached[1]

    def valid_cookie(self, value: str | None) -> bool:
        token = self.token()
        return value is not None and token is not None and hmac.compare_digest(value.encode(), token.encode())


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def pair_urls(key: str, host: str, port: int, allowed_hosts: Iterable[str]) -> list[str]:
    """One pair link per address: localhost, each allowed host, and the listen host unless it
    is a wildcard (every address)."""
    names = ["localhost", *allowed_hosts]
    if host not in WILDCARDS:
        names.append(host)
    unique = dict.fromkeys(names)
    return [f"http://{_url_host(name)}:{port}/?k={key}" for name in unique]


def is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True
