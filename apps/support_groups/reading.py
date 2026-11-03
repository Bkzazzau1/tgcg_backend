"""Reading a support group's list of members from the file its leader
uploads: Excel (.xlsx), CSV, Word (.docx), a PDF, or a photo of a list.

Each row with a Nigerian mobile number becomes a person: the name is the
words beside the number, and columns headed State, LGA, Ward, Polling unit or
Gender give those too. A scanned PDF or a photo is read with Tesseract, which
can misread names and numbers, so the leader checks every line before
anything is added. Nothing read is kept, and neither the file nor what it
says is ever logged.
"""

import csv
import io
import logging
import re
import shutil
import subprocess
import time
import zipfile
from xml.etree import ElementTree

from django.conf import settings
from django.core.exceptions import ValidationError
from PIL import Image, ImageOps, UnidentifiedImageError

from apps.accounts.phone import normalize_ng_phone
from apps.membership import pvc_reading

logger = logging.getLogger(__name__)

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 5000
MAX_PAGES = 300
# Scans are read one page at a time within one request.
MAX_SCANNED_PAGES = 8
SCAN_SECONDS = 60
# What a zipped Office file may unpack to.
MAX_UNZIPPED = 80 * 1024 * 1024
TESSERACT_SECONDS = 30

KINDS = {
    ".xlsx": "excel",
    ".csv": "csv",
    ".docx": "word",
    ".pdf": "pdf",
    ".jpg": "photo",
    ".jpeg": "photo",
    ".png": "photo",
}
OLD_KINDS = {".xls": "Excel", ".doc": "Word"}

_PHONE = re.compile(r"(?<![\d+])(?:\+?\s*234|0)?[\s-]?[789][01](?:[\s-]?\d){8}(?!\d)")
_UNIT_CODE = re.compile(
    r"(?<!\d)(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,3})(?!\d)"
)
_GENDER_WORDS = {"male": "male", "female": "female", "m": "male", "f": "female"}
_LEADING_NUMBER = re.compile(r"^\W*(?:s/?n\W*)?\d{1,5}\s*[.):\-]?\s*", re.IGNORECASE)
_NAME_CHARS = re.compile(r"[^A-Za-zÀ-ɏ .'\-]")

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


class Unreadable(ValidationError):
    def __init__(self, message):
        super().__init__({"file": message})


def kind_of(name: str) -> str:
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if suffix in OLD_KINDS:
        old = OLD_KINDS[suffix]
        raise Unreadable(
            f"Save this {old} file in the newer format ({suffix}x) and upload it again."
        )
    if suffix not in KINDS:
        raise Unreadable("Upload the list as Excel, CSV, Word, PDF or a photo.")
    return KINDS[suffix]


def read(upload) -> dict:
    """The people in an uploaded list, in the order written, and notes on
    what could not be read: {"kind", "people", "notes"}."""
    kind = kind_of(getattr(upload, "name", "") or "")
    if upload.size > MAX_BYTES:
        raise Unreadable("The file must be under 10 MB. Split the list into smaller files.")
    data = upload.read()
    notes = []
    if kind == "excel":
        tables, lines = _excel(data), []
    elif kind == "csv":
        tables, lines = [_csv(data)], []
    elif kind == "word":
        tables, lines = _word(data)
    elif kind == "pdf":
        tables, lines = [], _pdf(data, notes)
    else:
        tables, lines = [], _photo(data)
    people = []
    for table in tables:
        people += _from_table(table)
    people += _from_lines(lines)
    if not people:
        raise Unreadable(
            "No phone numbers were found in this file. Each person needs a name and a "
            "Nigerian mobile number on the same line."
        )
    if len(people) > MAX_ROWS:
        raise Unreadable(
            f"A list can have at most {MAX_ROWS:,} people. Split it into smaller files."
        )
    seen = {}
    for number, person in enumerate(people, start=1):
        person["line"] = number
        phone = person["phone_number"]
        if not person["full_name"]:
            person["problem"] = "No name was found next to this number."
        elif phone in seen:
            person["problem"] = f"The same phone number as line {seen[phone]}."
        seen.setdefault(phone, number)
    return {"kind": kind, "people": people, "notes": notes}


