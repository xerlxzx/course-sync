"""
Shared helpers for the unittest suite: a fake Moodle server (no network) and a
sandboxed run of course_sync.main().

Everything here is synthetic. The fake replaces ``requests.get`` while a test
runs, so any call that escapes the helpers fails loudly instead of touching the
network.
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import requests

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

import course_sync as cs  # noqa: E402

BASE_URL = "https://moodle.example.test"
TOKEN = "FAKE_TOKEN_abc123"
COURSE_CODE = "UNIT1000"
COURSE_SHORTNAME = "UNIT1000_T1_2026"
COURSE2_CODE = "OTHR2000"
COURSE2_SHORTNAME = "OTHR2000_T1_2026"


def pf_url(filename, itemid=1, host=BASE_URL, area="mod_resource/content"):
    """A pluginfile URL as Moodle returns it in module contents."""
    return f"{host}/pluginfile.php/55/{area}/{itemid}/{filename}"


class FakeResponse(object):
    def __init__(self, url, status=200, content=b"", headers=None, json_data=None):
        self.url = url
        self.status_code = status
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.headers = headers or {}
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            # Same shape as requests: the message quotes the full URL.
            raise requests.HTTPError(
                f"{self.status_code} Client Error: Error for url: {self.url}")

    def json(self):
        return self._json


class FakeMoodle(object):
    """
    In-memory stand-in for a Moodle site plus whatever else a test links to.

    Bytes served for a file URL are registered with add_file(); the server
    demands the token on Moodle file URLs like the real one does. Every call is
    appended to ``calls`` as (url, params) so tests can assert on what was fetched.
    """

    def __init__(self):
        self.courses = []        # [{"id", "shortname", "fullname", "sections"}]
        self.files = {}          # "host/path" -> bytes
        self.status = {}         # "host/path" -> HTTP status override
        self.errors = {}         # "host/path" -> exception instance to raise
        self.redirects = {}      # "host/path" -> Location header
        self.api_status = {}     # (wsfunction, course id or None) -> HTTP status
        self.hooks = []          # [(predicate(url), fn(url, params) -> response)]
        self.calls = []

    # -- registration -----------------------------------------------------

    @staticmethod
    def key(url):
        p = urlparse(url)
        path = p.path.replace("/webservice/pluginfile.php/", "/pluginfile.php/")
        return f"{p.hostname}{path}"

    def add_file(self, url, data):
        self.files[self.key(url)] = data
        return url

    def add_course(self, course_id, sections, shortname=COURSE_SHORTNAME):
        self.courses.append({"id": course_id, "shortname": shortname,
                             "fullname": f"Course {course_id}", "sections": sections})

    def resource(self, cmid, name, files, modname="resource"):
        """
        A resource/folder module. ``files`` is a list of (filename, bytes) or
        (filename, bytes, itemid); a distinct itemid gives a distinct source URL.
        """
        contents = []
        for i, spec in enumerate(files):
            filename, data = spec[0], spec[1]
            itemid = spec[2] if len(spec) > 2 else cmid * 10 + i
            url = self.add_file(pf_url(filename, itemid), data)
            contents.append({"type": "file", "filename": filename, "fileurl": url,
                             "filesize": len(data), "timemodified": 1000})
        return {"id": cmid, "name": name, "modname": modname, "contents": contents}

    @staticmethod
    def section(name, modules, summary=""):
        return {"name": name, "summary": summary, "modules": modules}

    # -- the requests.get replacement ---------------------------------------

    def get(self, url, params=None, timeout=None, allow_redirects=True, **kw):
        full = url
        if params:
            full = requests.Request("GET", url, params=params).prepare().url
        self.calls.append((full, dict(params or {}), allow_redirects))
        for predicate, fn in self.hooks:
            if predicate(url):
                return fn(url, params)
        p = urlparse(url)
        if p.hostname == urlparse(BASE_URL).hostname and p.path == "/webservice/rest/server.php":
            return self._api(full, params or {})
        key = self.key(url)
        if key in self.errors:
            raise self.errors[key]
        if key in self.redirects:
            return FakeResponse(full, 302, headers={"Location": self.redirects[key]})
        if key in self.status:
            return FakeResponse(full, self.status[key])
        if key in self.files:
            if p.hostname == urlparse(BASE_URL).hostname:
                # The real site rejects file requests that carry no token.
                if parse_qs(p.query).get("token") != [TOKEN]:
                    return FakeResponse(full, 403)
            return FakeResponse(full, 200, self.files[key],
                                headers={"Content-Type": "application/octet-stream"})
        return FakeResponse(full, 404)

    def _api(self, full, params):
        fn = params.get("wsfunction")
        course_id = params.get("courseid")
        status = self.api_status.get((fn, course_id))
        if status:
            return FakeResponse(full, status)
        if params.get("wstoken") != TOKEN:
            return FakeResponse(full, 200, json_data={
                "exception": "moodle_exception", "message": "Invalid token"})
        if fn == "core_webservice_get_site_info":
            return FakeResponse(full, 200, json_data={
                "userid": 7, "fullname": "Test User", "sitename": "Test Moodle",
                "release": "4.4", "version": "2024042200"})
        if fn == "core_enrol_get_users_courses":
            return FakeResponse(full, 200, json_data=[
                {"id": c["id"], "shortname": c["shortname"], "fullname": c["fullname"]}
                for c in self.courses])
        if fn == "core_course_get_contents":
            for c in self.courses:
                if c["id"] == course_id:
                    return FakeResponse(full, 200, json_data=c["sections"])
        raise AssertionError(f"unexpected API call {fn}")

    # -- assertions helpers ---------------------------------------------------

    def file_gets(self, name_fragment=""):
        """Calls that fetched a pluginfile URL whose text contains name_fragment."""
        return [c for c in self.calls
                if "pluginfile.php" in c[0] and name_fragment in c[0]]

    def hosts_contacted(self):
        return {urlparse(c[0]).hostname for c in self.calls}


class SyncCase(unittest.TestCase):
    """A scratch output dir, a config for the fake site and a fake server."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="course-sync-test-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.config = self.tmp / "config.yaml"
        cs._SECRETS.clear()
        self.addCleanup(cs._SECRETS.clear)
        self.server = FakeMoodle()
        patcher = mock.patch("requests.get", self.server.get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_config(self, extra="", two_courses=False):
        courses = f"  - code: {COURSE_CODE}\n    folder: UNIT\n"
        if two_courses:
            courses += f"  - code: {COURSE2_CODE}\n    folder: OTHER\n"
        self.config.write_text(
            "moodle:\n"
            f"  base_url: {BASE_URL}\n"
            f"output_dir: {self.out}\n"
            "courses:\n" + courses + extra,
            encoding="utf-8")

    def run_main(self, *flags):
        """Run course_sync.main(); returns (exit_code, stdout, stderr)."""
        if not self.config.exists():
            self.write_config()
        out, err = io.StringIO(), io.StringIO()
        code = 0
        argv = ["course_sync.py", "--config", str(self.config), "--token", TOKEN, *flags]
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cs.main()
            except SystemExit as e:
                code = e.code if e.code is not None else 0
        return code, out.getvalue(), err.getvalue()

    # -- filesystem helpers -----------------------------------------------------

    def tree(self):
        """{relative path: bytes} for every file under the output dir, index included."""
        return {str(p.relative_to(self.out)): p.read_bytes()
                for p in sorted(self.out.rglob("*")) if p.is_file()}

    def index(self):
        path = self.out / cs.INDEX_DIRNAME / cs.INDEX_FILENAME
        return json.loads(path.read_text(encoding="utf-8"))

    def assert_no_token_anywhere(self, *texts):
        for rel, data in self.tree().items():
            self.assertNotIn(TOKEN.encode(), data, f"token leaked into {rel}")
        for text in texts:
            self.assertNotIn(TOKEN, text)
