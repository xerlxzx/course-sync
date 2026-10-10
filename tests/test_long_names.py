"""F9: names longer than the filesystem allows are shortened deterministically."""
import unittest

from support import SyncCase, cs

LONG = "Very long lecture title " * 20  # ~480 bytes


class ShortenNameTests(unittest.TestCase):
    def test_short_names_are_untouched(self):
        self.assertEqual(cs.sanitize_filename("Week 1 notes.pdf"), "Week 1 notes.pdf")
        exact = "a" * (cs.MAX_NAME_BYTES - 4) + ".pdf"
        self.assertEqual(len(exact.encode()), cs.MAX_NAME_BYTES)
        self.assertEqual(cs.sanitize_filename(exact), exact)

    def test_one_byte_over_is_shortened(self):
        over = "a" * (cs.MAX_NAME_BYTES - 3) + ".pdf"
        out = cs.sanitize_filename(over)
        self.assertLessEqual(len(out.encode()), cs.MAX_NAME_BYTES)
        self.assertTrue(out.endswith(".pdf"))
        self.assertNotEqual(out, over)

    def test_long_name_keeps_extension_and_fits(self):
        out = cs.sanitize_filename(LONG + ".pptx")
        self.assertLessEqual(len(out.encode("utf-8")), cs.MAX_NAME_BYTES)
        self.assertTrue(out.endswith(".pptx"))
        self.assertRegex(out, r"~[0-9a-f]{8}\.pptx$")

    def test_deterministic_and_idempotent(self):
        once = cs.sanitize_filename(LONG + ".pdf")
        self.assertEqual(once, cs.sanitize_filename(LONG + ".pdf"))
        self.assertEqual(cs.sanitize_filename(once), once)

    def test_long_names_with_a_shared_prefix_do_not_collide(self):
        a = cs.sanitize_filename(LONG + "A.pdf")
        b = cs.sanitize_filename(LONG + "B.pdf")
        self.assertNotEqual(a, b)

    def test_multibyte_names_are_cut_on_a_character_boundary(self):
        for name in ("é" * 300 + ".txt", "漢字" * 100 + ".txt", "😀" * 80 + ".txt"):
            out = cs.sanitize_filename(name)
            self.assertLessEqual(len(out.encode("utf-8")), cs.MAX_NAME_BYTES)
            self.assertNotIn("�", out)
            out.encode("utf-8").decode("utf-8")  # valid UTF-8
            self.assertTrue(out.endswith(".txt"))

    def test_no_usable_extension_still_gets_a_hash(self):
        out = cs.sanitize_filename("x" * 400)
        self.assertLessEqual(len(out.encode()), cs.MAX_NAME_BYTES)
        self.assertRegex(out, r"~[0-9a-f]{8}$")
        weird = cs.sanitize_filename("y" * 300 + ".this is not an ext " + "z" * 40)
        self.assertLessEqual(len(weird.encode()), cs.MAX_NAME_BYTES)

    def test_trailing_dot_and_space_are_not_left_by_the_cut(self):
        out = cs.sanitize_filename(("word " * 80) + ".pdf")
        stem = out[: out.index("~")]
        self.assertEqual(stem, stem.rstrip(" ."))

    def test_section_folder_names_are_shortened_too(self):
        folder = cs.section_folder_name("Topic " + "x" * 400, 3)
        self.assertLessEqual(len(folder.encode()), cs.MAX_NAME_BYTES)


class LongNameEndToEnd(SyncCase):
    def test_long_file_page_book_and_section_names_sync_without_error(self):
        srv = self.server
        long_file = srv.resource(1, "Long file", [(LONG + ".pdf", b"PDF")])
        page = {"id": 2, "name": LONG + "page", "modname": "page", "contents": [
            {"type": "file", "filename": "index.html",
             "fileurl": srv.add_file("https://moodle.example.test/pluginfile.php/55/mod_page/content/2/index.html",
                                     b"<div><p>Some page text that is long enough to keep.</p></div>")}]}
        book = {"id": 3, "name": LONG + "book", "modname": "book", "contents": [
            {"type": "file", "filename": "index.html", "filepath": "/1/",
             "fileurl": srv.add_file("https://moodle.example.test/pluginfile.php/55/mod_book/content/3/1/index.html",
                                     b"<div><p>Chapter text that is long enough to keep.</p></div>")}]}
        srv.add_course(101, [srv.section("Week 1", [long_file, page, book]),
                             srv.section("Reading " + "z" * 400, [srv.resource(4, "R", [("r.pdf", b"R")])])])
        code, out, _ = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertNotIn("[fail]", out)
        names = [p.name for p in self.out.rglob("*") if p.is_file() or p.is_dir()]
        self.assertTrue(all(len(n.encode()) <= 255 for n in names))
        self.assertTrue(any(n.endswith(".pdf") and "~" in n for n in names))

        code, out, _ = self.run_main()  # stable on rerun: nothing re-downloaded
        self.assertEqual(code, 0)
        self.assertNotIn("[dl]", out)
        self.assertNotIn("[upd]", out)


if __name__ == "__main__":
    unittest.main()
