"""Generic rendering extracted from the tested controlled document pipeline.

No remote content is fetched. Raw HTML is escaped. Both outputs use one source.
"""

from __future__ import annotations

import hashlib
import html
import re
import textwrap
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from memoryking.reporting.document import blocks, plain


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inline(value: str, pdf: bool = False) -> str:
    value = html.escape(value)
    value = re.sub(r"\[([^]]+)\]\(([^)]+)\)", lambda m: m[1], value)
    value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>" if pdf else r"<strong>\1</strong>", value)
    value = re.sub(
        r"`([^`]+)`",
        r'<font name="Courier" size="8.4">\1</font>' if pdf else r"<code>\1</code>",
        value,
    )
    return value


def figure(source_path: Path, reference: str) -> tuple[Path, int, int]:
    """Only bounded local raster evidence under the report directory; no network/SVG."""
    from PIL import Image

    relative = Path(reference)
    base = Path(source_path).resolve().parent
    path = base / relative
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or ":" in reference
        or relative.suffix.lower() not in {".png", ".jpg", ".jpeg"}
        or not path.resolve().is_relative_to(base)
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > 8 * 1024 * 1024
    ):
        raise ValueError("Figure must be a bounded local PNG/JPEG under the report directory")
    with Image.open(path) as image:
        width, height = image.size
        if image.format not in {"PNG", "JPEG"} or width * height > 12_000_000:
            raise ValueError("Figure encoding or dimensions exceed limits")
        image.verify()
    return path, width, height


def figure_inputs(source_path: Path) -> dict[str, str]:
    return {
        reference: digest(figure(source_path, reference)[0])
        for kind, data in blocks(Path(source_path).read_text())
        if kind == "figure"
        for _caption, reference in [data]
    }


