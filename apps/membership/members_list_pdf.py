"""Members and exco of an area as one printable list: a PDF on A4 landscape.

Everyone in the area appears once, grouped by place: the area's own top
posts, then state by state, LGA by LGA and ward by ward. In each place its
exco come first (anyone holding a post there, listed under their highest
post in the area), then the members registered there, by polling unit.

Each row gives the full name, membership number, polling unit and every post
the person holds now. Agreed with the user on 19 October 2026: no phone
numbers or other personal details, so the list can be printed and shared.
Text is set in Noto Sans, as on the membership card.
"""

import io
import re
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from django.db.models import Q
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.access.models import AssignmentStatus
from apps.core.choices import GeographyLevel
from apps.geography.scope import LEVEL_FIELD, within_scope_q

from . import analysis, card_pdf
from .models import Member

L = GeographyLevel
PAGE = landscape(A4)
MARGIN = 12 * mm
# Column widths: number, full name, membership number, polling unit, posts.
# 99% of INEC unit codes and names fit the unit column on one line.
WIDTHS = [11 * mm, 60 * mm, 26 * mm, 92 * mm, 84 * mm]
HEAD = ["#", "Full name", "Membership no.", "Polling unit", "Positions"]
EXCO_SHADE = HexColor("#FBF4E2")
PLACE_SHADE = HexColor("#EEF2FA")
LINE = HexColor("#DFE4EF")
# How deep a place sits in the list: the area's own posts, state, LGA, ward.
PLACE_DEPTH = {
    L.COUNTRY: 0,
    L.GEOPOLITICAL_ZONE: 0,
    L.STATE: 1,
    L.SENATORIAL_DISTRICT: 1,
    L.LGA: 2,
    L.WARD: 3,
    L.POLLING_UNIT: 3,
}


@dataclass
class Row:
    name: str
    number: str
    unit: str
    posts: str
    exco: bool


@dataclass
class Place:
    units: tuple  # (), (state,), (state, lga) or (state, lga, ward)
    title: str
    rows: list[Row] = field(default_factory=list)
    path: str = ""  # "Chamo ward, Dutse LGA, Jigawa State": repeated on each page of its table

    @property
    def depth(self) -> int:
        return len(self.units)


@dataclass
class Listing:
    title: str
    places: list[Place]
    people: int
    exco: int
    parts_label: str = ""
    parts: list[tuple[str, int, int]] = field(default_factory=list)  # (place, people, exco)


def area_title(scope) -> str:
    level = scope.scope_level
    if level == L.COUNTRY:
        return "Nigeria"
    unit = getattr(scope, LEVEL_FIELD[level])
    return _title(level, unit)


def _title(level, unit) -> str:
    if level == L.STATE:
        return unit.name if unit.code == "FCT" else f"{unit.name} State"
    if level == L.LGA:
        return f"{unit.name} LGA"
    if level == L.WARD:
        return f"{unit.name} ward"
    if level == L.POLLING_UNIT:
        return f"{unit.name} ({unit.code})"
    if level == L.GEOPOLITICAL_ZONE:
        return f"{unit.name} zone"
    return unit.name


def _units(obj) -> tuple:
    """The places above and at `obj`'s level down to its ward: (state, lga, ward)."""
    chain = (obj.state, obj.lga, obj.ward)[: PLACE_DEPTH[obj.scope_level]]
    units = []
    for unit in chain:
        if unit is None:
            break
        units.append(unit)
    return tuple(units)


def _post_text(post) -> str:
    level = post.scope_level
    where = "Nigeria" if level == L.COUNTRY else getattr(post, LEVEL_FIELD[level]).name
    text = f"{post.assignment.role.name}, {where}"
    if post.assignment.status == AssignmentStatus.SUSPENDED:
        text += " (suspended)"
    return text


