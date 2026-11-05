"""A group's list that is a spreadsheet's table printed to PDF (5 November
2026: the Kaduna State persons with disabilities farmers, for the Persons
with Disabilities TGCG Support Group). A header row names the columns, and
each cell is its own piece of text at its column's place on the page, so
cells are told apart by where they stand: a cell cut short runs straight
into the next one in the page's text. The columns are found by their names
in the header on the first page and kept for the pages after it; columns
not read here (codes, qualifications) only mark where a column ends.

Rows become group_list.Row and are planned as any list: only a phone or NIN
the list gives under one name counts.
"""

import re
import unicodedata

from apps.accounts.phone import normalize_ng_phone
from apps.core.choices import Disability, Gender
from apps.membership.management.commands.import_executives import person_name

from .group_list import PHONE, Row

# Header names, as compared: capitals and digits only.
HEADERS = {
    "serial": ("SNO", "SN", "SNO2", "SERIALNO"),
    "name": ("FULLNAMES", "FULLNAME", "NAMES", "NAME"),
    "disability": ("DISABILITY", "TYPEOFDISABILITY", "DISABILITYTYPE"),
    "gender": ("GENDER", "SEX"),
    "occupation": ("OCCUPATION",),
    "state": ("STATE",),
    "lga": ("LOCALGOVERNMENT", "LGA", "LOCALGOVERNMENTAREA"),
    "ward": ("WARD",),
    "phone": ("PHONENUMBER", "PHONE", "PHONENO", "GSM"),
    "nin": ("NIN",),
    "vin": ("VIN",),
}
# How lists name disabilities; "spinal cord injury" is often cut short.
DISABILITIES = {
    "PHYSICAL": (Disability.PHYSICAL, ""),
    "SPINALCORDINJURY": (Disability.PHYSICAL, ""),
    "CRIPPLE": (Disability.PHYSICAL, ""),
    "DEAF": (Disability.HEARING, ""),
    "HEARING": (Disability.HEARING, ""),
    "BLIND": (Disability.VISUAL, ""),
    "VISUAL": (Disability.VISUAL, ""),
    "DUMB": (Disability.SPEECH, ""),
    "SPEECH": (Disability.SPEECH, ""),
    "ALBINO": (Disability.ALBINISM, ""),
    "ALBINISM": (Disability.ALBINISM, ""),
    "LEPER": (Disability.OTHER, "Leprosy"),
    "LEPROSY": (Disability.OTHER, "Leprosy"),
}
NOTHING = {"", "NO", "NIL", "NILL", "NONE", "NA", "N/A", "-"}
LINE = 1.0  # points: pieces of text this close in height are on one line
NEAR = 4.0  # a wrapped cell's other line, this close to its row


