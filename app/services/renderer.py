"""The single, predefined certificate template - rendered to PDF with ReportLab.

The template is drawn in code (no external design file), A4 landscape:

    ┌───────────────────────────────────────────────┐
    │                 ISSUER NAME                   │
    │          CERTIFICATE OF COMPLETION            │
    │                 ───── ◆ ─────                 │
    │            This is to certify that            │
    │                RECIPIENT NAME                 │
    │          has successfully completed           │
    │                 COURSE NAME                   │
    │             (optional description)            │
    │                                               │
    │   date ____        ( SEAL )       ____ issuer │
    │        Certificate ID · verification URL      │
    └───────────────────────────────────────────────┘

A Unicode TTF font (DejaVu Serif, bundled in ``app/assets/fonts``) is used so
accented / Cyrillic / Greek names render correctly. Characters the font cannot
draw (e.g. CJK) raise ``UnsupportedCharactersError`` - the certificate is then
marked *failed* with a clear message rather than producing a PDF with blank boxes.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

NAVY = HexColor("#1F2A44")
GOLD = HexColor("#B8893A")
GOLD_LIGHT = HexColor("#E9D9B6")
INK = HexColor("#2E2E2E")
MUTED = HexColor("#6B6B6B")
PAPER = HexColor("#FBF7EE")

PAGE_W, PAGE_H = landscape(A4)  # 841.9 x 595.3 pt
CENTER_X = PAGE_W / 2


class RenderError(Exception):
    """Base class for certificate rendering problems."""


class UnsupportedCharactersError(RenderError):
    pass


@dataclass(frozen=True)
class CertificateData:
    recipient_name: str
    title: str
    course_name: str
    issuer_name: str
    issue_date: date
    verification_code: str
    verify_url: str
    description: str | None = None


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #
_FONTS = {"regular": "Times-Roman", "bold": "Times-Bold", "italic": "Times-Italic"}
_USING_TTF = False


def _register_fonts() -> None:
    """Register bundled TTF fonts once; fall back to built-in Times if missing."""
    global _USING_TTF
    files = {
        "regular": FONT_DIR / "DejaVuSerif.ttf",
        "bold": FONT_DIR / "DejaVuSerif-Bold.ttf",
        "italic": FONT_DIR / "DejaVuSerif-Italic.ttf",
    }
    if not all(p.exists() for p in files.values()):
        return
    for style, path in files.items():
        name = f"CertSerif-{style}"
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(path)))
        _FONTS[style] = name
    _USING_TTF = True


_register_fonts()


def _unsupported_chars(text: str) -> set[str]:
    if _USING_TTF:
        glyphs = pdfmetrics.getFont(_FONTS["bold"]).face.charToGlyph
        return {ch for ch in text if ord(ch) not in glyphs and not ch.isspace()}
    # Built-in Type1 fonts only cover cp1252.
    bad = set()
    for ch in text:
        try:
            ch.encode("cp1252")
        except UnicodeEncodeError:
            bad.add(ch)
    return bad


def check_renderable(data: CertificateData) -> None:
    for label, value in (
        ("recipient name", data.recipient_name),
        ("course name", data.course_name),
        ("issuer name", data.issuer_name),
        ("title", data.title),
        ("description", data.description or ""),
    ):
        bad = _unsupported_chars(value)
        if bad:
            chars = " ".join(sorted(bad))
            raise UnsupportedCharactersError(
                f"{label} contains characters the certificate font cannot render: {chars}"
            )


# --------------------------------------------------------------------------- #
# Drawing helpers
# --------------------------------------------------------------------------- #
def _fit_size(text: str, font: str, max_size: float, min_size: float, max_width: float) -> float:
    size = max_size
    while size > min_size and pdfmetrics.stringWidth(text, font, size) > max_width:
        size -= 1
    return size


def _draw_fitted(
    c: Canvas, text: str, y: float, font: str, max_size: float, min_size: float, max_width: float,
    color=INK, line_gap: float = 1.15,
) -> float:
    """Draw centred text, shrinking to fit; wraps onto 2 lines as a last resort.

    Returns the font size used (handy for positioning decorations).
    """
    size = _fit_size(text, font, max_size, min_size, max_width)
    c.setFillColor(color)
    if pdfmetrics.stringWidth(text, font, size) <= max_width:
        c.setFont(font, size)
        c.drawCentredString(CENTER_X, y, text)
        return size

    lines = simpleSplit(text, font, size, max_width)
    if len(lines) > 2:  # keep the layout intact; extremely long values are truncated
        lines = [lines[0], lines[1][:-1].rstrip() + "…"]
    c.setFont(font, size)
    # Keep the block vertically centred on the single-line baseline, nudged up
    # so the second line clears any decoration drawn underneath.
    last_baseline = y - size * 0.45
    first_baseline = last_baseline + (len(lines) - 1) * size * line_gap
    for i, line in enumerate(lines):
        c.drawCentredString(CENTER_X, first_baseline - i * size * line_gap, line)
    return size


def _draw_spaced(c: Canvas, text: str, y: float, font: str, size: float, spacing: float, color) -> None:
    """Centred text with letter-spacing (tracking)."""
    width = pdfmetrics.stringWidth(text, font, size) + spacing * (len(text) - 1)
    # Char spacing is part of the PDF graphics state, so isolate it - otherwise
    # it leaks into every string drawn afterwards.
    c.saveState()
    t = c.beginText(CENTER_X - width / 2, y)
    t.setFont(font, size)
    t.setCharSpace(spacing)
    t.setFillColor(color)
    t.textOut(text)
    c.drawText(t)
    c.restoreState()


def _diamond(c: Canvas, x: float, y: float, r: float) -> None:
    p = c.beginPath()
    p.moveTo(x, y + r)
    p.lineTo(x + r, y)
    p.lineTo(x, y - r)
    p.lineTo(x - r, y)
    p.close()
    c.drawPath(p, fill=1, stroke=0)


def _draw_frame(c: Canvas) -> None:
    c.setFillColor(PAPER)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

    # Heavy navy outer band.
    c.setStrokeColor(NAVY)
    c.setLineWidth(10)
    c.rect(20, 20, PAGE_W - 40, PAGE_H - 40)
    # Double gold inner rules.
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.6)
    c.rect(34, 34, PAGE_W - 68, PAGE_H - 68)
    c.setLineWidth(0.6)
    c.rect(40, 40, PAGE_W - 80, PAGE_H - 80)

    # Corner ornaments: gold L-brackets with a diamond.
    c.setFillColor(GOLD)
    c.setLineWidth(2)
    inset, arm = 52, 34
    for cx, cy, dx, dy in (
        (inset, inset, 1, 1),
        (PAGE_W - inset, inset, -1, 1),
        (inset, PAGE_H - inset, 1, -1),
        (PAGE_W - inset, PAGE_H - inset, -1, -1),
    ):
        c.line(cx, cy, cx + dx * arm, cy)
        c.line(cx, cy, cx, cy + dy * arm)
        _diamond(c, cx + dx * 9, cy + dy * 9, 4)


def _divider(c: Canvas, y: float, half_width: float = 70) -> None:
    c.setStrokeColor(GOLD)
    c.setFillColor(GOLD)
    c.setLineWidth(0.9)
    c.line(CENTER_X - half_width, y, CENTER_X - 10, y)
    c.line(CENTER_X + 10, y, CENTER_X + half_width, y)
    _diamond(c, CENTER_X, y, 4.5)


def _seal(c: Canvas, x: float, y: float) -> None:
    c.setFillColor(GOLD)
    c.circle(x, y, 40, fill=1, stroke=0)
    c.setStrokeColor(PAPER)
    c.setLineWidth(1.2)
    c.circle(x, y, 34, fill=0, stroke=1)
    c.setLineWidth(0.5)
    c.circle(x, y, 31, fill=0, stroke=1)
    c.setFillColor(PAPER)
    _diamond(c, x, y + 13, 4)
    c.setFont(_FONTS["bold"], 9)
    c.drawCentredString(x, y - 3, "CERTIFIED")
    c.setFont(_FONTS["regular"], 6.5)
    c.drawCentredString(x, y - 14, "AUTHENTIC")


def _signature_block(c: Canvas, x: float, y: float, value: str, label: str, value_font: str) -> None:
    width = 180
    c.setFillColor(INK)
    size = _fit_size(value, value_font, 14, 8, width)
    c.setFont(value_font, size)
    c.drawCentredString(x, y + 8, value)
    c.setStrokeColor(NAVY)
    c.setLineWidth(0.8)
    c.line(x - width / 2, y, x + width / 2, y)
    c.setFillColor(MUTED)
    c.setFont(_FONTS["regular"], 8.5)
    c.drawCentredString(x, y - 14, label.upper())


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def render_certificate(data: CertificateData, output_path: Path) -> Path:
    """Render one certificate PDF to ``output_path``.

    The PDF is written to a temporary file first and atomically moved into
    place, so a crash mid-render never leaves a half-written certificate.
    """
    check_renderable(data)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=output_path.parent, suffix=".pdf.tmp")
    os.close(fd)
    try:
        c = Canvas(tmp_name, pagesize=(PAGE_W, PAGE_H), pageCompression=1)
        c.setTitle(f"{data.title} - {data.recipient_name}")
        c.setAuthor(data.issuer_name)
        c.setSubject(data.course_name)
        c.setCreator("Bulk Certificate Generator")

        _draw_frame(c)

        # Header
        _draw_spaced(c, data.issuer_name.upper()[:60], 500, _FONTS["bold"], 11, 3, GOLD)
        title = data.title.upper()
        title_size = _fit_size(title, _FONTS["bold"], 34, 18, 600)
        _draw_spaced(c, title, 452, _FONTS["bold"], title_size, 2, NAVY)
        _divider(c, 432)

        # Body
        c.setFillColor(MUTED)
        c.setFont(_FONTS["italic"], 14)
        c.drawCentredString(CENTER_X, 395, "This is to certify that")

        _draw_fitted(c, data.recipient_name, 345, _FONTS["bold"], 40, 22, 620, color=NAVY)
        c.setStrokeColor(GOLD)
        c.setLineWidth(1)
        c.line(CENTER_X - 220, 325, CENTER_X + 220, 325)

        c.setFillColor(MUTED)
        c.setFont(_FONTS["italic"], 14)
        c.drawCentredString(CENTER_X, 295, "has successfully completed")

        _draw_fitted(c, data.course_name, 260, _FONTS["bold"], 22, 13, 600, color=INK)

        if data.description:
            c.setFillColor(MUTED)
            c.setFont(_FONTS["italic"], 10.5)
            for i, line in enumerate(simpleSplit(data.description, _FONTS["italic"], 10.5, 520)[:2]):
                c.drawCentredString(CENTER_X, 228 - i * 14, line)

        # Footer: date - seal - issuer
        _signature_block(
            c, 200, 128, data.issue_date.strftime("%d %B %Y"), "Date of issue", _FONTS["regular"]
        )
        _seal(c, CENTER_X, 132)
        _signature_block(c, PAGE_W - 200, 128, data.issuer_name, "Authorized by", _FONTS["italic"])

        c.setFillColor(MUTED)
        c.setFont(_FONTS["regular"], 7.5)
        c.drawCentredString(
            CENTER_X,
            56,
            f"Certificate ID: {data.verification_code}   ·   Verify at {data.verify_url}",
        )

        c.showPage()
        c.save()
        os.chmod(tmp_name, 0o644)  # mkstemp creates files as 0600
        os.replace(tmp_name, output_path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise
    return output_path
