from app.corpus.chunk import TARGET_TOKENS, chunk_document, count_tokens
from app.corpus.parse import Block, _drop_running_headers, _looks_like_heading, parse_html
from app.corpus.sources import Publisher, Source

HTML = b"""<html><head><title>Riskometer</title></head>
<body class="page-header-fixed">
  <nav><a>Home</a><a>About</a></nav>
  <div class="header">Site header</div>
  <main>
    <h1>Understanding the Riskometer</h1>
    <p>The riskometer shows the risk level of a scheme.</p>
    <table><tr><th>Level</th><th>Meaning</th></tr>
           <tr><td>Very High</td><td>Highest risk band</td></tr></table>
  </main>
  <footer>Copyright</footer>
</body></html>"""


def test_html_keeps_content_and_drops_chrome() -> None:
    doc = parse_html(HTML)
    texts = [b.text for b in doc.blocks]
    assert doc.title == "Riskometer"
    assert "Understanding the Riskometer" in texts
    assert "Very High | Highest risk band" in texts
    assert not any(t in texts for t in ("Home", "Site header", "Copyright"))
    row = next(b for b in doc.blocks if b.text.startswith("Very High"))
    assert row.is_table_row and row.table_header == "Level | Meaning"


def test_faq_questions_and_caps_lines_are_headings() -> None:
    assert _looks_like_heading("17) When will the investor get statement of account?", 10, 10)
    assert _looks_like_heading("SYSTEMATIC INVESTMENT PLAN (SIP)", 10, 10)
    assert not _looks_like_heading("The units are allotted at the applicable NAV.", 10, 10)


def test_running_headers_are_removed() -> None:
    blocks = []
    for page in range(1, 6):
        body = [Block(f"Body line {i} on page {page}", page) for i in range(5)]
        blocks += [Block("FAQs for Mutual Fund Investors", page), *body]
        blocks.append(Block(f"Page {page} of 5", page))
    kept = [b.text for b in _drop_running_headers(blocks)]
    assert kept == [f"Body line {i} on page {p}" for p in range(1, 6) for i in range(5)]


def test_short_pages_keep_numbered_body_lines() -> None:
    blocks = []
    for page in range(1, 6):
        blocks += [Block("Header", page), Block(f"Body text {page}", page), Block("Footer", page)]
    kept = [b.text for b in _drop_running_headers(blocks)]
    assert kept == [f"Body text {p}" for p in range(1, 6)]


def make_source(schemes: tuple[str, ...]) -> Source:
    return Source("s", "https://www.amfiindia.com/x", Publisher.AMFI, "education", schemes, "auto")


def test_single_scheme_source_tags_every_chunk() -> None:
    chunks = chunk_document(
        make_source(("hdfc_liquid",)), [Block("Exit load is 0.0070% on day 1.", 1)]
    )
    assert [c.scheme for c in chunks] == ["hdfc_liquid"]
    assert chunks[0].text.startswith("[HDFC Liquid Fund]")


def test_multi_scheme_source_switches_and_drops_out_of_scope() -> None:
    source = make_source(("hdfc_elss_tax_saver", "hdfc_flexi_cap"))
    blocks = [
        Block("For HDFC ELSS Tax saver:", 62),
        Block("Minimum SIP is Rs. 500 and in multiples of Rs. 500.", 62),
        Block("For HDFC Small Cap Fund:", 62),
        Block("This scheme is out of scope.", 62),
        Block("For HDFC Flexi Cap Fund through online mode:", 62),
        Block("Minimum daily SIP is Rs. 20.", 62),
    ]
    chunks = chunk_document(source, blocks)
    assert [c.scheme for c in chunks] == ["hdfc_elss_tax_saver", "hdfc_flexi_cap"]
    assert all("out of scope" not in c.text for c in chunks)


def test_table_rows_stay_whole_and_repeat_their_header() -> None:
    header = "SIP Frequency | Minimum Amount | Installments"
    rows = [Block(header, 1, is_table_row=True, table_header=header)]
    rows += [
        Block(f"Frequency {i} | {i * 100} | {i}", 1, is_table_row=True, table_header=header)
        for i in range(1, 120)
    ]
    chunks = chunk_document(make_source(("general",)), rows)
    assert len(chunks) > 1
    for chunk in chunks:
        assert header in chunk.text
        assert chunk.token_count <= TARGET_TOKENS + count_tokens(header) + 5
    every_row = "\n".join(c.text for c in chunks)
    assert all(f"Frequency {i} | {i * 100} | {i}" in every_row for i in range(1, 120))


