"""F10: a corrupt hash index is never silently discarded; a stale _links.md goes."""
import contextlib
import io
import json
import unittest
from unittest import mock

from support import SyncCase, cs


class CorruptIndexTests(SyncCase):
    def index_path(self):
        d = self.out / ".course_sync"
        d.mkdir(exist_ok=True)
        return d / "downloaded.json"

    def load(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            data = cs.load_hash_index(self.out)
        return data, err.getvalue()

    def test_garbage_is_moved_aside_with_a_loud_warning(self):
        self.index_path().write_text("{not json at all")
        data, err = self.load()
        self.assertEqual(data, {"files": {}})
        self.assertIn("WARNING", err)
        self.assertIn("downloaded.json.corrupt-", err)
        backups = list(self.index_path().parent.glob("downloaded.json.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "{not json at all")
        self.assertFalse(self.index_path().exists())

    def test_wrong_structure_is_treated_the_same(self):
        for payload in ("[]", '{"foo": 1}', '{"files": []}', "null"):
            for old in self.index_path().parent.glob("downloaded.json*"):
                old.unlink()
            self.index_path().write_text(payload)
            data, err = self.load()
            self.assertEqual(data, {"files": {}}, payload)
            self.assertIn("WARNING", err, payload)
            self.assertEqual(len(list(self.index_path().parent.glob("*.corrupt-*"))), 1, payload)

    def test_invalid_utf8_is_treated_the_same(self):
        self.index_path().write_bytes(b'{"files": "\xff\xfe"}')
        data, err = self.load()
        self.assertEqual(data, {"files": {}})
        self.assertIn("WARNING", err)

    def test_valid_and_missing_indexes_are_silent(self):
        data, err = self.load()
        self.assertEqual((data, err), ({"files": {}}, ""))
        good = {"files": {"aa": {"size": 1, "paths": ["x"]}}}
        self.index_path().write_text(json.dumps(good))
        data, err = self.load()
        self.assertEqual((data, err), (good, ""))
        self.assertTrue(self.index_path().exists())

    def test_dry_run_warns_but_leaves_the_file_where_it_is(self):
        self.index_path().write_text("garbage")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            data = cs.load_hash_index(self.out, move_aside=False)
        self.assertEqual(data, {"files": {}})
        self.assertIn("WARNING", err.getvalue())
        self.assertIn("Dry run", err.getvalue())
        self.assertEqual(self.index_path().read_text(), "garbage")
        self.assertEqual(list(self.index_path().parent.glob("*.corrupt-*")), [])

    def test_a_dry_run_main_writes_nothing_even_with_a_corrupt_index(self):
        self.index_path().write_text("garbage")
        self.server.add_course(101, [self.server.section("Week 1", [
            self.server.resource(1, "A", [("a.pdf", b"A", 11)])])])
        before = self.tree()
        code, out, err = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("WARNING", err)
        self.assertEqual(self.tree(), before)

    def test_a_run_with_a_corrupt_index_warns_keeps_the_file_and_continues(self):
        self.index_path().write_text("garbage")
        self.server.add_course(101, [self.server.section("Week 1", [
            self.server.resource(1, "A", [("a.pdf", b"A", 11)])])])
        code, out, err = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("WARNING", err)
        self.assertEqual((self.out / "UNIT/Week1/a.pdf").read_bytes(), b"A")
        self.assertEqual(len(list(self.index_path().parent.glob("downloaded.json.corrupt-*"))), 1)
        self.assertIn("files", self.index())  # a fresh, valid index was saved


class StaleLinksFileTests(SyncCase):
    def label(self, with_link):
        link = '<a href="https://example.org/reading">Reading list</a>' if with_link else ""
        return {"id": 1, "name": "Resources", "modname": "label",
                "description": f"<p>Resources for the week, please read before class.</p>{link}"}

    def setup_course(self, with_link, extra=()):
        self.server.courses.clear()
        self.server.add_course(101, [self.server.section("Week 1", [self.label(with_link), *extra])])

    def links(self):
        return self.out / "UNIT/Week1/_links.md"

    def test_generated_links_file_is_removed_when_the_last_link_goes(self):
        self.setup_course(True)
        self.run_main()
        self.assertIn("https://example.org/reading", self.links().read_text())
        self.setup_course(False)
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertFalse(self.links().exists())
        self.assertIn("[links] UNIT/Week1/_links.md removed", out)
        self.assertIn("_None._", (self.out / "UNIT/Week1/_index.md").read_text().split("## External links")[1][:20])

    def test_hand_written_or_edited_file_is_kept(self):
        self.setup_course(False)
        (self.out / "UNIT/Week1").mkdir(parents=True)
        for text in ("my own notes\n", "# External links - Week1\n\n- [a](http://x)\nmy extra line\n",
                     "# Something else\n- [a](http://x)\n"):
            self.links().write_text(text)
            self.run_main()
            self.assertEqual(self.links().read_text(), text, text)

    def test_dry_run_only_reports(self):
        self.setup_course(True)
        self.run_main()
        self.setup_course(False)
        code, out, _ = self.run_main("--dry-run")
        self.assertTrue(self.links().exists())
        self.assertIn("would remove", out)

    def test_a_failed_module_keeps_the_old_links_file(self):
        self.setup_course(True)
        self.run_main()
        self.setup_course(False)
        with mock.patch.object(cs, "handle_label_module", side_effect=RuntimeError("boom")):
            code, out, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertTrue(self.links().exists())

    def test_sections_sharing_a_folder_never_remove_a_links_file_written_this_run(self):
        # "Study Week 5" and "Week 5" both map to Week5_StudyWeek; only one has links.
        with_link = self.server.section("Week 5 Study", [self.label(True)])
        without = self.server.section("Study Week 5", [
            {"id": 2, "name": "Plain", "modname": "label",
             "description": "<p>Nothing to link here, only prose for the week.</p>"}])
        for order in ((with_link, without), (without, with_link)):
            self.server.courses.clear()
            self.server.add_course(101, list(order))
            for _ in range(2):  # the second run starts with the file already on disk
                code, out, _ = self.run_main()
                self.assertEqual(code, 0)
                self.assertNotIn("removed", out)
                self.assertNotIn("would remove", out)
                self.assertIn("https://example.org/reading",
                              (self.out / "UNIT/Week5_StudyWeek/_links.md").read_text())

    def test_a_section_that_never_had_links_is_untouched(self):
        self.setup_course(False)
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertFalse(self.links().exists())
        self.assertNotIn("removed", out)


if __name__ == "__main__":
    unittest.main()
