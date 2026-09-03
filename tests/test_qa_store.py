import ctypes
import os
import shutil
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QThreadPool

from generate_meeting_pdf import load_qa_member_names
from main import (
    ClusterCard,
    Dashboard,
    DetailPage,
    MainWindow,
    NetworkBanner,
    QAAssignment,
    QAMemberStore,
    ReportJob,
    ResultsModel,
    ResultsProxy,
    ScanJob,
    ScopeGroup,
    ScopeLoadJob,
    TestRecord,
    comment_table_preview,
    is_network_path,
    parse_result,
)


class QAMemberStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = Path(__file__).parent / f".qa_store_{self._testMethodName}"
        self.folder.mkdir()
        self.data_file = self.folder / "Test_report_data.xml"
        self.relationships_file = self.folder / "Relationships.xml"
        self.result = self.folder / "30.4 result.xml"
        self.legacy_file = self.relationships_file
        self.obsolete_qa_file = self.folder / "One4All_QA_data.xml"
        initial = (
            '<Tests legacy="yes"><Test Number="30.4" Duration="0" BugIDs="" '
            'BugIDsFI="" DID="" Tester="WB" Comment="legacy comment">'
            '<Index>0</Index><Name>30.4 result.xml</Name></Test></Tests>'
        )
        self.data_file.write_text(initial, encoding="utf-8")
        self.relationships_file.write_text(initial, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.folder)

    def test_report_data_is_the_primary_source_for_qa_and_comments(self):
        self.obsolete_qa_file.write_text(
            '<One4AllQAData><QAMembers><Member id="QA001" name="Wrong QA"/>'
            '</QAMembers></One4AllQAData>',
            encoding="utf-8",
        )
        self.relationships_file.write_text(
            '<Tests><Test Number="30.4" Tester="WRONG" Comment="wrong comment">'
            '<Index>0</Index><Name>30.4 result.xml</Name></Test></Tests>',
            encoding="utf-8",
        )
        obsolete_qa_before = self.obsolete_qa_file.read_bytes()
        relationships_before = self.relationships_file.read_bytes()
        store = QAMemberStore()
        self.assertEqual(store.members, [])
        store.set_data_file(self.data_file)
        self.assertEqual([member.name for member in store.members], ["WB"])

        member = store.add_member("Alice QA")
        self.assertEqual(member.member_id, "Alice QA")
        self.assertEqual(self.relationships_file.read_bytes(), relationships_before)
        self.assertEqual(self.obsolete_qa_file.read_bytes(), obsolete_qa_before)
        self.assertEqual(
            store.assignment_for(self.result), QAAssignment("WB", "legacy comment")
        )

    def test_member_and_comment_are_saved_in_both_legacy_files(self):
        store = QAMemberStore()
        store.set_data_file(self.data_file)

        store.assign_comment(self.result, "first comment")
        self.assertEqual(store.assignment_for(self.result).member_id, "WB")
        member = store.add_member("Alice QA")
        store.assign_member(self.result, member.member_id)
        self.assertEqual(
            store.assignment_for(self.result), QAAssignment("Alice QA", "first comment")
        )
        store.assign_comment(self.result, "changed comment")
        self.assertEqual(
            store.assignment_for(self.result), QAAssignment("Alice QA", "changed comment")
        )

        reloaded = QAMemberStore()
        reloaded.set_data_file(self.data_file)
        self.assertEqual(reloaded.members[0].name, "Alice QA")
        self.assertEqual(
            reloaded.assignment_for(self.result), QAAssignment("Alice QA", "changed comment")
        )
        for legacy_path in (self.data_file, self.relationships_file):
            legacy_test = ET.parse(legacy_path).getroot().find("./Test")
            self.assertEqual(legacy_test.get("Comment"), "changed comment")
            self.assertEqual(legacy_test.get("Tester"), "Alice QA")
            self.assertEqual(legacy_test.get("Duration"), "0")
            self.assertEqual(ET.parse(legacy_path).getroot().get("legacy"), "yes")
            self.assertFalse(legacy_path.read_bytes().lstrip().startswith(b"<?xml"))
        self.assertFalse(self.obsolete_qa_file.exists())

    def test_dual_save_preserves_each_files_duration(self):
        report_tree = ET.parse(self.data_file)
        report_tree.getroot().find("./Test").set("Duration", "81")
        report_tree.write(self.data_file, encoding="utf-8")
        relationships_tree = ET.parse(self.relationships_file)
        relationships_tree.getroot().find("./Test").set("Duration", "5")
        relationships_tree.write(self.relationships_file, encoding="utf-8")
        store = QAMemberStore()
        store.set_data_file(self.data_file)

        store.assign_comment(self.result, "same QA comment")

        report = ET.parse(self.data_file).getroot().find("./Test")
        relationship = ET.parse(self.relationships_file).getroot().find("./Test")
        self.assertEqual(report.get("Comment"), "same QA comment")
        self.assertEqual(relationship.get("Comment"), "same QA comment")
        self.assertEqual(report.get("Duration"), "81")
        self.assertEqual(relationship.get("Duration"), "5")

    def test_locked_relationships_file_leaves_both_files_unchanged(self):
        store = QAMemberStore()
        store.set_data_file(self.data_file)
        report_original = self.data_file.read_bytes()
        relationships_original = self.relationships_file.read_bytes()
        locked_error = OSError(
            "Relationships.xml is in use by another application. "
            "The QA change was not saved."
        )

        with patch.object(
            store,
            "_assert_path_available",
            side_effect=[None, locked_error],
        ):
            with self.assertRaisesRegex(OSError, "in use by another application"):
                store.assign_comment(self.result, "must not be written")

        self.assertEqual(self.data_file.read_bytes(), report_original)
        self.assertEqual(self.relationships_file.read_bytes(), relationships_original)
        self.assertEqual(list(self.folder.glob(".Relationships.xml.*.tmp")), [])
        self.assertEqual(list(self.folder.glob(".Test_report_data.xml.*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows file-sharing semantics")
    def test_windows_open_file_is_reported_as_in_use(self):
        store = QAMemberStore()
        store.set_data_file(self.data_file)
        report_original = self.data_file.read_bytes()
        relationships_original = self.relationships_file.read_bytes()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.restype = ctypes.c_void_p
        handle = create_file(
            os.fspath(self.relationships_file),
            0x80000000,
            0,
            None,
            3,
            0x80,
            None,
        )
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            with self.assertRaisesRegex(OSError, "in use by another application"):
                store.assign_comment(self.result, "must not be written")
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))

        self.assertEqual(self.data_file.read_bytes(), report_original)
        self.assertEqual(self.relationships_file.read_bytes(), relationships_original)

    def test_comment_preview_uses_three_lines_and_continuation_arrow(self):
        preview = comment_table_preview(
            "1 - first line\n2 - second line\n3 - third line\n4 - hidden line",
            line_width=40,
            max_lines=3,
        )

        self.assertEqual(preview.splitlines()[:2], ["1 - first line", "2 - second line"])
        self.assertEqual(len(preview.splitlines()), 3)
        self.assertTrue(preview.endswith("→"))
        self.assertEqual(
            comment_table_preview("1 - one\n2 - two\n3 - three"),
            "1 - one\n2 - two\n3 - three",
        )
        self.assertEqual(comment_table_preview("short comment"), "short comment")

    def test_comment_entry_is_created_dynamically_when_result_is_missing(self):
        other_result = self.folder / "31.2 new result.xml"
        store = QAMemberStore()
        store.set_data_file(self.data_file)

        store.assign_comment(other_result, "new shared comment")

        tests = ET.parse(self.data_file).getroot().findall("./Test")
        created = next(item for item in tests if item.findtext("Name") == other_result.name)
        self.assertEqual(created.get("Number"), "31.2")
        self.assertEqual(created.get("Comment"), "new shared comment")
        self.assertEqual(store.assignment_for(other_result).qa_comment, "new shared comment")
        self.assertEqual(
            len(ET.parse(self.relationships_file).getroot().findall("./Test")), 1
        )

    def test_comment_line_breaks_round_trip_through_relationships(self):
        store = QAMemberStore()
        store.set_data_file(self.data_file)
        multiline = "1 - first action\n2 - second action\n3 - third action"

        store.assign_comment(self.result, multiline)

        self.assertEqual(store.assignment_for(self.result).qa_comment, multiline)
        for legacy_path in (self.data_file, self.relationships_file):
            saved = ET.parse(legacy_path).getroot().find("./Test")
            self.assertEqual(saved.get("Comment"), multiline)

    def test_unique_relationship_number_matches_an_abbreviated_name(self):
        self.data_file.write_text(
            '<Tests><Test Number="30.4" Comment="shared abbreviated comment">'
            '<Index>0</Index><Name>30.4 abbreviated legacy name</Name>'
            '</Test></Tests>',
            encoding="utf-8",
        )
        store = QAMemberStore()
        store.set_data_file(self.data_file)

        self.assertEqual(
            store.assignment_for(self.result).qa_comment,
            "shared abbreviated comment",
        )

    def test_duplicate_test_numbers_are_matched_by_legacy_scope_index(self):
        scope = self.folder / "test_scope.xml"
        scope.write_text(
            '<Tests>'
            '<Test Number="4.13"><Index>37</Index>'
            '<Name>Z:\\ST-I_SDB\\04_DC-EM\\4.13 DC switching basic test_polarity 1.vdx</Name>'
            '</Test>'
            '<Test Number="4.13"><Index>38</Index>'
            '<Name>Z:\\ST-I_SDB\\04_DC-EM\\4.13 DC switching basic test_polarity 2.vdx</Name>'
            '</Test>'
            '</Tests>',
            encoding="utf-8",
        )
        duplicate_xml = (
            '<Tests>'
            '<Test Number="4.13" Tester="QA1" Comment="first">'
            '<Index>37</Index><Name>4.13 DC switching basic test_polarity 1_new run.xml</Name>'
            '</Test>'
            '<Test Number="4.13" Tester="QA2" Comment="second">'
            '<Index>38</Index><Name>4.13 DC switching basic test_polarity 2_new run.xml</Name>'
            '</Test>'
            '</Tests>'
        )
        self.data_file.write_text(duplicate_xml, encoding="utf-8")
        self.relationships_file.write_text(duplicate_xml, encoding="utf-8")
        older_result = self.folder / "4.13 DC switching basic test_polarity 2_old run.xml"
        store = QAMemberStore()
        store.set_data_file(self.legacy_file)

        self.assertEqual(store.assignment_for(older_result), QAAssignment("QA2", "second"))
        store.assign_comment(older_result, "updated by new viewer")

        saved_tests = ET.parse(self.data_file).getroot().findall("./Test")
        self.assertEqual(len(saved_tests), 2)
        self.assertEqual(saved_tests[0].get("Comment"), "first")
        self.assertEqual(saved_tests[1].get("Comment"), "updated by new viewer")
        self.assertEqual(saved_tests[1].findtext("Index"), "38")
        self.assertEqual(
            saved_tests[1].findtext("Name"),
            "4.13 DC switching basic test_polarity 2_new run.xml",
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
        self.assertEqual(record.qa_member_id, "Alice QA")

        page._start_comment_edit()
        self.assertTrue(page.qa_combo.isHidden())
        self.assertFalse(page.comments_editor.isHidden())
        self.assertEqual(page.comments_editor.minimumWidth(), 900)
        self.assertEqual(page.comments_editor.maximumWidth(), 900)
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
        member = store.add_member("Alice QA")
        store.assign_member(self.result, member.member_id)
        record.qa_member_id = member.member_id
        record.qa_member_name = member.name
        page._start_comment_edit()
        page._remove_comment()
        self.assertEqual(store.assignment_for(self.result), QAAssignment("Alice QA", ""))
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
        member = store.add_member("Alice QA")
        store.assign_member(self.result, member.member_id)
        finished = []
        job = ScopeLoadJob(scope)
        job.signals.finished.connect(lambda payload, error, path: finished.append((payload, error, path)))

        job.run()

        self.assertEqual(len(finished), 1)
        payload, error, loaded_path = finished[0]
        self.assertEqual(error, "")
        groups, members, assignments, warning, scope_entries = payload
        self.assertEqual(next(group for group in groups if group.folder_id == "30").test_ids, {"30.4"})
        self.assertEqual(members[0].name, "Alice QA")
        self.assertEqual(assignments[self.result.name.casefold()].member_id, "Alice QA")
        self.assertEqual(warning, "")
        self.assertEqual(scope_entries, [])

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

    def test_report_job_forwards_real_generation_progress(self):
        progress = []
        finished = []
        output = self.folder / "report.pdf"

        def generate_with_progress(results, scope, output_dir, progress_callback):
            progress_callback(10, "Found 12 XML result files")
            progress_callback(55, "Reading XML results — 8/12")
            progress_callback(100, "Report complete")
            return output

        with patch("main.generate_report_pdf", side_effect=generate_with_progress):
            job = ReportJob(self.folder, self.folder / "scope.xml", self.folder)
            job.signals.progress.connect(
                lambda percent, message: progress.append((percent, message))
            )
            job.signals.finished.connect(
                lambda result, error: finished.append((result, error))
            )
            job.run()

        self.assertEqual([percent for percent, _message in progress], [10, 55, 100])
        self.assertEqual(finished, [(output, "")])

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
        self.assertTrue(window.scanning or window._scope_job is not None)
        self.assertFalse(window.dashboard.choose_scope_button.isEnabled())
        active_scope_job = window._scope_job
        window.set_scope_file(scope)
        self.assertIs(window._scope_job, active_scope_job)
        deadline = time.monotonic() + 10
        while (window.scanning or window._scope_job is not None) and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(window.scanning)
        self.assertTrue(window.dashboard.choose_scope_button.isEnabled())
        self.assertGreater(len(window.model.records), 0)
        window.close()

    def test_refresh_reloads_external_relationship_changes(self):
        results = self.folder / "results"
        results.mkdir()
        result = results / "30.4 result.xml"
        result.write_text(
            '<XML filestart="01-09-26_10h-00min-00s">'
            '<TEST><DATA name="DUT" value="DUT A"/></TEST>'
            '<DATA filestop="01-09-26_10h-01min-00s"/></XML>',
            encoding="utf-8",
        )
        scope = self.folder / "test_scope.xml"
        scope.write_text(
            '<Tests><Test Number="30.4"><Name>30.4 result.xml</Name></Test></Tests>',
            encoding="utf-8",
        )
        window = MainWindow()
        window.set_scope_file(scope)
        deadline = time.monotonic() + 5
        while window._scope_job is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        window.set_folder(results)
        deadline = time.monotonic() + 5
        while (window._scope_job is not None or window.scanning) and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)

        self.assertEqual(window.model.records[0].qa_comment, "legacy comment")
        self.assertIn(str(self.data_file.absolute()), window.watcher.files())
        self.assertIn(str(self.relationships_file.absolute()), window.watcher.files())
        tree = ET.parse(self.data_file)
        tree.getroot().find("./Test").set("Comment", "changed in legacy")
        tree.write(self.data_file, encoding="utf-8")

        window.refresh()
        deadline = time.monotonic() + 5
        while (window._scope_job is not None or window.scanning) and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)

        self.assertEqual(window.model.records[0].qa_comment, "changed in legacy")
        window.close()

    def test_dynamic_function_block_filter_combines_with_status_and_search(self):
        store = QAMemberStore()
        store.set_data_file(self.data_file)
        model = ResultsModel(store)
        proxy = ResultsProxy()
        proxy.setSourceModel(model)
        records = [
            TestRecord(
                path=self.folder / "9.1 CLO - passed.xml",
                file_name="9.1 CLO - passed.xml",
                title="CLO function disabled",
                test_id="9.1",
                family="9",
                status="passed",
                is_draft=False,
                started=None,
                stopped=None,
                duration_seconds=None,
            ),
            TestRecord(
                path=self.folder / "21.2 Thermal protection - failed.xml",
                file_name="21.2 Thermal protection - failed.xml",
                title="Thermal protection",
                test_id="21.2",
                family="21",
                status="failed",
                is_draft=False,
                started=None,
                stopped=None,
                duration_seconds=None,
            ),
            TestRecord(
                path=self.folder / "21.3 Overtemperature - passed.xml",
                file_name="21.3 Overtemperature - passed.xml",
                title="Overtemperature recovery",
                test_id="21.3",
                family="21",
                status="passed",
                is_draft=False,
                started=None,
                stopped=None,
                duration_seconds=None,
            ),
        ]
        model.set_records(records)
        dashboard = Dashboard(model, proxy)
        dashboard.update_data(self.folder, records)

        self.assertEqual(
            [dashboard.number_filter.itemText(index) for index in range(dashboard.number_filter.count())],
            ["Filter by number", "FB 9", "FB 21"],
        )
        dashboard.number_filter.setCurrentIndex(dashboard.number_filter.findData("21"))
        self.assertEqual(proxy.rowCount(), 2)
        self.assertEqual(dashboard.chart.counts["passed"], 1)
        self.assertEqual(dashboard.chart.counts["failed"], 1)
        self.assertEqual(sum(dashboard.chart.counts.values()), 2)
        dashboard.status_filter.setCurrentIndex(dashboard.status_filter.findData("failed"))
        self.assertEqual(proxy.rowCount(), 1)
        self.assertEqual(dashboard.chart.counts["failed"], 1)
        self.assertEqual(sum(dashboard.chart.counts.values()), 1)
        dashboard.search.setText("thermal")
        self.assertEqual(proxy.rowCount(), 1)
        dashboard.search.setText("recovery")
        self.assertEqual(proxy.rowCount(), 0)
        dashboard._clear_filters()
        self.assertEqual(proxy.rowCount(), 3)
        self.assertEqual(dashboard.chart.counts["passed"], 2)
        self.assertEqual(dashboard.chart.counts["failed"], 1)

        scope = self.folder / "test_scope.xml"
        scope.write_text("<Tests/>", encoding="utf-8")
        dashboard.update_data(
            self.folder,
            records,
            scope,
            [ScopeGroup("21_LCS", "21", {"21.2"})],
        )
        self.assertTrue(dashboard.scope_mode.isEnabled())
        self.assertTrue(dashboard.scope_mode.isChecked())
        self.assertEqual(dashboard.scope_mode.text(), "Load scope results")
        self.assertEqual(proxy.rowCount(), 1)
        self.assertEqual(dashboard.cards["failed"].value.text(), "1")
        self.assertIn("later removed from the scope", dashboard.scope_mode.toolTip())

        dashboard.scope_mode.setChecked(False)
        self.assertEqual(dashboard.scope_mode.text(), "Load all results")
        self.assertEqual(proxy.rowCount(), 3)
        self.assertEqual(dashboard.cards["passed"].value.text(), "2")

        dashboard.update_data(self.folder, records)
        self.assertFalse(dashboard.scope_mode.isEnabled())
        self.assertFalse(dashboard.scope_mode.isChecked())
        self.assertEqual(proxy.rowCount(), 3)
        dashboard.deleteLater()

    def test_scope_group_uses_one_pass_fail_missing_bar_and_completion_percent(self):
        records = [
            TestRecord(
                path=self.folder / "7.1 passed.xml",
                file_name="7.1 passed.xml",
                title="ITG passed one",
                test_id="7.1",
                family="7",
                status="passed",
                is_draft=False,
                started=None,
                stopped=None,
                duration_seconds=None,
            ),
            TestRecord(
                path=self.folder / "7.2 passed.xml",
                file_name="7.2 passed.xml",
                title="ITG passed two",
                test_id="7.2",
                family="7",
                status="passed",
                is_draft=False,
                started=None,
                stopped=None,
                duration_seconds=None,
            ),
            TestRecord(
                path=self.folder / "7.3 failed.xml",
                file_name="7.3 failed.xml",
                title="ITG failed",
                test_id="7.3",
                family="7",
                status="failed",
                is_draft=False,
                started=None,
                stopped=None,
                duration_seconds=None,
            ),
        ]
        card = ClusterCard(ScopeGroup("07_ITG", "07", {"7.1", "7.2", "7.3", "7.4"}), records)

        self.assertEqual(card.bar.passed, 2)
        self.assertEqual(card.bar.non_passed, 1)
        self.assertEqual(card.bar.total, 4)
        self.assertEqual(card.percent_label.text(), "75%")
        self.assertIn("Scope completed: <b>75.0%</b>", card.toolTip())
        card.show()
        self.app.processEvents()
        card.deleteLater()

    def test_large_result_summary_matches_full_metadata_and_is_cached(self):
        results = self.folder / "results"
        results.mkdir()
        result = results / "21.4 Network summary - passed.xml"
        result.write_bytes(
            b'<XML filestart="31-08-26_10h-00min-00s">\n'
            b'<TEST><DATA name="DUT" value="DUT A"/>'
            b'<DATA name="Tester ID" value="ALICE"/></TEST>\n'
            + (b" " * (1024 * 1024))
            + b'\n<DATA filestop="31-08-26_10h-02min-30s" />\n</XML>'
        )

        summary = parse_result(result, include_evaluations=False)
        complete = parse_result(result, include_evaluations=True)
        for field in (
            "test_id", "title", "status", "started", "stopped",
            "duration_seconds", "dut", "tester",
        ):
            self.assertEqual(getattr(summary, field), getattr(complete, field))

        cache = {}
        with patch("main.parse_result", wraps=parse_result) as parser:
            ScanJob(results, cache).run()
            self.assertEqual(parser.call_count, 1)
            parser.reset_mock()
            ScanJob(results, cache).run()
            self.assertEqual(parser.call_count, 0)


if __name__ == "__main__":
    unittest.main()
