"""The membership card as a PDF: two pages at ID-card size (85.6 x 54 mm),
the front and the back, ready for a card printer or a print shop.

The front carries the logo, the member's photo, name, membership number,
roles and polling unit; the back the QR code coordinators scan to check the
card, the issue date and a line asking whoever finds it to return it. The
text is set in Noto Sans (SIL Open Font License, card_assets/OFL.txt), so
names in Hausa, Igbo and Yoruba spelling print as written.
"""

import io
from pathlib import Path

from django.utils import timezone
from PIL import Image, ImageOps
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import HexColor, white
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .card import card_roles
from .serializers import card_token

ASSETS = Path(__file__).resolve().parent / "card_assets"
WIDTH, HEIGHT = 85.6 * mm, 54 * mm
REGULAR, BOLD = "CardSans", "CardSans-Bold"

# The app's colours (lib/tgcg/ui/tgcg_design.dart).
NAVY_DARK = HexColor("#061740")
NAVY = HexColor("#0A2463")
NAVY_MID = HexColor("#1D3F94")
GOLD = HexColor("#C9971A")
GOLD_BRIGHT = HexColor("#E6BE4C")
MIST = HexColor("#C9D3EC")
INK = HexColor("#101A33")
MUTED = HexColor("#5D6782")

_ready = False


def _fonts():
    global _ready
    if not _ready:
        pdfmetrics.registerFont(TTFont(REGULAR, ASSETS / "NotoSans-Regular.ttf"))
        pdfmetrics.registerFont(TTFont(BOLD, ASSETS / "NotoSans-Bold.ttf"))
        _ready = True


def _shorten(text, font, size, width):
    """The text, cut with an ellipsis if it is wider than [width]."""
    if stringWidth(text, font, size) <= width:
        return text
    while text and stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap(text, font, size, width, most):
    """Words in lines no wider than [width]: at most [most], the last one cut
    with an ellipsis when the text does not fit."""
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and stringWidth(trial, font, size) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    if len(lines) > most:
        lines = [*lines[: most - 1], " ".join(lines[most - 1 :])]
    return [_shorten(line, font, size, width) for line in lines]


def _fitted(text, font, width, most, sizes):
    """The largest size in [sizes] at which [text] fits in [most] lines."""
    for size in sizes:
        lines = _wrap(text, font, size, width, most)
        if not any(line.endswith("…") for line in lines):
            return size, lines
    return sizes[-1], _wrap(text, font, sizes[-1], width, most)


def _title(name):
    """INEC names are upper case; print them in title case."""
    return " ".join(word.capitalize() for word in (name or "").split())


def _place(member):
    """The member's voting place, from the polling unit up to the state."""
    lines = []
    if member.polling_unit_id:
        lines.append(_title(member.polling_unit.name))
    if member.ward_id and member.lga_id:
        lines.append(f"{_title(member.ward.name)} ward, {_title(member.lga.name)} LGA")
    elif member.lga_id:
        lines.append(f"{_title(member.lga.name)} LGA")
    if member.state_id:
        name = member.state.name
        lines.append(name if "Capital Territory" in name else f"{name} State")
    return lines


def _photo(member, width, height):
    """The member's photo cropped to the frame (a little above the middle,
    where faces are) and made small enough for the card."""
    member.photo.open("rb")
    try:
        data = member.photo.read()
    finally:
        member.photo.close()
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    shape = width / height
    w, h = image.size
    if w / h > shape:
        keep = int(h * shape)
        left = (w - keep) // 2
        image = image.crop((left, 0, left + keep, h))
    else:
        keep = int(w / shape)
        top = (h - keep) // 3
        image = image.crop((0, top, w, top + keep))
    image.thumbnail((480, 600))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88)
    out.seek(0)
    return ImageReader(out)


def _qr(page, value, x, y, size):
    widget = QrCodeWidget(value, barLevel="M")
    x1, y1, x2, y2 = widget.getBounds()
    drawing = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
    drawing.add(widget)
    renderPDF.draw(drawing, page, x, y)


