"""F4: a unit guide whose PDF differs only in its /ID and dates is not rewritten;
unit_guide_period picks one offering when the portal lists several."""
import contextlib
import hashlib
import io
import os
import unittest

from support import FakeResponse, SyncCase, cs

GUIDE_URL = "https://unitguides.example.test/unit_offerings/search?full_code=UNIT1000_T1_2026"
OFFERING_URL = "https://unitguides.example.test/unit_offerings/4242"


def make_pdf(id_a="0A1B2C", id_b="0A1B2C", created="20260101000000", text="Unit outline"):
    return (b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n"
            + text.encode() + b"\n"
            + b"trailer<</Root 1 0 R/Info<</CreationDate (D:" + created.encode()
            + b")/ModDate (D:" + created.encode() + b")>>/ID [<" + id_a.encode()
            + b"><" + id_b.encode() + b">]>>\n%%EOF\n")


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FingerprintTests(unittest.TestCase):
    def test_id_and_dates_are_masked(self):
        a = make_pdf("AAAA", "BBBB", "20260101000000")
        b = make_pdf("cccc1111", "dddd2222eeee", "20271231235959")
        self.assertNotEqual(a, b)
        self.assertEqual(cs.pdf_fingerprint(a), cs.pdf_fingerprint(b))

    def test_literal_string_id_and_whitespace_variants(self):
        a = b"%PDF-1.7 body /ID [ (ab\\)c) (de) ] /ModDate(D:1) tail"
        b = b"%PDF-1.7 body /ID [(zz)(yy)] /ModDate (D:2) tail"
        self.assertEqual(cs.pdf_fingerprint(a), cs.pdf_fingerprint(b))

    def test_real_content_changes_are_not_masked(self):
        self.assertNotEqual(cs.pdf_fingerprint(make_pdf(text="Assessment 1 is worth 30%")),
                            cs.pdf_fingerprint(make_pdf(text="Assessment 1 is worth 40%")))

    def test_bytes_without_volatile_parts_hash_normally(self):
        self.assertEqual(cs.pdf_fingerprint(b"plain"), sha(b"plain"))


class UnitGuideSyncTests(SyncCase):
    def setUp(self):
        super().setUp()
        self.pdf = [make_pdf()]
        self.pdf_gets = []

        def serve(url, params):
            if url.endswith("print.pdf"):
                self.pdf_gets.append(url)
                return FakeResponse(url, 200, self.pdf[0], headers={"Content-Type": "application/pdf"})
            return FakeResponse(OFFERING_URL, 200, b"<html>guide</html>")

        self.server.hooks.append((lambda u: "unitguides.example.test" in u, serve))
        mod = {"id": 1, "name": "Unit Guide", "modname": "url", "contents": [
            {"type": "url", "fileurl": GUIDE_URL}]}
        self.server.add_course(101, [self.server.section("General", [mod])])
        self.guide = self.out / "UNIT" / "Unit_Guide.pdf"

    def test_first_run_downloads_then_render_with_new_id_is_skipped(self):
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("[dl]   UNIT/Unit_Guide.pdf", out)
        first = self.guide.read_bytes()
        stamp = os.stat(self.guide).st_mtime_ns

        self.pdf[0] = make_pdf("FFFF", "EEEE", "20270101000000")  # fresh render, same document
        code, out, _ = self.run_main()
        self.assertIn("[skip] UNIT/Unit_Guide.pdf (unchanged)", out)
        self.assertNotIn("[dl]", out)
        self.assertNotIn("[upd]", out)
        self.assertEqual(self.guide.read_bytes(), first)       # not rewritten
        self.assertEqual(os.stat(self.guide).st_mtime_ns, stamp)
        self.assertEqual(len(self.pdf_gets), 2)  # it still checks the portal each run

    def test_index_records_the_bytes_on_disk_not_the_fetched_render(self):
        self.run_main()
        on_disk = self.guide.read_bytes()
        self.pdf[0] = make_pdf("1111", "2222")
        self.run_main()
        recorded = {p: d for d, e in self.index()["files"].items() for p in e["paths"]}
        self.assertEqual(recorded["UNIT/Unit_Guide.pdf"], sha(on_disk))
        self.assertNotIn(sha(self.pdf[0]), self.index()["files"])

    def test_many_reruns_never_rewrite(self):
        self.run_main()
        for i in range(4):
            self.pdf[0] = make_pdf("%04X" % i, "%04X" % (i + 9))
            _, out, _ = self.run_main()
            self.assertIn("(unchanged)", out)
            self.assertNotIn("[dl]", out)

    def test_a_real_edit_by_the_convenor_is_downloaded_even_with_the_same_offering_id(self):
        self.run_main()
        self.pdf[0] = make_pdf("9999", "8888", text="Unit outline, revised assessment weights")
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("[upd]  UNIT/Unit_Guide.pdf  (unit guide changed)", out)
        self.assertEqual(self.guide.read_bytes(), self.pdf[0])
        recorded = {p: d for d, e in self.index()["files"].items() for p in e["paths"]}
        self.assertEqual(recorded["UNIT/Unit_Guide.pdf"], sha(self.pdf[0]))
        self.assertEqual(len(self.index()["files"]), 1)  # the superseded hash is gone

        _, out, _ = self.run_main()  # and the new version is then stable
        self.assertIn("(unchanged)", out)

    def test_byte_identical_download_is_skipped_too(self):
        self.run_main()
        _, out, _ = self.run_main()
        self.assertIn("[skip] UNIT/Unit_Guide.pdf (unchanged)", out)

    def test_dry_run_does_not_fetch_or_write(self):
        code, out, _ = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(self.guide.exists())
        self.assertEqual(self.pdf_gets, [])


