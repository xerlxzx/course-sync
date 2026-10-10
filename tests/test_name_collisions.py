"""F3: two different files whose names differ only by case (or not at all) must
both survive, under stable names, on any filesystem."""
import hashlib
import json
import unittest

from support import SyncCase, cs, pf_url

C1 = b"A" * 1107     # "sample output.txt" on the server
C2 = b"B" * 3186     # "Sample output.txt" on the server
GOOD_HTML = b"<div><p>Page body text that is comfortably long enough to keep.</p></div>"


def sha(data):
    return hashlib.sha256(data).hexdigest()


class ClaimNameTests(unittest.TestCase):
    def state(self):
        return cs.new_section_state()

    def test_first_claimant_keeps_the_name_and_later_ones_are_numbered(self):
        st, d = self.state(), cs.Path("/x")
        self.assertEqual(cs.claim_name(st, d, "Sample output.txt", "u1"), "Sample output.txt")
        self.assertEqual(cs.claim_name(st, d, "sample output.txt", "u2"), "sample output (2).txt")
        self.assertEqual(cs.claim_name(st, d, "SAMPLE OUTPUT.TXT", "u3"), "SAMPLE OUTPUT (3).TXT")

    def test_same_source_gets_its_name_back(self):
        st, d = self.state(), cs.Path("/x")
        first = cs.claim_name(st, d, "a.txt", "u1")
        cs.claim_name(st, d, "A.txt", "u2")
        self.assertEqual(cs.claim_name(st, d, "a.txt", "u1"), first)
        self.assertEqual(cs.claim_name(st, d, "A.txt", "u2"), "A (2).txt")

    def test_folders_are_independent(self):
        st = self.state()
        self.assertEqual(cs.claim_name(st, cs.Path("/x"), "a.txt", "u1"), "a.txt")
        self.assertEqual(cs.claim_name(st, cs.Path("/y"), "a.txt", "u2"), "a.txt")

    def test_unicode_normalisation_and_full_case_folding(self):
        st, d = self.state(), cs.Path("/x")
        nfc, nfd = "café.txt", "café.txt"
        self.assertEqual(cs.claim_name(st, d, nfc, "u1"), nfc)
        self.assertEqual(cs.claim_name(st, d, nfd, "u2"), "café (2).txt")
        self.assertEqual(cs.claim_name(st, d, "Straße.txt", "u3"), "Straße.txt")
        self.assertEqual(cs.claim_name(st, d, "STRASSE.txt", "u4"), "STRASSE (2).txt")

    def test_generated_file_names_are_never_handed_out(self):
        st, d = self.state(), cs.Path("/x")
        self.assertEqual(cs.claim_name(st, d, "_index.md", "u1"), "_index (2).md")
        self.assertEqual(cs.claim_name(st, d, "_LINKS.md", "u2"), "_LINKS (2).md")

    def test_numbering_skips_names_already_taken(self):
        st, d = self.state(), cs.Path("/x")
        cs.claim_name(st, d, "a (2).txt", "u0")
        cs.claim_name(st, d, "a.txt", "u1")
        self.assertEqual(cs.claim_name(st, d, "A.txt", "u2"), "A (3).txt")

    def test_source_key_ignores_query_and_webservice_prefix(self):
        a = cs.file_source_key("https://m.example/pluginfile.php/5/c/1/f.pdf?forcedownload=1")
        b = cs.file_source_key("https://m.example/webservice/pluginfile.php/5/c/1/f.pdf")
        self.assertEqual(a, b)
        self.assertNotEqual(a, cs.file_source_key("https://m.example/pluginfile.php/5/c/2/f.pdf"))


