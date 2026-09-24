"""Shared validation for setup and configuration flows."""

import re
from typing import Any

from pyNukiBT import NukiConst


def parse_security_pin(value: Any, device_type=None) -> int | None:
    """Allow leading zeroes and validate the device's PIN wire range."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError("invalid_pin")
    pin = int(value)
    # Ultra has a 32-bit PIN. Before discovering the device type, do not reject
    # a valid Ultra PIN using the legacy 16-bit limit.
    maximum = 0xFFFFFFFF if device_type in (None, NukiConst.NukiDeviceType.SMARTLOCK_ULTRA) else 0xFFFF
    if pin > maximum:
        raise ValueError("invalid_pin")
    return pin
