"""Turn a PDF, HTML or .xlsx document into an ordered list of text blocks.

A block is a heading, a paragraph line, or one table row. Table rows are never split, and
each row remembers its table's header row, so a chunk that starts mid-table can repeat it.
"""

import io
import re
import statistics
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pdfplumber
from lxml import etree as lxml_etree  # type: ignore[import-untyped]
from lxml import html as lxml_html

_WS = re.compile(r"\s+")


@dataclass
class Block:
    text: str
    page: int | None = None
    is_heading: bool = False
    is_table_row: bool = False
    table_header: str | None = None


@dataclass
class ParsedDocument:
    title: str | None
    blocks: list[Block] = field(default_factory=list)


def _clean(text: str | None) -> str:
    return _WS.sub(" ", (text or "").replace(" ", " ")).strip()


def _row_text(cells: list[str | None]) -> str:
    return " | ".join(c for c in (_clean(cell) for cell in cells) if c)


# --- PDF ------------------------------------------------------------------------------------


def _inside(obj: dict[str, Any], bboxes: list[tuple[float, float, float, float]]) -> bool:
    x = (obj["x0"] + obj["x1"]) / 2
    y = (obj["top"] + obj["bottom"]) / 2
    return any(x0 <= x <= x1 and top <= y <= bottom for x0, top, x1, bottom in bboxes)


_FAQ_QUESTION = re.compile(r"^(?:Q\.?\s*)?\d{1,3}[.)]\s+\S.{5,200}\?$")


def _outside_tables(
    bboxes: list[tuple[float, float, float, float]],
) -> Callable[[dict[str, Any]], bool]:
    def keep(obj: dict[str, Any]) -> bool:
        return not _inside(obj, bboxes)

    return keep


def _looks_like_heading(text: str, size: float, body_size: float) -> bool:
    if _FAQ_QUESTION.match(text):  # "17) When will the investor get a statement?"
        return True
    if len(text) > 90 or len(text) < 3 or text.endswith((".", ",", ";")):
        return False
    letters = [c for c in text if c.isalpha()]
    mostly_upper = bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.8
    return size >= body_size * 1.15 or (mostly_upper and len(letters) >= 4)


def _column_gutter(page: Any) -> float | None:
    """x position of the gap between two text columns, or None for a single-column page.

    For each candidate x in the middle of the page, count the words that straddle it. A real
    gutter is crossed by almost no words; in single-column text most lines cross the middle.
    """
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    if len(words) < 40:
        return None
    line_tops = {round(w["top"]) for w in words}
    best_x, best_count = None, None
    x = page.width * 0.3
    while x <= page.width * 0.7:
        count = sum(1 for w in words if w["x0"] < x < w["x1"])
        if best_count is None or count < best_count:
            best_x, best_count = x, count
        x += 2.0
    if best_x is None or best_count is None or best_count > 0.15 * len(line_tops):
        return None
    left = sum(1 for w in words if w["x1"] <= best_x)
    right = sum(1 for w in words if w["x0"] >= best_x)
    return best_x if left >= 20 and right >= 20 else None


def _lines_by_column(page: Any, gutter: float | None) -> list[tuple[int, dict[str, Any]]]:
    """(column, line) pairs. Column 0 is the left or only column, 1 the right column."""
    if gutter is None:
        return [(0, ln) for ln in page.extract_text_lines(layout=False, strip=True)]
    pairs: list[tuple[int, dict[str, Any]]] = []
    for column, (x0, x1) in enumerate(((0, gutter), (gutter, page.width))):
        part = page.within_bbox((x0, 0, x1, page.height))
        pairs.extend((column, ln) for ln in part.extract_text_lines(layout=False, strip=True))
    return pairs


_MAX_FILL_CHARS = 400


