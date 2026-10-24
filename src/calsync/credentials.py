"""Credential backends: macOS keychain (via keyring), environment variables, 0600 files."""

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

from calsync.config import CredentialRef
from calsync.errors import AuthError, ConfigError

KEYRING_USER = "calsync"


def _keyring() -> ModuleType:
    import keyring  # imported lazily: only the keychain backend needs it

    return keyring


def _keyring_error() -> type[Exception]:
    from keyring.errors import KeyringError  # lazy, like _keyring()

    return KeyringError


def read_secret(ref: CredentialRef, *, environ: Mapping[str, str] = os.environ) -> str:
    if ref.backend == "env":
        value = environ.get(ref.ref)
        if not value:
            raise AuthError(f"environment variable {ref.ref} is not set")
        return value
    if ref.backend == "file":
        path = Path(ref.ref).expanduser()
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            raise AuthError(f"credential file not found: {path}") from None
        if mode & 0o077:
            raise ConfigError([f"credential file {path} must have mode 0600 (found {mode:o})"])
        return path.read_text().strip()
    try:
        value = _keyring().get_password(ref.ref, KEYRING_USER)
    except _keyring_error() as exc:
        raise AuthError(f"keychain unavailable for {ref.ref}: {exc}") from exc
    if not value:
        raise AuthError(f"no keychain entry {ref.ref!r}; run `calsync auth <account>`")
    return value


def write_secret(ref: CredentialRef, value: str) -> None:
    if ref.backend == "env":
        raise ConfigError([f"cannot store a secret in environment variable {ref.ref}"])
    if ref.backend == "file":
        path = Path(ref.ref).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)  # tighten a pre-existing file before any secret is written
        with os.fdopen(fd, "w") as fh:
            fh.write(value)
        return
    try:
        _keyring().set_password(ref.ref, KEYRING_USER, value)
    except _keyring_error() as exc:
        raise ConfigError([f"could not store keychain entry {ref.ref!r}: {exc}"]) from exc