class CollisionEndToEnd(SyncCase):
    def assessments(self, *modules):
        self.server.add_course(101, [self.server.section("Assessment tasks", list(modules))])
        return self.out / "UNIT" / "Assessments"

    def test_case_variants_in_different_modules_both_survive_and_are_stable(self):
        srv = self.server
        folder = self.assessments(
            srv.resource(1, "Sample A", [("sample output.txt", C1, 11)]),
            srv.resource(2, "Sample B", [("Sample output.txt", C2, 12)]))
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual((folder / "sample output.txt").read_bytes(), C1)
        self.assertEqual((folder / "Sample output (2).txt").read_bytes(), C2)
        names = sorted(p.name for p in folder.iterdir() if "ample" in p.name)
        self.assertEqual(names, ["Sample output (2).txt", "sample output.txt"])
        index_md = (folder / "_index.md").read_text()
        self.assertIn("Sample output (2).txt", index_md)

        before = self.tree()
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        for tag in ("[dl]", "[upd]", "[dup]"):
            self.assertNotIn(tag, out)
        self.assertEqual(self.tree(), before)

    def test_identical_names_from_different_sources_are_numbered(self):
        srv = self.server
        folder = self.assessments(
            srv.resource(1, "One", [("notes.pdf", b"first", 11)]),
            srv.resource(2, "Two", [("notes.pdf", b"second", 12)]),
            srv.resource(3, "Three", [("NOTES.pdf", b"third", 13)]))
        self.run_main()
        self.assertEqual((folder / "notes.pdf").read_bytes(), b"first")
        self.assertEqual((folder / "notes (2).pdf").read_bytes(), b"second")
        self.assertEqual((folder / "NOTES (3).pdf").read_bytes(), b"third")

    def test_two_files_in_one_module_with_clashing_names(self):
        srv = self.server
        folder = self.assessments(
            srv.resource(1, "Both", [("data.csv", b"lower", 11), ("DATA.csv", b"upper", 12)],
                         modname="folder"))
        self.run_main()
        self.assertEqual((folder / "data.csv").read_bytes(), b"lower")
        self.assertEqual((folder / "DATA (2).csv").read_bytes(), b"upper")

    def test_the_same_source_in_two_modules_is_not_copied(self):
        srv = self.server
        a = srv.resource(1, "One", [("shared.pdf", b"S", 11)])
        b = {"id": 2, "name": "Two", "modname": "resource", "contents": [
            dict(a["contents"][0], fileurl=a["contents"][0]["fileurl"] + "?forcedownload=1")]}
        folder = self.assessments(a, b)
        self.run_main()
        self.assertEqual(sorted(p.name for p in folder.glob("shared*")), ["shared.pdf"])

    def test_clashing_names_in_two_sections_that_share_a_folder(self):
        srv = self.server
        srv.add_course(101, [
            srv.section("Assessment 1", [srv.resource(1, "A", [("brief.pdf", b"one", 11)])]),
            srv.section("Quiz 2", [srv.resource(2, "B", [("Brief.pdf", b"two", 12)])])])
        self.run_main()
        folder = self.out / "UNIT" / "Assessments"
        self.assertEqual((folder / "brief.pdf").read_bytes(), b"one")
        self.assertEqual((folder / "Brief (2).pdf").read_bytes(), b"two")

    def test_same_name_in_different_folders_is_left_alone(self):
        srv = self.server
        srv.add_course(101, [
            srv.section("Week 1", [srv.resource(1, "A", [("a.pdf", b"one", 11)])]),
            srv.section("Week 2", [srv.resource(2, "B", [("A.pdf", b"two", 12)])])])
        self.run_main()
        self.assertEqual((self.out / "UNIT/Week1/a.pdf").read_bytes(), b"one")
        self.assertEqual((self.out / "UNIT/Week2/A.pdf").read_bytes(), b"two")

    def test_resource_named_like_a_generated_file_does_not_collide_with_it(self):
        srv = self.server
        srv.add_course(101, [srv.section("Week 1", [srv.resource(1, "A", [("_index.md", b"mine", 11)])])])
        self.run_main()
        self.assertEqual((self.out / "UNIT/Week1/_index (2).md").read_bytes(), b"mine")
        self.assertIn("auto-generated", (self.out / "UNIT/Week1/_index.md").read_text())

    def test_pages_and_books_with_the_same_name_do_not_overwrite_each_other(self):
        srv = self.server

        def page(cmid, name, body):
            url = srv.add_file(pf_url("index.html", 100 + cmid, area="mod_page/content"), body)
            return {"id": cmid, "name": name, "modname": "page", "contents": [
                {"type": "file", "filename": "index.html", "fileurl": url}]}

        def book(cmid, name, body):
            url = srv.add_file(pf_url("index.html", 200 + cmid, area="mod_book/content"), body)
            return {"id": cmid, "name": name, "modname": "book", "contents": [
                {"type": "file", "filename": "index.html", "filepath": "/1/", "fileurl": url}]}

        srv.add_course(101, [srv.section("Week 1", [
            page(1, "Spec", b"<div><p>First spec page with enough words to keep.</p></div>"),
            page(2, "Spec", b"<div><p>Second spec page with enough words to keep.</p></div>"),
            page(3, "Spec?", b"<div><p>Third spec page, same name once sanitised.</p></div>"),
            book(4, "spec", b"<div><p>A book that is named like the pages above.</p></div>")])])
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        week = self.out / "UNIT/Week1"
        self.assertIn("First spec", (week / "Spec.md").read_text())
        self.assertIn("Second spec", (week / "Spec (2).md").read_text())
        self.assertIn("Third spec", (week / "Spec (3).md").read_text())
        self.assertIn("named like the pages", (week / "spec (4).md").read_text())

        out2 = self.run_main()[1]
        self.assertNotIn("[page]", out2)
        self.assertNotIn("[book]", out2)

    def test_a_locked_book_does_not_reserve_a_name(self):
        srv = self.server
        locked = {"id": 1, "name": "Notes", "modname": "book", "uservisible": False,
                  "availabilityinfo": "later", "contents": []}
        url = srv.add_file(pf_url("index.html", 300, area="mod_page/content"), GOOD_HTML)
        page = {"id": 2, "name": "Notes", "modname": "page", "contents": [
            {"type": "file", "filename": "index.html", "fileurl": url}]}
        srv.add_course(101, [srv.section("Week 1", [locked, page])])
        self.run_main()
        self.assertTrue((self.out / "UNIT/Week1/Notes.md").exists())

    def test_dry_run_reports_the_same_names(self):
        srv = self.server
        self.assessments(srv.resource(1, "A", [("x.txt", b"1", 11)]),
                         srv.resource(2, "B", [("X.txt", b"2", 12)]))
        code, out, _ = self.run_main("--dry-run")
        self.assertIn("UNIT/Assessments/x.txt", out)
        self.assertIn("UNIT/Assessments/X (2).txt", out)


