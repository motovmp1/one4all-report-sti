from __future__ import annotations

import ctypes
import html
import os
import re
import sys
import textwrap
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QFileSystemWatcher,
    QModelIndex,
    QObject,
    QRectF,
    QRunnable,
    QSize,
    QSortFilterProxyModel,
    QPropertyAnimation,
    QSignalBlocker,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStyle,
    QTabBar,
    QTabWidget,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from generate_meeting_pdf import generate_report_pdf


STATUS_META = {
    "passed": ("Passed", "#4F947A", "#EDF7F3"),
    "failed": ("Failed", "#DC4C64", "#FDEDEF"),
    "skipped": ("Skipped", "#D38A16", "#FFF6E5"),
    "aborted": ("Aborted", "#8B5CF6", "#F3EEFF"),
    "draft": ("Draft", "#3974D8", "#EAF2FF"),
    "unknown": ("Unknown", "#64748B", "#F1F5F9"),
}

APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
RELATIONSHIPS_DATA_FILE_NAME = "Relationships.xml"


def normalized_path(path: Path) -> Path:
    """Make a path absolute without resolving it across a potentially slow network."""
    return Path(os.path.abspath(os.fspath(path)))


def is_network_path(path: Path | None) -> bool:
    if path is None:
        return False
    value = os.fspath(path)
    if value.startswith(("\\\\", "//")):
        return True
    drive = Path(value).drive
    if drive.upper() == "Z:":
        return True
    if os.name == "nt" and drive:
        try:
            return ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\") == 4
        except (AttributeError, OSError):
            pass
    return False


def network_location_name(path: Path) -> str:
    value = os.fspath(path)
    if value.startswith(("\\\\", "//")):
        return "Network location"
    return f"Network drive {path.drive}" if path.drive else "Network location"


@dataclass(frozen=True, slots=True)
class QAMember:
    member_id: str
    name: str


@dataclass(frozen=True, slots=True)
class QAAssignment:
    member_id: str = ""
    qa_comment: str = ""


class QAMemberStore:
    """QA assignments and comments shared with the legacy Test Manager."""

    def __init__(self):
        self.members: list[QAMember] = []
        self.assignments: dict[str, QAAssignment] = {}
        self.data_file: Path | None = None
        self.warning = ""

    def reload(self):
        self.members = []
        self.assignments = {}
        self.warning = ""
        path = self.data_file
        if not path:
            return
        if not path.is_file():
            return
        try:
            root = ET.parse(path).getroot()
            if root.tag != "Tests":
                raise ET.ParseError("unsupported root element")
            seen: set[str] = set()
            by_number: dict[str, list[QAAssignment]] = {}
            for element in root.findall("./Test"):
                result_name = (element.findtext("Name", default="") or "").strip()
                tester = (element.get("Tester") or "").strip()
                comment = (element.get("Comment") or "").strip()
                if tester and tester.casefold() not in seen:
                    self.members.append(QAMember(tester, tester))
                    seen.add(tester.casefold())
                if result_name:
                    assignment = QAAssignment(tester, comment)
                    self.assignments[result_name.casefold()] = assignment
                    number = (element.get("Number") or "").strip().casefold()
                    if number:
                        by_number.setdefault(number, []).append(assignment)
            for number, assignments in by_number.items():
                if len(assignments) == 1:
                    self.assignments[f"@test:{number}"] = assignments[0]
            self.members.sort(key=lambda member: member.name.casefold())
        except (OSError, ET.ParseError) as exc:
            self.warning = f"{RELATIONSHIPS_DATA_FILE_NAME} could not be read: {exc}"

    def set_data_file(self, path: Path | None):
        self.data_file = normalized_path(path) if path else None
        self.reload()

    def set_loaded_data(
        self,
        path: Path | None,
        members: list[QAMember],
        assignments: dict[str, QAAssignment],
        warning: str = "",
    ):
        self.data_file = normalized_path(path) if path else None
        self.members = members
        self.assignments = assignments
        self.warning = warning

    def _assert_data_file_available(self):
        """Fail before editing when Relationships.xml is open or unavailable."""
        path = self.data_file
        if path is None:
            raise OSError("Select the matching test_scope.xml before saving QA data.")
        if not path.is_file():
            return
        if os.name != "nt":
            try:
                with path.open("r+b"):
                    return
            except OSError as exc:
                raise OSError(
                    f"{RELATIONSHIPS_DATA_FILE_NAME} is not available for writing: {exc}"
                ) from exc

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        )
        create_file.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_int
        handle = create_file(
            os.fspath(path),
            0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
            0,  # exclusive access: no read/write/delete sharing
            None,
            3,  # OPEN_EXISTING
            0x80,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            error_code = ctypes.get_last_error()
            if error_code in (32, 33):  # sharing or lock violation
                raise OSError(
                    f"{RELATIONSHIPS_DATA_FILE_NAME} is in use by another application. "
                    "The QA change was not saved."
                )
            raise OSError(
                f"{RELATIONSHIPS_DATA_FILE_NAME} is not available for writing "
                f"(Windows error {error_code}). The QA change was not saved."
            )
        close_handle(handle)

    def _load_or_create_tree(self) -> tuple[ET.ElementTree, ET.Element]:
        path = self.data_file
        if path is None:
            raise OSError("Select the matching test_scope.xml before saving QA data.")
        if path.is_file():
            try:
                tree = ET.parse(path)
            except ET.ParseError as exc:
                raise OSError(f"{RELATIONSHIPS_DATA_FILE_NAME} is not valid XML: {exc}") from exc
            root = tree.getroot()
            if root.tag != "Tests":
                raise OSError(f"{RELATIONSHIPS_DATA_FILE_NAME} has an unsupported root element.")
        else:
            root = ET.Element("Tests")
            tree = ET.ElementTree(root)
        return tree, root

    def _write_tree(self, tree: ET.ElementTree):
        path = self.data_file
        if path is None:
            raise OSError("Select the matching test_scope.xml before saving QA data.")
        path.parent.mkdir(parents=True, exist_ok=True)
        ET.indent(tree, space="  ")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            tree.write(temporary, encoding="utf-8", xml_declaration=True)
            ET.parse(temporary)  # never replace the shared file with invalid XML
            self._assert_data_file_available()
            temporary.replace(path)
            ET.parse(path)  # confirm that the shared destination is readable
        except (OSError, ET.ParseError) as exc:
            if "change was not saved" in str(exc):
                raise
            if getattr(exc, "winerror", None) in (32, 33):
                raise OSError(
                    f"{RELATIONSHIPS_DATA_FILE_NAME} is in use by another application. "
                    "The QA change was not saved."
                ) from exc
            raise OSError(
                f"{RELATIONSHIPS_DATA_FILE_NAME} could not be saved. "
                f"The QA change was not saved: {exc}"
            ) from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        self.reload()
        if self.warning:
            raise OSError(
                f"{RELATIONSHIPS_DATA_FILE_NAME} was written but could not be verified: "
                f"{self.warning}"
            )

    def add_member(self, name: str) -> QAMember:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Enter a QA name.")
        existing = next(
            (item for item in self.members if item.name.casefold() == clean_name.casefold()), None
        )
        if existing:
            return existing
        # Relationships.xml has no global member directory. Keep the new value
        # available in the editor; assigning it to a test persists it in Tester.
        member = QAMember(clean_name, clean_name)
        self.members.append(member)
        self.members.sort(key=lambda item: item.name.casefold())
        return member

    def member_by_id(self, member_id: str) -> QAMember | None:
        wanted = member_id.casefold().strip()
        return next((member for member in self.members if member.member_id.casefold() == wanted), None)

    def assignment_for(self, path: Path) -> QAAssignment:
        exact = self.assignments.get(path.name.casefold())
        if exact is not None:
            return exact
        test_id_match = re.match(r"(\d+(?:\.\d+)*)", path.name)
        if not test_id_match:
            return QAAssignment()
        return self.assignments.get(
            f"@test:{test_id_match.group(1).casefold()}", QAAssignment()
        )

    def resolved_member(self, path: Path) -> QAMember | None:
        return self.member_by_id(self.assignment_for(path).member_id)

    def _relationship_element(self, root: ET.Element, path: Path) -> ET.Element:
        wanted = path.name.casefold()
        element = next(
            (
                item
                for item in root.findall("./Test")
                if (item.findtext("Name", default="") or "").strip().casefold() == wanted
            ),
            None,
        )
        if element is None:
            test_id_match = re.match(r"(\d+(?:\.\d+)*)", path.name)
            test_id = test_id_match.group(1) if test_id_match else ""
            same_number = [
                item
                for item in root.findall("./Test")
                if (item.get("Number") or "").strip().casefold() == test_id.casefold()
            ]
            # A unique test number is a safe fallback when the legacy Name is
            # an older/short form. Multiple temperature variants are not.
            element = same_number[0] if len(same_number) == 1 else None
        if element is None:
            indices = []
            for item in root.findall("./Test/Index"):
                try:
                    indices.append(int((item.text or "").strip()))
                except ValueError:
                    pass
            test_id_match = re.match(r"(\d+(?:\.\d+)*)", path.name)
            element = ET.SubElement(
                root,
                "Test",
                Number=test_id_match.group(1) if test_id_match else "",
                Duration="0",
                BugIDs="",
                BugIDsFI="",
                DID="",
                Tester="",
                Comment="",
            )
            ET.SubElement(element, "Index").text = str(max(indices, default=-1) + 1)
            ET.SubElement(element, "Name").text = path.name
        return element

    def assign_member(self, path: Path, member_id: str):
        self._assert_data_file_available()
        tree, root = self._load_or_create_tree()
        member = self.member_by_id(member_id)
        tester = member.name if member else member_id.strip()
        self._relationship_element(root, path).set("Tester", tester)
        self._write_tree(tree)

    def assign_comment(self, path: Path, qa_comment: str):
        self._assert_data_file_available()
        tree, root = self._load_or_create_tree()
        self._relationship_element(root, path).set("Comment", qa_comment.strip())
        self._write_tree(tree)


def copy_path_icon() -> QIcon:
    """Return a small platform-independent copy icon for the report dialog."""
    pixmap = QPixmap(18, 18)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#3E63A8"), 1.6))
    painter.drawRoundedRect(QRectF(6, 3, 9, 11), 1.5, 1.5)
    painter.drawRoundedRect(QRectF(3, 6, 9, 9), 1.5, 1.5)
    painter.end()
    return QIcon(pixmap)


def edit_details_icon() -> QIcon:
    pixmap = QPixmap(22, 22)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor("#3974D8"), 2.4, Qt.SolidLine, Qt.RoundCap))
    painter.drawLine(5, 17, 16, 6)
    painter.drawLine(7, 19, 18, 8)
    painter.setPen(QPen(QColor("#6B7C96"), 1.4))
    painter.drawLine(4, 19, 8, 18)
    painter.end()
    return QIcon(pixmap)


def save_details_icon() -> QIcon:
    pixmap = QPixmap(22, 22)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor("#FFFFFF"), 1.8))
    painter.setBrush(QColor("#4F9A7D"))
    painter.drawRoundedRect(QRectF(3, 3, 16, 16), 2, 2)
    painter.setBrush(QColor("#FFFFFF"))
    painter.drawRect(QRectF(6, 5, 8, 5))
    painter.drawRect(QRectF(6, 13, 10, 4))
    painter.end()
    return QIcon(pixmap)

# Stable Test Manager clusters. The selected tests inside each cluster come
# dynamically from test_scope.xml; this list changes only when a new formal
# cluster is introduced.
FIXED_SCOPE_GROUPS = (
    ("00", "00_Initial Check"), ("01", "01_eDALI"), ("02", "02_DSI"),
    ("03", "03_SD"), ("04", "04_DC-EM"), ("05", "05_o4a"),
    ("06", "06_CF"), ("07", "07_ITG"), ("08", "08_LTA"),
    ("09", "09_CLO"), ("10", "10_chronoSTEP2"), ("11", "11_CS"),
    ("12", "12_ePOL"), ("13", "13_DimAtDC"), ("14", "14_eDSI"),
    ("15", "15_easySys"), ("16", "16_Mains Dimming"), ("17", "17_IVG"),
    ("18", "18_chronostep3"), ("19", "19_f2z"), ("20", "20_chronoSTEP+"),
    ("21", "21_LCS"), ("22", "22_IVG+"), ("23", "23_ratioSwitch"),
    ("24", "24_NFC"), ("25", "25_ColorSwitch"), ("26", "26_selvDIM"),
    ("27", "27_o4a_selvDIM"), ("28", "28_ETM"), ("29", "29_Last Gasp"),
    ("30", "30_Sensor Mode"), ("31", "31_CF_selvDIM"),
    ("32", "32_dynamicPHM"), ("33", "33_Grouping"),
    ("101", "101_DALI_SystemComponents"), ("102", "102_DALI_ControlGear"),
    ("105", "105_DALI_FU"),
    ("201", "201_DT0"), ("202", "202_DT1"), ("203", "203_DT2"),
    ("204", "204_DT3"), ("205", "205_DT4"), ("206", "206_DT5"),
    ("207", "207_DT6"), ("208", "208_DT7"), ("209", "209_DT8"),
    ("250", "250_DT49"), ("251", "251_DT50"), ("252", "252_DT51"),
    ("253", "253_DT52"),
)


@dataclass(slots=True)
class Evaluation:
    test_id: str = ""
    step_id: str = ""
    step_name: str = ""
    kind: str = ""
    summary: str = ""
    expected: str = ""
    condition: str = ""
    comment: str = ""
    result: str = ""
    elapsed: str = ""


@dataclass(slots=True)
class TestRecord:
    path: Path
    file_name: str
    title: str
    test_id: str
    family: str
    status: str
    is_draft: bool
    started: datetime | None
    stopped: datetime | None
    duration_seconds: int | None
    dut: str = ""
    tester: str = ""
    sw_version: str = ""
    hw_version: str = ""
    chamber: str = ""
    qa_member_id: str = ""
    qa_member_name: str = ""
    qa_comment: str = ""
    evaluations: list[Evaluation] = field(default_factory=list)
    parse_warning: str = ""
    details_loaded: bool = True
    raw_xml: str = ""

    @property
    def failed_evaluations(self) -> list[Evaluation]:
        failures = []
        for item in self.evaluations:
            marker = f"{item.result} {item.comment}".lower()
            if item.result not in ("", "0") or any(x in marker for x in ("fail", "error", "wrong")):
                failures.append(item)
        return failures



@dataclass(slots=True)
class ScopeGroup:
    folder_name: str
    folder_id: str
    test_ids: set[str]


