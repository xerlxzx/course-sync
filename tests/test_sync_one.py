"""F8: sync_one routes a module through the same handlers as a full sync and never
reports a silent success for a module it cannot write."""
import contextlib
import io
import os
import unittest
from unittest import mock

import sync_one
from support import TOKEN, FakeMoodle, SyncCase, cs, pf_url

GOOD_HTML = b"<div><h2>Heading</h2><p>Body text that is comfortably long enough to keep.</p></div>"


class SyncOneCase(SyncCase):
    def run_one(self, *argv):
        """Run sync_one.main(argv); returns (exit code, stdout)."""
        if not self.config.exists():
            self.write_config()
        out = io.StringIO()
        code = 0
        with mock.patch.dict(os.environ, {"MOODLE_TOKEN": TOKEN}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = sync_one.main([*[str(a) for a in argv], "--config", str(self.config)])
            except SystemExit as e:
                code = e.code
        self.assertNotIn(TOKEN, out.getvalue())
        return code, out.getvalue()

    def section_files(self, week="UNIT/Week1"):
        return {k: v for k, v in self.tree().items() if k.startswith(week + "/")}

    def generated(self):
        return {k: v for k, v in self.tree().items() if k.rsplit("/", 1)[-1] in cs.GENERATED_NAMES}

    def course(self, *modules):
        self.server.add_course(101, [self.server.section("Week 1", list(modules))])


class DispatchTests(SyncOneCase):
    def test_book_writes_markdown_not_a_raw_index_html(self):
        srv = self.server
        url = srv.add_file(pf_url("1/index.html", 50, area="mod_book/content"), GOOD_HTML)
        book = {"id": 7, "name": "Solutions", "modname": "book", "contents": [
            {"type": "file", "filename": "index.html", "filepath": "/1/", "fileurl": url}]}
        self.course(book)
        code, out = self.run_one(7)
        self.assertEqual(code, 0, out)
        files = self.section_files()
        self.assertEqual(sorted(files), ["UNIT/Week1/Solutions.md"])
        self.assertIn("Body text", files["UNIT/Week1/Solutions.md"].decode())

    def test_page_writes_markdown_and_its_embedded_files(self):
        srv = self.server
        url = srv.add_file(pf_url("index.html", 51, area="mod_page/content"), GOOD_HTML)
        img = srv.add_file(pf_url("diagram.png", 51, area="mod_page/content"), b"PNG")
        page = {"id": 8, "name": "Spec", "modname": "page", "contents": [
            {"type": "file", "filename": "index.html", "fileurl": url},
            {"type": "file", "filename": "diagram.png", "fileurl": img}]}
        self.course(page)
        code, out = self.run_one(8)
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(self.section_files()), ["UNIT/Week1/Spec.md", "UNIT/Week1/diagram.png"])

    def test_resource_and_folder_write_their_files(self):
        res = self.server.resource(1, "Slides", [("s.pdf", b"S")])
        folder = self.server.resource(2, "Pack", [("a.pdf", b"A"), ("b.pdf", b"B")], modname="folder")
        self.course(res, folder)
        self.assertEqual(self.run_one(1)[0], 0)
        self.assertEqual(self.run_one(2)[0], 0)
        self.assertEqual(sorted(self.section_files()),
                         ["UNIT/Week1/a.pdf", "UNIT/Week1/b.pdf", "UNIT/Week1/s.pdf"])

    def test_label_with_a_file_downloads_it_and_says_what_it_left_alone(self):
        link = self.server.add_file(pf_url("reading.pdf", 60, area="mod_label/intro"), b"R")
        label = {"id": 3, "name": "Reading", "modname": "label",
                 "description": f'<p>Read this before class, it matters.</p><a href="{link}">r</a>'
                                '<a href="https://example.org/more">more</a>'}
        self.course(label)
        code, out = self.run_one(3)
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(self.section_files()), ["UNIT/Week1/reading.pdf"])
        self.assertIn("Not written (section-level files untouched)", out)
        self.assertIn("1 external link(s)", out)

    def test_url_module_with_a_moodle_file_downloads_it(self):
        link = self.server.add_file(pf_url("linked.pdf", 61), b"L")
        mod = {"id": 4, "name": "Linked", "modname": "url", "contents": [
            {"type": "url", "fileurl": link, "filesize": 1}]}
        self.course(mod)
        code, out = self.run_one(4)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.section_files()["UNIT/Week1/linked.pdf"], b"L")

    def test_dry_run_writes_nothing(self):
        self.course(self.server.resource(1, "Slides", [("s.pdf", b"S")]))
        code, out = self.run_one(1, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("[dry]", out)
        self.assertEqual(self.tree(), {})


class NothingToWriteTests(SyncOneCase):
    """Module types whose only output is section-level: say so, exit 3, write nothing."""

    def check(self, mod, fragment):
        self.course(mod)
        before = self.tree()
        code, out = self.run_one(mod["id"])
        self.assertEqual(code, 3, out)
        self.assertIn("Nothing was written", out)
        self.assertIn(fragment, out)
        self.assertEqual(self.tree(), before)

    def test_quiz_and_assign(self):
        self.check({"id": 1, "name": "Quiz 1", "modname": "quiz", "description": "<p>x</p>"}, "_content.md")
        self.server.courses.clear()
        self.check({"id": 2, "name": "Task", "modname": "assign", "description": "<p>x</p>"}, "_content.md")

    def test_label_with_only_text(self):
        self.check({"id": 3, "name": "Intro", "modname": "label",
                    "description": "<p>Just some welcome prose for the week.</p>"}, "_content.md")

    def test_external_url(self):
        self.check({"id": 4, "name": "Site", "modname": "url", "contents": [
            {"type": "url", "fileurl": "https://example.org/x"}]}, "_links.md")

    def test_unsupported_type(self):
        self.check({"id": 5, "name": "Forum", "modname": "forum",
                    "description": "<p>Discuss things here with everyone.</p>"}, "type 'forum'")

    def test_locked_book(self):
        self.check({"id": 6, "name": "Later", "modname": "book", "uservisible": False,
                    "availabilityinfo": "soon", "contents": []}, "Skipped list")


class OtherBehaviourTests(SyncOneCase):
    def test_section_files_are_never_rewritten(self):
        res = self.server.resource(1, "Slides", [("s.pdf", b"S")])
        label = {"id": 2, "name": "Intro", "modname": "label",
                 "description": '<p>Prose that goes to content.</p><a href="https://example.org/z">z</a>'}
        self.course(res, label)
        self.run_main()
        before = self.generated()
        self.assertTrue(before)
        (self.out / "UNIT/Week1/s.pdf").unlink()
        self.assertEqual(self.run_one(1)[0], 0)
        self.assertEqual(self.generated(), before)
        self.assertEqual(self.section_files()["UNIT/Week1/s.pdf"], b"S")

    def test_unknown_cmid_exits_1(self):
        self.course(self.server.resource(1, "Slides", [("s.pdf", b"S")]))
        code, out = self.run_one(999)
        self.assertEqual(code, 1)
        self.assertIn("cmid 999 not found", out)

    def test_failed_fetch_exits_1_with_a_fail_line(self):
        res = self.server.resource(1, "Slides", [("s.pdf", b"S")])
        del self.server.files[FakeMoodle.key(res["contents"][0]["fileurl"])]
        self.course(res)
        code, out = self.run_one(1)
        self.assertEqual(code, 1)
        self.assertIn("[fail] UNIT/Week1/s.pdf", out)

    def test_bad_usage_exits_2(self):
        for argv in ([], ["abc"]):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    sync_one.main(argv)
            self.assertEqual(ctx.exception.code, 2)

    def test_a_clashing_name_gets_the_same_name_as_in_a_full_sync(self):
        srv = self.server
        a = srv.resource(1, "A", [("sample output.txt", b"first", 11)])
        b = srv.resource(2, "B", [("Sample output.txt", b"second", 12)])
        self.course(a, b)
        self.run_main()
        folder = self.out / "UNIT/Week1"
        self.assertEqual((folder / "Sample output (2).txt").read_bytes(), b"second")
        (folder / "Sample output (2).txt").unlink()
        code, out = self.run_one(2)
        self.assertEqual(code, 0, out)
        self.assertEqual((folder / "sample output.txt").read_bytes(), b"first")  # not overwritten
        self.assertEqual((folder / "Sample output (2).txt").read_bytes(), b"second")

    def test_crash_is_one_redacted_line(self):
        self.course(self.server.resource(1, "Slides", [("s.pdf", b"S")]))
        with mock.patch.object(cs, "dispatch_module", side_effect=KeyError("x")):
            code, out = self.run_one(1)
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", out)


if __name__ == "__main__":
    unittest.main()
