"""Error types shared by every module.

Adapters translate service-specific failures into these so the engine can
decide what to retry, what to alert on, and what to surface to the user.
"""


class CalsyncError(Exception):
    """Base class for all calsync errors."""


class ConfigError(CalsyncError):
    """The configuration is invalid. Carries every problem found, not just the first."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class ListenError(ConfigError):
    """The web UI's TCP address cannot be listened on (in use, not allowed, not this
    computer's): a configuration problem, exit code 2, so the reason reaches launchd's log."""

    def __init__(self, message: str) -> None:
        super().__init__([message])


class AuthError(CalsyncError):
    """Credentials are missing, revoked or rejected. Never retried automatically."""


class TransientError(CalsyncError):
    """Network failure, throttling or a 5xx response that survived retries."""


class ConflictError(CalsyncError):
    """A conditional write failed (HTTP 412): the resource changed or already exists."""


class NotFoundError(CalsyncError):
    """The requested calendar or resource does not exist."""


class CalendarNotListed(NotFoundError):
    """The calendar listing was read and no calendar in it matches the ref. Unlike any other
    ``NotFoundError`` (a 404 on the principal or the calendar home during discovery), this is
    evidence that the calendar does not exist."""


class CursorExpired(CalsyncError):
    """An incremental sync cursor is no longer valid; a full resync is required."""


class ForbiddenError(CalsyncError):
    """The server understood the credentials but refused the request (HTTP 403 that is
    neither throttling nor an expired sync token): a permission or data problem, so
    re-entering credentials would not help."""


class RulePaused(ValueError):
    """A cycle was requested for a paused rule (the API answers 409 ``paused``)."""

    def __init__(self, name: str) -> None:
        super().__init__(f'rule "{name}" is paused')
        self.rule = name


def public_name(exc: BaseException) -> str:
    """The error's class name, or its nearest public base's for a private class."""
    return next((c.__name__ for c in type(exc).__mro__ if not c.__name__.startswith("_")), "Exception")


def plain_error(exc: BaseException, subject: str, *, account: str | None = None, setup: bool = False) -> str:
    """``"<ExceptionType>: <plain sentence>"`` for text calsync stores and shows (run history,
    ``status``, the API): built from the error's class and config names only, never from the
    exception's text, which can carry request URLs, hrefs and calendar IDs (that text belongs in
    ``debug.details``). ``subject`` is a phrase of config names, e.g. ``the source of rule "family"``;
    ``account`` names the account whose adapter raised; ``setup``: building that adapter failed."""
    if setup and account is not None:
        if isinstance(exc, AuthError):
            sentence = f'account "{account}" has no usable credential'
        elif isinstance(exc, ConfigError):
            sentence = f'account "{account}" is not configured correctly'
        else:
            sentence = f'account "{account}" could not be set up'
    elif isinstance(exc, AuthError):
        sentence = (
            f'account "{account}" was rejected' if account else f"{subject}: the credentials were rejected"
        )
    elif isinstance(exc, ConfigError):
        sentence = f"{subject} is not configured correctly"
    elif isinstance(exc, TransientError):
        sentence = f"{subject} could not be reached"
    elif isinstance(exc, ConflictError):
        sentence = f"{subject} changed during a write"
    elif isinstance(exc, NotFoundError):
        sentence = f"{subject} was not found"
    elif isinstance(exc, ForbiddenError):
        sentence = f"{subject} refused access"
    elif isinstance(exc, CursorExpired):
        sentence = f"{subject} needs a full resync"
    elif isinstance(exc, TimeoutError):
        sentence = f"{subject} did not answer in time"
    else:
        sentence = f"{subject} failed"
    return f"{public_name(exc)}: {sentence}"