def _fill_merged_down(table: Any) -> list[list[str | None]]:
    """Table rows, with vertically merged cells repeated on every row they cover.

    pdfplumber puts a merged cell's text on its first row only and leaves the covered cells
    empty (None). In a factsheet, one "Rs. 100 and any amount thereafter" cell can span 30
    scheme rows; without this, every scheme but the first loses its value. A cell is filled
    only when the cell above it really extends into this row, so horizontal merges and plain
    empty cells are left alone. Long cells are not repeated: when pdfplumber reads a page's
    whole layout as one table (factsheet portfolio pages), a "merged cell" holding the page's
    text would otherwise be copied onto every row.
    """
    extracted = table.extract()
    last: dict[int, tuple[float, str | None]] = {}  # column -> (bottom of last cell, its text)
    rows: list[list[str | None]] = []
    for row, cells in zip(table.rows, extracted, strict=False):
        filled = list(cells)
        top = row.bbox[1]
        for j, bbox in enumerate(row.cells):
            if j >= len(filled):
                break
            if bbox is not None:
                last[j] = (bbox[3], filled[j])
            elif j in last and last[j][0] > top + 1 and len(last[j][1] or "") <= _MAX_FILL_CHARS:
                filled[j] = last[j][1]
        rows.append(filled)
    return rows


def parse_pdf(content: bytes, keep_page: Any = None) -> ParsedDocument:
    """`keep_page(n)` (1-based) decides which pages to keep. Default: all."""
    blocks: list[Block] = []
    title: str | None = None
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        sizes = [
            c["size"] for page in pdf.pages[:5] for c in page.chars if c.get("text", "").strip()
        ]
        body_size = statistics.median(sizes) if sizes else 10.0
        for number, page in enumerate(pdf.pages, start=1):
            if keep_page is not None and not keep_page(number):
                continue
            tables = page.find_tables()
            bboxes = [t.bbox for t in tables]
            text_page = page.filter(_outside_tables(bboxes)) if bboxes else page
            gutter = _column_gutter(text_page)
            items: list[tuple[tuple[int, float], Block]] = []
            for column, line in _lines_by_column(text_page, gutter):
                text = _clean(line["text"])
                if not text:
                    continue
                sizes_in_line = [c["size"] for c in line.get("chars", []) if "size" in c]
                size = max(sizes_in_line) if sizes_in_line else body_size
                heading = _looks_like_heading(text, size, body_size)
                if heading and title is None and number <= 2:
                    title = text
                items.append(((column, line["top"]), Block(text, number, is_heading=heading)))
            for table in tables:
                rows = [_row_text(r) for r in _fill_merged_down(table) if r]
                rows = [r for r in rows if r]
                if not rows:
                    continue
                header = rows[0]
                x0, top = table.bbox[0], table.bbox[1]
                column = 1 if gutter is not None and x0 >= gutter else 0
                for i, row in enumerate(rows):
                    block = Block(row, number, is_table_row=True, table_header=header)
                    items.append(((column, top + i * 0.01), block))
            items.sort(key=lambda pair: pair[0])
            blocks.extend(block for _, block in items)
    return ParsedDocument(title, _drop_running_headers(blocks))


_PAGE_NUMBER = re.compile(r"^(?:page\s*)?[-–]?\s*\d{1,4}\s*[-–]?(?:\s*(?:of|/)\s*\d{1,4})?$", re.I)


def _edge_key(text: str) -> str:
    # Page numbers differ on every page but are the same running element.
    return "<page number>" if _PAGE_NUMBER.match(text.strip()) else text


def _drop_running_headers(blocks: list[Block]) -> list[Block]:
    """Remove page headers and footers: the same short line at the top or bottom of many pages."""
    pages: dict[int | None, list[Block]] = {}
    for block in blocks:
        if not block.is_table_row:
            pages.setdefault(block.page, []).append(block)
    if len(pages) < 3:
        return blocks
    edge_counts: dict[str, int] = {}
    for page_blocks in pages.values():
        depth = 2 if len(page_blocks) >= 6 else 1  # on short pages only the outer lines
        edges = page_blocks[:depth] + page_blocks[-depth:]
        for key in {_edge_key(b.text) for b in edges if len(b.text) <= 120}:
            edge_counts[key] = edge_counts.get(key, 0) + 1
    threshold = max(3, int(0.4 * len(pages)))
    running = {key for key, count in edge_counts.items() if count >= threshold}
    return [b for b in blocks if b.is_table_row or _edge_key(b.text) not in running]


