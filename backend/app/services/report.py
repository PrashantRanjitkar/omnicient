"""The investigation as a short PDF anyone can read.

Written for a reader who has never seen Omnicient: no scores, no weights, no
evidence tables. Every account goes into one of three plain groups -
strongly linked, possibly linked, same name only - with a one-line reason in
everyday words, followed by anything else the search turned up and a short
note on how to read it. The JSON export holds the full record.

Built from a template, never written by a model: every line is fixed wording
or a value stored in the investigation, so the report cannot claim more than
the evidence does. And because a PDF travels without the app around it, the
"this is not proof" caveat is on every page.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Circle, Drawing, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    CondPageBreak,
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from ..config import ScoringConfig, get_settings
from ..models.enums import (
    AnalystStatus,
    ConfidenceLevel,
    EntityType,
    EntityVerdict,
    EvidenceType,
    RelationshipType,
)
from ..schemas.entity import EntityRead
from ..schemas.investigation import InvestigationExport
from ..schemas.relationship import RelationshipRead
from ..schemas.results import SourceResults
from ..utils.activity import ActivityStatus

CAVEAT = "Not proof of identity - based on public information only."

#: The three groups an account can fall into, by its strongest link.
STRONG = (ConfidenceLevel.VERY_HIGH, ConfidenceLevel.HIGH)
POSSIBLE = (ConfidenceLevel.MEDIUM,)

#: Each kind of evidence, as a person would say it.
PLAIN_REASON: dict[str, str] = {
    EvidenceType.EXPLICIT_LINK: "one profile links directly to the other",
    EvidenceType.SHARED_EMAIL: "same email address",
    EvidenceType.SAME_AVATAR: "same profile photo",
    EvidenceType.SAME_WEBSITE: "same personal website",
    EvidenceType.SIMILAR_BIO: "very similar profile description",
    EvidenceType.SHARED_ORGANIZATION: "same workplace or organisation",
    EvidenceType.SAME_DISPLAY_NAME: "same display name",
    EvidenceType.SAME_USERNAME: "same username",
    EvidenceType.SIMILAR_USERNAME: "similar username",
    EvidenceType.USERNAME_TRANSFORMATION: "a small variation of the same username",
    EvidenceType.SHARED_ROOT_TOKEN: "username shares a distinctive part",
    EvidenceType.ANALYST_ASSERTION: "linked by the person reviewing this report",
}

GREEN = colors.HexColor("#1f8a4c")
AMBER = colors.HexColor("#c07d0a")
GREY = colors.HexColor("#8a8f98")
INK = colors.HexColor("#1b1e24")
DIM = colors.HexColor("#5b616b")
LINE = colors.HexColor("#d9dce1")
PANEL = colors.HexColor("#f3f4f6")
NIGHT = colors.HexColor("#11151c")
ACCENT = colors.HexColor("#22b8cf")
LINK = "#1d63c4"

PAGE_W, PAGE_H = A4
MARGIN = 20 * mm
WIDTH = PAGE_W - 2 * MARGIN
COVER_BAND = 46 * mm

HOW_TO_READ = [
    "<b>Strongly linked</b> accounts have clear evidence connecting them to the "
    "search - most often, one profile links directly to the other. They are "
    "likely, but not certainly, the same person or organisation.",
    "<b>Possibly linked</b> accounts share some details, such as a photo or a "
    "description, but not enough to be sure.",
    "<b>Same name only</b> accounts just use the same or a similar username. "
    "Many people share usernames, so most of these may be strangers.",
    "Only public pages were read, without logging in. Private or deleted "
    "accounts are not included, and profiles may have changed since.",
    "This report shows evidence, not conclusions. Use it fairly, lawfully and "
    "only about people you have a legitimate reason to look into.",
]


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

#: Searched in order. A TrueType font is needed for anything outside
#: Latin-1 - Helvetica, the fallback, prints a name like "Ž" as a box.
FONT_CANDIDATES = [
    (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ),
    ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    (
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ),
    (
        "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    ),
]


def _fonts() -> tuple[str, str]:
    """Register a Unicode font once; fall back to the built-in Helvetica."""
    if "ReportSans" in pdfmetrics.getRegisteredFontNames():
        return "ReportSans", "ReportSans-Bold"
    for regular, bold in FONT_CANDIDATES:
        if Path(regular).is_file() and Path(bold).is_file():
            pdfmetrics.registerFont(TTFont("ReportSans", regular))
            pdfmetrics.registerFont(TTFont("ReportSans-Bold", bold))
            return "ReportSans", "ReportSans-Bold"
    return "Helvetica", "Helvetica-Bold"


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _hex(color: colors.Color) -> str:
    return "#" + color.hexval()[2:]


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _seed_title(export: InvestigationExport) -> str:
    """What was searched, as the analyst would write it: ``@mrbeast``."""
    investigation = export.investigation
    text = investigation.seed_input or str(export.seed.get("identifier", ""))
    if investigation.seed_type.upper() == "USERNAME" and not text.startswith("@"):
        text = f"@{text}"
    return text


# ---------------------------------------------------------------------------
# Profile pictures
# ---------------------------------------------------------------------------

#: How long the whole set of pictures may take before the report goes ahead
#: without the stragglers.
AVATAR_BUDGET_SECONDS = 15.0
AVATAR_PIXELS = 128
#: Colours for the initial drawn when an account has no picture.
FALLBACK_COLORS = ["#3b6fb6", "#2f8a6a", "#9a5bb5", "#c0663a", "#4a7f8c", "#8a6d3b"]


def circle_png(data: bytes) -> bytes | None:
    """A profile picture cropped to a circle, as a small PNG. None if unreadable."""
    try:
        from PIL import Image as PILImage
        from PIL import ImageDraw

        with PILImage.open(BytesIO(data)) as source:
            source.seek(0)
            picture = source.convert("RGBA")
        side = min(picture.size)
        left = (picture.width - side) // 2
        top = (picture.height - side) // 2
        picture = picture.crop((left, top, left + side, top + side)).resize(
            (AVATAR_PIXELS, AVATAR_PIXELS), PILImage.LANCZOS
        )
        mask = PILImage.new("L", picture.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, AVATAR_PIXELS - 1, AVATAR_PIXELS - 1), fill=255)
        picture.putalpha(mask)
        out = BytesIO()
        picture.save(out, "PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001 - a broken picture just falls back
        return None


def fetch_avatars(urls: list[str]) -> dict[str, bytes]:
    """Download and circle-crop profile pictures for the report.

    Through the crawler's own fetcher, so the SSRF guard and size limit apply
    to every picture. Each failure is silent - the row gets an initial - and
    the whole set is cut off after :data:`AVATAR_BUDGET_SECONDS`.
    """
    from ..sources.base import SafeFetcher

    unique = list(dict.fromkeys(u for u in urls if u))
    if not unique:
        return {}

    async def run() -> dict[str, bytes]:
        fetcher = SafeFetcher(replace(get_settings(), request_delay=0.2))
        found: dict[str, bytes] = {}

        async def one(url: str) -> None:
            try:
                image = circle_png(await fetcher.get_bytes(url))
            except Exception:  # noqa: BLE001
                return
            if image:
                found[url] = image

        try:
            await asyncio.wait_for(
                asyncio.gather(*(one(url) for url in unique)), AVATAR_BUDGET_SECONDS
            )
        except TimeoutError:
            pass
        finally:
            await fetcher.aclose()
        return found

    return asyncio.run(run())


class _Report:
    def __init__(
        self,
        export: InvestigationExport,
        results: SourceResults | None,
        scoring: ScoringConfig | None = None,
        mask: bool = False,
        avatars: dict[str, bytes] | None = None,
    ) -> None:
        self.export = export
        self.avatars = avatars or {}
        self.results = results
        self.mask = mask
        self.entities = {entity.id: entity for entity in export.entities}
        self.evidence: dict[str, list] = {}
        for item in export.evidence:
            if item.relationship_id:
                self.evidence.setdefault(item.relationship_id, []).append(item)
        self.ruled_out = {
            e.id for e in export.entities
            if e.analyst_verdict == EntityVerdict.DIFFERENT_IDENTITY
        }
        # Commit-metadata addresses and their on-screen masks.
        self.masks = [
            (e.identifier, e.name) for e in export.entities if e.metadata.get("exposed_by")
        ]
        # A "potential alias" says two handles look alike - which, to this
        # reader, is the same-name case, not evidence. It scored 90 for
        # mrbeast / mr-beast and put a dash in a name above real links.
        live = [
            r for r in export.relationships
            if r.relationship_type != RelationshipType.POTENTIAL_ALIAS
            and r.analyst_status != AnalystStatus.REJECTED
            and r.source_entity_id not in self.ruled_out
            and r.target_entity_id not in self.ruled_out
        ]
        # Each entity's strongest standing link.
        self.best: dict[str, RelationshipRead] = {}
        for relationship in live:
            for end in (relationship.source_entity_id, relationship.target_entity_id):
                held = self.best.get(end)
                if held is None or relationship.confidence_score > held.confidence_score:
                    self.best[end] = relationship

        regular, bold = _fonts()
        self.font, self.bold = regular, bold
        base = ParagraphStyle("base", fontName=regular, fontSize=10, leading=14.5,
                              textColor=INK, alignment=TA_LEFT)
        self.styles = {
            "base": base,
            "small": ParagraphStyle("small", parent=base, fontSize=8.5, leading=12,
                                    textColor=DIM),
            "cell": ParagraphStyle("cell", parent=base, fontSize=9, leading=12.5),
            "head": ParagraphStyle("head", parent=base, fontSize=8, leading=10,
                                   textColor=DIM),
            "h1": ParagraphStyle("h1", parent=base, fontName=bold, fontSize=13.5,
                                 leading=18, spaceBefore=14, spaceAfter=2),
            "answer": ParagraphStyle("answer", parent=base, fontSize=11, leading=16),
            "tile_n": ParagraphStyle("tile_n", parent=base, fontSize=20, leading=24,
                                     alignment=TA_CENTER),
            "tile_l": ParagraphStyle("tile_l", parent=base, fontSize=8, leading=10,
                                     textColor=DIM, alignment=TA_CENTER),
            "bullet": ParagraphStyle("bullet", parent=base, leftIndent=11,
                                     firstLineIndent=-11, spaceAfter=5),
        }

    # -- helpers -----------------------------------------------------------

    def clean(self, value: object) -> str:
        text = "" if value is None else str(value)
        for full, masked in self.masks:
            if self.mask:
                text = re.sub(re.escape(full), masked, text, flags=re.IGNORECASE)
            elif masked != full:
                text = text.replace(masked, full)
        return escape(text)

    def p(self, text: str, style: str = "base") -> Paragraph:
        return Paragraph(text, self.styles[style])

    def name_of(self, entity: EntityRead) -> str:
        if entity.metadata.get("exposed_by") and not self.mask:
            return entity.identifier
        return entity.name

    def account(self, entity: EntityRead | None) -> str:
        """``Instagram @mrbeast``, the handle a link when there is a page."""
        if entity is None:
            return "another account"
        name = escape(self.name_of(entity))
        if entity.url and entity.type != EntityType.EMAIL:
            href = escape(entity.url, {'"': "&quot;"})
            name = f'<a href="{href}" color="{LINK}">{name}</a>'
        return f"{escape(entity.platform_name)} <b>{name}</b>"

    def avatar(self, entity: EntityRead | None, size: float = 10 * mm):
        """The account's picture in a circle, or its initial on a colour."""
        data = self.avatars.get(entity.avatar_url or "") if entity else None
        if data:
            return Image(BytesIO(data), width=size, height=size, mask="auto")
        name = (entity.display_name or entity.name or "?") if entity else "?"
        letter = (re.sub(r"[^A-Za-z0-9]", "", name)[:1] or "?").upper()
        color = FALLBACK_COLORS[sum(map(ord, name)) % len(FALLBACK_COLORS)]
        drawing = Drawing(size, size)
        drawing.add(Circle(size / 2, size / 2, size / 2, fillColor=colors.HexColor(color),
                           strokeColor=None))
        drawing.add(String(size / 2, size * 0.31, letter, fontName=self.bold,
                           fontSize=size * 0.45, fillColor=colors.white, textAnchor="middle"))
        return drawing

    def account_cell(self, entity: EntityRead) -> Table:
        """Picture, then platform / handle / display name stacked beside it."""
        handle = escape(self.name_of(entity))
        if entity.url:
            href = escape(entity.url, {'"': "&quot;"})
            handle = f'<a href="{href}" color="{LINK}">{handle}</a>'
        lines = [f'<font size="7.5" color="{_hex(DIM)}">{escape(entity.platform_name)}</font>',
                 f"<b>{handle}</b>"]
        if entity.display_name and entity.display_name.lower() != entity.name.lstrip("@").lower():
            lines.append(f'<font size="8" color="{_hex(DIM)}">'
                         f"{escape(entity.display_name[:40])}</font>")
        cell = Table([[self.avatar(entity), self.p("<br/>".join(lines), "cell")]],
                     colWidths=[12 * mm, 46 * mm])
        cell.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        return cell

    def heading(self, title: str, color: colors.Color = INK, note: str = "") -> list:
        dot = f'<font color="{_hex(color)}">&#9679;</font>&#160; ' if color != INK else ""
        parts: list = [CondPageBreak(35 * mm), self.p(f"{dot}{escape(title)}", "h1")]
        if note:
            parts.append(self.p(note, "small"))
        parts.append(Spacer(1, 5))
        return parts

    def table(self, rows: list[list], widths: list[float]) -> Table:
        data = [[self.p(c, "head" if i == 0 else "cell") if isinstance(c, str) else c
                 for c in row]
                for i, row in enumerate(rows)]
        table = Table(data, colWidths=widths, repeatRows=1)
        table.setStyle(TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, INK),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ]
            + [("BACKGROUND", (0, r), (-1, r), PANEL) for r in range(2, len(rows), 2)]
        ))
        return table

    def reason(self, entity: EntityRead) -> str:
        """Why an account is in its group, in a short plain sentence."""
        relationship = self.best[entity.id]
        items = sorted(
            (e for e in self.evidence.get(relationship.id, []) if e.supports and e.weight >= 0),
            key=lambda e: -e.weight,
        )
        phrases: list[str] = []
        for item in items:
            phrase = PLAIN_REASON.get(item.type)
            if phrase and phrase not in phrases:
                phrases.append(phrase)
        if relationship.analyst_asserted:
            phrases.insert(0, PLAIN_REASON[EvidenceType.ANALYST_ASSERTION])
        text = ", ".join(phrases[:3]) or "related details on both profiles"
        text = text[0].upper() + text[1:]
        other_id = (relationship.target_entity_id
                    if relationship.source_entity_id == entity.id
                    else relationship.source_entity_id)
        other = self.entities.get(other_id)
        tail = ""
        if relationship.analyst_status == AnalystStatus.CONFIRMED:
            tail = f' <font color="{_hex(GREEN)}">(checked and confirmed)</font>'
        return f"{text} - with {self.account(other)}.{tail}"

    def grouped(self) -> tuple[list, list, list]:
        accounts = [
            e for e in self.export.entities
            if e.type == EntityType.ACCOUNT and e.id not in self.ruled_out
            and not e.is_seed and e.resolved
        ]
        strong, possible, name_only = [], [], []
        for entity in accounts:
            best = self.best.get(entity.id)
            level = best.confidence_level if best else None
            (strong if level in STRONG else possible if level in POSSIBLE
             else name_only).append(entity)
        key = lambda e: (-(self.best[e.id].confidence_score if e.id in self.best else 0),  # noqa: E731
                         e.platform_name.lower())
        return sorted(strong, key=key), sorted(possible, key=key), \
            sorted(name_only, key=lambda e: e.platform_name.lower())

    # -- sections ----------------------------------------------------------

    def build(self) -> list:
        strong, possible, name_only = self.grouped()
        story: list = [Spacer(1, COVER_BAND - 10 * mm)]
        story += self.short_answer(strong, possible, name_only)
        story += self.group("Strongly linked", GREEN, strong,
                            "Clear evidence connects these accounts to the search.")
        story += self.group("Possibly linked", AMBER, possible,
                            "Some shared details, but not enough to be sure.")
        story += self.name_only(name_only)
        story += self.other_findings()
        story += self.reviewed()
        story += self.how_to_read()
        return story

    def short_answer(self, strong: list, possible: list, name_only: list) -> list:
        seed = escape(_seed_title(self.export))
        total = len(strong) + len(possible) + len(name_only)
        if self.results is not None:
            searched = (f"We searched <b>{self.results.queried}</b> public websites for "
                        f"<b>{seed}</b> and found <b>{_count(total, 'account', 'accounts')}</b>.")
        else:
            searched = (f"We searched public websites for <b>{seed}</b> and found "
                        f"<b>{_count(total, 'account', 'accounts')}</b>.")
        lines = [searched]
        if strong:
            lines.append(f'<font color="{_hex(GREEN)}"><b>{len(strong)}</b></font> '
                         f"{'is' if len(strong) == 1 else 'are'} <b>strongly linked</b> - "
                         "likely the same person or organisation.")
        if possible:
            lines.append(f'<font color="{_hex(AMBER)}"><b>{len(possible)}</b></font> '
                         f"{'is' if len(possible) == 1 else 'are'} <b>possibly linked</b>.")
        if name_only:
            lines.append(f"<b>{len(name_only)}</b> only share the name and may belong "
                         "to other people.")
        if not total:
            lines.append("Nothing public could be connected to it.")
        box = Table([[self.p("<br/>".join(lines), "answer")]], colWidths=[WIDTH])
        box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), PANEL),
            ("LINEBEFORE", (0, 0), (0, -1), 3, ACCENT),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ]))
        tiles = [
            (len(strong), "strongly linked", GREEN),
            (len(possible), "possibly linked", AMBER),
            (len(name_only), "same name only", GREY),
        ]
        tile_row = Table(
            [[[self.p(f'<font color="{_hex(c)}"><b>{n}</b></font>', "tile_n"),
               self.p(label, "tile_l")]
              for n, label, c in tiles]],
            colWidths=[WIDTH / 3] * 3,
        )
        tile_row.setStyle(TableStyle(
            [("BOX", (i, 0), (i, 0), 0.5, LINE) for i in range(3)]
            + [("LINEABOVE", (i, 0), (i, 0), 2.5, c) for i, (_, _, c) in enumerate(tiles)]
            + [("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]
        ))
        parts: list = [self.p("The short answer", "h1"), Spacer(1, 4), box,
                       Spacer(1, 8), tile_row]
        faces = [e for e in strong if self.avatars.get(e.avatar_url or "")][:10]
        if faces:
            strip = Table([[self.avatar(e, 11 * mm) for e in faces]],
                          colWidths=[13 * mm] * len(faces), hAlign="LEFT")
            strip.setStyle(TableStyle([
                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ]))
            parts += [Spacer(1, 8),
                      self.p("Profile pictures of the strongly linked accounts:", "small"),
                      Spacer(1, 3), strip]
        if self.export.investigation.demo:
            parts += [Spacer(1, 4),
                      self.p("<b>Demo data:</b> every account here is made up.", "small")]
        return parts

    def group(self, title: str, color: colors.Color, entities: list, note: str) -> list:
        if not entities:
            return []
        rows = [["Account", "Why we think it is linked"]]
        rows += [[self.account_cell(e), self.reason(e)] for e in entities]
        return self.heading(f"{title} ({len(entities)})", color, note) + [
            self.table(rows, [58 * mm, WIDTH - 58 * mm])
        ]

    def name_only(self, entities: list) -> list:
        if not entities:
            return []
        listing = ", ".join(self.account(e) for e in entities)
        return self.heading(
            f"Same name only ({len(entities)})", GREY,
            "These accounts use the same or a similar username, and nothing else "
            "connects them. They may belong to other people.",
        ) + [self.p(listing, "cell")]

    def other_findings(self) -> list:
        lines: list[str] = []
        visible = [e for e in self.export.entities if e.id not in self.ruled_out]

        sites = [e for e in visible if e.type in (EntityType.WEBSITE, EntityType.DOMAIN)
                 and not (e.platform == "domain" and e.identifier in _WEBMAIL)]
        if sites:
            lines.append("<b>Websites:</b> " + ", ".join(
                f'<a href="{escape(e.url or "https://" + e.identifier, {chr(34): "&quot;"})}" '
                f'color="{LINK}">{escape(e.name)}</a>' for e in sites))

        emails = [e for e in visible if e.type == EntityType.EMAIL]
        for email in emails:
            where = ("found in the author details of their public code commits - "
                     "it is not shown on any profile"
                     if email.metadata.get("exposed_by") else "published on a profile")
            lines.append(f"<b>Email address:</b> {self.clean(self.name_of(email))} ({where}).")

        for entity in visible:
            zones = entity.metadata.get("commit_time_zones") or []
            if zones:
                zone = str(zones[0]).split(" on ")[0]
                lines.append(
                    f"<b>Time zone:</b> code commits on {self.account(entity)} were made "
                    f"in <b>{self.clean(zone)}</b> - a hint at where they are, not proof."
                )

        dormant = [e for e in visible if e.activity == ActivityStatus.DORMANT
                   and e.type == EntityType.ACCOUNT]
        if dormant:
            lines.append("<b>Inactive for over two years:</b> "
                         + ", ".join(self.account(e) for e in dormant) + ".")

        if not lines:
            return []
        return self.heading("Other things we found") + [
            self.p(f"&#8226;&#160; {line}", "bullet") for line in lines
        ]

    def reviewed(self) -> list:
        lines: list[str] = []
        for relationship in self.export.relationships:
            if relationship.analyst_status == AnalystStatus.UNREVIEWED:
                continue
            a = self.entities.get(relationship.source_entity_id)
            b = self.entities.get(relationship.target_entity_id)
            verdict = ("confirmed as linked"
                       if relationship.analyst_status == AnalystStatus.CONFIRMED
                       else "judged not linked")
            note = f' - "{self.clean(relationship.analyst_note)}"' \
                if relationship.analyst_note else ""
            lines.append(f"{self.account(a)} and {self.account(b)}: {verdict}{note}.")
        for entity_id in self.ruled_out:
            entity = self.entities[entity_id]
            note = f' - "{self.clean(entity.analyst_note)}"' if entity.analyst_note else ""
            lines.append(f"{self.account(entity)}: ruled out as a different person{note}. "
                         "Left out of this report.")
        if not lines:
            return []
        return self.heading("Checked by the reviewer") + [
            self.p(f"&#8226;&#160; {line}", "bullet") for line in lines
        ]

    def how_to_read(self) -> list:
        return self.heading("How to read this report") + [
            self.p(f"&#8226;&#160; {text}", "bullet") for text in HOW_TO_READ
        ]


#: A webmail provider's domain says nothing about anyone.
_WEBMAIL = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "icloud.com", "me.com", "proton.me", "protonmail.com", "aol.com",
    "gmx.com", "yandex.com", "mail.com", "zoho.com",
})


