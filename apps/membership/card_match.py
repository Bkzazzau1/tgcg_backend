"""Whether a member's name and VIN appear in the text read from their card photo.

This compares the details a member gave with what the server itself reads off
the photo of their PVC (apps.membership.pvc_reading). It shows the details
were taken from that card; it does not show the card is genuine, which needs
an authoritative INEC source. Neither the text nor the details are logged or
stored.
"""

import difflib
import re

# Characters a reader confuses on the card's patterned background, mapped the
# same way on both sides of the comparison.
_LOOKALIKES = str.maketrans("OQDILZSGTB", "0001125678")
# A digit inside a word on the card is a misread letter of a name.
_LETTERS = str.maketrans("0125678", "OIZSGTB")
# Titles some members put before their name, which no card carries.
HONORIFICS = frozenset(
    "ALH ALHAJI ALHAJA HAJIYA HAJIA HAJ HON DR ENGR COMR MAL MALAM MALLAM AMB BARR MR MRS "
    "MISS PROF CHIEF PRINCE SIR".split()
)
# How alike a name on the card and a name given must be: a reader may get a
# letter or two of a long name wrong.
NAME_LIKENESS = 0.8


def _compact(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper()).translate(_LOOKALIKES)


def vin_on_card(text: str, vin: str) -> bool:
    """The VIN, give or take one misread character, anywhere in the text."""
    wanted = _compact(vin)
    if len(wanted) != 19:
        return False
    compact = _compact(text)
    for start in range(len(compact) - len(wanted) + 1):
        window = compact[start : start + len(wanted)]
        if sum(a != b for a, b in zip(window, wanted, strict=True)) <= 1:
            return True
    return False


def name_words(name: str) -> list[str]:
    return [w for w in re.findall(r"[A-Z]{2,}", (name or "").upper()) if w not in HONORIFICS]


def _among(word: str, seen: set[str]) -> bool:
    return word in seen or any(
        difflib.SequenceMatcher(None, word, other).ratio() >= NAME_LIKENESS for other in seen
    )


def name_on_card(text: str, full_name: str) -> bool:
    """Every word of the name (at least two) among the words on the card, in
    any order: cards print the surname and the other names on separate lines.
    A name printed as two words may be given as one, and the other way round."""
    wanted = name_words(full_name)
    if len(wanted) < 2:
        return False
    words = [
        token.translate(_LETTERS)
        for token in re.findall(r"[A-Z0-9]{2,}", (text or "").upper())
        if re.search(r"[A-Z]", token)
    ]
    seen = {*words, *(first + second for first, second in zip(words, words[1:], strict=False))}
    found = [_among(word, seen) for word in wanted]
    for i in range(len(wanted) - 1):
        if not (found[i] and found[i + 1]) and _among(wanted[i] + wanted[i + 1], seen):
            found[i] = found[i + 1] = True
    return all(found)


def compare(text: str, full_name: str, vin: str) -> tuple[bool, str]:
    """(matched, detail): whether both the name and the VIN appear on the card.
    The detail names what was not found, never the details themselves."""
    vin_found, name_found = vin_on_card(text, vin), name_on_card(text, full_name)
    if vin_found and name_found:
        return True, "The name and VIN given match the text read from the card photo."
    missing = [what for what, found in (("name", name_found), ("VIN", vin_found)) if not found]
    return False, f"The {' and '.join(missing)} given could not be found on the card photo."
