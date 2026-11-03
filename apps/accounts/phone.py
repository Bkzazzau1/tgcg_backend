"""Phone number normalisation to E.164: Nigerian mobiles (+234XXXXXXXXXX),
and since 3 November 2026 numbers abroad, for members in the diaspora."""

import re

_DIGITS = re.compile(r"\D+")

# Calling codes of countries where numbers are dialled at home with a 0 that
# is never part of the number from abroad: +44 07700 900123 is +44 7700
# 900123. Not every country: in Italy or Côte d'Ivoire, for one, the 0 stays.
_TRUNK_ZERO = frozenset(
    {
        *("20", "27", "31", "32", "33", "41", "43", "44", "46", "49", "60", "61", "62"),
        *("63", "64", "66", "81", "82", "84", "86", "90", "91", "92", "212", "233", "234"),
        *("250", "251", "254", "255", "256", "260", "263", "353", "358", "966", "971", "972"),
    }
)


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


def normalize_phone(value: str | None) -> str | None:
    """A Nigerian mobile as normalize_ng_phone gives it; or a number abroad
    written with its country code (+44 7700 900123, 0044 7700 900123) as +
    and its digits, else None. Written with + or 00, a number is read by its
    country code, so +82 2 123 4567 (Seoul) is never taken for a Nigerian
    mobile, and +234 is only ever a Nigerian mobile. The 0 dialled at home
    in many countries is dropped after their code, written (0) or not."""
    if not value:
        return None
    text = value.strip().replace("(0)", "")
    if text.startswith("+"):
        digits = _DIGITS.sub("", text)
    elif text.startswith("00"):
        digits = _DIGITS.sub("", text)[2:]
    else:
        return normalize_ng_phone(text)
    for size in (2, 3):
        if digits[:size] in _TRUNK_ZERO and digits[size : size + 1] == "0":
            digits = digits[:size] + digits[size + 1 :]
            break
    if digits.startswith("234"):
        return normalize_ng_phone(digits)
    if not 7 <= len(digits) <= 15 or digits[0] == "0":
        return None
    return f"+{digits}"


def is_nigerian(phone: str | None) -> bool:
    """Whether a normalised number is a Nigerian mobile (SMS and USSD reach only these)."""
    return bool(phone) and phone.startswith("+234")


def local_ng_phone(value: str | None) -> str | None:
    """Return the 11 digits people write (08031234567) for a valid Nigerian
    mobile number, else None."""
    phone = normalize_ng_phone(value)
    return f"0{phone[4:]}" if phone else None


def local_phone(value: str | None) -> str | None:
    """A number as people write it: 08031234567 for a Nigerian mobile, + and
    the digits for a number abroad; None when it is neither."""
    return local_ng_phone(value) or normalize_phone(value)
