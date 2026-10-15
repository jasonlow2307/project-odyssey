from __future__ import annotations

import html
import re
import textwrap
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    KeepTogether,
    ListFlowable,
    ListItem,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/MIGRATION_GUIDE.md"
OUTPUT = ROOT / "docs/MIGRATION_GUIDE.pdf"

PAGE_WIDTH, PAGE_HEIGHT = A4
LEFT = 19 * mm
RIGHT = 19 * mm
TOP = 20 * mm
BOTTOM = 18 * mm

def register_fonts() -> None:
    # Use ReportLab's built-in fonts so the helper is portable across NAS,
    # Linux, macOS, and container environments.
    return None


def inline_markup(text: str) -> str:
    """Convert a small, safe subset of Markdown inline syntax to ReportLab XML."""
    escaped = html.escape(text, quote=False)
    code_tokens: list[str] = []

    def save_code(match: re.Match[str]) -> str:
        code_tokens.append(f'<font name="Courier" color="#0B5E55">{match.group(1)}</font>')
        return f"\x00CODE{len(code_tokens) - 1}\x00"

    escaped = re.sub(r"`([^`]+)`", save_code, escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"__([^_]+)__", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", escaped)
    escaped = re.sub(r"(?<![\w_])_([^_]+)_(?![\w_])", r"<i>\1</i>", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<font color="#0B6E69"><u>\1</u></font>', escaped)

    for index, token in enumerate(code_tokens):
        escaped = escaped.replace(f"\x00CODE{index}\x00", token)
    return escaped


def clean_code_line(line: str) -> str:
    # A PDF-friendly substitute for a few box-drawing glyphs that can be absent
    # from some platform font versions. The diagram remains readable in mono.
    replacements = {
        "→": "->",
        "▼": "v",
        "┌": "+",
        "┐": "+",
        "└": "+",
        "┘": "+",
        "├": "+",
        "┤": "+",
        "┬": "+",
        "┴": "+",
        "┼": "+",
        "─": "-",
        "│": "|",
    }
    for old, new in replacements.items():
        line = line.replace(old, new)
    return line


class GuideDocTemplate(BaseDocTemplate):
    def __init__(self, filename: str, **kwargs: object) -> None:
        super().__init__(filename, **kwargs)
        frame = Frame(LEFT, BOTTOM, PAGE_WIDTH - LEFT - RIGHT, PAGE_HEIGHT - TOP - BOTTOM, id="normal")
        self.addPageTemplates([PageTemplate(id="guide", frames=frame, onPage=draw_page_chrome)])

    def afterFlowable(self, flowable: object) -> None:
        if isinstance(flowable, Paragraph) and getattr(flowable.style, "name", "").startswith("Heading"):
            level = int(flowable.style.name.replace("Heading", ""))
            text = re.sub(r"<[^>]+>", "", flowable.getPlainText())
            key = f"heading-{self.page}-{level}-{len(text)}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=max(0, level - 1), closed=False)
            self.notify("TOCEntry", (level, text, self.page, key))


def draw_page_chrome(canvas: object, document: GuideDocTemplate) -> None:
    page = canvas.getPageNumber()
    if page == 1:
        return
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D8E5E1"))
    canvas.setLineWidth(0.5)
    canvas.line(LEFT, PAGE_HEIGHT - 13 * mm, PAGE_WIDTH - RIGHT, PAGE_HEIGHT - 13 * mm)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#5D6B6A"))
    canvas.drawString(LEFT, PAGE_HEIGHT - 10 * mm, "Google Photos -> NAS / Immich migration")
    canvas.drawRightString(PAGE_WIDTH - RIGHT, 10 * mm, f"{page}")
    canvas.line(LEFT, 14 * mm, PAGE_WIDTH - RIGHT, 14 * mm)
    canvas.restoreState()


def make_styles() -> dict[str, ParagraphStyle]:
    styles = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "GuideTitle", parent=styles["Title"], fontName="Helvetica", fontSize=28,
            leading=33, textColor=colors.HexColor("#123E3A"), alignment=TA_LEFT,
            spaceAfter=12 * mm,
        ),
        "subtitle": ParagraphStyle(
            "GuideSubtitle", parent=styles["Normal"], fontName="Helvetica", fontSize=12,
            leading=18, textColor=colors.HexColor("#48615D"), spaceAfter=7 * mm,
        ),
        "meta": ParagraphStyle(
            "GuideMeta", parent=styles["Normal"], fontName="Helvetica", fontSize=9.5,
            leading=15, textColor=colors.HexColor("#5D6B6A"), spaceAfter=3 * mm,
        ),
        "h1": ParagraphStyle(
            "Heading1", parent=styles["Heading1"], fontName="Helvetica", fontSize=18,
            leading=23, textColor=colors.HexColor("#123E3A"), spaceBefore=9 * mm,
            spaceAfter=4 * mm, keepWithNext=True,
        ),
        "h2": ParagraphStyle(
            "Heading2", parent=styles["Heading2"], fontName="Helvetica", fontSize=13.5,
            leading=18, textColor=colors.HexColor("#0B6E69"), spaceBefore=6 * mm,
            spaceAfter=2.5 * mm, keepWithNext=True,
        ),
        "h3": ParagraphStyle(
            "Heading3", parent=styles["Heading3"], fontName="Helvetica", fontSize=11,
            leading=15, textColor=colors.HexColor("#405451"), spaceBefore=4 * mm,
            spaceAfter=2 * mm, keepWithNext=True,
        ),
        "contents_title": ParagraphStyle(
            "ContentsTitle", parent=styles["Heading1"], fontName="Helvetica", fontSize=18,
            leading=23, textColor=colors.HexColor("#123E3A"), spaceBefore=9 * mm,
            spaceAfter=4 * mm, keepWithNext=True,
        ),
        "body": ParagraphStyle(
            "Body", parent=styles["BodyText"], fontName="Helvetica", fontSize=9.1,
            leading=13.1, textColor=colors.HexColor("#253533"), spaceAfter=2.5 * mm,
        ),
        "small": ParagraphStyle(
            "Small", parent=styles["BodyText"], fontName="Helvetica", fontSize=8,
            leading=11.5, textColor=colors.HexColor("#5D6B6A"), spaceAfter=1.5 * mm,
        ),
        "bullet": ParagraphStyle(
            "Bullet", parent=styles["BodyText"], fontName="Helvetica", fontSize=8.9,
            leading=12.7, leftIndent=5 * mm, firstLineIndent=0, bulletIndent=1.5 * mm,
            textColor=colors.HexColor("#253533"), spaceAfter=1.2 * mm,
        ),
        "quote": ParagraphStyle(
            "Quote", parent=styles["BodyText"], fontName="Helvetica", fontSize=8.8,
            leading=12.8, leftIndent=6 * mm, rightIndent=4 * mm,
            textColor=colors.HexColor("#48615D"), borderColor=colors.HexColor("#8CC6BD"),
            borderWidth=0.8, borderPadding=4 * mm, spaceBefore=2 * mm, spaceAfter=3 * mm,
            backColor=colors.HexColor("#F0F8F6"),
        ),
        "code": ParagraphStyle(
            "Code", parent=styles["Code"], fontName="Courier", fontSize=6.8,
            leading=8.7, textColor=colors.HexColor("#EAF7F4"), backColor=colors.HexColor("#14322F"),
            borderColor=colors.HexColor("#275E57"), borderWidth=0.5, borderPadding=4 * mm,
            leftIndent=0, rightIndent=0, spaceBefore=2 * mm, spaceAfter=3 * mm,
        ),
        "toc1": ParagraphStyle(
            "TOC1", parent=styles["Normal"], fontName="Helvetica", fontSize=10,
            leading=15, leftIndent=0, textColor=colors.HexColor("#123E3A"), spaceAfter=2 * mm,
        ),
        "toc2": ParagraphStyle(
            "TOC2", parent=styles["Normal"], fontName="Helvetica", fontSize=8.8,
            leading=12, leftIndent=6 * mm, textColor=colors.HexColor("#48615D"), spaceAfter=1 * mm,
        ),
    }