def collect(scope, now=None) -> Listing:
    """Everyone in `scope`: its members and the holders of posts in it."""
    here = Member.objects.filter(within_scope_q(scope))
    posts_here = analysis.exco_areas(scope, now)
    places = ("state", "lga", "ward", "polling_unit")
    people = list(
        Member.objects.filter(
            Q(pk__in=here.values("pk")) | Q(pk__in=posts_here.values("assignment__member_id"))
        ).select_related(*places)
    )
    inside = set(posts_here.values_list("pk", flat=True))
    posts = (
        analysis.exco_areas(now=now)
        .filter(
            Q(assignment__member_id__in=here.values("pk"))
            | Q(assignment__member_id__in=posts_here.values("assignment__member_id"))
        )
        .select_related("assignment__role", "zone", "senatorial_district", *places)
        .annotate(depth=analysis.depth_order())
        .order_by("depth", "assignment__role__name", "pk")
    )
    held = {}
    for post in posts:
        held.setdefault(post.assignment.member_id, []).append(post)

    by_place: dict[tuple, Place] = {}

    def place(units: tuple) -> Place:
        # Headings above it come first; the area's own group () holds only
        # national and zonal posts, so it is made only when used.
        for size in range(0 if not units else 1, len(units) + 1):
            key = units[:size]
            if key not in by_place:
                title = (
                    area_title(scope)
                    if not key
                    else _title((L.STATE, L.LGA, L.WARD)[size - 1], key[-1])
                )
                parent = by_place.get(key[:-1]) if len(key) > 1 else None
                path = f"{title}, {parent.path}" if parent else title
                by_place[key] = Place(key, title, path=path)
        return by_place[units]

    order = []
    exco_count = 0
    for member in people:
        mine = held.get(member.pk, [])
        top = next((post for post in mine if post.pk in inside), None)
        unit = member.polling_unit
        row = Row(
            name=member.full_name or "(no name)",
            number=member.membership_number,
            unit=f"{unit.code} {unit.name}" if unit else "",
            posts="; ".join(_post_text(post) for post in mine),
            exco=top is not None,
        )
        exco_count += row.exco
        target = place(_units(top if top is not None else member))
        target.rows.append(row)
        # Exco by their highest post here, then members by polling unit.
        order.append(
            (
                row,
                (0, top.depth, top.assignment.role.name.lower(), row.name.lower())
                if top is not None
                else (1, row.unit or "~", row.name.lower(), ""),
            )
        )
    rank = {id(row): key for row, key in order}
    for entry in by_place.values():
        entry.rows.sort(key=lambda row: rank[id(row)])
    ordered = sorted(by_place.values(), key=lambda p: tuple(u.name.lower() for u in p.units))
    listing = Listing(area_title(scope), ordered, people=len(people), exco=exco_count)
    _parts(listing, scope)
    return listing


def _parts(listing: Listing, scope) -> None:
    """People and exco in each next place down (states, LGAs or wards)."""
    size = {L.COUNTRY: 1, L.GEOPOLITICAL_ZONE: 1, L.STATE: 2, L.SENATORIAL_DISTRICT: 2, L.LGA: 3}
    depth = size.get(scope.scope_level)
    if depth is None:
        return
    listing.parts_label = {1: "State", 2: "LGA", 3: "Ward"}[depth]
    totals: dict[tuple, list] = {}
    for entry in listing.places:
        if entry.depth < depth:
            continue
        key = entry.units[:depth]
        counts = totals.setdefault(key, [0, 0])
        counts[0] += len(entry.rows)
        counts[1] += sum(row.exco for row in entry.rows)
    listing.parts = [
        (key[-1].name, people, exco)
        for key, (people, exco) in sorted(totals.items(), key=lambda kv: kv[0][-1].name.lower())
    ]


# --- the PDF ---------------------------------------------------------------------


def _styles():
    card_pdf._fonts()
    regular, bold = card_pdf.REGULAR, card_pdf.BOLD
    return {
        "title": ParagraphStyle(
            "title", fontName=bold, fontSize=20, leading=24, textColor=card_pdf.NAVY
        ),
        "lede": ParagraphStyle(
            "lede", fontName=regular, fontSize=10, leading=14, textColor=card_pdf.MUTED
        ),
        "h1": ParagraphStyle(
            "h1",
            fontName=bold,
            fontSize=15,
            leading=19,
            spaceBefore=10,
            spaceAfter=4,
            textColor=card_pdf.NAVY,
        ),
        "h2": ParagraphStyle(
            "h2",
            fontName=bold,
            fontSize=12.5,
            leading=16,
            spaceBefore=8,
            spaceAfter=3,
            textColor=card_pdf.NAVY_MID,
        ),
        "h3": ParagraphStyle(
            "h3",
            fontName=bold,
            fontSize=10.5,
            leading=14,
            spaceBefore=6,
            spaceAfter=3,
            textColor=card_pdf.INK,
        ),
        "place": ParagraphStyle(
            "place", fontName=bold, fontSize=9.5, leading=12, textColor=card_pdf.NAVY
        ),
        "cell": ParagraphStyle(
            "cell", fontName=regular, fontSize=8.5, leading=10.5, textColor=card_pdf.INK
        ),
        "cellb": ParagraphStyle(
            "cellb", fontName=bold, fontSize=8.5, leading=10.5, textColor=card_pdf.INK
        ),
    }


def _cell(text: str, style, width: float):
    """Plain text when it fits on one line; a wrapping paragraph otherwise."""
    if not text:
        return ""
    if stringWidth(text, style.fontName, style.fontSize) <= width - 8:
        return text
    return Paragraph(escape(text), style)