def scan_test_scope(scope_file: Path) -> list[ScopeGroup]:
    """Read the exact selected test IDs from a Test Manager test_scope.xml."""
    if not scope_file.is_file():
        raise FileNotFoundError(f"Test scope is unavailable: {scope_file}")
    raw, _ = read_xml_text(scope_file)
    root = ET.fromstring(raw)
    groups = [ScopeGroup(name, folder_id, set()) for folder_id, name in FIXED_SCOPE_GROUPS]
    by_id = {str(int(group.folder_id)): group for group in groups}
    for test in root.findall(".//Test"):
        test_id = (test.get("Number") or "").strip()
        definition = (test.findtext("Name") or "").strip()
        if not test_id or test_id == "0" or definition == "-":
            continue
        major_id = str(int(test_id.split(".")[0]))
        group = by_id.get(major_id)
        if group:
            group.test_ids.add(test_id)
    return groups


def empty_scope_groups() -> list[ScopeGroup]:
    return [ScopeGroup(name, folder_id, set()) for folder_id, name in FIXED_SCOPE_GROUPS]


DATA_RE = re.compile(r'<DATA\b(?P<attrs>[^>]*)/?>', re.IGNORECASE)
ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')
EVAL_RE = re.compile(r'<STEP_EVALUATE\b[^>]*>([\s\S]*?)</STEP_EVALUATE>', re.IGNORECASE)
STEP_RE = re.compile(r'<STEP\b[^>]*>([\s\S]*?)</STEP>', re.IGNORECASE)


def _attrs(source: str) -> dict[str, str]:
    return {key: html.unescape(value) for key, value in ATTR_RE.findall(source)}


