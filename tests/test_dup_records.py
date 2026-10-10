"""F5: a content duplicate (never written at its own path) is recorded in the
hash index and not re-fetched on later runs."""
import json
import unittest

from support import BASE_URL, FakeMoodle, SyncCase, cs, pf_url

IMG = b"\x89PNG-synthetic-image-bytes" * 20


class DupRecordTests(SyncCase):
    def two_weeks(self, second_bytes=IMG):
        srv = self.server
        self.orig = srv.resource(1, "Week 1 image", [("READ.png", IMG, 11)])
        self.dup = srv.resource(2, "Week 2 image", [("READ.png", second_bytes, 22)])
        srv.add_course(101, [srv.section("Week 1", [self.orig]), srv.section("Week 2", [self.dup])])
        self.dup_url = self.dup["contents"][0]["fileurl"]

    def gets_for(self, url):
        return [c for c in self.server.calls if c[0].startswith(url)]

    def test_first_run_dups_second_run_does_not_fetch(self):
        self.two_weeks()
        _, out, _ = self.run_main()
        self.assertIn("[dup]  UNIT/Week2/READ.png  (same content as UNIT/Week1/READ.png)", out)
        self.assertFalse((self.out / "UNIT/Week2/READ.png").exists())
        self.assertEqual(len(self.server.file_gets("/22/READ.png")), 1)

        self.server.calls.clear()
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.file_gets(), [])                 # zero file GETs at all
        self.assertIn("[dup]  UNIT/Week2/READ.png", out)
        self.assertIn("not re-fetched", out)
        self.assertNotIn("[dl]", out)

    def test_record_contents(self):
        self.two_weeks()
        self.run_main()
        rec = self.index()["dups"]["UNIT/Week2/READ.png"]
        self.assertEqual(rec["original"], "UNIT/Week1/READ.png")
        self.assertEqual(rec["size"], len(IMG))
        self.assertEqual(rec["remote_size"], len(IMG))
        self.assertEqual(rec["remote_mtime"], 1000)
        self.assertEqual(rec["sha256"], next(iter(self.index()["files"])))
        # the dup's own path is not claimed as a place the content lives
        paths = [p for e in self.index()["files"].values() for p in e["paths"]]
        self.assertEqual(paths, ["UNIT/Week1/READ.png"])

    def test_dry_run_reports_recorded_dups_without_fetching(self):
        self.two_weeks()
        self.run_main()
        self.server.calls.clear()
        _, out, _ = self.run_main("--dry-run")
        self.assertIn("[dup]  UNIT/Week2/READ.png", out)
        self.assertEqual(self.server.file_gets(), [])

    def test_changed_remote_size_refetches_and_writes_the_new_file(self):
        self.two_weeks()
        self.run_main()
        new = b"different image" * 30
        self.server.files[FakeMoodle.key(self.dup_url)] = new
        self.dup["contents"][0]["filesize"] = len(new)
        _, out, _ = self.run_main()
        self.assertIn("[dl]   UNIT/Week2/READ.png", out)
        self.assertEqual((self.out / "UNIT/Week2/READ.png").read_bytes(), new)
        self.assertNotIn("UNIT/Week2/READ.png", self.index().get("dups", {}))

    def test_newer_remote_time_refetches_then_rerecords(self):
        self.two_weeks()
        self.run_main()
        self.dup["contents"][0]["timemodified"] = 5000
        self.server.calls.clear()
        _, out, _ = self.run_main()
        self.assertEqual(len(self.server.file_gets("/22/READ.png")), 1)
        self.assertIn("(same content as UNIT/Week1/READ.png)", out)
        self.assertEqual(self.index()["dups"]["UNIT/Week2/READ.png"]["remote_mtime"], 5000)
        self.server.calls.clear()
        self.run_main()
        self.assertEqual(self.server.file_gets(), [])

    def test_original_gone_from_disk_and_server_turns_the_dup_into_a_real_file(self):
        self.two_weeks()
        self.run_main()
        (self.out / "UNIT/Week1/READ.png").unlink()
        self.server.courses[0]["sections"] = self.server.courses[0]["sections"][1:]  # Week 1 removed
        _, out, _ = self.run_main()
        self.assertEqual((self.out / "UNIT/Week2/READ.png").read_bytes(), IMG)
        self.assertIn("[dl]   UNIT/Week2/READ.png", out)
        self.assertNotIn("UNIT/Week2/READ.png", self.index().get("dups", {}))

    def test_original_deleted_but_still_listed_is_restored_and_the_dup_stays_a_dup(self):
        self.two_weeks()
        self.run_main()
        (self.out / "UNIT/Week1/READ.png").unlink()
        _, out, _ = self.run_main()
        self.assertIn("[dl]   UNIT/Week1/READ.png", out)
        self.assertFalse((self.out / "UNIT/Week2/READ.png").exists())

    def test_original_replaced_by_other_content_stops_the_dup_claim(self):
        self.two_weeks()
        self.run_main()
        new = b"X" * len(IMG)  # same size, different bytes
        self.server.files[FakeMoodle.key(self.orig["contents"][0]["fileurl"])] = new
        self.orig["contents"][0]["timemodified"] = 9_999_999_999
        self.run_main()
        # Week 1 now holds the new bytes, so Week 2 (old bytes) is no longer a duplicate.
        self.assertEqual((self.out / "UNIT/Week1/READ.png").read_bytes(), new)
        self.assertEqual((self.out / "UNIT/Week2/READ.png").read_bytes(), IMG)

    def test_links_scraped_from_labels_have_no_metadata_and_trust_the_record(self):
        srv = self.server
        orig = srv.resource(1, "Original", [("chart.png", IMG, 11)])
        link = srv.add_file(pf_url("same.png", 33, area="mod_label/intro"), IMG)
        label = {"id": 2, "name": "Intro label", "modname": "label",
                 "description": f'<p>Welcome to the unit, please read everything.</p><a href="{link}">pic</a>'}
        srv.add_course(101, [srv.section("Week 1", [orig, label])])
        _, out, _ = self.run_main()
        self.assertIn("[dup]  UNIT/Week1/same.png", out)
        self.assertIsNone(self.index()["dups"]["UNIT/Week1/same.png"]["remote_size"])
        self.server.calls.clear()
        self.run_main()
        self.assertEqual(self.server.file_gets(), [])

    def test_legacy_index_with_the_dup_path_listed_as_a_holder_is_cleaned(self):
        self.two_weeks()
        self.run_main()
        idx = self.index()
        idx.pop("dups")
        (sha, entry), = idx["files"].items()
        entry["paths"].append("UNIT/Week2/READ.png")  # what older versions recorded for a dup
        (self.out / ".course_sync" / "downloaded.json").write_text(json.dumps(idx))
        self.run_main()
        self.assertEqual(self.index()["files"][sha]["paths"], ["UNIT/Week1/READ.png"])
        self.assertIn("UNIT/Week2/READ.png", self.index()["dups"])

    def test_same_folder_duplicate_content_under_a_second_name_costs_one_fetch_only(self):
        srv = self.server
        a = srv.resource(1, "A", [("chart.png", IMG, 11)])
        b = srv.resource(2, "B", [("Chart.png", IMG, 12)])
        srv.add_course(101, [srv.section("Week 1", [a, b])])
        self.run_main()
        names = sorted(p.name for p in (self.out / "UNIT/Week1").glob("*.png"))
        self.assertEqual(names, ["chart.png"])  # identical bytes: no second copy
        self.server.calls.clear()
        self.run_main()
        self.assertEqual(self.server.file_gets(), [])


if __name__ == "__main__":
    unittest.main()