# --- HTML -----------------------------------------------------------------------------------

_DROP_TAGS = {
    "script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg",
    "iframe", "button", "select",
}  # fmt: skip
# Whole class names only: "page-header-fixed" on <body> must not remove the page.
_DROP_CLASSES = {
    "header", "footer", "menu", "navbar", "nav", "breadcrumb", "breadcrumbs", "sidebar",
    "site-header", "site-footer", "top-menu", "mega-menu", "skip-link",
}  # fmt: skip
_CONTENT_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "dt", "dd", "td", "th"}


def _should_drop(el: Any) -> bool:
    tag = el.tag if isinstance(el.tag, str) else ""
    if tag in ("html", "body", "main"):
        return False
    if tag in _DROP_TAGS or el.get("role") == "navigation":
        return True
    classes = set((el.get("class") or "").split())
    return bool(classes & _DROP_CLASSES)


def parse_html(content: bytes) -> ParsedDocument:
    tree = lxml_html.fromstring(content)
    title_el = tree.find(".//title")
    title = _clean(title_el.text_content()) if title_el is not None else None
    for el in [e for e in tree.iter() if isinstance(e.tag, str) and _should_drop(e)]:
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)
    root = next((r for r in (tree.find(".//main"), tree.find(".//body")) if r is not None), tree)
    blocks: list[Block] = []
    seen: set[str] = set()
    header_for_table: dict[int, str] = {}
    for el in root.iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag not in _CONTENT_TAGS:
            continue
        # Cells are covered by their row.
        if tag in ("td", "th") and el.getparent() is not None and el.getparent().tag == "tr":
            continue
        if tag == "tr":
            cells = [_clean(c.text_content()) for c in el.findall("./td") + el.findall("./th")]
            text = _row_text(cells)  # type: ignore[arg-type]
            table = next(el.iterancestors("table"), None)
            key = id(table)
            if key not in header_for_table:
                header_for_table[key] = text
            block = Block(text, is_table_row=True, table_header=header_for_table[key])
        else:
            text = _clean(el.text_content())
            block = Block(text, is_heading=tag.startswith("h"))
        if not text or text in seen or len(text) < 3:
            continue
        seen.add(text)
        blocks.append(block)
    return ParsedDocument(title, blocks)


# --- Spreadsheet (.xlsx) ----------------------------------------------------------------------
# Read straight from the Office Open XML parts (a zip of XML files) with lxml, so no extra
# dependency is needed. Only the first worksheet is read.

_XLSX_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XLSX_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_REF = re.compile(r"([A-Z]+)(\d+)")
_DATE_FORMATS = ("%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y")


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _xml_text(el: Any) -> str:
    return "".join(t.text or "" for t in el.iter(f"{{{_XLSX_MAIN}}}t"))


def _number_text(value: str) -> str:
    try:
        return f"{float(value):.6g}"
    except ValueError:
        return value


