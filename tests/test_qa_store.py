import hashlib
import os
import shutil
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QThreadPool

from generate_meeting_pdf import load_qa_member_names
from main import (
    DetailPage,
    MainWindow,
    NetworkBanner,
    QAAssignment,
    QAMemberStore,
    ScopeLoadJob,
    TestRecord,
    is_network_path,
)


class QAMemberStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = Path(__file__).parent / f".qa_store_{self._testMethodName}"
        self.folder.mkdir()
        self.data_file = self.folder / "One4All_QA_data.xml"
        self.result = self.folder / "30.4 result.xml"
        self.legacy_file = self.folder / "Test_report_data.xml"
        self.legacy_file.write_bytes(b'<Tests legacy="yes"/>')
        self.legacy_hash = hashlib.sha256(self.legacy_file.read_bytes()).digest()

    def tearDown(self):
        shutil.rmtree(self.folder)

    def test_scope_data_is_lazy_and_does_not_touch_legacy_file(self):
        store = QAMemberStore()
        self.assertEqual(store.members, [])
        store.set_data_file(self.data_file)
        self.assertFalse(self.data_file.exists())

        member = store.add_member("Alice QA")
        self.assertEqual(member.member_id, "QA001")
        self.assertTrue(self.data_file.exists())
        self.assertEqual(
            hashlib.sha256(self.legacy_file.read_bytes()).digest(), self.legacy_hash
        )

    def test_member_and_comment_are_saved_independently(self):
        store = QAMemberStore()
        store.set_data_file(self.data_file)
        member = store.add_member("Alice QA")

        store.assign_comment(self.result, "first comment")
        self.assertEqual(store.assignment_for(self.result).member_id, "")
        store.assign_member(self.result, member.member_id)
        self.assertEqual(
            store.assignment_for(self.result), QAAssignment("QA001", "first comment")
        )
        store.assign_comment(self.result, "changed comment")
        self.assertEqual(
            store.assignment_for(self.result), QAAssignment("QA001", "changed comment")
        )

        reloaded = QAMemberStore()
        reloaded.set_data_file(self.data_file)
        self.assertEqual(reloaded.members[0].name, "Alice QA")
        self.assertEqual(
            reloaded.assignment_for(self.result), QAAssignment("QA001", "changed comment")
        )
        self.assertEqual(
            hashlib.sha256(self.legacy_file.read_bytes()).digest(), self.legacy_hash
        )

    def test_clearing_scope_clears_loaded_qa_data(self):
        store = QAMemberStore()
        store.set_data_file(self.data_file)
        store.add_member("Alice QA")
        store.assign_comment(self.result, "comment")

        store.set_data_file(None)

        self.assertEqual(store.members, [])
        self.assertEqual(store.assignments, {})

    def test_member_and_comment_editors_have_independent_cancel_actions(self):
        store = QAMemberStore()
        store.set_data_file(self.data_file)
        member = store.add_member("Alice QA")
        record = TestRecord(
            path=self.result,
            file_name=self.result.name,
            test_id="30.4",
            title="Test",
            family="30",
            status="draft",
            is_draft=True,
            started=None,
            stopped=None,
            duration_seconds=None,
            qa_member_id=member.member_id,
            qa_member_name=member.name,
            qa_comment="saved comment",
        )
        page = DetailPage(record, store)

        page._start_member_edit()
        self.assertFalse(page.qa_combo.isHidden())
        self.assertTrue(page.comments_editor.isHidden())
        page.qa_combo.setCurrentIndex(0)
        page._cancel_member_edit()
        self.assertEqual(record.qa_member_id, "QA001")

        page._start_comment_edit()
        self.assertTrue(page.qa_combo.isHidden())
        self.assertFalse(page.comments_editor.isHidden())
        page.comments_editor.setPlainText("discarded comment")
        page._cancel_comment_edit()
        self.assertEqual(record.qa_comment, "saved comment")

    def test_edit_controls_stay_close_and_remove_values_independently(self):
        long_comment = (
            "Grammar: Corrected spelling mistakes and sentence structure. "
            "Clarity: Made the explanation clearer and more professional. "
            "Tone: Improved the business communication style while keeping the message friendly. "
            "Consistency: Standardized capitalization and formatting of names and file references."
        )
        store = QAMemberStore()
        store.set_data_file(self.data_file)
        member = store.add_member("Alice QA")
        store.assign_member(self.result, member.member_id)
        store.assign_comment(self.result, long_comment)
        record = TestRecord(
            path=self.result,
            file_name=self.result.name,
            title="Test",
            test_id="30.4",
            family="30",
            status="draft",
            is_draft=True,
            started=None,
            stopped=None,
            duration_seconds=None,
            qa_member_id=member.member_id,
            qa_member_name=member.name,
            qa_comment=long_comment,
        )
        page = DetailPage(record, store)
        page.resize(1400, 850)
        page.show()
        self.app.processEvents()
        self.assertLessEqual(
            page.qa_edit_button.geometry().left() - page.qa_value.geometry().right(), 12
        )
        self.assertLessEqual(
            page.comment_edit_button.geometry().left() - page.comments_value.geometry().right(), 12
        )
        self.assertGreaterEqual(page.comments_value.width(), 800)

        page._start_member_edit()
        page._remove_member_assignment()
        self.assertEqual(store.assignment_for(self.result), QAAssignment("", long_comment))
        store.assign_member(self.result, member.member_id)
        record.qa_member_id = member.member_id
        record.qa_member_name = member.name
        page._start_comment_edit()
        page._remove_comment()
        self.assertEqual(store.assignment_for(self.result), QAAssignment("QA001", ""))
        page.close()

    def test_report_lists_only_qa_members_assigned_to_scoped_results(self):
        store = QAMemberStore()
        store.set_data_file(self.data_file)
        alice = store.add_member("Alice QA")
        store.add_member("Unassigned QA")
        store.assign_member(self.result, alice.member_id)
        scope = self.folder / "test_scope.xml"
        scope.write_text("<Tests/>", encoding="utf-8")
        scoped = {"30.4": SimpleNamespace(path=self.result)}

        self.assertEqual(load_qa_member_names(scope, scoped), ["Alice QA"])

    def test_network_paths_and_banner_are_detected(self):
        self.assertTrue(is_network_path(Path(r"\\server\share\results")))
        self.assertTrue(is_network_path(Path(r"Z:\results")))
        self.assertFalse(is_network_path(self.folder))

        banner = NetworkBanner()
        banner.start(Path(r"Z:\results"), "Discovering and reading XML results.")
        self.assertFalse(banner.isHidden())
        self.assertIn("may take a little longer to load", banner.label.text())
        self.assertEqual((banner.progress.minimum(), banner.progress.maximum()), (0, 0))
        banner.set_progress(3, 10)
        self.assertEqual(banner.progress.value(), 3)
        banner.stop()
        self.assertTrue(banner.isHidden())

    def test_scope_and_qa_data_can_be_loaded_in_worker(self):
        scope = self.folder / "test_scope.xml"
        scope.write_text(
            '<Tests><Test Number="30.4"><Name>Z:\\30_Sensor Mode\\30.4 Test.vdx</Name></Test></Tests>',
            encoding="utf-8",
        )
        store = QAMemberStore()
        store.set_data_file(self.data_file)
        store.add_member("Alice QA")
        store.assign_member(self.result, "QA001")
        finished = []
        job = ScopeLoadJob(scope)
        job.signals.finished.connect(lambda payload, error, path: finished.append((payload, error, path)))

        job.run()

        self.assertEqual(len(finished), 1)
        payload, error, loaded_path = finished[0]
        self.assertEqual(error, "")
        groups, members, assignments, warning = payload
        self.assertEqual(next(group for group in groups if group.folder_id == "30").test_ids, {"30.4"})
        self.assertEqual(members[0].name, "Alice QA")
        self.assertEqual(assignments[self.result.name.casefold()].member_id, "QA001")
        self.assertEqual(warning, "")

    def test_qa_save_runs_asynchronously_with_production_pool(self):
        store = QAMemberStore()
        store.set_data_file(self.data_file)
        record = TestRecord(
            path=self.result,
            file_name=self.result.name,
            title="Test",
            test_id="30.4",
            family="30",
            status="draft",
            is_draft=True,
            started=None,
            stopped=None,
            duration_seconds=None,
        )
        pool = QThreadPool()
        pool.setMaxThreadCount(1)
        page = DetailPage(record, store, pool)
        page._start_comment_edit()
        page.comments_editor.setPlainText("network-safe comment")
        page._save_comment()
        self.assertIsNotNone(page._qa_job)
        deadline = time.monotonic() + 3
        while page._qa_job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNone(page._qa_job)
        self.assertEqual(store.assignment_for(self.result).qa_comment, "network-safe comment")

    def test_main_window_loads_scope_and_results_without_blocking(self):
        scope = self.folder / "test_scope.xml"
        scope.write_text(
            '<Tests><Test Number="30.4"><Name>Z:\\30_Sensor Mode\\30.4 Test.vdx</Name></Test></Tests>',
            encoding="utf-8",
        )
        window = MainWindow()
        window.set_scope_file(scope)
        deadline = time.monotonic() + 3
        while window._scope_job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(window.scope_file, scope.absolute())
        self.assertEqual(
            next(group for group in window.scope_groups if group.folder_id == "30").test_ids,
            {"30.4"},
        )

        results = Path(__file__).parents[1] / "ST-I_Results"
        window.set_folder(results)
        deadline = time.monotonic() + 10
        while window.scanning and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(window.scanning)
        self.assertGreater(len(window.model.records), 0)
        window.close()


if __name__ == "__main__":
    unittest.main()