def _table(place: Place, start: int, styles):
    rows = place.rows
    # The place and the column names head every page the table runs onto.
    body = [[Paragraph(f"{escape(place.path)} ({len(rows):,})", styles["place"])], HEAD]
    shade = []
    for index, row in enumerate(rows, start=1):
        name_style = styles["cellb"] if row.exco else styles["cell"]
        body.append(
            [
                str(start + index - 1),
                _cell(row.name, name_style, WIDTHS[1]),
                _cell(row.number, styles["cell"], WIDTHS[2]),
                _cell(row.unit, styles["cell"], WIDTHS[3]),
                _cell(row.posts, styles["cell"], WIDTHS[4]),
            ]
        )
        if row.exco:
            shade.append(("BACKGROUND", (0, index + 1), (-1, index + 1), EXCO_SHADE))
    bold_rows = [
        ("FONTNAME", (1, index + 1), (1, index + 1), card_pdf.BOLD)
        for index, row in enumerate(rows, start=1)
        if row.exco
    ]
    table = Table(body, colWidths=WIDTHS, repeatRows=2)
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), card_pdf.REGULAR),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("LEADING", (0, 0), (-1, -1), 10.5),
                ("TEXTCOLOR", (0, 0), (-1, -1), card_pdf.INK),
                ("SPAN", (0, 0), (-1, 0)),
                ("BACKGROUND", (0, 0), (-1, 0), PLACE_SHADE),
                ("FONTNAME", (0, 1), (-1, 1), card_pdf.BOLD),
                ("FONTSIZE", (0, 1), (-1, 1), 7.5),
                ("TEXTCOLOR", (0, 1), (-1, 1), white),
                ("BACKGROUND", (0, 1), (-1, 1), card_pdf.NAVY),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 2), (0, -1), "RIGHT"),
                ("LINEBELOW", (0, 2), (-1, -1), 0.4, LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                *shade,
                *bold_rows,
            ]
        )
    )
    return table


def _summary(listing: Listing, styles):
    rows = [["", "People", "Exco", "Members"]]
    if listing.parts:
        for name, people, exco in listing.parts:
            rows.append([name, f"{people:,}", f"{exco:,}", f"{people - exco:,}"])
    rows.append(
        ["All", f"{listing.people:,}", f"{listing.exco:,}", f"{listing.people - listing.exco:,}"]
    )
    rows[0][0] = listing.parts_label or listing.title
    table = Table(rows, colWidths=[70 * mm, 28 * mm, 28 * mm, 28 * mm], repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), card_pdf.REGULAR),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("FONTNAME", (0, 0), (-1, 0), card_pdf.BOLD),
                ("FONTNAME", (0, -1), (-1, -1), card_pdf.BOLD),
                ("TEXTCOLOR", (0, 0), (-1, 0), card_pdf.MUTED),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
                ("LINEABOVE", (0, -1), (-1, -1), 0.8, card_pdf.NAVY),
            ]
        )
    )
    return table


def _canvas_with_footer(footer: str):
    class Numbered(canvas.Canvas):
        """Writes "Page n of N" once the number of pages is known."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved = []

        def showPage(self):  # noqa: N802 (reportlab's name)
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved)
            for number, state in enumerate(self._saved, start=1):
                self.__dict__.update(state)
                self.setFont(card_pdf.REGULAR, 7.5)
                self.setFillColor(card_pdf.MUTED)
                self.drawString(MARGIN, 7 * mm, footer)
                self.drawRightString(PAGE[0] - MARGIN, 7 * mm, f"Page {number} of {total}")
                super().showPage()
            super().save()

    return Numbered


def render(listing: Listing, made_by: str, made_at) -> bytes:
    styles = _styles()
    when = made_at.strftime("%d %B %Y, %H:%M")
    story = []
    logo = Image(str(card_pdf.ASSETS / "tgcg_logo.png"), width=16 * mm, height=16 * mm)
    heading = Table(
        [
            [
                logo,
                [
                    Paragraph(f"Members and exco: {escape(listing.title)}", styles["title"]),
                    Paragraph(
                        f"{listing.people:,} people: {listing.exco:,} exco and "
                        f"{listing.people - listing.exco:,} members. Exco are listed first in "
                        "each place, shaded, under their highest post here.",
                        styles["lede"],
                    ),
                    Paragraph(f"The Game Changer Group. Made {when}.", styles["lede"]),
                ],
            ]
        ],
        colWidths=[20 * mm, PAGE[0] - 2 * MARGIN - 20 * mm],
    )
    heading.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story += [heading, Spacer(1, 6 * mm), _summary(listing, styles), Spacer(1, 4 * mm)]

    number = 1
    for place in listing.places:
        # States and LGAs open their sections; a ward's table names it itself.
        if place.depth < 3:
            style = styles[("h1", "h1", "h2")[place.depth]]
            story.append(Paragraph(escape(place.title), style))
        if place.rows:
            story.append(Spacer(1, 2 * mm))
            story.append(_table(place, number, styles))
            number += len(place.rows)
    if not listing.people:
        story.append(Paragraph("No members or exco in this area yet.", styles["lede"]))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=PAGE,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=14 * mm,
        title=f"TGCG members and exco: {listing.title}",
        author="The Game Changer Group",
    )
    footer = f"TGCG. Private: members' names and posts. Downloaded by {made_by}, {when}."
    doc.build(story, canvasmaker=_canvas_with_footer(footer))
    return buffer.getvalue()


def filename(listing: Listing, made_at) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", listing.title.lower()).strip("-")
    return f"tgcg-members-and-exco-{slug}-{made_at:%Y-%m-%d}.pdf"
