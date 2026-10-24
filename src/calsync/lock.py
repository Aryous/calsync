"""Single-instance lock: an exclusive, non-blocking flock that also records the PID."""

import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import IO

from calsync.errors import CalsyncError


class AlreadyRunning(CalsyncError):
    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        super().__init__(f"another calsync process holds the lock (pid {pid or 'unknown'})")


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[str] | None = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fh = self.path.open("a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            text = fh.read().strip()
            fh.close()
            raise AlreadyRunning(int(text) if text.isdigit() else None) from None
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
