"""Nigerian phone number normalisation to E.164 (+234XXXXXXXXXX)."""

import re

_DIGITS = re.compile(r"\D+")


def normalize_ng_phone(value: str | None) -> str | None:
    """Return +234XXXXXXXXXX for a valid Nigerian mobile number, else None.

    Accepts 08031234567, 8031234567, 2348031234567, +234 803 123 4567.
    """
    if not value:
        return None
    digits = _DIGITS.sub("", value)
    if digits.startswith("234"):
        digits = digits[3:]
    elif digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] not in "789":
        return None
    return f"+234{digits}"