def _xlsx_rows(content: bytes) -> list[list[str]]:
    """The first worksheet as a grid of cleaned strings. Merged cells repeat their value."""
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = set(zf.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            sst = lxml_etree.fromstring(zf.read("xl/sharedStrings.xml"))
            shared = [_xml_text(si) for si in sst.iter(f"{{{_XLSX_MAIN}}}si")]
        workbook = lxml_etree.fromstring(zf.read("xl/workbook.xml"))
        first = workbook.find(f".//{{{_XLSX_MAIN}}}sheet")
        sheet_path = "xl/worksheets/sheet1.xml"
        if first is not None and "xl/_rels/workbook.xml.rels" in names:
            rid = first.get(f"{{{_XLSX_REL}}}id")
            rels = lxml_etree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            for rel in rels.iter(f"{{{_PKG_REL}}}Relationship"):
                if rel.get("Id") == rid:
                    target = rel.get("Target", "")
                    sheet_path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
        sheet = lxml_etree.fromstring(zf.read(sheet_path))

    cells: dict[tuple[int, int], str] = {}
    for c in sheet.iter(f"{{{_XLSX_MAIN}}}c"):
        m = _CELL_REF.match(c.get("r", ""))
        if not m:
            continue
        kind = c.get("t")
        v = c.find(f"{{{_XLSX_MAIN}}}v")
        if kind == "inlineStr":
            value = _xml_text(c)
        elif v is None or v.text is None:
            continue
        elif kind == "s":
            value = shared[int(v.text)]
        elif kind in ("str", "b", "e"):
            value = v.text
        else:
            value = _number_text(v.text)
        cleaned = _clean(value)
        if cleaned:
            cells[(int(m.group(2)) - 1, _col_index(m.group(1)))] = cleaned
    for merged in sheet.iter(f"{{{_XLSX_MAIN}}}mergeCell"):
        start, _, end = merged.get("ref", "").partition(":")
        a, b = _CELL_REF.match(start), _CELL_REF.match(end or start)
        if not a or not b:
            continue
        r0, c0 = int(a.group(2)) - 1, _col_index(a.group(1))
        r1, c1 = int(b.group(2)) - 1, _col_index(b.group(1))
        merged_value = cells.get((r0, c0))
        if merged_value:
            for r in range(r0, r1 + 1):
                for col in range(c0, c1 + 1):
                    cells.setdefault((r, col), merged_value)
    if not cells:
        return []
    n_rows = max(r for r, _ in cells) + 1
    n_cols = max(c for _, c in cells) + 1
    return [[cells.get((r, c), "") for c in range(n_cols)] for r in range(n_rows)]


def _parse_date(text: str) -> datetime | None:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_xlsx(content: bytes) -> ParsedDocument:
    """A disclosure table: title rows, an optional merged group row, a header row, data rows,
    then notes.

    - Each data row becomes one table-row block written as "label: value" pairs, starting with
      its first cell (the scheme name), so the chunker can tag it by scheme.
    - When the table has a date column and the same first-cell value appears on several dates
      (a daily disclosure), only the latest row for each value is kept.
    - Notes are placed before the table, so they are tagged as general text rather than
      inheriting the scheme of the last row.
    """
    rows = _xlsx_rows(content)
    distinct = [len({v for v in row if v}) for row in rows]
    header_at = next((i for i, d in enumerate(distinct) if d >= 3), None)
    if header_at is None:
        blocks = [Block(" ".join(v for v in row if v)) for row in rows if any(row)]
        return ParsedDocument(blocks[0].text if blocks else None, blocks)

    title: str | None = None
    headings: list[Block] = []
    groups: list[list[str]] = []
    for row, d in zip(rows[:header_at], distinct[:header_at], strict=True):
        if d == 1:
            text = next(v for v in row if v)
            title = title or text
            headings.append(Block(text, is_heading=True))
        elif d > 1:
            groups.append(row)
    labels = [
        " ".join(p for p in [*(g[c] for g in groups), rows[header_at][c]] if p)
        for c in range(len(rows[header_at]))
    ]

    data: list[list[str]] = []
    notes: list[Block] = []
    for row, d in zip(rows[header_at + 1 :], distinct[header_at + 1 :], strict=True):
        if d >= 3:
            data.append(row)
        elif d:
            notes.append(Block(" ".join(v for v in row if v)))

    date_col = next((c for c, label in enumerate(labels) if "date" in label.lower()), None)
    if date_col is not None and date_col != 0:
        latest: dict[str, tuple[datetime, list[str]]] = {}
        for row in data:
            when = _parse_date(row[date_col])
            if when is None:
                latest = {}
                break
            if row[0] not in latest or when >= latest[row[0]][0]:
                latest[row[0]] = (when, row)
        if latest:
            data = [row for _, row in latest.values()]

    table_rows: list[Block] = []
    for row in data:
        pairs = [f"{labels[c]}: {v}" for c, v in enumerate(row) if c > 0 and v]
        table_rows.append(Block(f"{row[0]} | " + " | ".join(pairs), is_table_row=True))
    return ParsedDocument(title, headings + notes + table_rows)


def parse(content: bytes, kind: str, keep_page: Any = None) -> ParsedDocument:
    if kind == "pdf":
        return parse_pdf(content, keep_page)
    if kind == "xlsx":
        return parse_xlsx(content)
    return parse_html(content)
