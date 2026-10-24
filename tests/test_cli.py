import io

from calsync import __version__
from calsync.cli import main


def test_version() -> None:
    out = io.StringIO()
    assert main(["--version"], out=out) == 0
    assert out.getvalue() == f"calsync {__version__}\n"


def test_no_command_prints_help() -> None:
    out = io.StringIO()
    assert main([], out=out) == 2
    assert "usage: calsync" in out.getvalue()