# --- the people in rows of cells, or in lines of text ---------------------------


def _clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # Excel keeps 08031234567 as the number 8031234567
    return " ".join(str(value).split())


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _header_role(cell: str) -> str | None:
    text = _letters(cell)
    if not text:
        return None
    if any(word in text for word in ("phone", "gsm", "mobile", "telephone", "whatsapp")):
        return "phone"
    if text in ("tel", "telno", "contact", "contactno", "phoneno", "number", "no"):
        return "phone"
    if text in ("gender", "sex"):
        return "gender"
    if "ward" in text:
        return "ward"
    if "pollingunit" in text or text in ("pu", "pucode", "puname", "unit", "pollingunitcode"):
        return "unit"
    if text.startswith("lga") or "localgov" in text:
        return "lga"
    if text.startswith("state"):
        return "state"
    if "name" in text and not any(word in text for word in ("group", "organi", "leader")):
        return "name"
    return None


def _header(rows):
    """The header row's index and the column of each kind, if a header is there."""
    for index, row in enumerate(rows[:15]):
        roles = {}
        for column, cell in enumerate(row):
            role = _header_role(cell)
            if role == "name":
                roles.setdefault("name", []).append(column)
            elif role and role not in roles:
                roles[role] = column
        if "name" in roles and len(roles) >= 2:
            return index, roles
    return None, {}


def _phone_in(text: str) -> tuple[str | None, str, str]:
    """The first valid number in the text, and the text before and after it."""
    for match in _PHONE.finditer(text):
        phone = normalize_ng_phone(match.group())
        if phone:
            return phone, text[: match.start()].strip(), text[match.end() :].strip()
    return None, text, ""


def _without_gender(text: str) -> tuple[str, str]:
    """The words without a gender written in full, and that gender."""
    gender, kept = "", []
    for word in text.split():
        letters = _letters(word)
        if letters in ("male", "female") and not gender:
            gender = letters
        else:
            kept.append(word)
    return " ".join(kept), gender


def _name(text: str) -> str:
    text = _LEADING_NUMBER.sub("", text)
    text = _NAME_CHARS.sub(" ", text)
    words = [word for word in text.split() if any(char.isalpha() for char in word)]
    return " ".join(words).strip(" .-'")[:150]


def _gender(text: str) -> str:
    return _GENDER_WORDS.get(_letters(text), "")


def _unit_code(text: str) -> str:
    match = _UNIT_CODE.search(text or "")
    if not match:
        return ""
    state, lga, ward, unit = (int(part) for part in match.groups())
    return f"{state:02d}-{lga:02d}-{ward:02d}-{unit:03d}"


def _person(name, phone, *, state="", lga="", ward="", unit="", gender=""):
    return {
        "full_name": _name(name),
        "phone_number": phone,
        "state": state,
        "lga": lga,
        "ward": ward,
        "unit": unit,
        "gender": gender,
        "problem": "",
    }


