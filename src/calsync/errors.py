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


class AuthError(CalsyncError):
    """Credentials are missing, revoked or rejected. Never retried automatically."""


class TransientError(CalsyncError):
    """Network failure, throttling or a 5xx response that survived retries."""


class ConflictError(CalsyncError):
    """A conditional write failed (HTTP 412): the resource changed or already exists."""


class NotFoundError(CalsyncError):
    """The requested calendar or resource does not exist."""


class CursorExpired(CalsyncError):
    """An incremental sync cursor is no longer valid; a full resync is required."""


class ForbiddenError(CalsyncError):
    """The server understood the credentials but refused the request (HTTP 403 that is
    neither throttling nor an expired sync token): a permission or data problem, so
    re-entering credentials would not help."""