def render_pdf(
    source_path: Path, output: Path, title: str | None = None, subtitle: str = "Structured report"
) -> str:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        BaseDocTemplate,
        Frame,
        Image,
        KeepTogether,
        PageBreak,
        PageTemplate,
        Paragraph,
        Preformatted,
        Spacer,
        Table,
        TableStyle,
    )
    from reportlab.platypus.tableofcontents import TableOfContents

    source_path, output = Path(source_path), Path(output)
    source = source_path.read_text()
    title = title or plain(source.splitlines()[0].lstrip("# "))
    font = "Helvetica"
    font_dir = Path("/System/Library/Fonts/Supplemental")
    if (font_dir / "Arial.ttf").exists():
        for name, filename in [
            ("ReportArial", "Arial.ttf"),
            ("ReportArial-Bold", "Arial Bold.ttf"),
            ("ReportArial-Italic", "Arial Italic.ttf"),
            ("ReportArial-BoldItalic", "Arial Bold Italic.ttf"),
        ]:
            pdfmetrics.registerFont(TTFont(name, str(font_dir / filename)))
        pdfmetrics.registerFontFamily(
            "ReportArial",
            normal="ReportArial",
            bold="ReportArial-Bold",
            italic="ReportArial-Italic",
            boldItalic="ReportArial-BoldItalic",
        )
        font = "ReportArial"
    navy, teal = colors.HexColor("#17304A"), colors.HexColor("#007F83")
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="BodyReport",
            fontName=font,
            fontSize=10.2,
            leading=14.6,
            spaceAfter=8,
            textColor=navy,
            splitLongWords=True,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SectionReport",
            fontName=font,
            fontSize=17,
            leading=21,
            textColor=navy,
            spaceBefore=16,
            spaceAfter=10,
            keepWithNext=True,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SubReport",
            fontName=font,
            fontSize=12.5,
            leading=16,
            textColor=teal,
            spaceBefore=12,
            spaceAfter=7,
            keepWithNext=True,
        )
    )
    styles.add(
        ParagraphStyle(
            name="TableReport",
            fontName=font,
            fontSize=8.7,
            leading=11.7,
            spaceAfter=0,
            textColor=navy,
            splitLongWords=True,
        )
    )
    styles.add(
        ParagraphStyle(name="TableHeadReport", parent=styles["TableReport"], textColor=colors.white)
    )
    styles.add(
        ParagraphStyle(
            name="CoverReport",
            fontName=font,
            fontSize=32,
            leading=38,
            textColor=navy,
            spaceAfter=22,
        )
    )
    styles.add(
        ParagraphStyle(
            name="CodeReport",
            fontName="Courier",
            fontSize=8,
            leading=10.6,
            spaceBefore=6,
            spaceAfter=10,
            backColor=colors.HexColor("#F0F4F7"),
            borderPadding=7,
        )
    )
    styles.add(
        ParagraphStyle(
            name="TOCReport",
            fontName=font,
            fontSize=10.2,
            leading=16,
            leftIndent=0,
            firstLineIndent=0,
            spaceBefore=3,
        )
    )
    styles.add(
        ParagraphStyle(
            name="TOCSubReport",
            parent=styles["TOCReport"],
            fontSize=9,
            leftIndent=12,
            spaceBefore=1,
        )
    )
    width, height = A4
    margin = 47

    class ReportDoc(BaseDocTemplate):  # type: ignore[misc]  # Untyped ReportLab base.
        def afterFlowable(self, flowable: Any) -> None:
            if isinstance(flowable, Paragraph) and hasattr(flowable, "report_key"):
                self.canv.bookmarkPage(flowable.report_key)
                self.canv.addOutlineEntry(
                    flowable.getPlainText(),
                    flowable.report_key,
                    level=flowable.report_level,
                    closed=False,
                )
                self.notify(
                    "TOCEntry",
                    (
                        flowable.report_level,
                        flowable.getPlainText(),
                        self.page,
                        flowable.report_key,
                    ),
                )

    def decorate(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setTitle(title)
        canvas.setAuthor("MemoryKing project")
        canvas.setSubject(subtitle + "; source SHA256 " + digest(source_path))
        canvas.setStrokeColor(teal)
        canvas.setLineWidth(1)
        canvas.line(margin, height - 30, width - margin, height - 30)
        canvas.setFont(font, 8)
        canvas.setFillColor(navy)
        canvas.drawString(margin, height - 24, title)
        canvas.drawString(margin, 25, subtitle)
        canvas.drawRightString(width - margin, 25, str(doc.page))
        canvas.restoreState()

    output.parent.mkdir(parents=True, exist_ok=True)
    doc = ReportDoc(
        str(output),
        pagesize=A4,
        topMargin=48,
        bottomMargin=46,
        leftMargin=margin,
        rightMargin=margin,
        invariant=1,
    )
    doc.addPageTemplates(
        PageTemplate(
            id="report",
            frames=[
                Frame(
                    margin,
                    46,
                    width - 2 * margin,
                    height - 94,
                    leftPadding=0,
                    rightPadding=0,
                    topPadding=0,
                    bottomPadding=0,
                )
            ],
            onPage=decorate,
        )
    )
    story = [
        Spacer(1, 95),
        Paragraph(title, styles["CoverReport"]),
        Paragraph(subtitle, styles["SubReport"]),
        Spacer(1, 20),
        Paragraph(
            "Immutable report artifact",
            styles["BodyReport"],
        ),
        Paragraph("Complete source: " + html.escape(source_path.name), styles["BodyReport"]),
        Paragraph("Source SHA256: " + digest(source_path), styles["TableReport"]),
        Spacer(1, 32),
        Paragraph(
            "This document preserves the complete narrative, requirements, and evidence obligations from its editable source.",
            styles["BodyReport"],
        ),
        PageBreak(),
        Paragraph("Contents", styles["SectionReport"]),
    ]
    toc = TableOfContents()
    toc.levelStyles = [styles["TOCReport"], styles["TOCSubReport"]]
    story.extend([toc, PageBreak()])
    counter = 0
    for kind, data in blocks(source):
        if kind == "heading":
            level, text = data
            if level == 1:
                continue
            p = Paragraph(
                inline(text, True), styles["SectionReport" if level == 2 else "SubReport"]
            )
            p.report_key = f"section-{counter}"
            p.report_level = 0 if level == 2 else 1
            counter += 1
            story.append(p)
        elif kind == "paragraph":
            story.append(Paragraph(inline(data, True), styles["BodyReport"]))
        elif kind == "item":
            story.append(Paragraph(inline(data, True), styles["BodyReport"]))
        elif kind == "code":
            wrapped = "\n".join(
                "\n".join(textwrap.wrap(line, 95, replace_whitespace=False, drop_whitespace=False))
                if len(line) > 95
                else line
                for line in data.splitlines()
            )
            story.append(Preformatted(wrapped, styles["CodeReport"]))
        elif kind == "figure":
            caption, reference = data
            path, iw, ih = figure(source_path, reference)
            scale = min((width - 2 * margin) / iw, 490 / ih)
            story.append(
                KeepTogether(
                    [
                        Image(str(path), width=iw * scale, height=ih * scale),
                        Paragraph(inline(caption, True), styles["TableReport"]),
                        Spacer(1, 12),
                    ]
                )
            )
        elif kind == "table":
            n = len(data[0])
            available = width - 2 * margin
            ratios = {2: [0.27, 0.73], 3: [0.23, 0.52, 0.25], 4: [0.14, 0.43, 0.27, 0.16]}.get(
                n, [1 / n] * n
            )
            if n == 3 and data[0][0] == "Gate":
                ratios = [0.22, 0.20, 0.58]
            if n == 3 and data[0][0] == "Requirement":
                ratios = [0.24, 0.22, 0.54]
            rows = [
                [
                    Paragraph(
                        inline(cell, True), styles["TableHeadReport" if i == 0 else "TableReport"]
                    )
                    for cell in row
                ]
                for i, row in enumerate(data)
            ]
            t = Table(rows, colWidths=[available * r for r in ratios], repeatRows=1, hAlign="LEFT")
            t.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), navy),
                        (
                            "ROWBACKGROUNDS",
                            (0, 1),
                            (-1, -1),
                            [colors.white, colors.HexColor("#EFF5F7")],
                        ),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 7),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                        ("TOPPADDING", (0, 0), (-1, -1), 7),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                        ("LINEBELOW", (0, 0), (-1, 0), 1, teal),
                    ]
                )
            )
            story.extend([t, Spacer(1, 10)])
    doc.multiBuild(story)
    return digest(output)