def _data_values(source: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for match in DATA_RE.finditer(source):
        attrs = _attrs(match.group("attrs"))
        if attrs.get("name"):
            values[attrs["name"]] = attrs.get("value", "")
    return values


def _parse_timestamp(value: str) -> datetime | None:
    for pattern in ("%d-%m-%y_%Hh-%Mmin-%Ss", "%d-%m-%Y_%Hh-%Mmin-%Ss"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            pass
    return None


def _decode_xml_bytes(data: bytes) -> tuple[str, str]:
    try:
        return data.decode("utf-8"), ""
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace").replace("\ufffd", "?")
        return text, "Decoded using the Windows-1252 fallback."


def read_xml_text(path: Path) -> tuple[str, str]:
    """Decode a complete One4All XML, including legacy Windows-1252 files."""
    return _decode_xml_bytes(path.read_bytes())


def read_xml_summary_text(path: Path) -> tuple[str, str]:
    """Read only the metadata needed by the results overview.

    One4All stores the test metadata at the beginning and ``filestop`` at the
    end. Large step histories between them are loaded later, when details open.
    """
    head_limit = 64 * 1024
    tail_limit = 8 * 1024
    with path.open("rb") as source:
        head = source.read(head_limit)
        if len(head) < head_limit:
            return _decode_xml_bytes(head)

        test_end = head.lower().find(b"</test>")
        if test_end >= 0:
            head = head[:test_end + len(b"</test>")]

        source.seek(0, os.SEEK_END)
        size = source.tell()
        source.seek(max(0, size - tail_limit))
        tail = source.read(tail_limit)

    # Start the tail on an XML tag so a split multibyte character cannot force
    # an otherwise UTF-8 file through the legacy decoder.
    filestop = tail.lower().rfind(b"filestop=")
    tag_start = tail.rfind(b"<", 0, filestop) if filestop >= 0 else tail.find(b"<")
    if tag_start >= 0:
        tail = tail[tag_start:]
    return _decode_xml_bytes(head + b"\n" + tail)


def _status_from_name(name: str) -> tuple[str, bool]:
    lower = name.lower()
    draft = "_draft" in lower
    for state in ("passed", "failed", "skipped", "aborted"):
        if re.search(rf"\b{state}(?:_draft)?\.xml$", lower):
            return ("draft" if draft else state), draft
    return ("draft" if draft else "unknown"), draft


def outcome_from_name(name: str) -> str:
    """Return the real result suffix, keeping DRAFT as a separate qualifier."""
    lower = name.lower()
    for state in ("passed", "failed", "skipped", "aborted"):
        if re.search(rf"\b{state}(?:_draft)?\.xml$", lower):
            return state
    return "unknown"


def parse_result(path: Path, include_evaluations: bool = True) -> TestRecord:
    warning = ""
    try:
        reader = read_xml_text if include_evaluations else read_xml_summary_text
        raw, warning = reader(path)
    except OSError as exc:
        raw, warning = "", str(exc)

    status, draft = _status_from_name(path.name)
    stem = path.stem
    clean = re.sub(r"\s+-\s+(passed|failed|skipped|aborted)(?:_DRAFT)?$", "", stem, flags=re.I)
    first_part = clean.split("_", 1)[0].strip()
    id_match = re.match(r"(\d+(?:\.\d+)*)\s*(.*)", first_part)
    test_id = id_match.group(1) if id_match else "—"
    title = (id_match.group(2).strip() if id_match else first_part) or clean
    family = test_id.split(".", 1)[0] if test_id != "—" else "Other"

    root_attrs = _attrs((re.search(r"<XML\b([^>]*)>", raw, re.I) or ["", ""])[1])
    stop_match = re.search(r'<DATA\b[^>]*\bfilestop="([^"]+)"', raw, re.I)
    started = _parse_timestamp(root_attrs.get("filestart", ""))
    stopped = _parse_timestamp(stop_match.group(1)) if stop_match else None
    duration = max(0, int((stopped - started).total_seconds())) if started and stopped else None

    test_block_match = re.search(r"<TEST\b[^>]*>([\s\S]*?)</TEST>", raw, re.I)
    info = _data_values(test_block_match.group(1)) if test_block_match else {}
    evaluation_blocks = EVAL_RE.findall(raw)
    evaluations = []
    if include_evaluations:
        step_details: dict[tuple[str, str], list[dict[str, str]]] = {}
        for block in STEP_RE.findall(raw):
            values = _data_values(block)
            key = (values.get("TestID", ""), values.get("StepID", ""))
            step_details.setdefault(key, []).append(values)
        step_offsets: dict[tuple[str, str], int] = {}
        for block in evaluation_blocks:
            values = _data_values(block)
            key = (values.get("TestID", ""), values.get("StepID", ""))
            candidates = step_details.get(key, [])
            offset = step_offsets.get(key, 0)
            step = candidates[offset] if offset < len(candidates) else {}
            step_offsets[key] = offset + 1
            raw_step_name = step.get("Name", "")
            elapsed_match = re.search(r"\((\d{3}:\d{2}:\d{2}[,.]\d{3})\)", raw_step_name)
            clean_step_name = re.sub(r"\(\d{3}:\d{2}:\d{2}[,.]\d{3}\)", "", raw_step_name)
            clean_step_name = clean_step_name.strip(" \r\n-")
            evaluations.append(Evaluation(
                test_id=values.get("TestID", ""), step_id=values.get("StepID", ""),
                step_name=clean_step_name, kind=values.get("Type", ""),
                summary=values.get("Summary", ""), expected=values.get("Expected", ""),
                condition=values.get("Condition", ""), comment=values.get("Comment", ""),
                result=values.get("Result", ""), elapsed=elapsed_match.group(1) if elapsed_match else "",
            ))
    if raw and "</XML>" not in raw:
        incomplete = "Incomplete XML or invalid characters detected; tolerant parsing was applied."
        warning = f"{warning} {incomplete}".strip()

    return TestRecord(
        path=path, file_name=path.name, title=title, test_id=test_id, family=family,
        status=status, is_draft=draft, started=started, stopped=stopped,
        duration_seconds=duration, dut=info.get("DUT", ""), tester=info.get("Tester ID", ""),
        sw_version=info.get("SW version of DUT", ""), hw_version=info.get("HW version of DUT", ""),
        chamber=info.get("Temperature chamber", ""),
        evaluations=evaluations, parse_warning=warning,
        details_loaded=include_evaluations,
        raw_xml=raw if include_evaluations else "",
    )


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def comment_table_preview(value: str, line_width: int = 78, max_lines: int = 3) -> str:
    """Preserve authored line breaks and mark only genuinely hidden content."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return "-"
    visual_lines: list[str] = []
    overflow = False
    authored_lines = normalized.split("\n")
    for authored_index, authored_line in enumerate(authored_lines):
        wrapped = textwrap.wrap(
            authored_line,
            width=line_width,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        remaining_slots = max_lines - len(visual_lines)
        if remaining_slots <= 0:
            overflow = True
            break
        visual_lines.extend(wrapped[:remaining_slots])
        if len(wrapped) > remaining_slots or authored_index < len(authored_lines) - 1 and len(visual_lines) >= max_lines:
            overflow = True
            break
    if overflow:
        arrow = "  →"
        last = visual_lines[-1]
        if len(last) + len(arrow) > line_width:
            last = last[: line_width - len(arrow)].rstrip()
        visual_lines[-1] = last + arrow
    return "\n".join(visual_lines)


class ScanSignals(QObject):
    finished = Signal(object, object, object)
    progress = Signal(int, int, str, object)


class ScanJob(QRunnable):
    def __init__(
        self,
        source: Path,
        cache: dict[str, tuple[int, int, TestRecord]] | None = None,
    ):
        super().__init__()
        self.source = source
        self.cache = cache if cache is not None else {}
        self.signals = ScanSignals()

    @staticmethod
    def _discover(source: Path) -> list[tuple[Path, int, int]]:
        if source.is_file():
            if source.suffix.lower() != ".xml":
                return []
            stat = source.stat()
            return [(source, stat.st_size, stat.st_mtime_ns)]

        discovered: list[tuple[Path, int, int]] = []
        pending = [source]
        while pending:
            folder = pending.pop()
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".xml"):
                        stat = entry.stat(follow_symlinks=False)
                        discovered.append((Path(entry.path), stat.st_size, stat.st_mtime_ns))
        discovered.sort(key=lambda item: item[0].name.lower())
        return discovered

    def run(self):
        try:
            files = self._discover(self.source)
            total = len(files)
            records: list[TestRecord | None] = [None] * total
            next_cache: dict[str, tuple[int, int, TestRecord]] = {}
            missing: list[tuple[int, Path, int, int, str]] = []
            completed = 0

            for index, (path, size, modified_ns) in enumerate(files):
                key = os.path.normcase(os.path.abspath(os.fspath(path)))
                cached = self.cache.get(key)
                if cached and cached[:2] == (size, modified_ns):
                    record = cached[2]
                    records[index] = record
                    next_cache[key] = cached
                    completed += 1
                    self.signals.progress.emit(completed, total, path.name, self.source)
                else:
                    missing.append((index, path, size, modified_ns, key))

            if missing:
                worker_count = min(4, len(missing))
                with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="result-summary") as pool:
                    futures = {
                        pool.submit(parse_result, path, False): (index, path, size, modified_ns, key)
                        for index, path, size, modified_ns, key in missing
                    }
                    for future in as_completed(futures):
                        index, path, size, modified_ns, key = futures[future]
                        record = future.result()
                        records[index] = record
                        next_cache[key] = (size, modified_ns, record)
                        completed += 1
                        self.signals.progress.emit(completed, total, path.name, self.source)

            self.cache.clear()
            self.cache.update(next_cache)
            records = [record for record in records if record is not None]
            error = ""
        except Exception as exc:  # keep worker failures visible in the UI
            records, error = [], str(exc)
        try:
            self.signals.finished.emit(records, error, self.source)
        except RuntimeError:
            # The application may be closed while a large folder is still scanning.
            pass


class DetailSignals(QObject):
    finished = Signal(object, str, str)


class DetailJob(QRunnable):
    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.signals = DetailSignals()

    def run(self):
        try:
            record = parse_result(self.path, include_evaluations=True)
            error = ""
        except Exception as exc:
            record, error = None, str(exc)
        try:
            self.signals.finished.emit(record, error, str(normalized_path(self.path)))
        except RuntimeError:
            pass


class ReportSignals(QObject):
    finished = Signal(object, str)
    progress = Signal(int, str)


class ReportJob(QRunnable):
    def __init__(self, results: Path, scope: Path, output_dir: Path):
        super().__init__()
        self.results = results
        self.scope = scope
        self.output_dir = output_dir
        self.signals = ReportSignals()

    def run(self):
        try:
            output = generate_report_pdf(
                self.results,
                self.scope,
                self.output_dir,
                progress_callback=self.signals.progress.emit,
            )
            error = ""
        except Exception as exc:
            output, error = None, str(exc)
        try:
            self.signals.finished.emit(output, error)
        except RuntimeError:
            pass


class ScopeLoadSignals(QObject):
    finished = Signal(object, str, object)


class ScopeLoadJob(QRunnable):
    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.signals = ScopeLoadSignals()

    def run(self):
        try:
            groups = scan_test_scope(self.path)
            if not groups or not any(group.test_ids for group in groups):
                raise ValueError("The selected XML does not contain any valid selected tests.")
            qa_path = self.path.parent / RELATIONSHIPS_DATA_FILE_NAME
            qa_store = QAMemberStore()
            qa_store.set_data_file(qa_path)
            payload = (groups, list(qa_store.members), dict(qa_store.assignments), qa_store.warning)
            error = ""
        except Exception as exc:
            payload, error = None, str(exc)
        try:
            self.signals.finished.emit(payload, error, self.path)
        except RuntimeError:
            pass


class QAOperationSignals(QObject):
    finished = Signal(object, str)


class QAOperationJob(QRunnable):
    def __init__(self, store: QAMemberStore, action: str, *values):
        super().__init__()
        self.store = store
        self.action = action
        self.values = values
        self.signals = QAOperationSignals()

    def run(self):
        try:
            if self.action == "add_member":
                result = self.store.add_member(*self.values)
            elif self.action == "assign_member":
                self.store.assign_member(*self.values)
                result = self.values[-1]
            elif self.action == "assign_comment":
                self.store.assign_comment(*self.values)
                result = self.values[-1]
            else:
                raise ValueError(f"Unsupported QA operation: {self.action}")
            error = ""
        except Exception as exc:
            result, error = None, str(exc)
        try:
            self.signals.finished.emit(result, error)
        except RuntimeError:
            pass


class ResultsModel(QAbstractTableModel):
    columns = (
        "ID", "Test", "Status", "Duration", "Date", "Tester", "DUT",
        "QA member", "QA comment",
    )
    QA_COLUMN = 7
    QA_COMMENT_COLUMN = 8

    def __init__(self, qa_store: QAMemberStore):
        super().__init__()
        self.qa_store = qa_store
        self.records: list[TestRecord] = []

    def set_records(self, records: list[TestRecord]):
        self.beginResetModel()
        for record in records:
            self.hydrate_record(record)
        self.records = records
        self.endResetModel()

    def hydrate_record(self, record: TestRecord):
        assignment = self.qa_store.assignment_for(record.path)
        member = self.qa_store.resolved_member(record.path)
        record.qa_member_id = member.member_id if member else assignment.member_id
        record.qa_member_name = member.name if member else assignment.member_id
        record.qa_comment = assignment.qa_comment

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.records)

    def columnCount(self, parent=QModelIndex()):
        return len(self.columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.columns[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        record = self.records[index.row()]
        values = (
            record.test_id, record.title, STATUS_META[record.status][0],
            format_duration(record.duration_seconds),
            record.started.strftime("%d/%m/%Y %H:%M") if record.started else "—",
            record.tester or "-", record.dut or "-", record.qa_member_name or "Select QA member...",
            record.qa_comment or "-",
        )
        if role == Qt.DisplayRole:
            if index.column() == self.QA_COMMENT_COLUMN:
                return comment_table_preview(record.qa_comment)
            return values[index.column()]
        if role == Qt.EditRole:
            if index.column() == self.QA_COLUMN:
                return record.qa_member_id
            if index.column() == self.QA_COMMENT_COLUMN:
                return record.qa_comment
            return values[index.column()]
        if role == Qt.UserRole:
            return record
        if role == Qt.ForegroundRole and index.column() == 2:
            return QColor(STATUS_META[record.status][1])
        if role == Qt.FontRole and index.column() in (0, 2):
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ToolTipRole:
            if index.column() == self.QA_COMMENT_COLUMN:
                return record.qa_comment or "No QA comment"
            return record.file_name
        return None

    def flags(self, index):
        return super().flags(index)

    def setData(self, index, value, role=Qt.EditRole):
        if (
            not index.isValid()
            or index.column() not in (self.QA_COLUMN, self.QA_COMMENT_COLUMN)
            or role != Qt.EditRole
        ):
            return False
        record = self.records[index.row()]
        if index.column() == self.QA_COLUMN:
            member = self.qa_store.member_by_id(str(value))
            record.qa_member_id = member.member_id if member else ""
            record.qa_member_name = member.name if member else ""
        else:
            record.qa_comment = str(value).strip()
        try:
            if index.column() == self.QA_COLUMN:
                self.qa_store.assign_member(record.path, record.qa_member_id)
            else:
                self.qa_store.assign_comment(record.path, record.qa_comment)
        except OSError:
            return False
        self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.EditRole])
        return True


class ResultsProxy(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.query = ""
        self.status = "all"
        self.function_block = "all"
        self.scope_ids: set[str] | None = None

    def set_query(self, value: str):
        self.beginFilterChange()
        self.query = value.casefold().strip()
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def set_status(self, value: str):
        self.beginFilterChange()
        self.status = value
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def set_function_block(self, value: str):
        self.beginFilterChange()
        self.function_block = value
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def set_scope_ids(self, value: set[str] | None):
        self.beginFilterChange()
        self.scope_ids = set(value) if value is not None else None
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, source_row, source_parent):
        model: ResultsModel = self.sourceModel()
        record = model.records[source_row]
        status_ok = self.status == "all" or record.status == self.status
        function_block_ok = (
            self.function_block == "all" or record.family == self.function_block
        )
        scope_ok = self.scope_ids is None or record.test_id in self.scope_ids
        text = (
            f"{record.test_id} {record.title} {record.file_name} {record.dut} "
            f"{record.tester} {record.qa_member_name} {record.qa_comment}"
        ).casefold()
        return status_ok and function_block_ok and scope_ok and (not self.query or self.query in text)


class StepsModel(QAbstractTableModel):
    columns = ("Nr.", "Status", "Type", "Sequence", "Test step", "Answer", "Expected", "Condition", "Comment", "Elapsed")

    def __init__(self, evaluations: list[Evaluation]):
        super().__init__()
        self.evaluations = evaluations

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.evaluations)

    def columnCount(self, parent=QModelIndex()):
        return len(self.columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.columns[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self.evaluations[index.row()]
        passed = item.result in ("", "0")
        values = (
            str(index.row() + 1), "PASS" if passed else "FAIL", item.kind or "—",
            item.test_id or "—", item.step_name or "—", item.summary or "—",
            item.expected or "—", item.condition or "—", item.comment or "—",
            item.elapsed or "—",
        )
        if role == Qt.DisplayRole:
            return values[index.column()]
        if role == Qt.BackgroundRole:
            return None if passed else QBrush(QColor("#F8D7DA"))
        if role == Qt.ForegroundRole and index.column() == 1:
            return QColor("#477F6A" if passed else "#BD354D")
        if role == Qt.FontRole and index.column() == 1:
            font = QFont(); font.setBold(True); return font
        if role == Qt.TextAlignmentRole and index.column() in (0, 1, 2, 3, 7, 9):
            return Qt.AlignCenter
        if role == Qt.ToolTipRole:
            return values[index.column()]
        return None


class DonutChart(QWidget):
    def __init__(self):
        super().__init__()
        self.counts: dict[str, int] = {}
        self.compact = False
        self.set_compact(False)

    def set_compact(self, compact: bool):
        self.compact = compact
        if compact:
            self.setMinimumSize(130, 104)
            self.setMaximumHeight(120)
        else:
            self.setMinimumSize(160, 150)
            self.setMaximumHeight(175)
        self.updateGeometry()
        self.update()

    def set_counts(self, counts: dict[str, int]):
        self.counts = counts
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        side = max(48, min(self.width(), self.height()) - (26 if self.compact else 34))
        rect = self.rect().adjusted((self.width()-side)//2, (self.height()-side)//2,
                                    -(self.width()-side)//2, -(self.height()-side)//2)
        pen_width = max(12, min(22, round(side * 0.16)))
        pen = QPen(QColor("#E7ECF3"), pen_width, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, 0, 360 * 16)
        total = sum(self.counts.values())
        start = 90 * 16
        if total:
            for status in STATUS_META:
                value = self.counts.get(status, 0)
                if not value:
                    continue
                span = -int(360 * 16 * value / total)
                painter.setPen(QPen(QColor(STATUS_META[status][1]), pen_width, Qt.SolidLine, Qt.RoundCap))
                painter.drawArc(rect, start, span)
                start += span
        painter.setPen(QColor("#17233C"))
        font = painter.font(); font.setPointSize(18 if self.compact else 24); font.setBold(True); painter.setFont(font)
        center_y = rect.center().y()
        count_rect = QRectF(rect.left(), center_y - 33, rect.width(), 37)
        painter.drawText(count_rect, Qt.AlignHCenter | Qt.AlignBottom, str(total))
        font.setPointSize(8 if self.compact else 9); font.setBold(False); painter.setFont(font)
        painter.setPen(QColor("#718096"))
        label_rect = QRectF(rect.left(), center_y + 9, rect.width(), 24)
        painter.drawText(label_rect, Qt.AlignHCenter | Qt.AlignTop, "RESULTS")


class PortugalFlag(QWidget):
    """Small vector-rendered Portuguese flag for the application footer."""

    def __init__(self):
        super().__init__()
        self.setObjectName("ptFlag")
        self.setFixedSize(25, 17)
        self.setToolTip("Portugal · PT Team")
        self.setAccessibleName("Flag of Portugal")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        flag = self.rect().adjusted(1, 1, -1, -1)
        green_width = round(flag.width() * 0.4)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#046A38"))
        painter.drawRect(flag.x(), flag.y(), green_width, flag.height())
        painter.setBrush(QColor("#DA291C"))
        painter.drawRect(flag.x() + green_width, flag.y(), flag.width() - green_width, flag.height())
        center_x = flag.x() + green_width
        center_y = flag.center().y()
        painter.setBrush(QColor("#FFCD00"))
        painter.drawEllipse(center_x - 3, center_y - 4, 7, 8)
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(center_x - 1, center_y - 2, 3, 4)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("#D1D7E0"), 1))
        painter.drawRoundedRect(flag, 2, 2)


class InstantToolTipFilter(QObject):
    """Shows scope statistics immediately instead of using the OS hover delay."""

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Enter and watched.toolTip():
            QToolTip.showText(QCursor.pos(), watched.toolTip(), watched)
        elif event.type() == QEvent.Leave:
            QToolTip.hideText()
        return super().eventFilter(watched, event)


class ScopeStackedBar(QWidget):
    """Scope completion bar: passed, completed non-pass, and missing."""

    def __init__(self):
        super().__init__()
        self.passed = 0
        self.non_passed = 0
        self.total = 0
        self.setFixedHeight(8)
        self.setMinimumWidth(40)

    def set_values(self, passed: int, non_passed: int, total: int):
        self.passed = max(0, passed)
        self.non_passed = max(0, non_passed)
        self.total = max(0, total)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(rect, 4, 4)
        painter.setClipPath(clip)
        painter.fillRect(rect, QColor("#FFFFFF"))
        if self.total:
            passed_width = rect.width() * min(self.passed, self.total) / self.total
            completed_non_pass = min(self.non_passed, max(0, self.total - self.passed))
            non_passed_width = rect.width() * completed_non_pass / self.total
            if passed_width:
                painter.fillRect(QRectF(rect.left(), rect.top(), passed_width, rect.height()), QColor("#69AD92"))
            if non_passed_width:
                painter.fillRect(
                    QRectF(rect.left() + passed_width, rect.top(), non_passed_width, rect.height()),
                    QColor("#DC4C64"),
                )
        painter.setClipping(False)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("#E3E8EF"), 1))
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)


class ClusterCard(QFrame):
    def __init__(self, group: ScopeGroup, records: list[TestRecord]):
        super().__init__()
        self.setObjectName("clusterCard")
        total = len(group.test_ids)
        group_records = [record for record in records if record.test_id in group.test_ids]
        latest_by_id: dict[str, TestRecord] = {}
        for record in group_records:
            previous = latest_by_id.get(record.test_id)
            record_date = record.started or datetime.min
            previous_date = previous.started if previous and previous.started else datetime.min
            if previous is None or record_date >= previous_date:
                latest_by_id[record.test_id] = record
        result_counts = {status: 0 for status in ("passed", "failed", "skipped", "aborted", "unknown")}
        for record in latest_by_id.values():
            result_counts[outcome_from_name(record.file_name)] += 1
        draft_count = sum(record.is_draft for record in latest_by_id.values())
        passed = result_counts["passed"]
        executed = len(latest_by_id)
        missing = max(0, total - executed)
        self.passed = passed
        self.total = total
        self.pass_percent = passed * 100 / total if total else 0
        self.result_counts = result_counts
        self.draft_count = draft_count
        self.executed = executed
        self.missing = missing
        pass_percent = self.pass_percent
        execution_percent = executed * 100 / total if total else 0
        disabled = total == 0
        self.setProperty("scopeDisabled", disabled)
        self.setFixedHeight(24)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 1, 2, 1)
        layout.setSpacing(5)
        compact_name = group.folder_name.replace("_", " ")
        if len(compact_name) > 12:
            compact_name = compact_name[:11] + "…"
        name = QLabel(compact_name)
        name.setFixedWidth(60)
        name.setStyleSheet(f"color: {'#A6AFBE' if disabled else '#42516A'}; font-size: 10px;")
        selected_count = QLabel(str(total))
        selected_count.setObjectName("scopeCountDisabled" if disabled else "scopeCount")
        selected_count.setFixedSize(21, 17)
        selected_count.setAlignment(Qt.AlignCenter)
        percent = QLabel("—" if disabled else f"{execution_percent:.0f}%")
        percent.setFixedWidth(32)
        percent.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        percent.setStyleSheet(
            f"font-weight: 700; color: {'#A6AFBE' if disabled else '#253653'}; font-size: 10px;"
        )
        bar = ScopeStackedBar()
        bar.set_values(passed, executed - passed, total)
        self.bar = bar
        self.percent_label = percent
        layout.addWidget(name)
        layout.addWidget(selected_count)
        layout.addWidget(bar, 1)
        layout.addWidget(percent)
        if disabled:
            tooltip = (
                f"<b>{html.escape(group.folder_name)}</b><hr>"
                "No tests selected in the current test scope.<br>"
                "This group is disabled."
            )
        else:
            tooltip = (
                f"<b>{html.escape(group.folder_name)}</b><hr>"
                f"Selected: <b>{total}</b><br>"
                f"Passed: <b>{result_counts['passed']}</b><br>"
                f"Failed: <b>{result_counts['failed']}</b><br>"
                f"Skipped: <b>{result_counts['skipped']}</b><br>"
                f"Aborted: <b>{result_counts['aborted']}</b><br>"
                f"Draft: <b>{draft_count}</b><br>"
                f"Unknown: <b>{result_counts['unknown']}</b><br>"
                f"Completed: <b>{executed}</b><br>"
                f"Missing: <b>{missing}</b><br>"
                f"Pass rate: <b>{pass_percent:.1f}%</b><br>"
                f"Scope completed: <b>{execution_percent:.1f}%</b>"
            )
        for widget in (self, name, selected_count, bar, percent):
            widget.setToolTip(tooltip)
        self._tooltip_filter = InstantToolTipFilter(self)
        for widget in (self, name, selected_count, bar, percent):
            widget.installEventFilter(self._tooltip_filter)


class StatCard(QFrame):
    clicked = Signal(str)

    def __init__(self, status: str):
        super().__init__()
        self.status = status
        self.setObjectName("statCard")
        self.setCursor(Qt.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 7, 14, 7)
        layout.setSpacing(1)
        self.setFixedHeight(68)
        title = QLabel(STATUS_META[status][0].upper())
        title.setStyleSheet(f"color: {STATUS_META[status][1]}; font-weight: 700; font-size: 11px;")
        self.value = QLabel("0")
        self.value.setObjectName("statValue")
        self.percent = QLabel("0.0% of total")
        self.percent.setObjectName("muted")
        layout.addWidget(title); layout.addWidget(self.value); layout.addWidget(self.percent)

    def set_value(self, value: int, total: int):
        self.value.setText(str(value))
        pct = value * 100 / total if total else 0
        self.percent.setText(f"{pct:.1f}% of total")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.status)
        super().mousePressEvent(event)


def info_row(label: str, value: str) -> QWidget:
    widget = QWidget()
    layout = QHBoxLayout(widget); layout.setContentsMargins(0, 4, 0, 4)
    left = QLabel(label); left.setObjectName("muted"); left.setMinimumWidth(145)
    right = QLabel(value or "—"); right.setTextInteractionFlags(Qt.TextSelectableByMouse); right.setWordWrap(True)
    layout.addWidget(left); layout.addWidget(right, 1)
    return widget


class ClickableLabel(QLabel):
    clicked = Signal()

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        if self.wordWrap():
            single_line = " ".join(self.text().split())
            natural_width = self.fontMetrics().horizontalAdvance(single_line) + 12
            hint.setWidth(min(self.maximumWidth(), natural_width))
        return hint

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class DetailPage(QWidget):
    qa_assignment_changed = Signal(object)
    network_operation_started = Signal(object, str)
    network_operation_finished = Signal()

    def __init__(
        self,
        record: TestRecord,
        qa_store: QAMemberStore,
        qa_pool: QThreadPool | None = None,
    ):
        super().__init__()
        self.record = record
        self.qa_store = qa_store
        self.qa_pool = qa_pool
        self._qa_job: QAOperationJob | None = None
        self.editing_member = False
        self.editing_comment = False
        layout = QVBoxLayout(self); layout.setContentsMargins(28, 24, 28, 24); layout.setSpacing(16)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        code = QLabel(f"TEST {html.escape(record.test_id)}"); code.setObjectName("eyebrow")
        title = QLabel(record.title); title.setObjectName("pageTitle"); title.setWordWrap(True)
        titles.addWidget(code); titles.addWidget(title)
        badge = QLabel(STATUS_META[record.status][0]); badge.setObjectName(f"badge_{record.status}")
        badge.setAlignment(Qt.AlignCenter); badge.setFixedSize(104, 34)
        header.addLayout(titles, 1); header.addWidget(badge, 0, Qt.AlignTop)
        layout.addLayout(header)

        inner = QTabWidget(); inner.setObjectName("innerTabs")
        overview = QWidget(); ov = QVBoxLayout(overview); ov.setContentsMargins(18, 18, 18, 18)
        ov.addWidget(info_row("File", record.file_name))
        ov.addWidget(info_row("Started", record.started.strftime("%d/%m/%Y at %H:%M:%S") if record.started else "—"))
        ov.addWidget(info_row("Duration", format_duration(record.duration_seconds)))
        ov.addWidget(info_row("DUT", record.dut))
        ov.addWidget(info_row("Tester", record.tester))
        qa_row = QWidget()
        qa_layout = QHBoxLayout(qa_row); qa_layout.setContentsMargins(0, 4, 0, 4)
        qa_label = QLabel("QA member"); qa_label.setObjectName("muted"); qa_label.setMinimumWidth(145)
        self.qa_value = ClickableLabel(record.qa_member_name or "Not assigned")
        self.qa_value.setObjectName("editableDetailValue")
        self.qa_value.setCursor(Qt.PointingHandCursor)
        self.qa_value.setToolTip("Click to edit the QA member")
        self.qa_value.clicked.connect(self._start_member_edit)
        self.qa_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.qa_combo = QComboBox()
        self.qa_combo.setMaximumWidth(420)
        self.qa_combo.addItem("Not assigned", "")
        for member in self.qa_store.members:
            label = member.name if member.name == member.member_id else f"{member.name} ({member.member_id})"
            self.qa_combo.addItem(label, member.member_id)
        selected = self.qa_combo.findData(record.qa_member_id)
        self.qa_combo.setCurrentIndex(max(0, selected))
        self.qa_combo.hide()
        self.add_qa_button = QPushButton("+ Add QA")
        self.add_qa_button.setObjectName("detailAddButton")
        self.add_qa_button.clicked.connect(self._add_qa_member)
        self.add_qa_button.hide()
        self.qa_edit_button = QPushButton("Edit")
        self.qa_edit_button.setObjectName("detailEditButton")
        self.qa_edit_button.setIcon(edit_details_icon())
        self.qa_edit_button.clicked.connect(self._start_member_edit)
        self.qa_save_button = QPushButton("Save")
        self.qa_save_button.setObjectName("detailSaveButton")
        self.qa_save_button.setIcon(save_details_icon())
        self.qa_save_button.clicked.connect(self._save_member)
        self.qa_save_button.hide()
        self.qa_cancel_button = QPushButton("Cancel")
        self.qa_cancel_button.setObjectName("detailCancelButton")
        self.qa_cancel_button.clicked.connect(self._cancel_member_edit)
        self.qa_cancel_button.hide()
        self.qa_remove_button = QPushButton("Remove")
        self.qa_remove_button.setObjectName("detailRemoveButton")
        self.qa_remove_button.setToolTip("Remove the QA member from this task")
        self.qa_remove_button.clicked.connect(self._remove_member_assignment)
        self.qa_remove_button.hide()
        qa_layout.addWidget(qa_label)
        qa_layout.addWidget(self.qa_value)
        qa_layout.addWidget(self.qa_combo)
        qa_layout.addWidget(self.qa_edit_button)
        qa_layout.addWidget(self.add_qa_button)
        qa_layout.addWidget(self.qa_save_button)
        qa_layout.addWidget(self.qa_cancel_button)
        qa_layout.addWidget(self.qa_remove_button)
        qa_layout.addStretch(1)
        ov.addWidget(qa_row)
        comments_row = QWidget()
        comments_layout = QHBoxLayout(comments_row); comments_layout.setContentsMargins(0, 4, 0, 4)
        comments_label = QLabel("QA comment"); comments_label.setObjectName("muted"); comments_label.setMinimumWidth(145)
        self.comments_value = ClickableLabel(self._display_comment(record.qa_comment))
        self.comments_value.setObjectName("editableDetailValue")
        self.comments_value.setCursor(Qt.PointingHandCursor)
        self.comments_value.setToolTip("Click to edit the QA comment")
        self.comments_value.clicked.connect(self._start_comment_edit)
        self.comments_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.comments_value.setWordWrap(True)
        self.comments_value.setMaximumWidth(900)
        self.comments_editor = QPlainTextEdit()
        self.comments_editor.setMaximumHeight(115)
        self.comments_editor.setFixedWidth(900)
        self.comments_editor.hide()
        self.comment_edit_button = QPushButton("Edit")
        self.comment_edit_button.setObjectName("detailEditButton")
        self.comment_edit_button.setIcon(edit_details_icon())
        self.comment_edit_button.clicked.connect(self._start_comment_edit)
        self.comment_save_button = QPushButton("Save")
        self.comment_save_button.setObjectName("detailSaveButton")
        self.comment_save_button.setIcon(save_details_icon())
        self.comment_save_button.clicked.connect(self._save_comment)
        self.comment_save_button.hide()
        self.comment_cancel_button = QPushButton("Cancel")
        self.comment_cancel_button.setObjectName("detailCancelButton")
        self.comment_cancel_button.clicked.connect(self._cancel_comment_edit)
        self.comment_cancel_button.hide()
        self.comment_remove_button = QPushButton("Remove")
        self.comment_remove_button.setObjectName("detailRemoveButton")
        self.comment_remove_button.setToolTip("Remove the QA comment from this task")
        self.comment_remove_button.clicked.connect(self._remove_comment)
        self.comment_remove_button.hide()
        comments_layout.addWidget(comments_label)
        comments_layout.addWidget(self.comments_value)
        comments_layout.addWidget(self.comments_editor)
        comments_layout.addWidget(self.comment_edit_button)
        comments_layout.addWidget(self.comment_save_button)
        comments_layout.addWidget(self.comment_cancel_button)
        comments_layout.addWidget(self.comment_remove_button)
        comments_layout.addStretch(1)
        ov.addWidget(comments_row)
        ov.addWidget(info_row("SW / HW version", f"{record.sw_version or '—'} / {record.hw_version or '—'}"))
        ov.addWidget(info_row("Chamber", record.chamber))
        ov.addWidget(info_row("Evaluations", f"{len(record.evaluations)} total · {len(record.failed_evaluations)} failed"))
        if record.parse_warning:
            warning = QLabel("⚠  " + record.parse_warning); warning.setObjectName("warning"); warning.setWordWrap(True)
            ov.addWidget(warning)
        ov.addStretch()
        inner.addTab(overview, "Overview")

        steps_page = QWidget()
        steps_layout = QVBoxLayout(steps_page)
        steps_layout.setContentsMargins(12, 12, 12, 12)
        steps_layout.setSpacing(9)
        passed_steps = sum(item.result in ("", "0") for item in record.evaluations)
        failed_steps = len(record.evaluations) - passed_steps
        steps_header = QHBoxLayout()
        steps_title = QLabel(f"{len(record.evaluations)} evaluated steps")
        steps_title.setObjectName("sectionTitle")
        pass_label = QLabel(f"PASS  {passed_steps}")
        pass_label.setObjectName("passPill")
        fail_label = QLabel(f"FAIL  {failed_steps}")
        fail_label.setObjectName("failPill")
        steps_header.addWidget(steps_title)
        steps_header.addStretch()
        steps_header.addWidget(pass_label)
        steps_header.addWidget(fail_label)
        steps_layout.addLayout(steps_header)
        self.steps_model = StepsModel(record.evaluations)
        steps_table = QTableView()
        self.steps_table = steps_table
        steps_table.setModel(self.steps_model)
        steps_table.setAlternatingRowColors(False)
        steps_table.setWordWrap(True)
        steps_table.setEditTriggers(QTableView.NoEditTriggers)
        steps_table.setSelectionBehavior(QTableView.SelectRows)
        steps_table.setSelectionMode(QTableView.SingleSelection)
        steps_table.verticalHeader().hide()
        steps_table.verticalHeader().setDefaultSectionSize(76)
        steps_table.setSortingEnabled(False)
        steps_table.setTextElideMode(Qt.ElideNone)
        steps_table.setHorizontalScrollMode(QTableView.ScrollPerPixel)
        steps_table.setVerticalScrollMode(QTableView.ScrollPerPixel)
        steps_columns = (42, 64, 78, 76, 180, 180, 180, 76, 200, 108)
        steps_header_view = steps_table.horizontalHeader()
        for column, width in enumerate(steps_columns):
            mode = QHeaderView.Stretch if column in (4, 5, 6, 8) else QHeaderView.Fixed
            steps_header_view.setSectionResizeMode(column, mode)
            steps_table.setColumnWidth(column, width)
        steps_layout.addWidget(steps_table, 1)
        inner.addTab(steps_page, f"Steps ({len(record.evaluations)})")

        failures_page = QWidget()
        failures_layout = QVBoxLayout(failures_page)
        failures_layout.setContentsMargins(12, 12, 12, 12)
        failures_layout.setSpacing(9)
        if record.failed_evaluations:
            failures_heading = QLabel(
                f"{len(record.failed_evaluations)} failed evaluations — select a row to inspect its values"
            )
        else:
            failures_heading = QLabel("No failed evaluations were found in this XML.")
        failures_heading.setObjectName("sectionTitle")
        failures_layout.addWidget(failures_heading)
        self.failures_model = StepsModel(record.failed_evaluations)
        failures = QTableView()
        self.failures_table = failures
        failures.setModel(self.failures_model)
        failures.setWordWrap(True)
        failures.setEditTriggers(QTableView.NoEditTriggers)
        failures.setSelectionBehavior(QTableView.SelectRows)
        failures.setSelectionMode(QTableView.SingleSelection)
        failures.verticalHeader().hide()
        failures.verticalHeader().setDefaultSectionSize(76)
        failures.setTextElideMode(Qt.ElideNone)
        failures.setHorizontalScrollMode(QTableView.ScrollPerPixel)
        failures.setVerticalScrollMode(QTableView.ScrollPerPixel)
        failures_header = failures.horizontalHeader()
        for column, width in enumerate(steps_columns):
            mode = QHeaderView.Stretch if column in (4, 5, 6, 8) else QHeaderView.Fixed
            failures_header.setSectionResizeMode(column, mode)
            failures.setColumnWidth(column, width)
        failures_layout.addWidget(failures, 1)
        inner.addTab(failures_page, f"Failures ({len(record.failed_evaluations)})")

        raw = QPlainTextEdit(); raw.setReadOnly(True); raw.setLineWrapMode(QPlainTextEdit.NoWrap)
        raw.setPlainText(record.raw_xml or "Original XML is not available.")
        raw.setFont(QFont("Cascadia Mono", 9))
        inner.addTab(raw, "Original XML")
        layout.addWidget(inner, 1)

    def _require_scope(self) -> bool:
        if self.qa_store.data_file is not None:
            return True
        QMessageBox.information(
            self,
            "Test scope required",
            "Select the matching test_scope.xml before editing QA data.",
        )
        return False

    @staticmethod
    def _display_comment(value: str) -> str:
        return value.strip() or "-"

    def _reload_member_combo(self, selected_id: str = ""):
        self.qa_combo.clear()
        self.qa_combo.addItem("Not assigned", "")
        for member in self.qa_store.members:
            label = member.name if member.name == member.member_id else f"{member.name} ({member.member_id})"
            self.qa_combo.addItem(label, member.member_id)
        selected = self.qa_combo.findData(selected_id)
        self.qa_combo.setCurrentIndex(max(0, selected))

    def _start_member_edit(self):
        if not self._require_scope():
            return
        self.editing_member = True
        self._reload_member_combo(self.record.qa_member_id)
        self.qa_value.hide(); self.qa_edit_button.hide()
        self.qa_combo.show(); self.add_qa_button.show()
        self.qa_save_button.show(); self.qa_cancel_button.show(); self.qa_remove_button.show()
        self.qa_remove_button.setEnabled(bool(self.record.qa_member_id))
        self.qa_combo.setFocus()

    def _cancel_member_edit(self):
        self.editing_member = False
        self.qa_combo.hide(); self.add_qa_button.hide()
        self.qa_save_button.hide(); self.qa_cancel_button.hide(); self.qa_remove_button.hide()
        self.qa_value.show(); self.qa_edit_button.show()

    def _add_qa_member(self):
        if not self._require_scope():
            return
        name, accepted = QInputDialog.getText(self, "Add QA member", "QA name:")
        if not accepted:
            return
        self._run_qa_operation(
            "add_member",
            (name,),
            "Adding QA member from the network.",
            lambda member: self._reload_member_combo(member.member_id),
            self._add_qa_member,
        )

    def _save_member(self):
        member = self.qa_store.member_by_id(str(self.qa_combo.currentData()))
        member_id = member.member_id if member else ""
        self._run_qa_operation(
            "assign_member",
            (self.record.path, member_id),
            "Saving the QA member to the network.",
            lambda _result: self._member_saved(member),
            self._save_member,
        )

    def _member_saved(self, member: QAMember | None):
        member_id = member.member_id if member else ""
        self.record.qa_member_id = member_id
        self.record.qa_member_name = member.name if member else ""
        self.qa_value.setText(self.record.qa_member_name or "Not assigned")
        self._cancel_member_edit()
        self.qa_assignment_changed.emit(self.record)

    def _remove_member_assignment(self):
        self._run_qa_operation(
            "assign_member",
            (self.record.path, ""),
            "Removing the QA member from the network data.",
            lambda _result: self._member_removed(),
            self._remove_member_assignment,
        )

    def _member_removed(self):
        self.record.qa_member_id = ""
        self.record.qa_member_name = ""
        self.qa_value.setText("Not assigned")
        self._cancel_member_edit()
        self.qa_assignment_changed.emit(self.record)

    def _start_comment_edit(self):
        if not self._require_scope():
            return
        self.editing_comment = True
        self.comments_editor.setPlainText(self.record.qa_comment)
        self.comments_value.hide(); self.comment_edit_button.hide()
        self.comments_editor.show()
        self.comment_save_button.show(); self.comment_cancel_button.show(); self.comment_remove_button.show()
        self.comment_remove_button.setEnabled(bool(self.record.qa_comment))
        self.comments_editor.setFocus()

    def _cancel_comment_edit(self):
        self.editing_comment = False
        self.comments_editor.hide()
        self.comment_save_button.hide(); self.comment_cancel_button.hide(); self.comment_remove_button.hide()
        self.comments_value.show(); self.comment_edit_button.show()

    def _save_comment(self):
        qa_comment = self.comments_editor.toPlainText().strip()
        self._run_qa_operation(
            "assign_comment",
            (self.record.path, qa_comment),
            "Saving the QA comment to the network.",
            lambda _result: self._comment_saved(qa_comment),
            self._save_comment,
        )

    def _comment_saved(self, qa_comment: str):
        self.record.qa_comment = qa_comment
        self.comments_value.setText(self._display_comment(qa_comment))
        self._cancel_comment_edit()
        self.qa_assignment_changed.emit(self.record)

    def _remove_comment(self):
        self._run_qa_operation(
            "assign_comment",
            (self.record.path, ""),
            "Removing the QA comment from the network data.",
            lambda _result: self._comment_removed(),
            self._remove_comment,
        )

    def _comment_removed(self):
        self.record.qa_comment = ""
        self.comments_value.setText("-")
        self._cancel_comment_edit()
        self.qa_assignment_changed.emit(self.record)

    def _set_qa_controls_enabled(self, enabled: bool):
        for button in (
            self.add_qa_button,
            self.qa_save_button,
            self.qa_cancel_button,
            self.qa_remove_button,
            self.comment_save_button,
            self.comment_cancel_button,
            self.comment_remove_button,
        ):
            button.setEnabled(enabled)
        self.qa_combo.setEnabled(enabled)
        self.comments_editor.setEnabled(enabled)

    def _run_qa_operation(self, action, values, activity, on_success, retry):
        if self._qa_job is not None:
            return
        self._set_qa_controls_enabled(False)
        network_active = is_network_path(self.qa_store.data_file)
        if network_active:
            self.network_operation_started.emit(self.qa_store.data_file, activity)
        job = QAOperationJob(self.qa_store, action, *values)
        self._qa_job = job

        def finished(result, error):
            self._qa_job = None
            self._set_qa_controls_enabled(True)
            if network_active:
                self.network_operation_finished.emit()
            if error:
                self._show_qa_error(error, retry)
                return
            on_success(result)

        job.signals.finished.connect(finished)
        if self.qa_pool is None:
            job.run()
        else:
            self.qa_pool.start(job)

    def _show_qa_error(self, error: str, retry):
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        file_in_use = "is in use by another application" in error
        message.setWindowTitle(
            f"{RELATIONSHIPS_DATA_FILE_NAME} is in use"
            if file_in_use
            else "Unable to update QA data"
        )
        message.setText(
            "The QA change was not saved."
            if file_in_use
            else error
        )
        retry_button = None
        if file_in_use:
            message.setInformativeText(
                "Ask the team to close the legacy Test Manager using this scope, "
                "then click Retry."
            )
            message.setDetailedText(error)
            retry_button = message.addButton("Retry", QMessageBox.ButtonRole.AcceptRole)
        elif is_network_path(self.qa_store.data_file):
            message.setInformativeText(
                "Check the VPN or network drive connection and try again. "
                "If the legacy Test Manager is open, ask the team to close it."
            )
            retry_button = message.addButton("Retry", QMessageBox.ButtonRole.AcceptRole)
        message.addButton(QMessageBox.StandardButton.Cancel)
        message.exec()
        if retry_button is not None and message.clickedButton() is retry_button:
            retry()


class ScopeHelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Test scope guide")
        self.setModal(True)
        self.setFixedSize(610, 590)
        self.setObjectName("scopeHelpDialog")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setObjectName("scopeHelpHeader")
        header.setFixedHeight(112)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(26, 20, 26, 19)
        header_layout.setSpacing(5)
        eyebrow = QLabel("TEST SCOPE GUIDE")
        eyebrow.setObjectName("scopeHelpEyebrow")
        title = QLabel("Keep the scope and results together")
        title.setObjectName("scopeHelpTitle")
        subtitle = QLabel(
            "Use the test scope created for the same test campaign as the XML results."
        )
        subtitle.setObjectName("scopeHelpSubtitle")
        subtitle.setWordWrap(True)
        header_layout.addWidget(eyebrow)
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        root.addWidget(header)

        body = QWidget()
        body.setObjectName("scopeHelpBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(26, 22, 26, 22)
        body_layout.setSpacing(14)

        correct = QLabel(
            "<b>Recommended project structure</b><br><br>"
            "<span style='font-family:Consolas; color:#30405F;'>"
            "Test campaign/<br>"
            "&nbsp;&nbsp;test_scope.xml<br>"
            "&nbsp;&nbsp;ST-I_ Results/<br>"
            "&nbsp;&nbsp;&nbsp;&nbsp;result XML files..."
            "</span>"
        )
        correct.setObjectName("scopeStructureCard")
        correct.setTextFormat(Qt.RichText)
        correct.setContentsMargins(15, 12, 15, 12)
        correct.setMinimumHeight(128)
        body_layout.addWidget(correct)

        note = QLabel(
            "<b>The scope does not need to be inside the results folder.</b><br>"
            "It should be stored in the same project directory and must describe "
            "the tests selected for that results set."
        )
        note.setObjectName("scopeInfoCard")
        note.setContentsMargins(13, 10, 13, 10)
        note.setWordWrap(True)
        note.setTextFormat(Qt.RichText)
        body_layout.addWidget(note)

        warning = QFrame()
        warning.setObjectName("scopeWarningCard")
        warning_layout = QHBoxLayout(warning)
        warning_layout.setContentsMargins(13, 10, 13, 10)
        warning_layout.setSpacing(10)
        warning_icon = QLabel()
        warning_icon.setObjectName("scopeWarningIcon")
        warning_icon.setPixmap(
            self.style().standardIcon(QStyle.SP_MessageBoxWarning).pixmap(18, 18)
        )
        warning_icon.setFixedSize(20, 20)
        warning_icon.setAlignment(Qt.AlignCenter)
        warning_text = QLabel(
            "<b>Why this matters</b><br>"
            "A scope from another campaign can make Selected, Missing, Completed "
            "and Pass rate values look valid even when the comparison is wrong."
        )
        warning_text.setObjectName("scopeWarningText")
        warning_text.setWordWrap(True)
        warning_text.setTextFormat(Qt.RichText)
        warning_layout.addWidget(warning_icon, 0, Qt.AlignTop)
        warning_layout.addWidget(warning_text, 1)
        body_layout.addWidget(warning)

        actions = QHBoxLayout()
        actions.addStretch()
        close_button = QPushButton("Got it")
        close_button.setMinimumWidth(100)
        close_button.clicked.connect(self.accept)
        actions.addWidget(close_button)
        body_layout.addLayout(actions)
        root.addWidget(body)


class DetailLoadingPage(QWidget):
    def __init__(self, record: TestRecord):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.addStretch()
        self.title = QLabel(f"Loading details for test {record.test_id}")
        self.title.setObjectName("sectionTitle")
        self.title.setAlignment(Qt.AlignCenter)
        self.message = QLabel("Reading and preparing the XML steps. Large files may take a moment.")
        self.message.setObjectName("muted")
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setObjectName("loadingProgress")
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(9)
        layout.addWidget(self.title)
        layout.addSpacing(8)
        layout.addWidget(self.message)
        layout.addSpacing(14)
        layout.addWidget(self.progress)
        layout.addStretch()

    def show_error(self, error: str):
        self.title.setText("The test details could not be loaded")
        self.message.setText(error)
        self.message.setStyleSheet("color: #BD354D;")
        self.progress.hide()


class NetworkBanner(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("networkBanner")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 9, 18, 9)
        layout.setSpacing(14)
        icon = QLabel("NETWORK")
        icon.setObjectName("networkBannerTag")
        self.label = QLabel()
        self.label.setObjectName("networkBannerText")
        self.progress = QProgressBar()
        self.progress.setObjectName("networkBannerProgress")
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.progress.setMaximumWidth(260)
        layout.addWidget(icon)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.progress)
        self.hide()

    def start(self, path: Path, action: str):
        location = network_location_name(path)
        self.label.setText(
            f"{location} may take a little longer to load. {action} Please wait..."
        )
        self.progress.setRange(0, 0)
        self.show()

    def set_progress(self, current: int, total: int):
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(current)

    def stop(self):
        self.hide()


class Dashboard(QWidget):
    open_record = Signal(object)
    choose_xml = Signal()
    choose_folder = Signal()
    choose_scope = Signal()
    clear_scope = Signal()
    refresh = Signal()
    report_pdf = Signal()

    def __init__(self, model: ResultsModel, proxy: ResultsProxy):
        super().__init__()
        self.model, self.proxy = model, proxy
        self._records: list[TestRecord] = []
        self._scope_ids: set[str] = set()
        root = QVBoxLayout(self); root.setContentsMargins(18, 10, 18, 12); root.setSpacing(10)
        top = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(2)
        brand = QLabel(
            "<span style='font-size:20px; font-weight:800; color:#3E328A;'>TRIDONIC</span>"
            "&nbsp;&nbsp;<span style='font-size:18px; font-weight:400; color:#3E328A;'>WE MANAGE LIGHT</span>"
        )
        brand.setObjectName("brand")
        eyebrow = QLabel("ONE4ALL · TEST REPORTS"); eyebrow.setObjectName("eyebrow")
        title = QLabel("Test overview"); title.setObjectName("pageTitle")
        self.folder_label = QLabel("No result source selected"); self.folder_label.setObjectName("muted")
        self.scope_label = QLabel("Test scope: no XML selected"); self.scope_label.setObjectName("muted")
        self.path_toggle = QPushButton("Show paths")
        self.path_toggle.setObjectName("pathToggle")
        self.path_toggle.setCheckable(True)
        self.path_toggle.setChecked(False)
        self.path_toggle.setToolTip("Show or hide the selected result and test scope paths")
        self.path_toggle.toggled.connect(self._toggle_paths)
        title_row = QHBoxLayout()
        title_row.setSpacing(9)
        title_row.addWidget(title)
        title_row.addWidget(self.path_toggle)
        title_row.addStretch()
        self.paths_widget = QWidget()
        paths_layout = QVBoxLayout(self.paths_widget)
        paths_layout.setContentsMargins(0, 0, 0, 0)
        paths_layout.setSpacing(1)
        paths_layout.addWidget(self.folder_label)
        paths_layout.addWidget(self.scope_label)
        self.paths_widget.hide()
        heading.addWidget(brand)
        heading.addWidget(eyebrow)
        heading.addLayout(title_row)
        heading.addWidget(self.paths_widget)
        choose_xml = QPushButton("  Open XML")
        choose_xml.setIcon(self.style().standardIcon(QStyle.SP_FileIcon))
        choose_xml.clicked.connect(self.choose_xml)
        choose = QPushButton("Open folder")
        choose.setObjectName("secondaryButton")
        choose.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        choose.clicked.connect(self.choose_folder)
        self.choose_scope_button = QPushButton("Test scope XML")
        self.choose_scope_button.setObjectName("secondaryButton")
        self.choose_scope_button.setToolTip(
            "Select the test_scope.xml file containing the selected tests"
        )
        self.choose_scope_button.clicked.connect(self.choose_scope)
        scope_help = QPushButton("Scope help")
        scope_help.setObjectName("secondaryButton")
        scope_help.setIcon(self.style().standardIcon(QStyle.SP_MessageBoxQuestion))
        scope_help.setToolTip("Important information about matching the test scope to the results")
        scope_help.clicked.connect(self._show_scope_help)
        self.clear_scope_button = QPushButton("Clear scope")
        self.clear_scope_button.setObjectName("clearScopeButton")
        self.clear_scope_button.setToolTip("Unload the current test scope and disable all progress groups")
        self.clear_scope_button.clicked.connect(self.clear_scope)
        self.clear_scope_button.hide()
        reload_btn = QPushButton("Refresh"); reload_btn.setObjectName("secondaryButton"); reload_btn.clicked.connect(self.refresh)
        self.report_button = QPushButton("Report PDF")
        self.report_button.setObjectName("reportButton")
        self.report_button.setIcon(self.style().standardIcon(QStyle.SP_DialogSaveButton))
        self.report_button.setToolTip("Generate the meeting PDF from the selected results folder and test scope")
        self.report_button.clicked.connect(self.report_pdf)
        top.addLayout(heading, 1)
        top.addWidget(reload_btn)
        top.addWidget(self.report_button)
        top.addWidget(self.clear_scope_button)
        top.addWidget(self.choose_scope_button)
        top.addWidget(scope_help)
        top.addWidget(choose)
        top.addWidget(choose_xml)
        root.addLayout(top)

        cards = QHBoxLayout(); cards.setSpacing(8)
        self.cards = {status: StatCard(status) for status in ("passed", "failed", "skipped", "aborted", "draft")}
        for status, card in self.cards.items():
            card.clicked.connect(self._card_filter); cards.addWidget(card)
        root.addLayout(cards)

        self.loading_panel = QFrame()
        self.loading_panel.setObjectName("loadingPanel")
        loading_layout = QHBoxLayout(self.loading_panel)
        loading_layout.setContentsMargins(13, 7, 13, 7)
        loading_layout.setSpacing(12)
        self.loading_label = QLabel("Reading XML results…")
        self.loading_label.setObjectName("loadingLabel")
        self.loading_progress = QProgressBar()
        self.loading_progress.setObjectName("loadingProgress")
        self.loading_progress.setTextVisible(False)
        self.loading_progress.setFixedHeight(8)
        loading_layout.addWidget(self.loading_label)
        loading_layout.addWidget(self.loading_progress, 1)
        self.loading_panel.hide()
        root.addWidget(self.loading_panel)

        splitter = QSplitter(Qt.Horizontal); splitter.setChildrenCollapsible(False)
        table_panel = QFrame(); table_panel.setObjectName("panel")
        tp = QVBoxLayout(table_panel); tp.setContentsMargins(12, 10, 12, 10); tp.setSpacing(8)
        controls = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search by ID, name, DUT, or tester…"); self.search.setClearButtonEnabled(True)
        self.status_filter = QComboBox(); self.status_filter.addItem("Filter by status", "all")
        for status, data in STATUS_META.items(): self.status_filter.addItem(data[0], status)
        self.status_filter.setToolTip("Filter the loaded results by test status")
        self.number_filter = QComboBox(); self.number_filter.addItem("Filter by number", "all")
        self.number_filter.setToolTip("Filter by Function Block number found in the loaded results")
        self.scope_mode = QPushButton("Load all results")
        self.scope_mode.setObjectName("scopeModeButton")
        self.scope_mode.setCheckable(True)
        self.scope_mode.setEnabled(False)
        self.scope_mode.setToolTip(
            "When enabled, only result files whose test IDs are selected in the loaded "
            "test_scope.xml are visible. When disabled, every XML in the Results folder is "
            "visible, including tests that were executed and later removed from the scope."
        )
        self.filter_badge = QPushButton("●  FILTER ACTIVE — CLEAR")
        self.filter_badge.setObjectName("activeFilterBadge")
        self.filter_badge.setToolTip("A search, status, or Function Block filter is active. Click to clear all filters.")
        self.filter_badge.clicked.connect(self._clear_filters)
        self.filter_badge.hide()
        self.filter_opacity = QGraphicsOpacityEffect(self.filter_badge)
        self.filter_badge.setGraphicsEffect(self.filter_opacity)
        self.filter_animation = QPropertyAnimation(self.filter_opacity, b"opacity", self)
        self.filter_animation.setDuration(1000)
        self.filter_animation.setLoopCount(-1)
        self.filter_animation.setEasingCurve(QEasingCurve.InOutSine)
        self.filter_animation.setKeyValueAt(0.0, 1.0)
        self.filter_animation.setKeyValueAt(0.5, 0.38)
        self.filter_animation.setKeyValueAt(1.0, 1.0)
        self.search.textChanged.connect(self._filters_changed)
        self.status_filter.currentIndexChanged.connect(self._filters_changed)
        self.number_filter.currentIndexChanged.connect(self._filters_changed)
        self.scope_mode.toggled.connect(self._scope_mode_changed)
        self.visible_label = QLabel("0 results"); self.visible_label.setObjectName("muted")
        controls.addWidget(self.search, 1)
        controls.addWidget(self.status_filter)
        controls.addWidget(self.number_filter)
        controls.addWidget(self.scope_mode)
        controls.addWidget(self.filter_badge)
        controls.addWidget(self.visible_label)
        tp.addLayout(controls)
        self.table = QTableView(); self.table.setModel(proxy); self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QTableView.SelectRows); self.table.setSelectionMode(QTableView.SingleSelection)
        self.table.setEditTriggers(QTableView.NoEditTriggers); self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True); self.table.setShowGrid(False)
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.ElideNone)
        self.table.verticalHeader().setDefaultSectionSize(58)
        header = self.table.horizontalHeader(); header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents); header.setSectionResizeMode(1, QHeaderView.Stretch)
        for col in range(2, 6): header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.Interactive)
        header.setSectionResizeMode(7, QHeaderView.Interactive)
        header.setSectionResizeMode(8, QHeaderView.Stretch)
        self.table.setColumnWidth(6, 190)
        self.table.setColumnWidth(7, 170)
        self.table.clicked.connect(self._open_index)
        tp.addWidget(self.table, 1)
        splitter.addWidget(table_panel)

        side = QFrame(); side.setObjectName("panel"); side.setMaximumWidth(285); side.setMinimumWidth(245)
        self.distribution_panel = side
        sl = QVBoxLayout(side); sl.setContentsMargins(16, 10, 16, 10); sl.setSpacing(5)
        self.distribution_layout = sl
        side_title = QLabel("Distribution"); side_title.setObjectName("sectionTitle")
        self.chart = DonutChart(); self.legend = QVBoxLayout()
        sl.addWidget(side_title); sl.addWidget(self.chart); sl.addLayout(self.legend); sl.addStretch()
        hint = QLabel("Tip: click a test to open its details in a new tab.")
        self.distribution_hint = hint
        hint.setObjectName("hint"); hint.setWordWrap(True); sl.addWidget(hint)
        splitter.addWidget(side); splitter.setSizes([1050, 260])
        root.addWidget(splitter, 1)

        self.clusters_panel = QFrame()
        self.clusters_panel.setObjectName("panel")
        clusters_layout = QVBoxLayout(self.clusters_panel)
        clusters_layout.setContentsMargins(14, 7, 14, 7)
        clusters_layout.setSpacing(4)
        clusters_header = QHBoxLayout()
        clusters_title = QLabel("Selected test scope progress")
        clusters_title.setObjectName("sectionTitle")
        self.clusters_summary = QLabel("Select a test_scope.xml file")
        self.clusters_summary.setObjectName("muted")
        clusters_header.addWidget(clusters_title)
        clusters_header.addStretch()
        clusters_header.addWidget(self.clusters_summary)
        clusters_layout.addLayout(clusters_header)
        clusters_body = QHBoxLayout()
        clusters_body.setSpacing(14)
        self.clusters_content = QWidget()
        self.clusters_grid = QGridLayout(self.clusters_content)
        self.clusters_grid.setContentsMargins(0, 0, 0, 0)
        self.clusters_grid.setHorizontalSpacing(10)
        self.clusters_grid.setVerticalSpacing(0)
        clusters_body.addWidget(self.clusters_content, 1)

        overall = QFrame()
        overall.setObjectName("overallPanel")
        self.overall_panel = overall
        overall.setFixedWidth(238)
        ol = QVBoxLayout(overall)
        ol.setContentsMargins(14, 8, 14, 8)
        ol.setSpacing(5)
        overall_title = QLabel("Scope summary")
        overall_title.setStyleSheet("font-weight: 700; color: #253653;")
        ol.addWidget(overall_title)
        approval_row = QHBoxLayout()
        approval_row.addWidget(QLabel("Pass rate"))
        approval_row.addStretch()
        self.overall_pass_label = QLabel("0/0 · 0.0%")
        self.overall_pass_label.setStyleSheet("font-weight: 700;")
        approval_row.addWidget(self.overall_pass_label)
        ol.addLayout(approval_row)
        self.overall_pass_bar = QProgressBar()
        self.overall_pass_bar.setRange(0, 1000)
        self.overall_pass_bar.setTextVisible(False)
        self.overall_pass_bar.setFixedHeight(9)
        self.overall_pass_bar.setObjectName("scopeProgress")
        ol.addWidget(self.overall_pass_bar)
        execution_row = QHBoxLayout()
        execution_row.addWidget(QLabel("Scope completed"))
        execution_row.addStretch()
        self.overall_execution_label = QLabel("0/0 · 0.0%")
        self.overall_execution_label.setStyleSheet("font-weight: 700;")
        execution_row.addWidget(self.overall_execution_label)
        ol.addLayout(execution_row)
        self.overall_execution_bar = QProgressBar()
        self.overall_execution_bar.setRange(0, 1000)
        self.overall_execution_bar.setTextVisible(False)
        self.overall_execution_bar.setFixedHeight(9)
        self.overall_execution_bar.setObjectName("executionProgress")
        ol.addWidget(self.overall_execution_bar)
        ol.addStretch()
        self._scope_tooltip_filter = InstantToolTipFilter(self)
        for widget in (overall, *overall.findChildren(QWidget)):
            widget.installEventFilter(self._scope_tooltip_filter)
        clusters_body.addWidget(overall)
        clusters_layout.addLayout(clusters_body)
        root.addWidget(self.clusters_panel)
        proxy.rowsInserted.connect(self._update_visible); proxy.rowsRemoved.connect(self._update_visible); proxy.modelReset.connect(self._update_visible)
        self.update_data(None, [], None, [])

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, "distribution_panel"):
            return
        # A 4K display at 250% has roughly 864 logical pixels vertically.
        # Compact only this panel so the chart, legend, and tip are never clipped.
        compact = event.size().height() < 850
        if getattr(self, "_distribution_compact", None) == compact:
            return
        self._distribution_compact = compact
        self.chart.set_compact(compact)
        self.distribution_hint.setVisible(not compact)
        self.distribution_panel.setMinimumWidth(220 if compact else 245)
        self.distribution_panel.setMaximumWidth(265 if compact else 285)
        self.distribution_layout.setContentsMargins(
            12 if compact else 16,
            7 if compact else 10,
            12 if compact else 16,
            7 if compact else 10,
        )
        self.distribution_layout.setSpacing(2 if compact else 5)

    def _card_filter(self, status: str):
        idx = self.status_filter.findData(status); self.status_filter.setCurrentIndex(idx)

    def _show_scope_help(self):
        ScopeHelpDialog(self).exec()

    def _toggle_paths(self, visible: bool):
        self.paths_widget.setVisible(visible)
        self.path_toggle.setText("Hide paths" if visible else "Show paths")

    def _filters_changed(self, *args):
        self.proxy.set_query(self.search.text())
        self.proxy.set_status(self.status_filter.currentData())
        self.proxy.set_function_block(self.number_filter.currentData())
        self.proxy.set_scope_ids(self._scope_ids if self.scope_mode.isChecked() else None)
        active = (
            bool(self.search.text().strip())
            or self.status_filter.currentData() != "all"
            or self.number_filter.currentData() != "all"
        )
        self.filter_badge.setVisible(active)
        if active and self.filter_animation.state() != QAbstractAnimation.Running:
            self.filter_animation.start()
        elif not active:
            self.filter_animation.stop()
            self.filter_opacity.setOpacity(1.0)
        if hasattr(self, "chart"):
            self._update_distribution(self._records_matching_active_filters())

    def _scope_mode_changed(self, checked: bool):
        self.scope_mode.setText("Load scope results" if checked else "Load all results")
        self.proxy.set_scope_ids(self._scope_ids if checked else None)
        self._update_result_summary(self._records_for_current_mode())
        self._update_visible()

    def _records_for_current_mode(self) -> list[TestRecord]:
        if self.scope_mode.isChecked():
            return [record for record in self._records if record.test_id in self._scope_ids]
        return self._records

    def _records_matching_active_filters(self) -> list[TestRecord]:
        """Return the same records currently accepted by the table proxy."""
        query = self.search.text().casefold().strip()
        status = self.status_filter.currentData()
        function_block = self.number_filter.currentData()
        filtered: list[TestRecord] = []
        for record in self._records_for_current_mode():
            if status != "all" and record.status != status:
                continue
            if function_block != "all" and record.family != function_block:
                continue
            searchable = " ".join(
                (
                    record.test_id,
                    record.title,
                    record.file_name,
                    record.dut,
                    record.tester,
                    record.qa_member_name,
                    record.qa_comment,
                )
            ).casefold()
            if query and query not in searchable:
                continue
            filtered.append(record)
        return filtered

    def _update_result_summary(self, records: list[TestRecord]):
        counts = {status: sum(record.status == status for record in records) for status in STATUS_META}
        total = len(records)
        for status, card in self.cards.items():
            card.set_value(counts[status], total)
        self._update_distribution(self._records_matching_active_filters())

    def _update_distribution(self, records: list[TestRecord]):
        counts = {status: sum(record.status == status for record in records) for status in STATUS_META}
        total = len(records)
        self.chart.set_counts(counts)
        while self.legend.count():
            item = self.legend.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for status in STATUS_META:
            if not counts[status]:
                continue
            label, color, _ = STATUS_META[status]
            pct = counts[status] * 100 / total if total else 0
            row = QLabel(
                f"<span style='color:{color};font-size:14px'>●</span>  {label} &nbsp; "
                f"<b>{counts[status]}</b> · {pct:.1f}%"
            )
            row.setObjectName("distributionLegend")
            self.legend.addWidget(row)

    def _clear_filters(self):
        self.search.clear()
        self.status_filter.setCurrentIndex(0)
        self.number_filter.setCurrentIndex(0)
        self._filters_changed()

    def _update_number_filter(self, records: list[TestRecord]):
        """Build the Function Block choices only from the currently loaded results."""
        selected = self.number_filter.currentData()
        families = sorted(
            {record.family for record in records if record.family.isdigit()},
            key=int,
        )
        blocker = QSignalBlocker(self.number_filter)
        self.number_filter.clear()
        self.number_filter.addItem("Filter by number", "all")
        for family in families:
            self.number_filter.addItem(f"FB {family}", family)
        selected_index = self.number_filter.findData(selected)
        self.number_filter.setCurrentIndex(selected_index if selected_index >= 0 else 0)
        del blocker
        self._filters_changed()

    def start_loading(self, message: str = "Discovering XML results…"):
        self.loading_label.setText(message)
        self.loading_progress.setTextVisible(False)
        self.loading_progress.setRange(0, 0)
        self.loading_panel.show()

    def update_loading(self, current: int, total: int, file_name: str):
        self.loading_progress.setRange(0, max(1, total))
        self.loading_progress.setValue(current)
        self.loading_label.setText(f"Reading XML results — {current}/{total}")
        self.loading_label.setToolTip(file_name)

    def stop_loading(self):
        self.loading_panel.hide()
        self.loading_label.setToolTip("")

    def set_results_loading(self, active: bool):
        self.choose_scope_button.setEnabled(not active)

    def set_report_generating(self, active: bool):
        self.report_button.setEnabled(not active)
        self.report_button.setText("Generating PDF..." if active else "Report PDF")
        if active:
            self.loading_label.setText("Generating PDF report — 0%")
            self.loading_progress.setTextVisible(True)
            self.loading_progress.setRange(0, 100)
            self.loading_progress.setValue(0)
            self.loading_panel.show()
        else:
            self.stop_loading()
            self.loading_progress.setTextVisible(False)

    def update_report_progress(self, percent: int, message: str):
        percent = max(0, min(100, percent))
        self.loading_progress.setRange(0, 100)
        self.loading_progress.setValue(percent)
        self.loading_label.setText(f"Generating PDF report — {percent}% · {message}")

    def _open_index(self, proxy_index: QModelIndex):
        source = self.proxy.mapToSource(proxy_index)
        self.open_record.emit(self.model.records[source.row()])

    def _update_visible(self):
        self.visible_label.setText(f"{self.proxy.rowCount()} results")

    def update_data(
        self,
        folder: Path | None,
        records: list[TestRecord],
        scope_file: Path | None = None,
        scope_groups: list[ScopeGroup] | None = None,
    ):
        self._records = records
        groups = (scope_groups or empty_scope_groups()) if scope_file else empty_scope_groups()
        self._scope_ids = {
            test_id for group in groups for test_id in group.test_ids
        } if scope_file else set()
        scope_was_available = self.scope_mode.isEnabled()
        blocker = QSignalBlocker(self.scope_mode)
        self.scope_mode.setEnabled(bool(scope_file))
        if not scope_file:
            self.scope_mode.setChecked(False)
        elif not scope_was_available:
            self.scope_mode.setChecked(True)
        del blocker
        self._scope_mode_changed(self.scope_mode.isChecked())
        self._update_number_filter(records)
        if folder:
            source_kind = "Single XML" if folder.is_file() else "Results folder"
            self.folder_label.setText(f"{source_kind}: {folder}")
        else:
            self.folder_label.setText("No result source selected")
        while self.clusters_grid.count():
            item = self.clusters_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        cluster_cards: list[ClusterCard] = []
        column_count = 7
        row_count = max(1, (len(groups) + column_count - 1) // column_count)
        for index, group in enumerate(groups):
            card = ClusterCard(group, records)
            cluster_cards.append(card)
            row = index % row_count
            column = index // row_count
            self.clusters_grid.addWidget(card, row, column)
        self.clusters_panel.setFixedHeight(50 + row_count * 24)
        expected_total = sum(card.total for card in cluster_cards)
        passed_total = sum(card.passed for card in cluster_cards)
        executed_total = sum(card.executed for card in cluster_cards)
        scope_counts = {
            status: sum(card.result_counts[status] for card in cluster_cards)
            for status in ("passed", "failed", "skipped", "aborted", "unknown")
        }
        draft_total = sum(card.draft_count for card in cluster_cards)
        missing_total = sum(card.missing for card in cluster_cards)
        pass_pct = passed_total * 100 / expected_total if expected_total else 0
        execution_pct = executed_total * 100 / expected_total if expected_total else 0
        self.overall_pass_bar.setValue(round(pass_pct * 10))
        self.overall_execution_bar.setValue(round(execution_pct * 10))
        self.overall_pass_label.setText(f"{passed_total}/{expected_total} · {pass_pct:.1f}%")
        self.overall_execution_label.setText(
            f"{executed_total}/{expected_total} · {execution_pct:.1f}%"
        )
        if expected_total:
            scope_tooltip = (
                "<b>Selected test scope summary</b><hr>"
                f"Selected: <b>{expected_total}</b><br>"
                f"Passed: <b>{scope_counts['passed']}</b><br>"
                f"Failed: <b>{scope_counts['failed']}</b><br>"
                f"Skipped: <b>{scope_counts['skipped']}</b><br>"
                f"Aborted: <b>{scope_counts['aborted']}</b><br>"
                f"Draft: <b>{draft_total}</b><br>"
                f"Unknown: <b>{scope_counts['unknown']}</b><br>"
                f"Completed: <b>{executed_total}</b><br>"
                f"Missing: <b>{missing_total}</b><br>"
                f"Pass rate: <b>{pass_pct:.1f}%</b><br>"
                f"Scope completed: <b>{execution_pct:.1f}%</b>"
            )
        else:
            scope_tooltip = (
                "<b>Selected test scope summary</b><hr>"
                "No test scope is loaded.<br>All fixed groups are disabled."
            )
        for widget in (self.overall_panel, *self.overall_panel.findChildren(QWidget)):
            widget.setToolTip(scope_tooltip)
        if scope_file:
            self.clear_scope_button.show()
            self.scope_label.setText(f"Test scope XML: {scope_file}")
            expected = sum(len(group.test_ids) for group in groups)
            active_groups = sum(bool(group.test_ids) for group in groups)
            self.clusters_summary.setText(
                f"{expected} selected tests · {active_groups} active of {len(groups)} fixed groups"
            )
        else:
            self.clear_scope_button.hide()
            self.scope_label.setText("Test scope: no XML selected")
            self.clusters_summary.setText(
                f"{len(groups)} fixed groups · select a test_scope.xml to activate progress"
            )
        self._update_visible()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tridonic One4All Viewer — Version 1.9")
        self.resize(1440, 900); self.setMinimumSize(1200, 760)
        self.qa_store = QAMemberStore()
        self.model = ResultsModel(self.qa_store); self.proxy = ResultsProxy(); self.proxy.setSourceModel(self.model)
        self.tabs = QTabWidget(); self.tabs.setTabsClosable(True); self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        shell = QWidget()
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        self.network_banner = NetworkBanner()
        shell_layout.addWidget(self.network_banner)
        shell_layout.addWidget(self.tabs, 1)
        self.setCentralWidget(shell)
        self.dashboard = Dashboard(self.model, self.proxy)
        self.dashboard.choose_xml.connect(self.choose_xml)
        self.dashboard.choose_folder.connect(self.choose_folder); self.dashboard.refresh.connect(self.refresh)
        self.dashboard.report_pdf.connect(self.generate_pdf_report)
        self.dashboard.open_record.connect(self.open_record)
        self.tabs.addTab(self.dashboard, "Overview")
        self.tabs.tabBar().setTabButton(0, QTabBar.ButtonPosition.RightSide, None)
        self.watcher = QFileSystemWatcher(self); self.watcher.directoryChanged.connect(self._schedule_refresh)
        self.refresh_timer = QTimer(self); self.refresh_timer.setSingleShot(True); self.refresh_timer.setInterval(650)
        self.refresh_timer.timeout.connect(self.refresh)
        self.pool = QThreadPool.globalInstance()
        self.qa_pool = QThreadPool(self)
        self.qa_pool.setMaxThreadCount(1)
        self.folder: Path | None = None
        self.source_path: Path | None = None
        self.scope_file: Path | None = None
        self.scope_groups: list[ScopeGroup] = []
        self.scanning = False
        self.reporting = False
        self.pending_refresh = False
        self._detail_jobs: dict[str, DetailJob] = {}
        self._report_job: ReportJob | None = None
        self._scope_job: ScopeLoadJob | None = None
        self._scope_job_silent = False
        self._network_activity_count = 0
        self._scan_cache: dict[str, tuple[int, int, TestRecord]] = {}
        version_label = QLabel("VERSION 1.9")
        version_label.setObjectName("footerMeta")
        powered_label = QLabel("POWERED BY PT TEAM")
        powered_label.setObjectName("footerBrand")
        self.statusBar().addPermanentWidget(version_label)
        self.statusBar().addPermanentWidget(PortugalFlag())
        self.statusBar().addPermanentWidget(powered_label)
        self.dashboard.choose_scope.connect(self.choose_scope_file)
        self.dashboard.clear_scope.connect(self.clear_scope)
        self.default_results = APP_DIR / "ST-I_ Results"
        self.default_scope = APP_DIR / "test_scope.xml"
        # Let the first window frame render before any filesystem discovery.
        QTimer.singleShot(250, self._load_initial_sources)

    def _load_initial_sources(self):
        if self.scope_file is None and self.default_scope.is_file():
            self.set_scope_file(self.default_scope, silent=True)
        if self.source_path is None and self.default_results.is_dir():
            self.set_folder(self.default_results)

    def _network_start(self, path: Path | None, action: str):
        if not is_network_path(path):
            return
        self._network_activity_count += 1
        self.network_banner.start(path, action)

    def _network_stop(self, path: Path | None = None):
        if path is not None and not is_network_path(path):
            return
        self._network_activity_count = max(0, self._network_activity_count - 1)
        if self._network_activity_count == 0:
            self.network_banner.stop()

    def _offer_network_retry(self, title: str, error: str, retry):
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        message.setWindowTitle(title)
        message.setText(error)
        message.setInformativeText("Check the VPN or network drive connection and try again.")
        retry_button = message.addButton("Retry", QMessageBox.ButtonRole.AcceptRole)
        message.addButton(QMessageBox.StandardButton.Cancel)
        message.exec()
        if message.clickedButton() is retry_button:
            retry()

    def set_folder(self, folder: Path):
        folder = normalized_path(folder)
        self.folder = folder
        self.source_path = folder
        self._scan_cache.clear()
        old = self.watcher.directories()
        if old: self.watcher.removePaths(old)
        old_files = self.watcher.files()
        if old_files: self.watcher.removePaths(old_files)
        # Avoid a second recursive walk on the UI thread. The worker still scans
        # XML files recursively; the watcher covers the root and direct folders.
        watched = [str(folder)]
        if not is_network_path(folder):
            try:
                watched.extend(str(path) for path in folder.iterdir() if path.is_dir())
            except OSError:
                pass
        self.watcher.addPaths(watched); self.refresh()

    def set_file(self, file_path: Path):
        file_path = normalized_path(file_path)
        if file_path.suffix.lower() != ".xml":
            QMessageBox.warning(self, "Invalid file", "Please select a valid XML file.")
            return
        self.folder = file_path.parent
        self.source_path = file_path
        self._scan_cache.clear()
        old = self.watcher.directories()
        if old: self.watcher.removePaths(old)
        old_files = self.watcher.files()
        if old_files: self.watcher.removePaths(old_files)
        self.watcher.addPaths([str(file_path.parent), str(file_path)])
        self.refresh()

    def choose_folder(self):
        # Use the native folder-only picker. Showing XML files here forces Qt to
        # enumerate every result while browsing a network drive, before the
        # user has even selected the folder.
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Select results folder",
            str(self.folder or Path.home()),
            QFileDialog.Option.ShowDirsOnly,
        )
        if chosen:
            self.set_folder(Path(chosen))

    def choose_scope_file(self):
        start = str(self.scope_file.parent if self.scope_file else Path.home())
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Select Test Manager scope XML",
            start,
            "Test scope XML (*.xml);;All files (*)",
        )
        if chosen:
            self.set_scope_file(Path(chosen))

    def set_scope_file(self, scope_file: Path, silent: bool = False):
        if self.scanning:
            self.statusBar().showMessage(
                "Wait for all results to finish loading before selecting the test scope.",
                5000,
            )
            return
        scope_file = normalized_path(scope_file)
        self._scope_job_path = scope_file
        self._scope_job_silent = silent
        self.dashboard.start_loading("Loading test scope and QA data…")
        self._network_start(scope_file, "Loading the test scope and QA data.")
        job = ScopeLoadJob(scope_file)
        self._scope_job = job
        job.signals.finished.connect(self._scope_loaded)
        self.pool.start(job)

    def _scope_loaded(self, payload, error: str, loaded_path):
        loaded_path = normalized_path(Path(loaded_path))
        self._network_stop(loaded_path)
        if loaded_path != getattr(self, "_scope_job_path", None):
            return
        self._scope_job = None
        self.dashboard.stop_loading()
        if error:
            if self._scope_job_silent:
                self.statusBar().showMessage(f"Test scope could not be loaded: {error}", 6000)
            elif is_network_path(loaded_path):
                self._offer_network_retry(
                    "Unable to load test scope",
                    error,
                    lambda: self.set_scope_file(loaded_path),
                )
            else:
                QMessageBox.warning(
                    self, "Invalid test scope", f"The scope XML could not be read:\n{error}"
                )
            return
        groups, members, assignments, warning = payload
        self.scope_file = loaded_path
        self.scope_groups = groups
        self.qa_store.set_loaded_data(
            loaded_path.parent / RELATIONSHIPS_DATA_FILE_NAME, members, assignments, warning
        )
        if self.model.records:
            self.model.set_records(list(self.model.records))
        if self.source_path:
            self.dashboard.update_data(
                self.source_path, self.model.records, self.scope_file, self.scope_groups
            )
        else:
            self.dashboard.scope_label.setText(f"Test scope XML: {self.scope_file}")
            expected = sum(len(group.test_ids) for group in self.scope_groups)
            active_groups = sum(bool(group.test_ids) for group in self.scope_groups)
            self.dashboard.clusters_summary.setText(
                f"{expected} selected tests · {active_groups} active of "
                f"{len(self.scope_groups)} fixed groups"
            )

    def clear_scope(self):
        self._scope_job_path = None
        self.dashboard.stop_loading()
        self.scope_file = None
        self.scope_groups = []
        self.qa_store.set_data_file(None)
        if self.model.records:
            self.model.set_records(list(self.model.records))
        if self.source_path:
            self.dashboard.update_data(self.source_path, self.model.records, None, [])
        else:
            self.dashboard.scope_label.setText("Test scope: no XML selected")
            self.dashboard.clusters_summary.setText(
                f"{len(FIXED_SCOPE_GROUPS)} fixed groups · select a test_scope.xml to activate progress"
            )
            self.dashboard.clear_scope_button.hide()
        self.statusBar().showMessage("Test scope cleared — all progress groups are disabled", 4000)

    def choose_xml(self):
        start = str(self.folder or Path.home())
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Open a single XML result",
            start,
            "XML results (*.xml);;All files (*)",
        )
        if chosen:
            self.set_file(Path(chosen))

    def generate_pdf_report(self):
        if self.reporting:
            return
        if not self.source_path or self.source_path.suffix.lower() == ".xml":
            QMessageBox.warning(
                self,
                "Results folder required",
                "Select the complete results folder before generating the PDF report.",
            )
            return
        if not self.scope_file:
            QMessageBox.warning(
                self,
                "Test scope required",
                "Select the matching test_scope.xml before generating the PDF report.",
            )
            return

        self.reporting = True
        self.dashboard.set_report_generating(True)
        self.statusBar().showMessage("Generating meeting PDF...")
        self._report_network_path = (
            self.source_path if is_network_path(self.source_path) else self.scope_file
        )
        self._network_start(self._report_network_path, "Reading network data for the PDF report.")
        output_dir = APP_DIR / "output" / "pdf"
        job = ReportJob(self.source_path, self.scope_file, output_dir)
        self._report_job = job
        job.signals.progress.connect(self.dashboard.update_report_progress)
        job.signals.finished.connect(self.report_finished)
        self.pool.start(job)

    def report_finished(self, output, error: str):
        self.reporting = False
        report_network_path = getattr(self, "_report_network_path", None)
        self._network_stop(report_network_path)
        self.dashboard.set_report_generating(False)
        self._report_job = None
        if error:
            self.statusBar().showMessage("PDF report generation failed", 5000)
            if is_network_path(report_network_path):
                self._offer_network_retry(
                    "Unable to generate PDF report", error, self.generate_pdf_report
                )
            else:
                QMessageBox.warning(self, "Unable to generate PDF report", error)
            return

        output_path = Path(output).resolve()
        self.statusBar().showMessage(f"PDF report saved: {output_path.name}", 7000)
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Information)
        message.setWindowTitle("PDF report generated")
        message.setText("The meeting report was created successfully.")
        message.setInformativeText(str(output_path))
        copy_button = message.addButton("Copy path", QMessageBox.ButtonRole.ActionRole)
        copy_button.setIcon(copy_path_icon())
        message.addButton(QMessageBox.StandardButton.Ok)
        message.exec()
        if message.clickedButton() is copy_button:
            QApplication.clipboard().setText(str(output_path))
            self.statusBar().showMessage("PDF report path copied to the clipboard", 5000)

    def _schedule_refresh(self):
        self.refresh_timer.start()

    def refresh(self):
        if not self.source_path:
            return
        if self.scanning:
            self.pending_refresh = True
            return
        self.scanning = True
        self.dashboard.set_results_loading(True)
        self.dashboard.start_loading()
        self._network_start(self.source_path, "Discovering and reading XML results.")
        self.statusBar().showMessage("Reading XML results…")
        job = ScanJob(self.source_path, self._scan_cache)
        job.signals.progress.connect(self.scan_progress)
        job.signals.finished.connect(self.scan_finished)
        self.pool.start(job)

    def scan_progress(self, current: int, total: int, file_name: str, scanned_source):
        if self.source_path and normalized_path(Path(scanned_source)) == normalized_path(self.source_path):
            self.dashboard.update_loading(current, total, file_name)
            if is_network_path(self.source_path):
                self.network_banner.set_progress(current, total)

    def scan_finished(self, records, error, scanned_source):
        self.scanning = False
        scanned_source = normalized_path(Path(scanned_source))
        self._network_stop(scanned_source)
        if not self.source_path or scanned_source != normalized_path(self.source_path):
            self.pending_refresh = False
            if not self.source_path:
                self.dashboard.stop_loading()
                self.dashboard.set_results_loading(False)
                return
            self.refresh()
            return
        refresh_requested = self.pending_refresh
        self.pending_refresh = False
        if error:
            self.dashboard.stop_loading()
            if is_network_path(scanned_source):
                self._offer_network_retry("Unable to read results", error, self.refresh)
            else:
                QMessageBox.warning(self, "Unable to read results", error)
        else:
            # Commit every valid scan before processing a queued watcher event.
            # This prevents large folders from appearing empty while refreshes race.
            self.model.set_records(records)
            self.dashboard.update_data(
                self.source_path, records, self.scope_file, self.scope_groups
            )
            self.dashboard.stop_loading()
            self.statusBar().showMessage(f"{len(records)} results loaded", 4000)
        self.dashboard.set_results_loading(False)
        if refresh_requested:
            self.refresh_timer.start()

    def open_record(self, record: TestRecord):
        key = str(normalized_path(record.path))
        for index in range(1, self.tabs.count()):
            if self.tabs.widget(index).property("record_path") == key:
                self.tabs.setCurrentIndex(index); return
        if not record.details_loaded:
            page = DetailLoadingPage(record)
            page.setProperty("record_path", key)
            label = f"{record.test_id} · {record.title}"
            index = self.tabs.addTab(page, label[:42] + ("…" if len(label) > 42 else ""))
            self.tabs.setTabToolTip(index, record.file_name)
            self.tabs.setCurrentIndex(index)
            if key not in self._detail_jobs:
                self._start_detail_job(record.path)
            return
        page = DetailPage(record, self.qa_store, self.qa_pool); page.setProperty("record_path", key)
        self._connect_detail_page(page)
        label = f"{record.test_id} · {record.title}"
        index = self.tabs.addTab(page, label[:42] + ("…" if len(label) > 42 else ""))
        self.tabs.setTabToolTip(index, record.file_name); self.tabs.setCurrentIndex(index)

    def _start_detail_job(self, path: Path):
        key = str(normalized_path(path))
        job = DetailJob(path)
        self._detail_jobs[key] = job
        job.signals.finished.connect(self._detail_finished)
        self._network_start(path, "Loading the selected XML details.")
        self.pool.start(job)

    def _connect_detail_page(self, page: DetailPage):
        page.qa_assignment_changed.connect(self._qa_assignment_changed)
        page.network_operation_started.connect(self._network_start)
        page.network_operation_finished.connect(self._network_stop)

    def _detail_finished(self, record, error: str, record_path: str):
        self._network_stop(Path(record_path))
        self._detail_jobs.pop(record_path, None)
        tab_index = -1
        for index in range(1, self.tabs.count()):
            if self.tabs.widget(index).property("record_path") == record_path:
                tab_index = index
                break
        if error or record is None:
            if tab_index >= 0 and isinstance(self.tabs.widget(tab_index), DetailLoadingPage):
                self.tabs.widget(tab_index).show_error(error or "Unknown XML parsing error")
            if is_network_path(Path(record_path)):
                self._offer_network_retry(
                    "Unable to load XML details",
                    error or "Unknown XML parsing error",
                    lambda: self._start_detail_job(Path(record_path)),
                )
            return
        for row, existing in enumerate(self.model.records):
            if str(normalized_path(existing.path)) == record_path:
                self.model.hydrate_record(record)
                self.model.records[row] = record
                break
        if tab_index < 0:
            return
        current_widget = self.tabs.currentWidget()
        old_page = self.tabs.widget(tab_index)
        label = f"{record.test_id} · {record.title}"
        tab_text = label[:42] + ("…" if len(label) > 42 else "")
        tooltip = record.file_name
        self.tabs.removeTab(tab_index)
        old_page.deleteLater()
        page = DetailPage(record, self.qa_store, self.qa_pool)
        self._connect_detail_page(page)
        page.setProperty("record_path", record_path)
        self.tabs.insertTab(tab_index, page, tab_text)
        self.tabs.setTabToolTip(tab_index, tooltip)
        if current_widget is old_page:
            self.tabs.setCurrentIndex(tab_index)
        elif current_widget is not None:
            self.tabs.setCurrentWidget(current_widget)

    def _qa_assignment_changed(self, record: TestRecord):
        for row, existing in enumerate(self.model.records):
            if normalized_path(existing.path) == normalized_path(record.path):
                self.model.records[row].qa_member_id = record.qa_member_id
                self.model.records[row].qa_member_name = record.qa_member_name
                self.model.records[row].qa_comment = record.qa_comment
                first = self.model.index(row, ResultsModel.QA_COLUMN)
                last = self.model.index(row, ResultsModel.QA_COMMENT_COLUMN)
                self.model.dataChanged.emit(first, last, [Qt.DisplayRole, Qt.EditRole])
                break

    def close_tab(self, index: int):
        if index == 0: return
        page = self.tabs.widget(index); self.tabs.removeTab(index); page.deleteLater()
        self.tabs.setCurrentIndex(0)


STYLE = """
* { font-family: "Segoe UI", Arial; font-size: 13px; color: #24324A; }
QMainWindow, QWidget { background: #F5F7FB; }
QDialog#scopeHelpDialog { background: #FFFFFF; }
QFrame#scopeHelpHeader { background: #3E328A; border: 0; }
QWidget#scopeHelpBody { background: #FFFFFF; }
QLabel#scopeHelpEyebrow { background: transparent; color: #CFC9F5; font-size: 10px; font-weight: 800; letter-spacing: 1px; }
QLabel#scopeHelpTitle { background: transparent; color: #FFFFFF; font-size: 22px; font-weight: 750; }
QLabel#scopeHelpSubtitle { background: transparent; color: #E9E6FA; font-size: 13px; }
QLabel#scopeStructureCard { background: #F7F8FC; color: #253653; border: 1px solid #DFE4EC; border-radius: 9px; }
QLabel#scopeInfoCard { background: #EEF4FF; color: #405579; border: 1px solid #D6E3F8; border-radius: 9px; }
QFrame#scopeWarningCard { background: #FFFCF0; border: 1px solid #E9E0B8; border-radius: 9px; }
QLabel#scopeWarningIcon, QLabel#scopeWarningText { background: transparent; border: 0; color: #655F43; }
QToolTip {
    background-color: #FFFBEA;
    color: #514A32;
    border: 1px solid #E8DDA8;
    padding: 7px;
}
QTabWidget::pane { border: 0; }
QTabBar::tab { background: #E9EEF5; color: #53627A; padding: 11px 18px; margin-right: 2px; border-top-left-radius: 8px; border-top-right-radius: 8px; }
QTabBar::tab:selected { background: #FFFFFF; color: #245BC8; font-weight: 700; }
QFrame#panel, QFrame#statCard { background: #FFFFFF; border: 1px solid #E3E8F0; border-radius: 12px; }
QFrame#clusterCard { background: transparent; border: 0; }
QFrame#overallPanel { background: #F7F9FC; border: 1px solid #E3E8F0; border-radius: 8px; }
QFrame#loadingPanel { background: #F0EDFF; border: 1px solid #CFC7F2; border-radius: 8px; }
QFrame#networkBanner { background: #FFF7E8; border: 0; border-bottom: 1px solid #EBCB8B; }
QLabel#networkBannerTag { background: #D38A16; color: white; border-radius: 4px; padding: 3px 7px; font-size: 9px; font-weight: 800; }
QLabel#networkBannerText { color: #77500F; font-size: 11px; font-weight: 700; }
QProgressBar#networkBannerProgress { background: #F0DFC0; border: 0; border-radius: 4px; }
QProgressBar#networkBannerProgress::chunk { background: #D38A16; border-radius: 4px; }
QFrame#statCard:hover { border: 1px solid #AFC6F3; background: #FBFDFF; }
QLabel#pageTitle { color: #16213A; font-size: 21px; font-weight: 700; }
QLabel#sectionTitle { color: #17233C; font-size: 16px; font-weight: 700; }
QLabel#eyebrow { color: #3974D8; font-size: 10px; font-weight: 800; letter-spacing: 1px; }
QLabel#brand { background: transparent; padding: 0; }
QLabel#muted { color: #718096; }
QLabel#editableDetailValue { color: #24324A; border-bottom: 1px dotted #A8B7CC; padding: 2px 3px; }
QLabel#editableDetailValue:hover { color: #285FBF; background: #EAF1FC; border-radius: 4px; }
QLabel#statValue { color: #17233C; font-size: 21px; font-weight: 750; }
QLabel#loadingLabel { color: #3E328A; font-size: 11px; font-weight: 750; min-width: 175px; }
QLabel#hint { background: #EEF4FF; color: #536A91; border-radius: 8px; padding: 12px; }
QLabel#clusterDetail { color: #65748B; font-size: 10px; }
QLabel#scopeCount { background: #E8EEF7; color: #40516D; border-radius: 4px; font-size: 9px; font-weight: 700; }
QLabel#scopeCountDisabled { background: #F0F2F5; color: #A6AFBE; border-radius: 4px; font-size: 9px; font-weight: 700; }
QProgressBar#scopeProgress, QProgressBar#executionProgress, QProgressBar#scopeProgressDisabled { background: #E3E8EF; border: 0; border-radius: 4px; }
QProgressBar#scopeProgress::chunk { background: #69AD92; border-radius: 4px; }
QProgressBar#scopeProgressDisabled::chunk { background: #C9D0DA; border-radius: 4px; }
QProgressBar#executionProgress::chunk { background: #4E86E4; border-radius: 4px; }
QProgressBar#loadingProgress { background: #DDD8F2; border: 0; border-radius: 4px; }
QProgressBar#loadingProgress::chunk { background: #3E328A; border-radius: 4px; }
QLabel#warning { background: #FFF6E5; color: #9A650E; border-radius: 8px; padding: 12px; }
QLabel#passPill { background: #EDF7F3; color: #477F6A; border-radius: 12px; padding: 5px 10px; font-weight: 700; }
QLabel#failPill { background: #FDEDEF; color: #BD354D; border-radius: 12px; padding: 5px 10px; font-weight: 700; }
QLabel#badge_passed { background: #EDF7F3; color: #477F6A; border-radius: 17px; font-weight: 700; }
QLabel#badge_failed { background: #FDEDEF; color: #BD354D; border-radius: 17px; font-weight: 700; }
QLabel#badge_skipped { background: #FFF6E5; color: #A66C10; border-radius: 17px; font-weight: 700; }
QLabel#badge_aborted { background: #F3EEFF; color: #7541D2; border-radius: 17px; font-weight: 700; }
QLabel#badge_draft { background: #EAF2FF; color: #285FBF; border-radius: 17px; font-weight: 700; }
QLabel#badge_unknown { background: #F1F5F9; color: #526174; border-radius: 17px; font-weight: 700; }
QPushButton { background: #3974D8; color: white; border: 0; border-radius: 8px; padding: 8px 12px; font-weight: 650; }
QPushButton:hover { background: #2E63BD; }
QPushButton#secondaryButton { background: #EAF1FC; color: #2E63BD; }
QPushButton#secondaryButton:disabled { background: #E2E5EA; color: #9AA3B1; border: 1px solid #D4D9E1; }
QPushButton#scopeModeButton { background: #EEF1F6; color: #53627A; border: 1px solid #D4DCE8; }
QPushButton#scopeModeButton:checked { background: #E7F5EE; color: #39785F; border: 1px solid #8EC5AE; }
QPushButton#scopeModeButton:disabled { background: #F1F3F6; color: #A0A8B5; border: 1px solid #E0E4EA; }
QPushButton#reportButton { background: #4F9A7D; color: white; }
QPushButton#reportButton:hover { background: #43866D; }
QPushButton#reportButton:disabled { background: #A9CDBF; color: #F3FAF7; }
QPushButton#detailEditButton { background: #EAF1FC; color: #285FBF; border: 1px solid #C9D9F3; padding: 7px 12px; }
QPushButton#detailEditButton:hover { background: #DDEAFF; border-color: #9DBBEA; }
QPushButton#detailSaveButton { background: #4F9A7D; color: white; border: 1px solid #43866D; padding: 7px 12px; }
QPushButton#detailSaveButton:hover { background: #43866D; }
QPushButton#detailCancelButton { background: #F1F3F6; color: #68758A; border: 1px solid #D9DFE8; padding: 7px 12px; }
QPushButton#detailCancelButton:hover { background: #E6E9EE; color: #475569; }
QPushButton#detailAddButton { background: #F3EEFF; color: #6246B5; border: 1px solid #D8CEF5; padding: 7px 12px; }
QPushButton#detailAddButton:hover { background: #E9E0FF; border-color: #BEAFE9; }
QPushButton#detailRemoveButton { background: #FCEFF1; color: #B3384C; border: 1px solid #F0C5CC; padding: 7px 12px; }
QPushButton#detailRemoveButton:hover { background: #F9DDE2; border-color: #E8A7B2; }
QPushButton#detailRemoveButton:disabled { background: #F4F5F7; color: #A5ADBA; border-color: #E2E5EA; }
QPushButton#clearScopeButton { background: #F1F3F6; color: #68758A; border: 1px solid #D9DFE8; }
QPushButton#clearScopeButton:hover { background: #FBE4E7; color: #A72F43; border: 1px solid #EDB9C1; }
QPushButton#pathToggle { background: transparent; color: #3E63A8; border: 0; padding: 3px 5px; font-size: 10px; font-weight: 700; }
QPushButton#pathToggle:hover, QPushButton#pathToggle:checked { background: #EAF1FC; color: #2859AC; }
QPushButton#activeFilterBadge { background: #FFF0D3; color: #9A5A00; border: 1px solid #F1C46F; padding: 7px 11px; font-size: 10px; font-weight: 800; }
QPushButton#activeFilterBadge:hover { background: #FFE5B5; color: #7A4600; }
QLineEdit, QComboBox { background: #F8FAFD; border: 1px solid #DCE3ED; border-radius: 8px; padding: 7px 9px; min-height: 18px; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #72A0EC; background: white; }
QTableView, QTableWidget, QPlainTextEdit { background: white; alternate-background-color: #F8FAFD; border: 1px solid #E5EAF1; border-radius: 8px; selection-background-color: #E2EDFF; selection-color: #173B79; }
QHeaderView::section { background: #F1F5FA; color: #64748B; border: 0; border-bottom: 1px solid #DDE4EE; padding: 10px 8px; font-weight: 700; }
QTableWidget::item { padding: 7px; border-bottom: 1px solid #EDF1F6; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #C6D1E0; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollArea { background: transparent; border: 0; }
QStatusBar { background: #FFFFFF; color: #718096; border-top: 2px solid #3E328A; }
QLabel#footerMeta { color: #6E7890; font-size: 10px; font-weight: 700; padding: 0 10px; }
QLabel#footerBrand { background: #3E328A; color: white; border-radius: 4px; font-size: 10px; font-weight: 800; padding: 4px 11px; margin-right: 8px; }
QTabWidget#innerTabs::pane { background: white; border: 1px solid #E3E8F0; border-radius: 10px; }
QTabWidget#innerTabs QTabBar::tab { background: transparent; border-radius: 0; padding: 10px 16px; }
QTabWidget#innerTabs QTabBar::tab:selected { color: #245BC8; border-bottom: 2px solid #3974D8; }
"""


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Tridonic One4All Viewer")
    app.setApplicationDisplayName("Tridonic One4All Viewer")
    app.setApplicationVersion("1.9")
    icon_path = APP_DIR / "app_icon.ico"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    elif getattr(sys, "frozen", False):
        app.setWindowIcon(QIcon(sys.executable))
    app.setStyle("Fusion"); app.setStyleSheet(STYLE)
    window = MainWindow(); window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
