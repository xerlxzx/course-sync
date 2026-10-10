"""F2: one failing file, module or course must not abort the run."""
import os
import unittest
from unittest import mock

import requests

from support import (COURSE2_SHORTNAME, TOKEN, FakeMoodle, SyncCase, cs, pf_url)


class FailureIsolationTests(SyncCase):
    def two_courses(self):
        srv = self.server
        res_a = srv.resource(1, "Slides A", [("a.pdf", b"AAA")])
        res_b = srv.resource(2, "Slides B", [("b.pdf", b"BBB")])
        res_c = srv.resource(3, "Slides C", [("c.pdf", b"CCC")])
        res_d = srv.resource(4, "Week 2 slides", [("d.pdf", b"DDD")])
        srv.add_course(101, [srv.section("Week 1", [res_a, res_b, res_c]),
                             srv.section("Week 2", [res_d])])
        other = srv.resource(5, "Other slides", [("o.pdf", b"OOO")])
        srv.add_course(102, [srv.section("Week 1", [other])], shortname=COURSE2_SHORTNAME)
        self.write_config(two_courses=True)
        return res_b

    def test_404_mid_course_does_not_stop_the_rest(self):
        res_b = self.two_courses()
        del self.server.files[FakeMoodle.key(res_b["contents"][0]["fileurl"])]  # now a 404
        code, out, err = self.run_main()

        self.assertEqual(code, 1)
        files = self.tree()
        self.assertEqual(files["UNIT/Week1/a.pdf"], b"AAA")
        self.assertEqual(files["UNIT/Week1/c.pdf"], b"CCC")
        self.assertEqual(files["UNIT/Week2/d.pdf"], b"DDD")
        self.assertEqual(files["OTHER/Week1/o.pdf"], b"OOO")  # the next course still synced
        self.assertNotIn("UNIT/Week1/b.pdf", files)

        self.assertIn("[fail] UNIT/Week1/b.pdf: ", out)
        self.assertIn("1 failure", out.splitlines()[-1])
        self.assertNotIn("Traceback", out + err)
        self.assert_no_token_anywhere(out, err)

        index_md = files["UNIT/Week1/_index.md"].decode()
        self.assertIn("UNIT/Week1/b.pdf - FAILED:", index_md)
        self.assertIn("a.pdf", index_md)  # section index written despite the failure
        self.assertIn("c.pdf", index_md)
        self.assertIn("UNIT/Week2/_index.md", files)

    def test_hash_index_is_saved_after_a_failed_course(self):
        res_b = self.two_courses()
        del self.server.files[FakeMoodle.key(res_b["contents"][0]["fileurl"])]
        self.run_main()
        recorded = {p for e in self.index()["files"].values() for p in e["paths"]}
        self.assertEqual(recorded, {"UNIT/Week1/a.pdf", "UNIT/Week1/c.pdf",
                                    "UNIT/Week2/d.pdf", "OTHER/Week1/o.pdf"})

    def test_failed_file_is_retried_on_the_next_run(self):
        res_b = self.two_courses()
        key = FakeMoodle.key(res_b["contents"][0]["fileurl"])
        data = self.server.files.pop(key)
        self.run_main()
        self.server.files[key] = data
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("[dl]   UNIT/Week1/b.pdf", out)
        self.assertEqual(out.splitlines()[-1], "Done.")

    def test_timeout_in_a_folder_keeps_the_other_files(self):
        srv = self.server
        folder = srv.resource(1, "Pack", [("one.pdf", b"1"), ("two.pdf", b"2"), ("three.pdf", b"3")],
                              modname="folder")
        srv.errors[FakeMoodle.key(folder["contents"][1]["fileurl"])] = requests.ConnectTimeout(
            f"timed out fetching {folder['contents'][1]['fileurl']}?token={TOKEN}")
        srv.add_course(101, [srv.section("Week 1", [folder])])
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        files = self.tree()
        self.assertEqual(files["UNIT/Week1/one.pdf"], b"1")
        self.assertEqual(files["UNIT/Week1/three.pdf"], b"3")
        self.assertNotIn("UNIT/Week1/two.pdf", files)
        self.assertIn("[fail] UNIT/Week1/two.pdf: ConnectTimeout", out)
        self.assert_no_token_anywhere(out, err)

    def test_oserror_while_writing_is_isolated_and_leaves_no_partial_file(self):
        srv = self.server
        srv.add_course(101, [srv.section("Week 1", [
            srv.resource(1, "A", [("good.pdf", b"G")]),
            srv.resource(2, "B", [("bad.pdf", b"B")])])])
        real_replace = os.replace

        def flaky_replace(src, dst, *a, **kw):
            if str(dst).endswith("bad.pdf"):
                raise OSError(63, "File name too long")
            return real_replace(src, dst, *a, **kw)

        with mock.patch("os.replace", flaky_replace):
            code, out, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("[fail] UNIT/Week1/bad.pdf: OSError", out)
        names = sorted(self.tree())
        self.assertIn("UNIT/Week1/good.pdf", names)
        self.assertNotIn("UNIT/Week1/bad.pdf", names)
        self.assertFalse([n for n in names if n.endswith(".part")])

    def test_contents_failure_for_one_course_skips_only_that_course(self):
        self.two_courses()
        self.server.api_status[("core_course_get_contents", 101)] = 503
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        files = self.tree()
        self.assertEqual(files["OTHER/Week1/o.pdf"], b"OOO")
        self.assertFalse([n for n in files if n.startswith("UNIT/")])
        self.assertIn("[fail] " + "UNIT1000_T1_2026" + ": could not read the course contents", out)
        self.assert_no_token_anywhere(out, err)

    def test_handler_exception_is_isolated_per_module(self):
        srv = self.server
        label = {"id": 9, "name": "Broken label", "modname": "label", "description": "<p>x</p>"}
        srv.add_course(101, [srv.section("Week 1", [label, srv.resource(1, "Good", [("g.pdf", b"G")])])])
        with mock.patch.object(cs, "handle_label_module", side_effect=KeyError("boom")):
            code, out, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("[fail] Broken label (label): KeyError", out)
        files = self.tree()
        self.assertEqual(files["UNIT/Week1/g.pdf"], b"G")
        self.assertIn("Broken label (label) - FAILED", files["UNIT/Week1/_index.md"].decode())

    def test_index_is_saved_even_if_sync_course_raises(self):
        self.server.add_course(101, [])

        def explode(base_url, token, course, course_dir, dry_run, output_dir, hash_index, *a):
            cs.record_hash_path(hash_index, "ab" * 32, 3, "UNIT/x.bin")
            raise RuntimeError("unexpected")

        with mock.patch.object(cs, "sync_course", explode):
            code, out, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("ab" * 32, self.index()["files"])

    def test_clean_run_still_exits_zero(self):
        self.two_courses()
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertNotIn("[fail]", out)
        self.assertEqual(out.splitlines()[-1], "Done.")

    def test_dry_run_writes_nothing_and_clean_dry_run_exits_zero(self):
        self.two_courses()
        code, _, _ = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.tree().get("UNIT/Week1/a.pdf"), None)


