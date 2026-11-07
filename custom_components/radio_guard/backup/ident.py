"""Identity normalisation shared by all sources.

Zigbee2MQTT reports ``0x00124b0029aa55ff``, OCBF stores ``00124b0029aa55ff``,
zigpy prints ``00:12:4b:00:29:aa:55:ff``. Compare them in one canonical form.
"""

from __future__ import annotations

import re
from typing import Any


def norm_hex(value: Any) -> str | None:
    """Lower-case hex digits only, no 0x, no separators. None stays None."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    s = re.sub(r"[^0-9a-f]", "", s)
    return s or None


def norm_pan_id(value: Any) -> str | None:
    """PAN ID as 0x-prefixed 4-digit hex, accepting ints, hex strings, 0x strings."""
    if value is None:
        return None
    if isinstance(value, int):
        return f"0x{value:04x}"
    s = str(value).strip().lower()
    try:
        if s.startswith("0x"):
            return f"0x{int(s, 16):04x}"
        if s.isdigit() and len(s) > 4:
            return f"0x{int(s):04x}"
        return f"0x{int(s, 16):04x}"
    except ValueError:
        return None
