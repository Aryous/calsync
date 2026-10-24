"""Parse short duration strings such as ``30s``, ``5m``, ``7d`` or ``-15m``."""

import re
from datetime import timedelta

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_PATTERN = re.compile(r"^([+-]?)(\d+)([smhdw])$")


def parse_duration(text: str) -> timedelta:
    match = _PATTERN.match(text.strip())
    if match is None:
        raise ValueError(f"invalid duration {text!r}; expected e.g. 30s, 5m, 2h, 7d, -15m")
    sign, amount, unit = match.groups()
    seconds = int(amount) * _UNITS[unit]
    return timedelta(seconds=-seconds if sign == "-" else seconds)