class BookAndPageFailureTests(SyncCase):
    def book(self, chapter_data):
        srv = self.server
        contents = [{"type": "file", "filename": "index.html", "filepath": f"/{i}/",
                     "fileurl": srv.add_file(pf_url(f"book{i}/index.html", 40 + i), data)}
                    if data is not None else
                    {"type": "file", "filename": "index.html", "filepath": f"/{i}/",
                     "fileurl": pf_url(f"book{i}/index.html", 40 + i)}
                    for i, data in enumerate(chapter_data, 1)]
        return {"id": 5, "name": "Notes book", "modname": "book", "contents": contents}

    def test_failed_chapter_keeps_the_previous_complete_copy(self):
        good = b"<div><h2>Chapter text</h2><p>Plenty of words in this chapter body.</p></div>"
        mod = self.book([good, good])
        self.server.add_course(101, [self.server.section("Week 1", [mod])])
        self.assertEqual(self.run_main()[0], 0)
        book_path = self.out / "UNIT/Week1/Notes book.md"
        before = book_path.read_bytes()

        del self.server.files[FakeMoodle.key(mod["contents"][1]["fileurl"])]
        code, out, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(book_path.read_bytes(), before)
        self.assertIn("[keep] UNIT/Week1/Notes book.md", out)
        self.assertIn("[fail] UNIT/Week1/Notes book.md: book chapter", out)

    def test_failed_chapter_on_a_first_capture_is_written_with_a_placeholder(self):
        good = b"<div><h2>Chapter text</h2><p>Plenty of words in this chapter body.</p></div>"
        self.server.add_course(101, [self.server.section("Week 1", [self.book([good, None])])])
        code, _, _ = self.run_main()
        self.assertEqual(code, 1)
        text = (self.out / "UNIT/Week1/Notes book.md").read_text()
        self.assertIn("chapter fetch failed", text)

    def test_failed_page_is_counted(self):
        page = {"id": 6, "name": "Spec", "modname": "page", "contents": [
            {"type": "file", "filename": "index.html", "fileurl": pf_url("index.html", 60)}]}
        self.server.add_course(101, [self.server.section("Week 1", [page])])
        code, out, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("[fail] UNIT/Week1/Spec.md: page fetch failed", out)


class WriteBytesTests(SyncCase):
    def test_failed_replace_leaves_neither_target_nor_temp_file(self):
        target = self.tmp / "sub" / "f.bin"
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                cs._write_bytes(target, b"data")
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
