"""``CALSYNC_DEBUG=1``: the opt-in path for diagnosing a failure that is otherwise logged by
type only. The daemon then also logs the failure's traceback, at DEBUG, and lets calsync's own
loggers through at DEBUG (httpx and httpcore stay at WARNING: they log every request URL).

A traceback and its message can carry an event title or a request URL, so this is off unless
asked for.
"""

import logging
import os

ENV = "CALSYNC_DEBUG"


def enabled() -> bool:
    return os.environ.get(ENV) == "1"


def details(log: logging.Logger, what: str, exc: BaseException) -> None:
    """``exc``'s traceback at DEBUG, as ``"<what>: details"``, when ``CALSYNC_DEBUG=1``."""
    if enabled():
        log.debug("%s: details", what, exc_info=exc)