def test_long_text_is_split_with_overlap() -> None:
    sentences = " ".join(f"Sentence number {i} explains a rule." for i in range(200))
    chunks = chunk_document(
        make_source(("general",)), [Block("Rules", 1, is_heading=True), Block(sentences, 1)]
    )
    assert len(chunks) >= 3
    assert all(c.section == "Rules" for c in chunks)
    assert all(c.token_count <= TARGET_TOKENS + 10 for c in chunks)


def _xlsx(rows: list[list[str | float | None]], merges: tuple[str, ...] = ()) -> bytes:
    """A minimal .xlsx workbook with inline strings, for parser tests."""
    import io
    import zipfile
    from xml.sax.saxutils import escape

    def col(i: int) -> str:
        return chr(65 + i)

    sheet_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, v in enumerate(row):
            ref = f"{col(c)}{r}"
            if isinstance(v, str):
                text = escape(v)
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>')
            elif v is not None:
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    merge_xml = "".join(f'<mergeCell ref="{m}"/>' for m in merges)
    sheet = (f'<worksheet {ns}><sheetData>{"".join(sheet_rows)}</sheetData>'
             f'<mergeCells>{merge_xml}</mergeCells></worksheet>')  # fmt: skip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/workbook.xml", f"<workbook {ns}><sheets/></workbook>")
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def test_xlsx_keeps_latest_row_per_scheme_with_group_labels() -> None:
    from app.corpus.fetch import _kind
    from app.corpus.parse import parse

    content = _xlsx(
        [
            ["Total Expense Ratio (TER)"],
            [None, None, "Regular Plan", None, "Direct Plan"],
            ["Scheme Name", "Date", "TER (%)", None, "TER (%)"],
            ["HDFC Liquid Fund", "01-Nov-2026", 0.31, None, 0.21],
            ["HDFC Liquid Fund", "16-Nov-2026", 0.3, None, 0.2],
            ["HDFC Large & Mid Cap Fund", "16-Nov-2026", 1.6, None, 0.9],
            ["Notes:"],
            ["1. Base Expense Ratio as per SEBI regulations."],
            ["2. Percentages are annualized."],
            ["3. Direct plans have a lower expense ratio."],
        ],
        merges=("A1:E1", "C2:D2"),
    )
    assert _kind("", "hdfc_ter.xls", content) == "xlsx"
    doc = parse(content, "xlsx")
    assert doc.title == "Total Expense Ratio (TER)"
    rows = [b.text for b in doc.blocks if b.is_table_row]
    assert rows[0] == (
        "HDFC Liquid Fund | Date: 16-Nov-2026 | Regular Plan TER (%): 0.3"
        " | Direct Plan TER (%): 0.2"
    )
    assert len(rows) == 2
    chunks = chunk_document(make_source(("hdfc_liquid", "hdfc_large_cap")), doc.blocks)
    by_scheme = {c.scheme: c.text for c in chunks}
    assert "Percentages are annualized" in by_scheme["general"]
    assert "0.3" in by_scheme["hdfc_liquid"]
    assert "Large & Mid" not in " ".join(by_scheme.values())


def test_vertically_merged_pdf_cells_repeat_on_every_covered_row() -> None:
    from types import SimpleNamespace

    from app.corpus.parse import _fill_merged_down

    def row(top: float, cells: list[tuple[float, float, float, float] | None]) -> SimpleNamespace:
        return SimpleNamespace(bbox=(0, top, 300, top + 10), cells=cells)

    table = SimpleNamespace(
        rows=[
            row(0, [(0, 0, 100, 10), (100, 0, 200, 10), (200, 0, 300, 10)]),
            # The amount cell spans rows 1-3; the third column is a plain one-row cell.
            row(10, [(0, 10, 100, 20), (100, 10, 200, 40), (200, 10, 300, 20)]),
            row(20, [(0, 20, 100, 30), None, None]),
            row(30, [(0, 30, 100, 40), None, (200, 30, 300, 40)]),
            row(40, [(0, 40, 100, 50), None, None]),
        ],
        extract=lambda: [
            ["Scheme", "Minimum", "Plans"],
            ["Fund A", "Rs. 100", "Growth"],
            ["Fund B", None, None],
            ["Fund C", None, "IDCW"],
            ["Fund D", None, None],
        ],
    )
    assert _fill_merged_down(table) == [
        ["Scheme", "Minimum", "Plans"],
        ["Fund A", "Rs. 100", "Growth"],
        ["Fund B", "Rs. 100", None],
        ["Fund C", "Rs. 100", "IDCW"],
        ["Fund D", None, None],
    ]