def _compact(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _disability(text: str):
    key = _compact(text)
    for name, found in DISABILITIES.items():
        if key and (key == name or (len(key) >= 6 and name.startswith(key))):
            return found
    return None


def _gender(text: str) -> str:
    key = _compact(text)
    if key in ("MALE", "M"):
        return Gender.MALE
    if key in ("FEMALE", "FEMAL", "F"):
        return Gender.FEMALE
    return ""


def _phone(text: str) -> str:
    compact = re.sub(r"[\s.\-()]", "", text)
    if re.fullmatch(r"[789][01]\d{8}", compact):
        compact = "0" + compact  # a spreadsheet drops the leading 0
    return (normalize_ng_phone(compact) or "") if PHONE.fullmatch(compact) else ""


def _blank(text: str) -> str:
    text = " ".join(text.split())
    return "" if text.upper() in NOTHING else text


def _code(text: str) -> str:
    """A VIN or NIN column's value, without spaces; empty for "No" and the like."""
    code = re.sub(r"\s", "", text).upper()
    return "" if code in NOTHING else code


def page_pieces(page) -> list[tuple[float, float, str]]:
    """(height, left, text) of each piece of text on a pypdf page."""
    pieces = []

    def visit(text, cm, tm, font, size):
        if text.strip():
            x = cm[0] * tm[4] + cm[2] * tm[5] + cm[4]
            y = cm[1] * tm[4] + cm[3] * tm[5] + cm[5]
            pieces.append((y, x, unicodedata.normalize("NFKC", text)))

    page.extract_text(visitor_text=visit)
    return pieces


def _lines(pieces):
    """Pieces gathered into lines, top first: [(height, [(left, text)])]."""
    lines = []
    for y, x, text in sorted(pieces, key=lambda piece: (-piece[0], piece[1])):
        if lines and abs(lines[-1][0] - y) <= LINE:
            lines[-1][1].append((x, text))
        else:
            lines.append((y, [(x, text)]))
    return lines


def _header(line):
    """[(left edge, column)] when this line is the header; a column not
    read here is ""."""
    columns, seen = [], set()
    for x, text in sorted(line):
        key = _compact(text)
        column = next((c for c, names in HEADERS.items() if key in names and c not in seen), "")
        seen.add(column)
        columns.append((x, column))
    if "name" not in seen or len(seen - {""}) < 3:
        return None
    return columns


def _cells(line, columns) -> dict:
    cells = {}
    for x, text in sorted(line):
        column = columns[0][1]
        for left, name in columns:
            if x + 0.5 >= left:
                column = name
        if column:
            cells[column] = (cells.get(column, "") + text).strip()
    return cells


def read_pages(pages, *, state: str = "") -> list[Row]:
    """Rows from each page's pieces of text (page_pieces), pages counted
    from 1. `state` is everyone's when the list has no STATE column."""
    columns, rows = None, []
    for number, pieces in enumerate(pages, start=1):
        lines = _lines(pieces)
        if columns is None:
            for index, (_, line) in enumerate(lines):
                if found := _header(line):
                    columns, lines = found, lines[index + 1 :]
                    break
            else:
                continue
        found = [(y, _cells(line, columns)) for y, line in lines]
        numbered = [(y, cells) for y, cells in found if cells.get("serial", "").isdigit()]
        # A wrapped cell's other line goes with the row it is nearest to.
        for y, cells in found:
            if cells.get("serial", "").isdigit() or not numbered:
                continue
            near_y, near = min(numbered, key=lambda item: abs(item[0] - y))
            if abs(near_y - y) <= NEAR:
                for column, text in cells.items():
                    parts = (
                        (near.get(column, ""), text) if y < near_y else (text, near.get(column, ""))
                    )
                    near[column] = " ".join(parts).strip()
        rows.extend(_row(number, cells, state) for _, cells in numbered)
    return rows


def _row(page: int, cells: dict, state: str) -> Row:
    if (
        re.search(r"\d", cells.get("name", ""))
        and not _disability(cells.get("disability", ""))
        and _disability(cells.get("gender", ""))
    ):
        # The cells moved one column right as far as GENDER: no gender given.
        cells = {
            **cells,
            "name": cells.get("disability", ""),
            "disability": cells["gender"],
            "gender": "",
        }
    name = cells.get("name", "")
    disability = _disability(cells.get("disability", ""))
    return Row(
        page=page,
        serial=int(cells["serial"]),
        name=person_name(name) if re.search(r"[A-Za-z]", name) else "",
        places=[
            _blank(cells.get("state", "")) or state,
            _blank(cells.get("lga", "")),
            _blank(cells.get("ward", "")),
        ],
        code=_code(cells.get("vin", "")),
        phone=_phone(cells.get("phone", "")),
        phone_column=_code(cells.get("phone", "")),
        nin_column=_code(cells.get("nin", "")),
        gender=_gender(cells.get("gender", "")),
        disabilities=[disability[0]] if disability else [],
        disability_other=disability[1] if disability else "",
        occupation=_blank(cells.get("occupation", "")).title()[:100],
    )
