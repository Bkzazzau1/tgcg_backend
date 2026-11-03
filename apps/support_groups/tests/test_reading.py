"""Reading a group's list of members from Excel, CSV, Word, PDF or a photo."""

import io
import shutil
import zipfile

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfgen import canvas

from apps.access.tests.test_access import client_for
from apps.support_groups import reading

pytestmark = pytest.mark.django_db


def upload(name, data):
    return SimpleUploadedFile(name, data)


def people(name, data):
    return reading.read(upload(name, data))["people"]


def short(found):
    return [(person["full_name"], person["phone_number"]) for person in found]


def xlsx(rows):
    """A workbook as Excel saves it: words in the shared strings, numbers in the cells."""
    strings, sheet = [], []
    for number, row in enumerate(rows, start=1):
        cells = []
        for column, value in enumerate(row):
            ref = f"{chr(65 + column)}{number}"
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                strings.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
        sheet.append(f'<row r="{number}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    saved = io.BytesIO()
    with zipfile.ZipFile(saved, "w") as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            f"<sst {ns}>" + "".join(f"<si><t>{text}</t></si>" for text in strings) + "</sst>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f"<worksheet {ns}><sheetData>{''.join(sheet)}</sheetData></worksheet>",
        )
    return saved.getvalue()


def docx(rows, paragraphs=()):
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

    def para(text):
        return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"

    table = "".join(
        "<w:tr>" + "".join(f"<w:tc>{para(cell)}</w:tc>" for cell in row) + "</w:tr>" for row in rows
    )
    body = "".join(para(text) for text in paragraphs) + f"<w:tbl>{table}</w:tbl>"
    saved = io.BytesIO()
    with zipfile.ZipFile(saved, "w") as archive:
        archive.writestr(
            "word/document.xml", f"<w:document {w}><w:body>{body}</w:body></w:document>"
        )
    return saved.getvalue()


def pdf(lines):
    saved = io.BytesIO()
    page = canvas.Canvas(saved)
    for number, line in enumerate(lines):
        page.drawString(40, 800 - 20 * number, line)
    page.save()
    return saved.getvalue()


def test_a_csv_with_a_header():
    data = (
        b"S/N,Full Name,Phone Number,State,LGA,Ward,Gender\n"
        b"1,Musa Bello,0803 555 0101,Kaduna,Zaria,Dambo,M\n"
        b"2,Aisha Sani,+2348035550102,,,,Female\n"
    )
    found = people("list.csv", data)
    assert short(found) == [("Musa Bello", "+2348035550101"), ("Aisha Sani", "+2348035550102")]
    assert (found[0]["state"], found[0]["lga"], found[0]["ward"]) == ("Kaduna", "Zaria", "Dambo")
    assert [person["gender"] for person in found] == ["male", "female"]


def test_excel_keeps_numbers_without_the_first_zero():
    data = xlsx(
        [
            ["SURNAME", "OTHER NAMES", "GSM", "PU CODE"],
            ["Bello", "Musa", 8035550101, "18-23-01-001"],
            ["Sani", "Aisha", 2348035550102, ""],
        ]
    )
    found = people("list.xlsx", data)
    assert short(found) == [("Bello Musa", "+2348035550101"), ("Sani Aisha", "+2348035550102")]
    assert found[0]["unit"] == "18-23-01-001"


def test_rows_without_a_header():
    data = xlsx([["1", "Musa Bello", "08035550101"], ["2", "08035550102", "Aisha Sani"]])
    assert short(people("list.xlsx", data)) == [
        ("Musa Bello", "+2348035550101"),
        ("Aisha Sani", "+2348035550102"),
    ]


def test_a_word_table_and_its_lines():
    data = docx(
        [["Name", "Phone"], ["Musa Bello", "08035550101"]],
        paragraphs=["Members of Arewa Youth", "Aisha Sani 08035550102"],
    )
    assert short(people("list.docx", data)) == [
        ("Musa Bello", "+2348035550101"),
        ("Aisha Sani", "+2348035550102"),
    ]