def _numbered_canvas(report: _Report, export: InvestigationExport) -> type[Canvas]:
    """Draws the title band, the footer and "Page X of Y" once pages are known."""
    seed = _seed_title(export)
    generated = export.generated_at.strftime("%d %B %Y")

    class NumberedCanvas(Canvas):
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            super().__init__(*args, **kwargs)
            self._pages: list[dict] = []

        def showPage(self) -> None:  # noqa: N802 - ReportLab API
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self.decorate(total)
                super().showPage()
            super().save()

        def decorate(self, total: int) -> None:
            page = self._pageNumber
            self.saveState()
            if page == 1:
                self.setFillColor(NIGHT)
                self.rect(0, PAGE_H - COVER_BAND, PAGE_W, COVER_BAND, stroke=0, fill=1)
                self.setFillColor(ACCENT)
                self.rect(0, PAGE_H - COVER_BAND, PAGE_W, 1.2 * mm, stroke=0, fill=1)
                self.setFont(report.bold, 8)
                self.drawString(MARGIN, PAGE_H - 13 * mm, "O M N I C I E N T")
                self.setFillColor(colors.white)
                self.setFont(report.bold, 22)
                self.drawString(MARGIN, PAGE_H - 27 * mm, f"Report on {seed}"[:44])
                self.setFillColor(colors.HexColor("#c9d1dc"))
                self.setFont(report.font, 9.5)
                self.drawString(MARGIN, PAGE_H - 35 * mm,
                                f"What public websites show about this name  ·  {generated}")
            else:
                self.setFont(report.font, 7.5)
                self.setFillColor(DIM)
                self.drawString(MARGIN, PAGE_H - 11 * mm, f"Report on {seed}")
                self.drawRightString(PAGE_W - MARGIN, PAGE_H - 11 * mm, generated)
            self.setStrokeColor(LINE)
            self.setLineWidth(0.4)
            self.line(MARGIN, 14 * mm, PAGE_W - MARGIN, 14 * mm)
            self.setFont(report.font, 7.5)
            self.setFillColor(DIM)
            self.drawString(MARGIN, 9.5 * mm, CAVEAT)
            self.drawRightString(PAGE_W - MARGIN, 9.5 * mm, f"Page {page} of {total}")
            self.restoreState()

    return NumberedCanvas


def render_report(
    export: InvestigationExport,
    results: SourceResults | None,
    scoring: ScoringConfig | None = None,
    *,
    mask: bool = False,
    pictures: bool = False,
) -> bytes:
    """The PDF bytes for one investigation.

    ``mask`` prints addresses found only in commit metadata as ``a***@x``.
    ``pictures`` downloads the profile pictures of the accounts the report
    lists in rows; off by default so a render never touches the network
    unless asked.
    """
    report = _Report(export, results, scoring, mask)
    if pictures:
        strong, possible, _ = report.grouped()
        report.avatars = fetch_avatars([e.avatar_url for e in strong + possible])
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=18 * mm, bottomMargin=20 * mm,
        title=f"Report on {_seed_title(export)}", author="Omnicient", subject=CAVEAT,
    )
    document.build(report.build(), canvasmaker=_numbered_canvas(report, export))
    return buffer.getvalue()