def parse_table(lines: list[str], styles: dict[str, ParagraphStyle]) -> Table:
    rows: list[list[str]] = []
    for index, line in enumerate(lines):
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if index == 1 and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
            continue
        rows.append([inline_markup(cell) for cell in cells])
    if not rows:
        return Table([])
    count = max(len(row) for row in rows)
    for row in rows:
        row.extend([""] * (count - len(row)))
    data: list[list[Paragraph]] = []
    for row_index, row in enumerate(rows):
        style = styles["small"] if row_index else ParagraphStyle(
            "TableHeader", parent=styles["small"], fontName="Helvetica", fontSize=8,
            leading=10.5, textColor=colors.white,
        )
        data.append([Paragraph(cell, style) for cell in row])
    table = Table(data, repeatRows=1, hAlign="LEFT", splitByRow=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B6E69")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#F5FAF8")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#F5FAF8"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#C8DAD5")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
    ]))
    return table


def build_story(styles: dict[str, ParagraphStyle]) -> list[object]:
    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    story: list[object] = []
    story.extend([
        Spacer(1, 28 * mm),
        Paragraph("Google Photos <font color=\"#0B6E69\">-&gt;</font> NAS / Immich", styles["title"]),
        Paragraph("Public migration guide", styles["subtitle"]),
        HRFlowable(width="100%", thickness=2, color=colors.HexColor("#8CC6BD"), spaceAfter=7 * mm),
        Paragraph("A consolidated operational guide covering imports, shared-album reconstruction, remote downloads, recovery, storage, and lessons learned.", styles["body"]),
        Spacer(1, 8 * mm),
        Paragraph("Compiled: 2026-10-14", styles["meta"]),
        Paragraph("Environment: UGREEN-class NAS, Docker, Immich, multiple source libraries", styles["meta"]),
        Paragraph("Privacy: generic labels and placeholders are used throughout this public edition.", styles["meta"]),
        Spacer(1, 19 * mm),
        Table([[Paragraph("PUBLIC EDITION", styles["small"]), Paragraph("Review deployment-specific additions before publishing.", styles["small"])]], colWidths=[35 * mm, 120 * mm], style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#E8F5F1")),
            ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#8CC6BD")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
            ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
        ])),
        PageBreak(),
        Paragraph("Contents", styles["contents_title"]),
        Paragraph("Use the bookmarks panel in a PDF viewer for fast navigation.", styles["small"]),
        TableOfContents(),
        PageBreak(),
    ])

    index = 0
    in_code = False
    code_lines: list[str] = []
    paragraph_lines: list[str] = []
    list_items: list[str] = []
    table_lines: list[str] = []
    quote_lines: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph_lines
        if paragraph_lines:
            text = " ".join(part.strip() for part in paragraph_lines).strip()
            if text:
                story.append(Paragraph(inline_markup(text), styles["body"]))
            paragraph_lines = []

    def flush_list() -> None:
        nonlocal list_items
        if not list_items:
            return
        items = [ListItem(Paragraph(inline_markup(item), styles["bullet"]), leftIndent=4 * mm) for item in list_items]
        story.append(ListFlowable(items, bulletType="bullet", start="circle", bulletFontName="Helvetica", bulletFontSize=6, leftIndent=5 * mm))
        story.append(Spacer(1, 1.5 * mm))
        list_items = []

    def flush_table() -> None:
        nonlocal table_lines
        if table_lines:
            story.append(parse_table(table_lines, styles))
            story.append(Spacer(1, 2 * mm))
            table_lines = []

    def flush_quote() -> None:
        nonlocal quote_lines
        if quote_lines:
            text = "<br/>".join(inline_markup(line.lstrip("> ")) for line in quote_lines)
            story.append(Paragraph(text, styles["quote"]))
            quote_lines = []

    def add_code_block(lines_to_render: list[str]) -> None:
        wrapped: list[str] = []
        for line in lines_to_render:
            if not line:
                wrapped.append("")
                continue
            wrapped.extend(textwrap.wrap(line, width=96, replace_whitespace=False, drop_whitespace=False) or [""])
        code = Preformatted("\n".join(clean_code_line(line) for line in wrapped), styles["code"], maxLineLength=96)
        story.append(Table([[code]], colWidths=[PAGE_WIDTH - LEFT - RIGHT], hAlign="LEFT", style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#14322F")),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#275E57")),
            ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
        ])))
        story.append(Spacer(1, 3 * mm))

    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()

        if in_code:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                add_code_block(code_lines)
                code_lines = []
                in_code = False
            else:
                code_lines.append(raw.rstrip())
            index += 1
            continue

        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush_paragraph(); flush_list(); flush_table(); flush_quote()
            in_code = True
            index += 1
            continue

        if not stripped:
            flush_paragraph(); flush_list(); flush_table(); flush_quote()
            index += 1
            continue

        if re.match(r"^\|.*\|$", stripped) and index + 1 < len(lines) and re.match(r"^\|?\s*:?-{3,}", lines[index + 1].strip()):
            flush_paragraph(); flush_list(); flush_quote()
            table_lines = [stripped]
            index += 1
            while index < len(lines) and re.match(r"^\|.*\|$", lines[index].strip()):
                table_lines.append(lines[index].strip())
                index += 1
            flush_table()
            continue

        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            flush_paragraph(); flush_list(); flush_table(); flush_quote()
            level = min(len(heading.group(1)), 3)
            style = styles[f"h{level}"]
            story.append(Paragraph(inline_markup(heading.group(2)), style))
            index += 1
            continue

        if stripped.startswith(">"):
            flush_paragraph(); flush_list(); flush_table()
            quote_lines.append(stripped)
            index += 1
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote_lines.append(lines[index].strip())
                index += 1
            flush_quote()
            continue

        bullet = re.match(r"^\s*[-*+]\s+(.*)$", raw)
        numbered = re.match(r"^\s*\d+[.)]\s+(.*)$", raw)
        if bullet or numbered:
            flush_paragraph(); flush_table(); flush_quote()
            list_items.append((bullet or numbered).group(1))
            index += 1
            while index < len(lines):
                next_match = re.match(r"^\s*[-*+]\s+(.*)$", lines[index]) or re.match(r"^\s*\d+[.)]\s+(.*)$", lines[index])
                if not next_match:
                    break
                list_items.append(next_match.group(1))
                index += 1
            flush_list()
            continue

        if re.match(r"^\s*([-*_])(?:\s*\1){2,}\s*$", raw):
            flush_paragraph(); flush_list(); flush_table(); flush_quote()
            story.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#C8DAD5"), spaceBefore=2 * mm, spaceAfter=3 * mm))
            index += 1
            continue

        paragraph_lines.append(raw)
        index += 1

    flush_paragraph(); flush_list(); flush_table(); flush_quote()
    return story


def main() -> None:
    register_fonts()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    styles = make_styles()
    doc = GuideDocTemplate(
        str(OUTPUT), pagesize=A4, leftMargin=LEFT, rightMargin=RIGHT,
        topMargin=TOP, bottomMargin=BOTTOM, title="Google Photos to NAS / Immich migration - public guide",
        author="Codex",
    )
    story = build_story(styles)
    doc.multiBuild(story)
    print(OUTPUT)


if __name__ == "__main__":
    main()