def test_a_pdf_of_text():
    data = pdf(
        [
            "ADAMAWA YOUTH FORUM MEMBERS",
            "1. Musa Bello Male 08035550101 18-23-01-001",
            "2) Aisha Sani 0803-555-0102",
            "08035550103 Grace Obi",
        ]
    )
    found = people("list.pdf", data)
    assert short(found) == [
        ("Musa Bello", "+2348035550101"),
        ("Aisha Sani", "+2348035550102"),
        ("Grace Obi", "+2348035550103"),
    ]
    assert found[0]["gender"] == "male" and found[0]["unit"] == "18-23-01-001"


def test_a_scan_is_read_as_pictures(monkeypatch):
    saved = io.BytesIO()
    Image.new("RGB", (900, 1200), "white").save(saved, "PDF")
    monkeypatch.setattr(reading, "_read_picture", lambda image: ["1 Musa Bello 08035550101"])
    assert short(people("scan.pdf", saved.getvalue())) == [("Musa Bello", "+2348035550101")]

    photo = io.BytesIO()
    Image.new("RGB", (900, 1200), "white").save(photo, "PNG")
    assert short(people("list.png", photo.getvalue())) == [("Musa Bello", "+2348035550101")]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed here")
def test_a_photo_of_a_printed_list_is_read_with_tesseract(settings):
    settings.TESSERACT_CMD = "tesseract"
    page = Image.new("RGB", (1800, 600), "white")
    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=56)
    for number, line in enumerate(["1 Musa Bello 08035550101", "2 Aisha Sani 08035550102"]):
        draw.text((60, 80 + 140 * number), line, fill="black", font=font)
    photo = io.BytesIO()
    page.save(photo, "PNG")
    assert short(people("list.png", photo.getvalue())) == [
        ("Musa Bello", "+2348035550101"),
        ("Aisha Sani", "+2348035550102"),
    ]


def test_lines_are_checked():
    data = b"Name,Phone\nMusa Bello,08035550101\n,08035550102\nMusa B.,0803 555 0101\nNo Phone,\n"
    found = people("list.csv", data)
    assert [person["line"] for person in found] == [1, 2, 3]
    assert found[1]["problem"] == "No name was found next to this number."
    assert found[2]["problem"] == "The same phone number as line 1."


def test_files_that_cannot_be_read():
    with pytest.raises(ValidationError, match="newer format"):
        reading.read(upload("old.xls", b"x"))
    with pytest.raises(ValidationError, match="Excel, CSV, Word, PDF or a photo"):
        reading.read(upload("list.txt", b"x"))
    with pytest.raises(ValidationError, match="No phone numbers"):
        reading.read(upload("list.csv", b"Name,Phone\nMusa Bello,\n"))
    with pytest.raises(ValidationError, match="could not be opened"):
        reading.read(upload("list.docx", b"not a zip"))


def test_the_leader_reads_a_list_with_places(group, leader):
    data = (
        b"Name,Phone,State,LGA,Ward,PU\n"
        b"Musa Bello,08035550101,Kaduna State,Zaria LGA,Dambo,\n"
        b"Aisha Sani,08035550102,,Zaria,,\n"
        b"Grace Obi,08035550103,,,,18-23-01-001\n"
        b"Joy Eze,08035550104,Nowhere,,,\n"
    )
    response = client_for(leader.user).post(
        f"/api/v1/my-groups/{group.uuid}/read-list/",
        {"file": upload("list.csv", data)},
        format="multipart",
    )
    assert response.status_code == 200, response.content
    rows = response.json()["people"]
    assert rows[0]["scope"] == {"level": "ward", "wardId": "18-23-10"}
    assert rows[0]["place"] == "Dambo, Zaria, Kaduna"
    assert rows[1]["scope"] == {"level": "lga", "lgaId": "KD-ZARIA"}
    assert rows[2]["scope"]["level"] == "pollingUnit"
    assert rows[3]["scope"] is None and rows[3]["place"] == ""
    assert rows[0]["phone_number"] == "08035550101"

    # The checked rows go back to be added, place and all.
    added = client_for(leader.user).post(
        f"/api/v1/my-groups/{group.uuid}/add/",
        {"people": rows[:1], "consent": True},
        format="json",
    )
    assert added.json()["results"][0]["outcome"] == "registered"