class MigrationTests(SyncCase):
    """
    The live checkout's bad state: on case-insensitive APFS the two files shared
    one path. Disk holds ONE file, "sample output.txt", with the 3186-byte
    content; the index maps sha(1107 B) to ".../sample output.txt" and sha(3186 B)
    to ".../Sample output.txt".
    """

    def seed(self):
        folder = self.out / "UNIT" / "Assessments"
        folder.mkdir(parents=True)
        (folder / "sample output.txt").write_bytes(C2)
        index = {"files": {
            sha(C1): {"size": len(C1), "first_seen": "2026-10-09T12:19:22Z",
                      "paths": ["UNIT/Assessments/sample output.txt"]},
            sha(C2): {"size": len(C2), "first_seen": "2026-10-09T12:19:23Z",
                      "paths": ["UNIT/Assessments/Sample output.txt"]}}}
        (self.out / ".course_sync").mkdir()
        (self.out / ".course_sync" / "downloaded.json").write_text(json.dumps(index))
        return folder

    def modules(self, order):
        srv = self.server
        mods = {"A": srv.resource(1, "Sample A", [("sample output.txt", C1, 11)]),
                "B": srv.resource(2, "Sample B", [("Sample output.txt", C2, 12)])}
        srv.add_course(101, [srv.section("Assessment tasks", [mods[c] for c in order])])

    def case_insensitive_fs(self):
        probe = self.tmp / "CaseProbe"
        probe.write_text("x")
        return (self.tmp / "caseprobe").exists()

    def assert_index_matches_disk(self):
        for digest, entry in self.index()["files"].items():
            self.assertTrue(entry["paths"], "entry without paths left behind")
            for rel in entry["paths"]:
                self.assertEqual(sha((self.out / rel).read_bytes()), digest, rel)

    def check_migration(self, order, plain, numbered):
        folder = self.seed()
        self.modules(order)
        code, out, _ = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual((folder / plain[0]).read_bytes(), plain[1])
        self.assertEqual((folder / numbered[0]).read_bytes(), numbered[1])
        self.assertNotIn("[dup]", out)  # the 3186-byte file must not be called a duplicate
        self.assert_index_matches_disk()
        if self.case_insensitive_fs():
            names = sorted(p.name for p in folder.iterdir() if "ample" in p.name)
            self.assertEqual(len(names), 2, names)

        before = self.tree()
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        for tag in ("[dl]", "[upd]", "[dup]", "[fail]"):
            self.assertNotIn(tag, out)
        self.assertEqual(self.tree(), before)
        return out

    def test_server_order_lowercase_first_as_in_the_live_log(self):
        # Module A (1107 B, "sample output.txt") is listed first.
        self.check_migration("AB", plain=("sample output.txt", C1),
                             numbered=("Sample output (2).txt", C2))

    def test_first_run_log_shows_one_update_and_one_download(self):
        folder = self.seed()
        self.modules("AB")
        _, out, _ = self.run_main()
        self.assertIn("[upd]  UNIT/Assessments/sample output.txt  (3186 -> 1107 bytes)", out)
        self.assertIn("[dl]   UNIT/Assessments/Sample output (2).txt", out)
        recorded = {p: d for d, e in self.index()["files"].items() for p in e["paths"]}
        self.assertEqual(recorded, {"UNIT/Assessments/sample output.txt": sha(C1),
                                    "UNIT/Assessments/Sample output (2).txt": sha(C2)})

    def test_server_order_uppercase_first(self):
        self.check_migration("BA", plain=("Sample output.txt", C2),
                             numbered=("sample output (2).txt", C1))

    def test_unseeded_fresh_install_gives_the_same_names(self):
        self.modules("AB")
        self.run_main()
        folder = self.out / "UNIT" / "Assessments"
        self.assertEqual((folder / "sample output.txt").read_bytes(), C1)
        self.assertEqual((folder / "Sample output (2).txt").read_bytes(), C2)


class IndexHygieneTests(unittest.TestCase):
    def test_forget_hash_path_matches_case_insensitively(self):
        index = {"files": {"aa": {"size": 1, "paths": ["U/Sample output.txt"]},
                           "bb": {"size": 1, "paths": ["U/other.txt"]}}}
        cs.forget_hash_path(index, "U/sample output.txt", "cc")
        self.assertNotIn("aa", index["files"])
        self.assertIn("bb", index["files"])

    def test_record_hash_path_does_not_add_a_case_variant_twice(self):
        index = {"files": {}}
        cs.record_hash_path(index, "aa", 1, "U/a.txt")
        cs.record_hash_path(index, "aa", 1, "U/A.txt")
        self.assertEqual(index["files"]["aa"]["paths"], ["U/a.txt"])


if __name__ == "__main__":
    unittest.main()