def render_epub(source_path: Path, output: Path, title: str | None = None) -> str:
    source_path, output = Path(source_path), Path(output)
    source = source_path.read_text()
    title = title or plain(source.splitlines()[0].lstrip("# "))
    sections = []
    nav = []
    images = {}
    counter = 0
    for kind, data in blocks(source):
        if kind == "heading":
            level, text = data
            counter += 1
            key = f"s{counter}"
            sections.append(f'<h{level} id="{key}">{inline(text)}</h{level}>')
            if level <= 3:
                nav.append(f'<li><a href="report.xhtml#{key}">{html.escape(plain(text))}</a></li>')
        elif kind == "paragraph":
            sections.append(f"<p>{inline(data)}</p>")
        elif kind == "item":
            sections.append(f'<p class="item">{inline(data)}</p>')
        elif kind == "code":
            sections.append("<pre><code>" + html.escape(data) + "</code></pre>")
        elif kind == "figure":
            caption, reference = data
            path, _width, _height = figure(source_path, reference)
            name = "images/" + digest(path) + path.suffix.lower()
            images[name] = path.read_bytes()
            sections.append(
                f'<figure><img src="{name}" alt="{html.escape(caption, quote=True)}"/><figcaption>{inline(caption)}</figcaption></figure>'
            )
        elif kind == "table":
            # Reflow wide tables into labeled rows, preserving every cell.
            headers = data[0]
            for row in data[1:]:
                sections.append(
                    '<div class="record">'
                    + "".join(
                        f"<p><strong>{inline(h)}:</strong> {inline(v)}</p>"
                        for h, v in zip(headers, row)
                    )
                    + "</div>"
                )

    def xhtml(body: str, navdoc: bool = False) -> str:
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en"><head><title>'
            + html.escape(title)
            + '</title><link rel="stylesheet" type="text/css" href="style.css"/></head><body>'
            + body
            + "</body></html>"
        )

    ident = "urn:sha256:" + digest(source_path)
    modified = datetime.fromtimestamp(source_path.stat().st_mtime, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    package = f"""<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">{ident}</dc:identifier><dc:title>{html.escape(title)}</dc:title><dc:language>en</dc:language><dc:creator>MemoryKing project</dc:creator><meta property="dcterms:modified">{modified}</meta></metadata><manifest><item id="report" href="report.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="css" href="style.css" media-type="text/css"/></manifest><spine><itemref idref="report"/></spine></package>"""
    container = '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
    image_manifest = "".join(
        f'<item id="image-{index}" href="{name}" media-type="{media}"/>'
        for index, name in enumerate(images)
        for media in ["image/png" if name.endswith(".png") else "image/jpeg"]
    )
    package = package.replace("</manifest>", image_manifest + "</manifest>")
    css = "body {font-family:serif;line-height:1.4;margin:0 4%;overflow-wrap:anywhere;word-wrap:break-word;} h1,h2,h3 {font-family:sans-serif;page-break-after:avoid;} h2 {page-break-before:always;} pre {font-size:0.8em;white-space:pre-wrap;overflow-wrap:anywhere;} code {font-family:monospace;} .record {border-top:1px solid #aaa;margin:1em 0;padding-top:0.4em;} .record p {margin:0.3em 0;} .item {margin-left:1em;}"
    css += " figure {margin:1em 0;} figure img {max-width:100%;height:auto;} figcaption {font-size:.85em;}"
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w") as z:

        def put(name: str, data: str | bytes, compression: int = zipfile.ZIP_DEFLATED) -> None:
            stamp = datetime.fromtimestamp(source_path.stat().st_mtime, timezone.utc)
            date = (
                max(1980, min(2107, stamp.year)),
                stamp.month,
                stamp.day,
                stamp.hour,
                stamp.minute,
                stamp.second,
            )
            info = zipfile.ZipInfo(name, date_time=date)
            info.compress_type = compression
            z.writestr(info, data)

        put("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        put("META-INF/container.xml", container)
        put("OEBPS/package.opf", package)
        put("OEBPS/report.xhtml", xhtml("".join(sections)))
        put(
            "OEBPS/nav.xhtml",
            xhtml(
                '<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>' + "".join(nav) + "</ol></nav>"
            ),
        )
        put("OEBPS/style.css", css)
        for name, raw in images.items():
            put("OEBPS/" + name, raw)
    validate_epub(output)
    return digest(output)


def validate_epub(path: Path) -> None:
    with zipfile.ZipFile(path) as z:
        assert z.namelist()[0] == "mimetype"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip"
        assert z.testzip() is None
        for name in [
            "META-INF/container.xml",
            "OEBPS/package.opf",
            "OEBPS/report.xhtml",
            "OEBPS/nav.xhtml",
        ]:
            ET.fromstring(z.read(name))
        body = ET.fromstring(z.read("OEBPS/report.xhtml"))
        manifest = ET.fromstring(z.read("OEBPS/package.opf"))
        assets = {el.attrib.get("href") for el in manifest.iter() if el.tag.endswith("}item")}
        for el in body.iter():
            if el.tag.endswith("}img"):
                assert el.attrib["src"] in assets
                assert "OEBPS/" + el.attrib["src"] in z.namelist()
                assert el.attrib.get("alt")
        ids = {el.attrib["id"] for el in body.iter() if "id" in el.attrib}
        nav = ET.fromstring(z.read("OEBPS/nav.xhtml"))
        for el in nav.iter():
            href = el.attrib.get("href", "")
            if "#" in href:
                assert href.split("#")[1] in ids
