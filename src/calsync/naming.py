"""Deterministic identities for managed resources.

The resource name alone says "calsync owns this, for rule X", so ownership
can be decided from a listing without downloading any event body.
"""

import hashlib
import re
from urllib.parse import unquote, urlsplit

_NAME = re.compile(r"^calsync-([0-9a-f]{8})-([0-9a-f]{24})\.ics$")


def _digest(text: str, length: int) -> str:
    return hashlib.blake2b(text.encode(), digest_size=16).hexdigest()[:length]


def rule_tag(rule_name: str) -> str:
    return _digest(f"rule\0{rule_name}", 8)


def object_id(rule_name: str, source_calendar_id: str, source_id: str) -> str:
    return _digest(f"{rule_name}\0{source_calendar_id}\0{source_id}", 24)


def resource_name(rule_name: str, oid: str) -> str:
    return f"calsync-{rule_tag(rule_name)}-{oid}.ics"


def uid_for(oid: str) -> str:
    return f"{oid}@calsync"


def parse_resource_name(href: str) -> tuple[str, str] | None:
    """Return ``(rule_tag, object_id)`` if the href's last segment is a calsync name."""
    match = _NAME.match(unquote(href.rstrip("/").rsplit("/", 1)[-1]))
    return (match.group(1), match.group(2)) if match else None


def same_href(href: str, ref: str) -> bool:
    """``href`` (absolute, as a CalDAV server lists it) is what ``ref`` names: an absolute URL,
    or just its path; a trailing slash does not count. ``CalDavTarget.ensure_calendar`` and the
    calendar cache (``ops``) match calendars with it."""
    if "://" not in ref:
        href = urlsplit(href).path
    return href.rstrip("/") == ref.rstrip("/")