SEARCH_URL = "https://unitguides.example.test/unit_offerings/search?full_code=UNIT1000_T2_2026"
SEARCH_PAGE = """
<a href="/unit_offerings/111/unit_guide">UNIT1000 Example unit - Term 1, 2026</a>
<a href="/unit_offerings/222/unit_guide">UNIT1000 Example unit - Term 2, 2026</a>
<a href="/unit_offerings/333/unit_guide">OTHR2000 Other unit - Term 2, 2026</a>
"""
PERIOD = (r"_T(\d+)_", r"\bTerm\s+{n}\b")


class SelectOfferingTests(unittest.TestCase):
    def test_redirect_to_an_offering_wins(self):
        self.assertEqual(cs.select_unit_offering(SEARCH_URL, OFFERING_URL, SEARCH_PAGE), "4242")

    def test_two_offerings_for_the_unit_are_ambiguous_without_a_period(self):
        self.assertIsNone(cs.select_unit_offering(SEARCH_URL, SEARCH_URL, SEARCH_PAGE))

    def test_period_config_picks_the_matching_offering(self):
        self.assertEqual(cs.select_unit_offering(SEARCH_URL, SEARCH_URL, SEARCH_PAGE, PERIOD), "222")

    def test_period_not_in_the_shortname_falls_back_to_the_course_code(self):
        url = "https://unitguides.example.test/unit_offerings/search?full_code=OTHR2000"
        self.assertEqual(cs.select_unit_offering(url, url, SEARCH_PAGE, PERIOD), "333")

    def test_period_with_no_matching_link_returns_none(self):
        url = SEARCH_URL.replace("_T2_", "_T3_")
        self.assertIsNone(cs.select_unit_offering(url, url, SEARCH_PAGE, PERIOD))


class ParsePeriodConfigTests(unittest.TestCase):
    def parse(self, cfg):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            result = cs.parse_unit_guide_period(cfg)
        return result, err.getvalue()

    def test_missing_key_is_none_and_silent(self):
        self.assertEqual(self.parse({}), (None, ""))

    def test_valid_mapping_is_returned(self):
        cfg = {"unit_guide_period": {"from_shortname": PERIOD[0], "label": PERIOD[1]}}
        self.assertEqual(self.parse(cfg), (PERIOD, ""))

    def test_invalid_values_are_ignored_with_a_warning(self):
        for raw in ("Term", {"from_shortname": PERIOD[0]},
                    {"from_shortname": "_T\\d+_", "label": PERIOD[1]},
                    {"from_shortname": "_T(\\d+_", "label": PERIOD[1]}):
            with self.subTest(raw=raw):
                result, err = self.parse({"unit_guide_period": raw})
                self.assertIsNone(result)
                self.assertIn("unit_guide_period", err)


if __name__ == "__main__":
    unittest.main()
