"""F7: sync_all.sh --dry-run must not run a live sync; the no-argument behaviour is unchanged.

The script is run in a scratch tree with stub interpreters in place of the two
virtualenvs, so nothing real is executed and the arguments each tool receives
can be asserted.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ZSH = shutil.which("zsh")

# scripts/sync_all.sh as it was before --dry-run existed, kept
# so the default path can be compared with it byte for byte.
ORIGINAL_SCRIPT = r'''#!/bin/zsh
# Full sync: Moodle files via course-sync, then Sway decks via sway_sync.
# Order matters - sway_sync discovers deck URLs from the _links.md files
# that course-sync writes.
# Runs from the repo root so config.yaml, the token and both venvs resolve
# the same way however this script was started.
set -uo pipefail
cd "$(dirname "$0")/.."

echo "=== 1/2 course-sync (Moodle files) ==="
.venv/bin/python src/course_sync.py
cs=$?

echo ""
echo "=== 2/2 sway_sync (Sway lecture decks) ==="
.venv-sway/bin/python src/sway_sync.py
sw=$?

echo ""
if [ $cs -ne 0 ]; then echo "WARNING: course-sync exited $cs"; fi
if [ $sw -eq 2 ]; then
  echo "WARNING: Sway session expired - run: .venv-sway/bin/python src/sway_sync.py --login"
elif [ $sw -ne 0 ]; then
  echo "WARNING: sway_sync exited $sw"
fi
exit $(( cs != 0 || sw != 0 ))'''

# A stand-in for a virtualenv's python: log "<name> <args>" and exit with the
# status the test chose for that tool.
STUB = """#!/bin/sh
echo "NAME $*" >> "$STUB_LOG"
exit "$STUB_EXIT_NAME"
"""


@unittest.skipUnless(ZSH, "zsh is not installed")
class SyncAllScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="course-sync-sh-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.log = self.tmp / "calls.log"
        for variant in ("new", "old"):
            root = self.tmp / variant
            (root / "scripts").mkdir(parents=True)
            (root / "src").mkdir()
            for venv, name in ((".venv", "cs"), (".venv-sway", "sw")):
                py = root / venv / "bin" / "python"
                py.parent.mkdir(parents=True)
                py.write_text(STUB.replace("NAME", name))
                py.chmod(py.stat().st_mode | stat.S_IXUSR)
        shutil.copy(str(REPO / "scripts" / "sync_all.sh"), str(self.tmp / "new" / "scripts"))
        shutil.copy(str(REPO / "sync_all.sh"), str(self.tmp / "new"))
        (self.tmp / "old" / "scripts" / "sync_all.sh").write_text(ORIGINAL_SCRIPT)

    def run_script(self, variant, args=(), cs=0, sw=0, via_shim=False, cwd=None):
        if self.log.exists():
            self.log.unlink()
        script = self.tmp / variant / ("sync_all.sh" if via_shim else "scripts/sync_all.sh")
        env = dict(os.environ, STUB_LOG=str(self.log), STUB_EXIT_cs=str(cs), STUB_EXIT_sw=str(sw))
        p = subprocess.run([ZSH, str(script), *args], capture_output=True, text=True,
                           env=env, cwd=str(cwd or self.tmp))
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return p.returncode, p.stdout, p.stderr, calls

    def test_script_is_valid_zsh(self):
        for path in (REPO / "scripts" / "sync_all.sh", REPO / "sync_all.sh"):
            self.assertEqual(subprocess.run([ZSH, "-n", str(path)]).returncode, 0, path)

    def test_no_argument_run_is_byte_identical_to_the_original_script(self):
        for cs, sw in ((0, 0), (1, 0), (0, 1), (0, 2), (1, 2), (0, 3), (5, 3)):
            new = self.run_script("new", cs=cs, sw=sw)
            old = self.run_script("old", cs=cs, sw=sw)
            self.assertEqual(new, old, f"cs={cs} sw={sw}")

    def test_no_argument_run_is_a_live_sync(self):
        code, out, _, calls = self.run_script("new")
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["cs src/course_sync.py", "sw src/sway_sync.py"])

    def test_dry_run_passes_dry_run_and_check(self):
        code, out, _, calls = self.run_script("new", ["--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["cs src/course_sync.py --dry-run", "sw src/sway_sync.py --check"])
        self.assertIn("dry run", out)

    def test_dry_run_treats_sway_exit_3_as_information(self):
        code, out, _, _ = self.run_script("new", ["--dry-run"], sw=3)
        self.assertEqual(code, 0)
        self.assertIn("NOTE: sway_sync found changes", out)
        self.assertNotIn("WARNING", out)

    def test_dry_run_still_fails_on_real_errors(self):
        self.assertEqual(self.run_script("new", ["--dry-run"], cs=1)[0], 1)
        self.assertEqual(self.run_script("new", ["--dry-run"], sw=1)[0], 1)
        code, out, _, _ = self.run_script("new", ["--dry-run"], sw=2)
        self.assertEqual(code, 1)
        self.assertIn("Sway session expired", out)
        code, out, _, _ = self.run_script("new", ["--dry-run"], cs=3, sw=3)
        self.assertEqual(code, 1)
        self.assertIn("WARNING: course-sync exited 3", out)

    def test_unknown_arguments_are_rejected_before_anything_runs(self):
        for args in (["--bogus"], ["--dry-run", "extra"], ["dry-run"], ["--dryrun"]):
            code, out, err, calls = self.run_script("new", args)
            self.assertEqual(code, 2, args)
            self.assertIn("usage: sync_all.sh [--dry-run]", err)
            self.assertIn("unknown argument", err)
            self.assertEqual(calls, [], args)
            self.assertEqual(out, "", args)

    def test_help_prints_usage_and_runs_nothing(self):
        code, out, _, calls = self.run_script("new", ["--help"])
        self.assertEqual((code, calls), (0, []))
        self.assertIn("--dry-run", out)

    def test_root_shim_forwards_arguments(self):
        code, _, _, calls = self.run_script("new", ["--dry-run"], via_shim=True)
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["cs src/course_sync.py --dry-run", "sw src/sway_sync.py --check"])
        self.assertEqual(self.run_script("new", ["--nope"], via_shim=True)[0], 2)

    def test_works_from_any_working_directory(self):
        other = self.tmp / "elsewhere"
        other.mkdir()
        code, _, _, calls = self.run_script("new", ["--dry-run"], cwd=other)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
