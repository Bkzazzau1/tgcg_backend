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
VIN_LENGTH = 19
# Two readers see the VIN: the phone when the member joined, and this server
# now. Between them they may misread, miss or add a character or two; another
# card's VIN differs in many more, and the name must be on the card as well.
VIN_EDITS = 2
# Beyond that but this close, the card's own VIN was probably misread when
# the member joined: a reviewer compares the two.
VIN_CLOSE = 6


def _compact(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper()).translate(_LOOKALIKES)


def vin_edits(text: str, vin: str) -> int | None:
    """How many characters stand between the VIN given and its closest
    likeness in the text: each misread, missed or added character counts one.
    None when what was given is not a VIN."""
    wanted = _compact(vin)
    if len(wanted) != VIN_LENGTH:
        return None
    compact = _compact(text)
    # The cheapest way to match the VIN's first i characters ending at each
    # place in the text; it may start anywhere.
    previous = [0] * (len(compact) + 1)
    for i, char in enumerate(wanted, start=1):
        current = [i] + [0] * len(compact)
        for j, seen in enumerate(compact, start=1):
            current[j] = min(
                previous[j - 1] + (char != seen),  # read right, or misread
                previous[j] + 1,  # missed by the reader
                current[j - 1] + 1,  # added by the reader
            )
        previous = current
    return min(previous)


def vin_on_card(text: str, vin: str) -> bool:
    """The VIN, give or take two characters, in the text."""
    edits = vin_edits(text, vin)
    return edits is not None and edits <= VIN_EDITS


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


MATCHED = "The name and VIN given match the text read from the card photo."


def compare(text: str, full_name: str, vin: str) -> tuple[bool, str]:
    """(matched, detail): whether both the name and the VIN appear on the card.
    The detail says what was not found, or how far the card's VIN is from the
    one given, never the details themselves."""
    edits = vin_edits(text, vin)
    vin_found = edits is not None and edits <= VIN_EDITS
    name_found = name_on_card(text, full_name)
    if vin_found and name_found:
        return True, MATCHED
    close = not vin_found and edits is not None and edits <= VIN_CLOSE
    missing = [
        what for what, found in (("name", name_found), ("VIN", vin_found or close)) if not found
    ]
    found = (
        f"The {' and '.join(missing)} given could not be found on the card photo."
        if missing
        else ""
    )
    if close:
        differs = (
            f"The VIN on the card photo differs from the one given in {edits} characters: "
            "compare them, as one may have been misread."
        )
        found = f"{found} {differs}".strip()
    return False, found
