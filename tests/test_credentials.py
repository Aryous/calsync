import os
from pathlib import Path

import pytest

from calsync import credentials
from calsync.config import CredentialRef
from calsync.credentials import read_secret, write_secret
from calsync.errors import AuthError, ConfigError


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, user: str) -> str | None:
        return self.store.get((service, user))

    def set_password(self, service: str, user: str, value: str) -> None:
        self.store[(service, user)] = value


def test_env_backend() -> None:
    ref = CredentialRef("env", "PW")
    assert read_secret(ref, environ={"PW": "s3cret"}) == "s3cret"
    with pytest.raises(AuthError, match="PW is not set"):
        read_secret(ref, environ={})
    with pytest.raises(ConfigError):
        write_secret(ref, "x")


def test_file_backend_roundtrip_and_mode(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "secret"
    ref = CredentialRef("file", str(path))
    write_secret(ref, "abc-def\n")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert read_secret(ref) == "abc-def"
    os.chmod(path, 0o644)
    with pytest.raises(ConfigError, match="must have mode 0600"):
        read_secret(ref)


def test_file_backend_missing(tmp_path: Path) -> None:
    with pytest.raises(AuthError, match="not found"):
        read_secret(CredentialRef("file", str(tmp_path / "nope")))


def test_keychain_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeKeyring()
    monkeypatch.setattr(credentials, "_keyring", lambda: fake)
    ref = CredentialRef("keychain", "calsync/test")
    with pytest.raises(AuthError, match="calsync auth"):
        read_secret(ref)
    write_secret(ref, "pw")
    assert fake.store == {("calsync/test", "calsync"): "pw"}
    assert read_secret(ref) == "pw"
