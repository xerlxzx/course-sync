"""F6: a file re-uploaded with identical content must not be re-fetched every run."""
import contextlib
import io
import os
import unittest

from support import BASE_URL, TOKEN, FakeMoodle, SyncCase, cs


class ReuploadTests(SyncCase):
    def setUp(self):
        super().setUp()
        self.mod = self.server.resource(1, "Video", [("lecture.bin", b"V" * 5000, 11)])
        self.server.add_course(101, [self.server.section("Week 1", [self.mod])])
        self.path = self.out / "UNIT/Week1/lecture.bin"

    def reupload(self, mtime):
        """The server stamps the same bytes with a newer timemodified."""
        self.mod["contents"][0]["timemodified"] = mtime

    def test_identical_content_with_a_newer_server_time_is_fetched_once_more_only(self):
        self.run_main()
        local_mtime = self.path.stat().st_mtime
        self.reupload(int(local_mtime) + 100000)

        gets = len(self.server.file_gets("lecture.bin"))
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("metadata changed, content identical", out)
        self.assertEqual(len(self.server.file_gets("lecture.bin")), gets + 1)
        self.assertEqual(int(self.path.stat().st_mtime), int(local_mtime) + 100000)

        code, out, _ = self.run_main()
        self.assertIn("[skip] UNIT/Week1/lecture.bin", out)
        self.assertNotIn("content identical", out)
        self.assertEqual(len(self.server.file_gets("lecture.bin")), gets + 1)  # no new GET

    def test_a_later_real_change_is_still_picked_up(self):
        self.run_main()
        local_mtime = self.path.stat().st_mtime
        self.reupload(int(local_mtime) + 100000)
        self.run_main()  # stamps the file

        new_bytes = b"W" * 5000  # same size, so only the timestamp reveals it
        self.server.files[FakeMoodle.key(self.mod["contents"][0]["fileurl"])] = new_bytes
        self.reupload(int(local_mtime) + 200000)
        code, out, _ = self.run_main()
        self.assertIn("[upd]", out)
        self.assertEqual(self.path.read_bytes(), new_bytes)

    def test_file_without_server_metadata_falls_back_to_now(self):
        # Direct call: remote_mtime None but a size mismatch forces the compare.
        self.run_main()
        old = 1_000_000_000
        os.utime(self.path, (old, old))
        with contextlib.redirect_stdout(io.StringIO()):
            state = cs.download_file(
                BASE_URL, TOKEN, self.mod["contents"][0]["fileurl"], "lecture.bin",
                self.path.parent, False, set(), self.out, cs.load_hash_index(self.out),
                remote_size=1, remote_mtime=None)
        self.assertEqual(state[0], "skipped")
        self.assertGreater(self.path.stat().st_mtime, old)


if __name__ == "__main__":
    unittest.main()