def _front(page, member):
    page.linearGradient(0, HEIGHT, WIDTH, 0, (NAVY_DARK, NAVY, NAVY_MID), extend=True)
    page.setStrokeColor(GOLD)
    page.setStrokeAlpha(0.7)
    page.setLineWidth(0.6)
    page.roundRect(1.6 * mm, 1.6 * mm, WIDTH - 3.2 * mm, HEIGHT - 3.2 * mm, 2.4 * mm)
    page.setStrokeAlpha(1)

    page.drawImage(
        ImageReader(str(ASSETS / "tgcg_logo.png")),
        4 * mm,
        HEIGHT - 12.4 * mm,
        8.4 * mm,
        8.4 * mm,
        mask="auto",
    )
    page.setFillColor(white)
    page.setFont(BOLD, 7)
    page.drawString(14.4 * mm, HEIGHT - 7.6 * mm, "THE GAME CHANGER GROUP")
    page.setFillColor(GOLD_BRIGHT)
    page.setFont(BOLD, 5)
    page.drawString(14.4 * mm, HEIGHT - 10.6 * mm, "MEMBERSHIP CARD", charSpace=1)

    # The photo.
    px, py, pw, ph = 4 * mm, 11.6 * mm, 20 * mm, 26 * mm
    page.drawImage(_photo(member, pw, ph), px, py, pw, ph)
    page.setStrokeColor(GOLD_BRIGHT)
    page.setLineWidth(0.8)
    page.rect(px, py, pw, ph)

    # Name, number, roles and place, top down beside the photo.
    x, width = 27.4 * mm, WIDTH - 31.4 * mm
    y = py + ph - 7
    size, lines = _fitted(member.full_name.upper(), BOLD, width, 2, (9, 8.5, 8, 7.5, 7))
    page.setFillColor(white)
    page.setFont(BOLD, size)
    for line in lines:
        page.drawString(x, y, line)
        y -= size * 1.15
    y -= 2.5
    page.setFillColor(GOLD_BRIGHT)
    page.setFont(BOLD, 8)
    page.drawString(x, y - 1, member.membership_number, charSpace=0.4)
    y -= 11.5
    roles = " · ".join(card_roles(member)) or "Member"
    page.setFillColor(white)
    page.setFont(REGULAR, 6)
    for line in _wrap(roles, REGULAR, 6, width, 2):
        page.drawString(x, y, line)
        y -= 7.2
    y -= 1.5
    page.setFillColor(MIST)
    page.setFont(REGULAR, 5.4)
    for line in _place(member):
        if y < py:
            break
        page.drawString(x, y, _shorten(line, REGULAR, 5.4, width))
        y -= 6.6

    # Member since, under a gold rule.
    page.setStrokeColor(GOLD)
    page.setLineWidth(0.4)
    page.line(4 * mm, 9 * mm, WIDTH - 4 * mm, 9 * mm)
    page.setFillColor(white)
    page.setFont(BOLD, 5)
    since = timezone.localtime(member.created_at).year
    page.drawString(4 * mm, 5 * mm, f"MEMBER SINCE {since}", charSpace=0.6)


def _back(page, member, issued):
    page.setFillColor(white)
    page.rect(0, 0, WIDTH, HEIGHT, stroke=0, fill=1)
    band = 8 * mm
    page.setFillColor(NAVY)
    page.rect(0, HEIGHT - band, WIDTH, band, stroke=0, fill=1)
    page.setStrokeColor(GOLD)
    page.setLineWidth(0.8)
    page.line(0, HEIGHT - band, WIDTH, HEIGHT - band)
    page.setFillColor(white)
    page.setFont(BOLD, 6)
    page.drawString(4 * mm, HEIGHT - 5.2 * mm, "THE GAME CHANGER GROUP")
    page.setFillColor(GOLD_BRIGHT)
    page.setFont(BOLD, 4.6)
    page.drawRightString(WIDTH - 4 * mm, HEIGHT - 5.2 * mm, "MEMBERSHIP CARD", charSpace=0.8)

    _qr(page, card_token(member), 4.6 * mm, 13.4 * mm, 25 * mm)

    x = 34 * mm
    page.setFillColor(MUTED)
    page.setFont(BOLD, 4.6)
    page.drawString(x, 36.4 * mm, "MEMBERSHIP NUMBER", charSpace=0.6)
    page.setFillColor(NAVY)
    page.setFont(BOLD, 10)
    page.drawString(x, 31.8 * mm, member.membership_number)
    page.setFillColor(MUTED)
    page.setFont(BOLD, 4.6)
    page.drawString(x, 26.6 * mm, "ISSUED", charSpace=0.6)
    page.setFillColor(INK)
    page.setFont(BOLD, 7)
    page.drawString(x, 23 * mm, f"{issued.day} {issued:%b %Y}")
    page.setFont(REGULAR, 5.6)
    page.drawString(x, 17.6 * mm, "Scan the code to check this card.")

    page.setStrokeColor(GOLD)
    page.setLineWidth(0.4)
    page.line(4 * mm, 9.6 * mm, WIDTH - 4 * mm, 9.6 * mm)
    page.setFillColor(MUTED)
    page.setFont(REGULAR, 5.4)
    page.drawCentredString(
        WIDTH / 2, 5.2 * mm, "If found, please return it to The Game Changer Group."
    )


def render(member, issued=None) -> bytes:
    """The card as PDF bytes. Check card.card_missing first: it needs the
    member's photo."""
    _fonts()
    issued = issued or timezone.localdate()
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=(WIDTH, HEIGHT))
    page.setTitle(f"TGCG membership card {member.membership_number}")
    page.setAuthor("The Game Changer Group")
    page.setSubject("Membership card")
    _front(page, member)
    page.showPage()
    _back(page, member, issued)
    page.showPage()
    page.save()
    return buffer.getvalue()
