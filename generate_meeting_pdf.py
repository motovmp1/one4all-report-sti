"""Generate a presentation-style PDF from One4All XML results.

This is a standalone reporting helper. It does not import or modify main.py.
"""

from __future__ import annotations

import argparse
import html
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.pagesizes import landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


PAGE = (13.333 * 72, 7.5 * 72)  # 16:9 presentation page
W, H = PAGE
MARGIN = 46

PURPLE = HexColor("#3E328A")
PURPLE_2 = HexColor("#6B5BC1")
BLUE = HexColor("#3974D8")
GREEN = HexColor("#4F9A7D")
RED = HexColor("#C63F55")
AMBER = HexColor("#C88418")
INK = HexColor("#24344E")
MUTED = HexColor("#6D7C92")
PALE = HexColor("#F4F6FA")
LINE = HexColor("#DCE2EB")
PALE_PURPLE = HexColor("#F0EDFF")
PALE_BLUE = HexColor("#EDF4FF")
PALE_GREEN = HexColor("#EDF8F3")
PALE_RED = HexColor("#FCEFF1")
PALE_AMBER = HexColor("#FFF6E7")
QA_DATA_FILE_NAME = "One4All_QA_data.xml"


@dataclass
class Result:
    test_id: str
    title: str
    family: str
    status: str
    started: datetime
    stopped: datetime | None
    path: Path
    is_draft: bool
    dut: str = ""
    xml_sw: str = ""
    xml_hw: str = ""
    tester: str = ""
    chamber: str = ""
    test_interface: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        default=None,
        help="Optional project override. By default ProjectName is read from Information.xml.",
    )
    parser.add_argument("--round", dest="round_name", default="Pre-release")
    parser.add_argument(
        "--date",
        default=None,
        help="Optional report generation date override, YYYY-MM-DD. It never filters XML results.",
    )
    parser.add_argument("--results", type=Path, default=Path("ST-I_Results"))
    parser.add_argument(
        "--scope",
        type=Path,
        default=Path("ST-I_Test_Scope/test_scope.xml"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output path. By default, date and ISO week are added to the PDF name.",
    )
    return parser.parse_args()


def attr_value(raw: str, name: str) -> str:
    pattern = rf'<DATA\b[^>]*\bname="{re.escape(name)}"[^>]*\bvalue="([^"]*)"'
    match = re.search(pattern, raw, re.I)
    if not match:
        pattern = rf'<DATA\b[^>]*\bvalue="([^"]*)"[^>]*\bname="{re.escape(name)}"'
        match = re.search(pattern, raw, re.I)
    return html.unescape(match.group(1)).strip() if match else ""


def parse_xml_timestamp(value: str) -> datetime | None:
    for pattern in ("%d-%m-%y_%Hh-%Mmin-%Ss", "%d-%m-%Y_%Hh-%Mmin-%Ss"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            pass
    return None


def parse_result(path: Path) -> Result | None:
    stamp = re.search(r"_(\d{2}-\d{2}-\d{2})_(\d{2})h-(\d{2})min-(\d{2})s", path.name)
    test = re.match(r"(\d+(?:\.\d+)*)\s*([^_]*)", path.name)
    state = re.search(r"\b(passed|failed|skipped|aborted)(?:_draft)?\.xml$", path.name, re.I)
    if not (stamp and test and state):
        return None
    filename_start = datetime.strptime(
        stamp.group(1) + " " + ":".join(stamp.groups()[1:]), "%d-%m-%y %H:%M:%S"
    )
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw = path.read_text(encoding="windows-1252", errors="replace")
    xml_start = re.search(r'<XML\b[^>]*\bfilestart="([^"]+)"', raw, re.I)
    xml_stop = re.search(r'\bfilestop="([^"]+)"', raw, re.I)
    started = parse_xml_timestamp(xml_start.group(1)) if xml_start else filename_start
    started = started or filename_start
    stopped = parse_xml_timestamp(xml_stop.group(1)) if xml_stop else None
    return Result(
        test_id=test.group(1),
        title=test.group(2).strip(),
        family=test.group(1).split(".", 1)[0],
        status=state.group(1).lower(),
        started=started,
        stopped=stopped,
        path=path,
        is_draft=bool(re.search(r"_draft\.xml$", path.name, re.I)),
        dut=attr_value(raw, "DUT"),
        xml_sw=attr_value(raw, "SW version of DUT"),
        xml_hw=attr_value(raw, "HW version of DUT"),
        tester=attr_value(raw, "Tester ID"),
        chamber=attr_value(raw, "Temperature chamber"),
        test_interface=attr_value(raw, "Test interface"),
    )


def load_scope(path: Path) -> tuple[set[str], dict[str, str]]:
    root = ET.parse(path).getroot()
    ids: set[str] = set()
    labels: dict[str, str] = {}
    for test in root.findall(".//Test"):
        test_id = (test.get("Number") or "").strip()
        definition = (test.findtext("Name") or "").strip()
        if not test_id or test_id == "0" or definition == "-":
            continue
        ids.add(test_id)
        parts = re.split(r"[\\/]", definition)
        family = test_id.split(".", 1)[0]
        if len(parts) >= 2:
            folder = parts[-2]
            folder = re.sub(r"^\d+_", "", folder).replace("_", " ")
            labels.setdefault(family, folder)
    return ids, labels


def load_campaign_info(scope_path: Path) -> dict[str, str]:
    """Read project and declared DUT metadata stored beside test_scope.xml."""
    information_path = scope_path.parent / "Information.xml"
    if not information_path.is_file():
        return {}
    try:
        root = ET.parse(information_path).getroot()
        attributes = root.attrib
    except ET.ParseError:
        try:
            raw = information_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}
        info_tag = re.search(r"<Info\b([^>]*)>", raw, re.I)
        if not info_tag:
            return {}
        attributes = {
            key: html.unescape(value)
            for key, value in re.findall(r'([\w:-]+)\s*=\s*"([^"]*)"', info_tag.group(1))
        }
    except OSError:
        return {}
    return {key: value.strip() for key, value in attributes.items() if value.strip()}


def load_qa_member_names(scope_path: Path, scoped: dict[str, Result]) -> list[str]:
    """Return QA members assigned to the latest scoped results."""
    qa_path = scope_path.parent / QA_DATA_FILE_NAME
    if not qa_path.is_file():
        return []
    try:
        root = ET.parse(qa_path).getroot()
    except (OSError, ET.ParseError):
        return []
    members = {
        (element.get("id") or "").strip().casefold(): (element.get("name") or "").strip()
        for element in root.findall("./QAMembers/Member")
        if (element.get("id") or "").strip() and (element.get("name") or "").strip()
    }
    current_results = {result.path.name.casefold() for result in scoped.values()}
    assigned_ids = {
        (element.get("qaMemberId") or "").strip().casefold()
        for element in root.findall("./Tests/Test")
        if (element.get("result") or "").strip().casefold() in current_results
        and (element.get("qaMemberId") or "").strip()
    }
    return sorted({members[member_id] for member_id in assigned_ids if member_id in members}, key=str.casefold)


def filename_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return slug or "test_campaign"


def next_available_path(path: Path) -> Path:
    """Preserve report history by adding a sequence when a name already exists."""
    if not path.exists():
        return path
    sequence = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{sequence:02d}{path.suffix}")
        if not candidate.exists():
            return candidate
        sequence += 1


def collect(results_dir: Path, scope_ids: set[str]):
    all_results = [r for p in results_dir.rglob("*.xml") if (r := parse_result(p))]
    latest: dict[str, Result] = {}
    for result in all_results:
        previous = latest.get(result.test_id)
        if previous is None or result.started >= previous.started:
            latest[result.test_id] = result
    scoped = {test_id: latest[test_id] for test_id in scope_ids if test_id in latest}
    return all_results, latest, scoped


def safe_text(value: str) -> str:
    """Turn XML/user text into clean WinAnsi text for ReportLab's Helvetica.

    In particular, line breaks and other control characters must not reach
    ``drawString``: ReportLab renders some of them as visible square glyphs.
    """
    replacements = {
        "\u00a0": " ", "\u00b7": " | ", "\u2022": " - ",
        "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2026": "...",
    }
    cleaned: list[str] = []
    for character in str(value or ""):
        if character in replacements:
            cleaned.append(replacements[character])
            continue
        if character.isspace() or unicodedata.category(character).startswith("C"):
            cleaned.append(" ")
            continue
        try:
            character.encode("cp1252")
            cleaned.append(character)
        except UnicodeEncodeError:
            fallback = unicodedata.normalize("NFKD", character).encode("ascii", "ignore").decode("ascii")
            cleaned.append(fallback)
    return re.sub(r"\s+", " ", "".join(cleaned)).strip()


def text(c: canvas.Canvas, value: str, x: float, y: float, size=12, color=INK,
         font="Helvetica", align="left") -> None:
    value = safe_text(value)
    c.setFont(font, size)
    c.setFillColor(color)
    if align == "right":
        c.drawRightString(x, y, value)
    elif align == "center":
        c.drawCentredString(x, y, value)
    else:
        c.drawString(x, y, value)


def wrap(value: str, width: float, size: float, font="Helvetica") -> list[str]:
    words = safe_text(value).split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if not current or stringWidth(candidate, font, size) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def paragraph(c: canvas.Canvas, value: str, x: float, y: float, width: float,
              size=11, leading=15, color=MUTED, font="Helvetica", max_lines=None) -> float:
    lines = wrap(value, width, size, font)
    if max_lines:
        lines = lines[:max_lines]
    for line in lines:
        text(c, line, x, y, size, color, font)
        y -= leading
    return y


def round_rect(c: canvas.Canvas, x, y, w, h, fill=white, stroke=LINE, radius=10):
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.setLineWidth(0.8)
    c.roundRect(x, y, w, h, radius, fill=1, stroke=1)


def footer(c: canvas.Canvas, page: int, project: str, report_date: datetime):
    c.setStrokeColor(LINE)
    c.line(MARGIN, 25, W - MARGIN, 25)
    text(c, f"{project} | Test campaign snapshot | Generated {report_date:%d %b %Y}", MARGIN, 10, 8, MUTED)
    text(c, f"{page:02d}", W - MARGIN, 10, 8, PURPLE, "Helvetica-Bold", "right")


def page_title(c: canvas.Canvas, kicker: str, title_value: str, subtitle: str = ""):
    text(c, kicker.upper(), MARGIN, H - 43, 9, PURPLE_2, "Helvetica-Bold")
    text(c, title_value, MARGIN, H - 78, 25, INK, "Helvetica-Bold")
    if subtitle:
        paragraph(c, subtitle, MARGIN, H - 99, W - 2 * MARGIN, 10, 13, MUTED, max_lines=2)


def metric_card(c: canvas.Canvas, x, y, w, h, label, value, detail, color, pale):
    round_rect(c, x, y, w, h, pale, Color(color.red, color.green, color.blue, alpha=0.22))
    text(c, label.upper(), x + 15, y + h - 23, 8, color, "Helvetica-Bold")
    text(c, value, x + 15, y + 32, 25, INK, "Helvetica-Bold")
    text(c, detail, x + 15, y + 15, 8, MUTED)


def progress(c: canvas.Canvas, x, y, w, value, color, label, right_label):
    text(c, label, x, y + 13, 9, INK, "Helvetica-Bold")
    text(c, right_label, x + w, y + 13, 9, color, "Helvetica-Bold", "right")
    c.setFillColor(HexColor("#E5EAF1"))
    c.roundRect(x, y, w, 7, 3.5, fill=1, stroke=0)
    c.setFillColor(color)
    c.roundRect(x, y, max(7, w * min(1, max(0, value))), 7, 3.5, fill=1, stroke=0)


def donut_gauge(c: canvas.Canvas, cx: float, cy: float, radius: float, value: float,
                color, label: str, ratio: str, note: str = ""):
    value = min(1, max(0, value))
    c.saveState()
    c.setLineCap(1)
    c.setFillColor(white)
    c.setStrokeColor(HexColor("#E4E9F0"))
    c.setLineWidth(9)
    c.circle(cx, cy, radius, fill=1, stroke=1)
    c.setStrokeColor(color)
    c.setLineWidth(9)
    c.arc(cx - radius, cy - radius, cx + radius, cy + radius, 90, -360 * value)
    c.restoreState()
    text(c, f"{value * 100:.1f}%", cx, cy - 5, 14, INK, "Helvetica-Bold", "center")
    text(c, label, cx, cy - radius - 22, 8, INK, "Helvetica-Bold", "center")
    text(c, ratio, cx, cy - radius - 36, 8, color, "Helvetica-Bold", "center")
    if note:
        text(c, note, cx, cy - radius - 49, 6.5, MUTED, align="center")


def evidence_values(scoped: dict[str, Result]) -> dict[str, list[str]]:
    fields = ("dut", "xml_sw", "xml_hw", "tester", "chamber", "test_interface")
    return {
        field: sorted({getattr(result, field) for result in scoped.values() if getattr(result, field)})
        for field in fields
    }


def joined(values: list[str], fallback: str = "Not recorded") -> str:
    return ", ".join(values) if values else fallback


def compact_joined(values: list[str], fallback: str = "Not assigned", limit: int = 82) -> str:
    if not values:
        return fallback
    shown: list[str] = []
    for index, value in enumerate(values):
        remaining = len(values) - index - 1
        suffix = f" +{remaining} more" if remaining else ""
        candidate = ", ".join([*shown, value]) + suffix
        if shown and len(candidate) > limit:
            return ", ".join(shown) + f" +{len(values) - len(shown)} more"
        shown.append(value)
    return ", ".join(shown)


def draw_cover(c, args, report_date, scoped, results, evidence, campaign_info):
    c.setFillColor(PURPLE)
    c.rect(0, 0, W, H, fill=1, stroke=0)
    c.setFillColor(PURPLE_2)
    c.circle(W - 35, H - 20, 180, fill=1, stroke=0)
    c.setFillColor(HexColor("#2E286B"))
    c.circle(W - 35, -20, 145, fill=1, stroke=0)
    text(c, "TRIDONIC", MARGIN, H - 55, 18, white, "Helvetica-Bold")
    text(c, "ONE4ALL TEST REPORT", MARGIN, H - 92, 9, HexColor("#D6D0FF"), "Helvetica-Bold")
    text(c, args.project, MARGIN, H - 170, 36, white, "Helvetica-Bold")
    declared_dut = campaign_info.get("NamesOfDut") or joined(evidence["dut"])
    text(c, declared_dut, MARGIN, H - 208, 18, HexColor("#D6D0FF"), "Helvetica")
    text(c, f"TEST ROUND  |  {args.round_name.upper()}", MARGIN, H - 234, 9, white, "Helvetica-Bold")
    paragraph(
        c,
        "Decision-ready snapshot of scope execution, quality signals and open test risk.",
        MARGIN,
        H - 265,
        480,
        13,
        18,
        HexColor("#E9E6FF"),
    )
    cards = [
        ("REPORT DATE", report_date.strftime("%d %b %Y")),
        ("FIRMWARE", joined(evidence["xml_sw"])),
        ("HARDWARE", joined(evidence["xml_hw"])),
        ("TEST STATION", "Interface " + joined(evidence["test_interface"])),
    ]
    x = MARGIN
    for label, value in cards:
        c.setFillColor(Color(1, 1, 1, alpha=0.10))
        c.roundRect(x, 82, 150, 62, 8, fill=1, stroke=0)
        text(c, label, x + 12, 124, 7, HexColor("#CFC9F5"), "Helvetica-Bold")
        text(c, value, x + 12, 96, 14, white, "Helvetica-Bold")
        x += 164
    first_test = min(result.started for result in results)
    last_test = max((result.stopped or result.started) for result in results)
    text(
        c,
        f"XML data range: {first_test:%d %b %Y %H:%M} to {last_test:%d %b %Y %H:%M} | {len(results)} result(s) read",
        MARGIN, 45, 8, HexColor("#D6D0FF"),
    )
    text(c, "PT Team", W - MARGIN, 45, 9, white, "Helvetica-Bold", "right")
    c.showPage()


def draw_summary(c, args, report_date, scope_ids, scoped, results):
    page_title(c, "02 / Executive snapshot", "Campaign readiness at a glance",
               "Latest execution per selected test ID across every XML result currently available.")
    counts = Counter(r.status for r in scoped.values())
    selected = len(scope_ids)
    executed = len(scoped)
    passed = counts["passed"]
    failed = counts["failed"]
    missing = selected - executed
    x0, y0, gap = MARGIN, 328, 12
    card_w = (W - 2 * MARGIN - 4 * gap) / 5
    data = [
        ("Selected", str(selected), "scope tests", PURPLE, PALE_PURPLE),
        ("Executed", str(executed), f"{executed/selected*100:.1f}% coverage", BLUE, PALE_BLUE),
        ("Passed", str(passed), f"{passed/selected*100:.1f}% of scope", GREEN, PALE_GREEN),
        ("Failed", str(failed), "latest outcomes", RED, PALE_RED),
        ("Missing", str(missing), "not yet executed", AMBER, PALE_AMBER),
    ]
    for i, item in enumerate(data):
        metric_card(c, x0 + i * (card_w + gap), y0, card_w, 96, *item)
    round_rect(c, MARGIN, 112, 535, 186, white, LINE)
    text(c, "READINESS SIGNALS", MARGIN + 18, 274, 9, PURPLE, "Helvetica-Bold")
    pass_executed = passed / executed if executed else 0
    gauge_y = 210
    gauge_radius = 33
    gauge_x = [MARGIN + 96, MARGIN + 267, MARGIN + 438]
    donut_gauge(
        c, gauge_x[0], gauge_y, gauge_radius, executed / selected, BLUE,
        "Execution completion", f"{executed}/{selected}",
    )
    donut_gauge(
        c, gauge_x[1], gauge_y, gauge_radius, passed / selected, GREEN,
        "Total scope passed", f"{passed}/{selected}",
    )
    donut_gauge(
        c, gauge_x[2], gauge_y, gauge_radius, pass_executed, PURPLE_2,
        "Executed tests passing", f"{passed}/{executed}", "Missing tests excluded",
    )
    round_rect(c, 606, 112, W - 606 - MARGIN, 186, PALE_RED if failed else PALE_GREEN, LINE)
    text(c, "EXECUTIVE READING", 624, 274, 9, RED if failed else GREEN, "Helvetica-Bold")
    headline = "Not release-ready yet" if failed or missing else "Scope complete and passing"
    text(c, headline, 624, 240, 19, INK, "Helvetica-Bold")
    note = (
        f"{failed} selected tests still fail and {missing} remain unexecuted. "
        "The strongest meeting focus should be failure containment, retest ownership and closure of scope gaps."
        if failed or missing else
        "All selected tests have a latest passing execution in the current XML result set."
    )
    paragraph(c, note, 624, 211, W - 624 - MARGIN - 12, 11, 16, MUTED, max_lines=5)
    paragraph(
        c,
        f"Evidence: all {len(results)} XML executions considered; latest outcome per selected test ID.",
        624,
        139,
        W - 624 - MARGIN - 14,
        8,
        11,
        MUTED,
        max_lines=2,
    )
    footer(c, 3, args.project, report_date)
    c.showPage()


def family_rows(scope_ids, scoped, labels):
    rows = []
    by_family = defaultdict(set)
    for test_id in scope_ids:
        by_family[test_id.split(".", 1)[0]].add(test_id)
    for family, ids in by_family.items():
        values = [scoped[i] for i in ids if i in scoped]
        counts = Counter(v.status for v in values)
        rows.append({
            "family": family,
            "label": labels.get(family, f"Group {family}"),
            "selected": len(ids),
            "executed": len(values),
            "passed": counts["passed"],
            "failed": counts["failed"],
            "missing": len(ids) - len(values),
        })
    return sorted(rows, key=lambda r: (-r["selected"], int(r["family"])))


def function_block_status(row) -> tuple[str, Color, Color]:
    """Return the management ratio, accent and background for a Function Block."""
    not_tested = max(0, row["selected"] - row["passed"] - row["failed"])
    if row["failed"]:
        return f"{row['failed']}/{row['selected']} failed", RED, PALE_RED
    if not_tested:
        return f"{not_tested}/{row['selected']} not tested", AMBER, PALE_AMBER
    return f"{row['passed']}/{row['selected']} passed", GREEN, PALE_GREEN


def draw_coverage(c, args, report_date, rows):
    page_title(c, "03 / Scope coverage", "Execution coverage by Function Block",
               "Largest selected Function Blocks are shown first. Bars represent execution coverage; labels show the latest result ratio.")
    shown = rows[:14]
    left, top = MARGIN, H - 128
    col_gap = 30
    col_w = (W - 2 * MARGIN - col_gap) / 2
    row_h = 47
    for idx, row in enumerate(shown):
        col = idx // 7
        line = idx % 7
        x = left + col * (col_w + col_gap)
        y = top - line * row_h
        label = f"{row['family']}  {row['label']}"
        if len(label) > 34:
            label = label[:33] + "..."
        text(c, label, x, y, 9, INK, "Helvetica-Bold")
        text(c, f"{row['executed']}/{row['selected']}", x + col_w, y, 9, MUTED, "Helvetica-Bold", "right")
        bar_y = y - 16
        c.setFillColor(HexColor("#E5EAF1"))
        c.roundRect(x, bar_y, col_w, 7, 3.5, fill=1, stroke=0)
        execution = row["executed"] / row["selected"] if row["selected"] else 0
        c.setFillColor(BLUE)
        c.roundRect(x, bar_y, max(6, col_w * execution), 7, 3.5, fill=1, stroke=0)
        status_label, status_color, _ = function_block_status(row)
        text(c, status_label, x + col_w, bar_y - 11, 7, status_color, "Helvetica-Bold", "right")
    not_shown = len(rows) - len(shown)
    if not_shown > 0:
        text(c, f"+ {not_shown} smaller active Function Blocks included in totals", MARGIN, 67, 8, MUTED)
    footer(c, 4, args.project, report_date)
    c.showPage()


def draw_failures(c, args, report_date, scoped, rows):
    active_rows = sorted(rows, key=lambda row: int(row["family"]))
    blocks_failed = sum(bool(row["failed"]) for row in active_rows)
    failed_total = sum(row["failed"] for row in active_rows)
    blocks_per_page = 18
    chunks = [
        active_rows[index:index + blocks_per_page]
        for index in range(0, len(active_rows), blocks_per_page)
    ] or [[]]
    total_pages = len(chunks)
    for page_index, page_rows in enumerate(chunks):
        page_title(c, "04 / Failure landscape", "Function Block failure overview",
                   "All selected tests and latest failures are summarised by Function Block. Individual test details are excluded.")
        text(
            c, f"Page {page_index + 1} of {total_pages}", W - MARGIN, H - 43,
            8, PURPLE, "Helvetica-Bold", "right",
        )
        round_rect(c, MARGIN, 380, W - 2 * MARGIN, 48, PALE_PURPLE, LINE)
        summary_x = [MARGIN + 22, MARGIN + 290, MARGIN + 570]
        summary = [
            ("ACTIVE FUNCTION BLOCKS", str(len(active_rows)), PURPLE),
            ("FUNCTION BLOCKS WITH FAILURES", str(blocks_failed), RED),
            ("FAILED LATEST OUTCOMES", str(failed_total), RED),
        ]
        for x, (label, value, color) in zip(summary_x, summary):
            text(c, label, x, 407, 7, color, "Helvetica-Bold")
            text(c, value, x, 388, 14, INK, "Helvetica-Bold")

        columns = 3
        row_count = max(1, (len(page_rows) + columns - 1) // columns)
        gap_x, gap_y = 12, 7
        card_w = (W - 2 * MARGIN - (columns - 1) * gap_x) / columns
        card_h = min(47, (350 - (row_count - 1) * gap_y) / row_count)
        top_y = 360
        for index, row in enumerate(page_rows):
            column = index // row_count
            line = index % row_count
            x = MARGIN + column * (card_w + gap_x)
            y = top_y - card_h - line * (card_h + gap_y)
            _, color, pale = function_block_status(row)
            round_rect(c, x, y, card_w, card_h, pale, Color(color.red, color.green, color.blue, alpha=0.25), 8)
            c.setFillColor(color)
            c.roundRect(x, y, 5, card_h, 2.5, fill=1, stroke=0)
            block_name = f"FB {row['family']}  {row['label']}"
            if len(block_name) > 31:
                block_name = block_name[:30] + "..."
            text(c, block_name, x + 15, y + card_h - 17, 8.5, INK, "Helvetica-Bold")
            selected = row["selected"]
            passed = row["passed"]
            failed = row["failed"]
            not_tested = max(0, selected - passed - failed)
            right = x + card_w - 13
            if failed:
                text(c, f"{failed}/{selected} failed", right, y + 18, 8.5, RED, "Helvetica-Bold", "right")
                passed_label = f"{passed}/{selected} passed"
                text(c, passed_label, right, y + 6, 7, GREEN, "Helvetica-Bold", "right")
                passed_width = stringWidth(passed_label, "Helvetica-Bold", 7)
                text(
                    c, f"{not_tested}/{selected} not tested", right - passed_width - 14,
                    y + 6, 7, AMBER, "Helvetica-Bold", "right",
                )
            elif not_tested:
                text(c, f"{not_tested}/{selected} not tested", right, y + 18, 8.5, AMBER, "Helvetica-Bold", "right")
                text(c, f"{passed}/{selected} passed", right, y + 6, 7, GREEN, "Helvetica-Bold", "right")
            else:
                text(c, f"{passed}/{selected} passed", right, y + 10, 8.5, GREEN, "Helvetica-Bold", "right")
        footer(c, 5 + page_index, args.project, report_date)
        c.showPage()
    return total_pages


def draw_gaps(c, args, report_date, scope_ids, scoped, rows, page_number):
    missing_ids = sorted(
        (test_id for test_id in scope_ids if test_id not in scoped),
        key=lambda value: [int(x) for x in value.split(".")],
    )
    missing_rows = sorted((r for r in rows if r["missing"]), key=lambda r: -r["missing"])
    page_title(c, "05 / Open scope", "What is still missing from the round",
               "Missing means no XML result was found for that selected test in the current result set.")
    metric_card(c, MARGIN, 342, 205, 88, "Missing tests", str(len(missing_ids)), "selected scope", AMBER, PALE_AMBER)
    metric_card(c, MARGIN + 220, 342, 205, 88, "Affected FBs", str(len(missing_rows)), "Function Blocks", PURPLE, PALE_PURPLE)
    completion = len(scoped) / len(scope_ids) if scope_ids else 0
    metric_card(c, MARGIN + 440, 342, 205, 88, "Scope complete", f"{completion*100:.1f}%", "current XML set", BLUE, PALE_BLUE)
    round_rect(c, MARGIN, 92, 370, 224, white, LINE)
    text(c, "GAPS BY FUNCTION BLOCK", MARGIN + 17, 292, 9, AMBER, "Helvetica-Bold")
    y = 260
    for row in missing_rows[:7]:
        label = f"FB {row['family']} {row['label']}"
        if len(label) > 33:
            label = label[:32] + "..."
        text(c, label, MARGIN + 17, y, 9, INK)
        text(c, f"{row['missing']}/{row['selected']} missing", MARGIN + 350, y, 9, AMBER, "Helvetica-Bold", "right")
        y -= 26
    x = MARGIN + 398
    round_rect(c, x, 92, W - MARGIN - x, 224, PALE_AMBER, LINE)
    text(c, "MISSING TEST IDS", x + 17, 292, 9, AMBER, "Helvetica-Bold")
    cols = 5
    col_w = (W - MARGIN - x - 34) / cols
    for index, test_id in enumerate(missing_ids[:35]):
        col = index // 7
        row = index % 7
        text(c, test_id, x + 17 + col * col_w, 258 - row * 25, 9, INK, "Helvetica-Bold")
    footer(c, page_number, args.project, report_date)
    c.showPage()


def draw_traceability(c, args, report_date, scoped, results, evidence, qa_members):
    page_title(c, "01 / Configuration", "Test configuration from XML evidence",
               "Firmware, hardware and station information are taken only from the available test result XML files.")
    cards = [
        ("FIRMWARE VERSION", joined(evidence["xml_sw"]), "SW version of DUT", PURPLE, PALE_PURPLE),
        ("HARDWARE VERSION", joined(evidence["xml_hw"]), "HW version of DUT", BLUE, PALE_BLUE),
        ("TEST STATION", "Interface " + joined(evidence["test_interface"]), "Test interface", GREEN, PALE_GREEN),
    ]
    card_w = (W - 2 * MARGIN - 24) / 3
    for index, item in enumerate(cards):
        metric_card(c, MARGIN + index * (card_w + 12), 330, card_w, 98, *item)

    left_w = 520
    round_rect(c, MARGIN, 132, left_w, 170, white, LINE)
    text(c, "DEVICE UNDER TEST", MARGIN + 18, 276, 9, PURPLE, "Helvetica-Bold")
    y = 244
    for index, value in enumerate(evidence["dut"] or ["Not recorded"], 1):
        text(c, f"{index:02d}", MARGIN + 18, y, 9, PURPLE, "Helvetica-Bold")
        paragraph(c, value, MARGIN + 52, y, left_w - 72, 11, 14, INK, "Helvetica-Bold", max_lines=2)
        y -= 42
    text(c, "Source", MARGIN + 18, 158, 8, MUTED, "Helvetica-Bold")
    text(c, "Latest XML result per selected test ID", MARGIN + 75, 158, 9, INK)

    right_x = MARGIN + left_w + 20
    round_rect(c, right_x, 132, W - MARGIN - right_x, 170, PALE_BLUE, LINE)
    text(c, "TEST ENVIRONMENT", right_x + 18, 276, 9, BLUE, "Helvetica-Bold")
    environment = [
        ("Tester", joined(evidence["tester"])),
        ("Chambers", joined(evidence["chamber"])),
        ("Scope", args.scope.name),
        ("QA members", compact_joined(qa_members)),
    ]
    y = 244
    for label, value in environment:
        text(c, label, right_x + 18, y, 9, MUTED, "Helvetica-Bold")
        paragraph(c, value, right_x + 150, y, W - MARGIN - right_x - 168, 9, 12, INK, "Helvetica-Bold", max_lines=2)
        y -= 32

    first_test = min(result.started for result in results)
    last_test = max((result.stopped or result.started) for result in results)
    elapsed_days = (last_test - first_test).total_seconds() / 86400
    runtime_hours = sum(
        (result.stopped - result.started).total_seconds()
        for result in results
        if result.stopped and result.stopped >= result.started
    ) / 3600
    timeline = [
        ("TEST STARTED AT", first_test.strftime("%d %b %Y %H:%M"), PURPLE, PALE_PURPLE),
        ("LAST TEST AT", last_test.strftime("%d %b %Y %H:%M"), BLUE, PALE_BLUE),
        ("DAYS PASSED SO FAR", f"{elapsed_days:.1f} days", AMBER, PALE_AMBER),
        ("XML TEST RUNTIME", f"{runtime_hours:.1f} hours", GREEN, PALE_GREEN),
    ]
    gap = 10
    box_w = (W - 2 * MARGIN - 3 * gap) / 4
    for index, (label, value, color, pale) in enumerate(timeline):
        x = MARGIN + index * (box_w + gap)
        round_rect(c, x, 72, box_w, 46, pale, Color(color.red, color.green, color.blue, alpha=0.22), 7)
        text(c, label, x + 12, 101, 7, color, "Helvetica-Bold")
        text(c, value, x + 12, 82, 11, INK, "Helvetica-Bold")
    footer(c, 2, args.project, report_date)
    c.showPage()


def generate(args: argparse.Namespace) -> Path:
    report_date = datetime.strptime(args.date, "%Y-%m-%d") if args.date else datetime.now()
    campaign_info = load_campaign_info(args.scope)
    args.project = args.project or campaign_info.get("ProjectName") or "Test campaign"
    if args.output is None:
        generated_date = report_date.strftime("%d_%b_%Y")
        iso_week = report_date.isocalendar().week
        project_slug = filename_slug(args.project)
        round_slug = filename_slug(args.round_name)
        output_dir = Path(getattr(args, "output_dir", Path("output/pdf")))
        args.output = output_dir / (
            f"{project_slug}_{round_slug}_test_report_"
            f"{generated_date}_week_{iso_week:02d}.pdf"
        )
        args.output = next_available_path(args.output)
    scope_ids, labels = load_scope(args.scope)
    if not scope_ids:
        raise ValueError("The selected test scope does not contain any active tests.")
    all_results, latest, scoped = collect(args.results, scope_ids)
    if not all_results:
        raise ValueError("No valid One4All XML result files were found in the selected folder.")
    rows = family_rows(scope_ids, scoped, labels)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(args.output), pagesize=PAGE, pageCompression=1)
    c.setTitle(safe_text(f"{args.project} - {args.round_name} Test Report"))
    c.setAuthor("PT Team")
    evidence = evidence_values(scoped)
    qa_members = load_qa_member_names(args.scope, scoped)
    draw_cover(c, args, report_date, scoped, all_results, evidence, campaign_info)
    draw_traceability(c, args, report_date, scoped, all_results, evidence, qa_members)
    draw_summary(c, args, report_date, scope_ids, scoped, all_results)
    draw_coverage(c, args, report_date, rows)
    failure_pages = draw_failures(c, args, report_date, scoped, rows)
    draw_gaps(c, args, report_date, scope_ids, scoped, rows, 5 + failure_pages)
    c.save()
    return args.output


def generate_report_pdf(
    results: Path,
    scope: Path,
    output_dir: Path,
    project: str | None = None,
    round_name: str = "Pre-release",
    report_date: str | None = None,
) -> Path:
    """Generate a report from the paths selected in the One4All Viewer UI."""
    options = argparse.Namespace(
        project=project,
        round_name=round_name,
        date=report_date,
        results=Path(results),
        scope=Path(scope),
        output=None,
        output_dir=Path(output_dir),
    )
    return generate(options)


if __name__ == "__main__":
    options = parse_args()
    output = generate(options)
    print(output.resolve())