def _from_table(rows) -> list[dict]:
    rows = [[_clean(cell) for cell in row] for row in rows]
    rows = [row for row in rows if any(row)]
    start, roles = _header(rows)
    people = []
    for row in rows[(start + 1) if start is not None else 0 :]:

        def cell(role, row=row):
            column = roles.get(role)
            return row[column] if column is not None and column < len(row) else ""

        phone = None
        if "phone" in roles:
            phone = _phone_in(cell("phone"))[0]
        used = {roles["phone"]} if phone and "phone" in roles else set()
        if phone is None:
            for column, text in enumerate(row):
                phone = _phone_in(text)[0]
                if phone:
                    used.add(column)
                    break
        if phone is None:
            continue
        if roles.get("name"):
            name = " ".join(row[column] for column in roles["name"] if column < len(row))
        else:
            # No header: the first cell of words, or failing that the words
            # left in the cell with the number.
            name = next(
                (
                    text
                    for column, text in enumerate(row)
                    if column not in used and len(_name(text).split()) >= 2
                ),
                "",
            ) or next(
                (
                    " ".join(_phone_in(text)[1:])
                    for column, text in enumerate(row)
                    if column in used
                ),
                "",
            )
        unit = _unit_code(cell("unit")) or next(
            (code for text in row if (code := _unit_code(text))), ""
        )
        people.append(
            _person(
                name,
                phone,
                state=cell("state"),
                lga=cell("lga"),
                ward=cell("ward"),
                unit=unit or cell("unit"),
                gender=_gender(cell("gender")),
            )
        )
    return people


def _from_lines(lines) -> list[dict]:
    people = []
    previous = ""
    for line in lines:
        phone, before, after = _phone_in(line)
        if phone is None:
            previous = line
            continue
        unit = _unit_code(after) or _unit_code(before)
        before, gender = _without_gender(_UNIT_CODE.sub(" ", before))
        after, gender_after = _without_gender(_UNIT_CODE.sub(" ", after))
        # The name is what comes before the number; on a line that starts
        # with the number, what follows it; failing both, a line of words
        # just above, as a PDF's table can come out one cell to a line.
        name = _name(before) or _name(after)
        if not name and len(_name(previous).split()) >= 2:
            name = previous
        people.append(_person(name, phone, unit=unit, gender=gender or gender_after))
        previous = ""
    return people


# --- the formats -------------------------------------------------------------------


def _unzipped(data: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise Unreadable("This file could not be opened. Save it again and upload it.") from exc
    if sum(info.file_size for info in archive.infolist()) > MAX_UNZIPPED:
        raise Unreadable("This file is too large to read. Split the list into smaller files.")
    return archive


def _xml(archive: zipfile.ZipFile, name: str):
    try:
        return ElementTree.fromstring(archive.read(name))  # noqa: S314 - no entities are resolved
    except (KeyError, ElementTree.ParseError) as exc:
        raise Unreadable("This file could not be opened. Save it again and upload it.") from exc


def _excel(data: bytes) -> list[list[list[str]]]:
    """Every sheet's rows, read from the file's XML."""
    archive = _unzipped(data)
    names = set(archive.namelist())
    shared = []
    if "xl/sharedStrings.xml" in names:
        for item in _xml(archive, "xl/sharedStrings.xml").iter(f"{_S}si"):
            shared.append("".join(text.text or "" for text in item.iter(f"{_S}t")))
    sheets = sorted(
        (name for name in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
        key=lambda name: int(re.search(r"\d+", name.rsplit("/", 1)[-1]).group()),
    )
    tables = []
    for sheet in sheets:
        rows = []
        for row in _xml(archive, sheet).iter(f"{_S}row"):
            cells = {}
            for cell in row.iter(f"{_S}c"):
                column = _column(cell.get("r", ""), len(cells))
                kind = cell.get("t", "")
                value = cell.find(f"{_S}v")
                if kind == "s" and value is not None and (value.text or "").isdigit():
                    index = int(value.text)
                    text = shared[index] if index < len(shared) else ""
                elif kind == "inlineStr":
                    text = "".join(t.text or "" for t in cell.iter(f"{_S}t"))
                elif value is not None and value.text is not None:
                    text = value.text
                    if kind not in ("str", "e", "b"):
                        try:
                            text = _clean(float(text))
                        except ValueError:
                            pass
                else:
                    text = ""
                cells[column] = text
            if cells:
                rows.append([cells.get(column, "") for column in range(max(cells) + 1)])
            if len(rows) > MAX_ROWS + 15:
                break
        tables.append(rows)
    return tables


def _column(reference: str, fallback: int) -> int:
    letters = re.match(r"[A-Z]+", reference)
    if not letters:
        return fallback
    number = 0
    for letter in letters.group():
        number = number * 26 + ord(letter) - 64
    return min(number - 1, 200)


def _csv(data: bytes) -> list[list[str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return list(csv.reader(io.StringIO(text), dialect))[: MAX_ROWS + 15]


def _word(data: bytes) -> tuple[list, list[str]]:
    """The document's tables, and its other lines of text."""
    body = _xml(_unzipped(data), "word/document.xml").find(f"{_W}body")
    if body is None:
        return [], []
    tables, lines = [], []

    def text_of(element) -> str:
        return " ".join(
            "".join(t.text or "" for t in paragraph.iter(f"{_W}t"))
            for paragraph in element.iter(f"{_W}p")
        )

    for element in body:
        if element.tag == f"{_W}tbl":
            tables.append(
                [[text_of(cell) for cell in row.iter(f"{_W}tc")] for row in element.iter(f"{_W}tr")]
            )
        elif element.tag == f"{_W}p":
            lines.append(text_of(element))
    return tables, lines


def _pdf(data: bytes, notes: list[str]) -> list[str]:
    """The lines of a PDF's text; a scan's pages are read as pictures."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise Unreadable("This PDF is locked with a password. Upload one that is not.")
        pages = reader.pages
        if len(pages) > MAX_PAGES:
            raise Unreadable(f"A PDF can have at most {MAX_PAGES} pages. Split it.")
        texts = [page.extract_text() or "" for page in pages]
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise Unreadable("This PDF could not be opened. Save it again and upload it.") from exc
    lines = [line for text in texts for line in text.splitlines()]
    if any(_phone_in(line)[0] for line in lines):
        return lines
    # No numbers in the text: a scan, read page by page as pictures.
    started = time.monotonic()
    lines = []
    for number, page in enumerate(pages, start=1):
        if number > MAX_SCANNED_PAGES or time.monotonic() - started > SCAN_SECONDS:
            notes.append(
                f"Only the first {number - 1} pages of this scan were read. "
                "Upload the other pages as another file."
            )
            break
        try:
            pictures = [picture.image for picture in page.images]
        except (PdfReadError, ValueError, KeyError, TypeError, OSError, NotImplementedError):
            pictures = []
        for picture in pictures:
            if picture is not None and picture.width * picture.height >= 200_000:
                lines += _read_picture(picture)
    return lines


def _photo(data: bytes) -> list[str]:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise Unreadable("This photo could not be opened. Take it again.") from exc
    return _read_picture(ImageOps.exif_transpose(image))


def _read_picture(image: Image.Image) -> list[str]:
    """A page of a list read with Tesseract, line by line."""
    if not shutil.which(settings.TESSERACT_CMD or ""):
        raise Unreadable(
            "Scans and photos cannot be read on this server. Upload the list as Excel or Word."
        )
    if image.width * image.height > 60_000_000:
        raise Unreadable("This picture is too large to read.")
    buffer = io.BytesIO()
    pvc_reading.prepare_for_tesseract(image).save(buffer, "PNG")
    try:
        result = subprocess.run(  # noqa: S603 - a fixed program and arguments, no shell
            [settings.TESSERACT_CMD, "stdin", "stdout", "-l", "eng", "--psm", "6"],
            input=buffer.getvalue(),
            capture_output=True,
            timeout=TESSERACT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.error("Reading a list with Tesseract failed: %s", type(error).__name__)
        raise Unreadable("This page could not be read in time. Upload fewer pages.") from error
    if result.returncode != 0:
        logger.error("Reading a list with Tesseract failed: exit code %s", result.returncode)
        raise Unreadable("This page could not be read. Upload the list as Excel or Word.")
    return result.stdout.decode("utf-8", errors="replace").splitlines()
