"""F1: the Moodle token must never reach stdout, stderr or a generated file, and
must only ever be sent to the configured Moodle host."""
import contextlib
import io
import sys
import unittest
from unittest import mock

import requests

from support import BASE_URL, TOKEN, FakeMoodle, SyncCase, cs, pf_url


class RedactTests(unittest.TestCase):
    def setUp(self):
        cs._SECRETS.clear()
        self.addCleanup(cs._SECRETS.clear)

    def test_strips_token_query_values(self):
        text = "boom https://m.example/x?a=1&token=SECRETVALUE&b=2 and wstoken=OTHERSECRET."
        out = cs.redact(text)
        self.assertNotIn("SECRETVALUE", out)
        self.assertNotIn("OTHERSECRET", out)
        self.assertIn("token=***&b=2", out)

    def test_strips_registered_value_without_a_parameter_name(self):
        cs.register_secret(TOKEN)
        self.assertEqual(cs.redact(f"bad token {TOKEN} here"), "bad token *** here")

    def test_strips_url_quoted_form(self):
        cs.register_secret("tok/en+value=abc")
        self.assertNotIn("tok%2Fen%2Bvalue%3Dabc", cs.redact("x tok%2Fen%2Bvalue%3Dabc y"))

    def test_leaves_ordinary_text_alone_and_accepts_exceptions(self):
        self.assertEqual(cs.redact("nothing to hide"), "nothing to hide")
        self.assertEqual(cs.redact(ValueError("plain")), "plain")

    def test_tiny_secrets_are_not_registered(self):
        cs.register_secret("a")
        self.assertEqual(cs.redact("a banana"), "a banana")

    def test_describe_error_names_the_class(self):
        cs.register_secret(TOKEN)
        e = requests.HTTPError(f"404 for url: https://m.example/f?token={TOKEN}")
        msg = cs.describe_error(e)
        self.assertTrue(msg.startswith("HTTPError: 404"))
        self.assertNotIn(TOKEN, msg)


class ApiTests(SyncCase):
    def test_api_error_text_has_no_token_and_chain_is_cut(self):
        self.server.api_status[("core_webservice_get_site_info", None)] = 500
        with self.assertRaises(RuntimeError) as ctx:
            cs.api(BASE_URL, TOKEN, "core_webservice_get_site_info")
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_api_passes_a_timeout(self):
        seen = {}
        orig = self.server.get

        def spy(url, params=None, timeout=None, **kw):
            seen["timeout"] = timeout
            return orig(url, params=params, timeout=timeout, **kw)
        with mock.patch("requests.get", spy):
            cs.api(BASE_URL, TOKEN, "core_webservice_get_site_info")
        self.assertEqual(seen["timeout"], cs.API_TIMEOUT)
        self.assertGreater(cs.API_TIMEOUT, 0)

    def test_moodle_error_payload_is_redacted(self):
        resp = mock.Mock()
        resp.json.return_value = {"exception": "x", "message": f"denied for wstoken={TOKEN}"}
        with mock.patch("requests.get", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                cs.api(BASE_URL, TOKEN, "fn")
        self.assertNotIn(TOKEN, str(ctx.exception))


class NoLeakEndToEnd(SyncCase):
    def build_failing_course(self):
        srv = self.server
        page = {"id": 1, "name": "Spec page", "modname": "page", "contents": [
            {"type": "file", "filename": "index.html", "fileurl": pf_url("index.html", 1)}]}
        book = {"id": 2, "name": "Solutions", "modname": "book", "contents": [
            {"type": "file", "filename": "index.html", "filepath": "/1/",
             "fileurl": pf_url("book1/index.html", 2)}]}
        label = {"id": 3, "name": "Links", "modname": "label",
                 "description": "<p>Read the notes carefully before the first class begins.</p>"
                                f'<a href="https://elsewhere.example.test/p?token={TOKEN}">notes</a>'}
        srv.add_course(101, [srv.section("Week 1", [page, book, label])])

    def test_failed_page_and_book_fetches_leave_no_token(self):
        self.build_failing_course()
        code, out, err = self.run_main()
        self.assertEqual(code, 1)  # failed fetches make the run exit 1
        self.assert_no_token_anywhere(out, err)
        index = (self.out / "UNIT" / "Week1" / "_index.md").read_text(encoding="utf-8")
        self.assertIn("page fetch failed", index)
        self.assertIn("token=***", index)
        self.assertIn("chapter fetch failed", (self.out / "UNIT" / "Week1" / "Solutions.md").read_text())

    def test_token_in_scraped_links_is_stripped_from_links_and_content(self):
        self.build_failing_course()
        self.run_main()
        links = (self.out / "UNIT" / "Week1" / "_links.md").read_text(encoding="utf-8")
        self.assertIn("https://elsewhere.example.test/p?token=***", links)

    def test_a_foreign_token_parameter_in_a_link_is_left_alone(self):
        label = {"id": 3, "name": "Links", "modname": "label",
                 "description": "<p>Read the notes carefully before the first class begins.</p>"
                                '<a href="https://portal.example.test/p?token=SOMEONE-ELSES-123">portal</a>'}
        self.server.add_course(101, [self.server.section("Week 1", [label])])
        self.run_main()
        links = (self.out / "UNIT" / "Week1" / "_links.md").read_text(encoding="utf-8")
        self.assertIn("https://portal.example.test/p?token=SOMEONE-ELSES-123", links)

    def test_uncaught_failure_is_one_redacted_line_exit_1(self):
        self.server.add_course(101, [])
        self.server.api_status[("core_course_get_contents", 101)] = 503
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", out + err)
        self.assert_no_token_anywhere(out, err)

    def test_exception_escaping_the_sync_is_redacted(self):
        self.server.add_course(101, [])
        boom = RuntimeError(f"explode {BASE_URL}/x?token={TOKEN}")
        with mock.patch.object(cs, "sync_course", side_effect=boom):
            code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", out + err)
        self.assert_no_token_anywhere(out, err)

    def test_authentication_failure_message_has_no_token(self):
        self.server.add_course(101, [])
        self.server.api_status[("core_webservice_get_site_info", None)] = 401
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("Could not authenticate", out)
        self.assert_no_token_anywhere(out, err)

    def test_debug_traceback_is_redacted_too(self):
        self.server.add_course(101, [])
        boom = RuntimeError(f"explode token={TOKEN}")
        with mock.patch.object(cs, "sync_course", side_effect=boom), \
                mock.patch.dict("os.environ", {"COURSE_SYNC_DEBUG": "1"}):
            code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assert_no_token_anywhere(out, err)

    def test_setup_wizard_connection_error_has_no_token(self):
        self.server.api_status[("core_webservice_get_site_info", None)] = 500
        answers = iter([BASE_URL, TOKEN, "n"])
        out = io.StringIO()
        with mock.patch.object(cs, "REPO_DIR", self.tmp), \
                mock.patch("builtins.input", lambda prompt="": next(answers)), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                cs.run_setup()
        self.assertIn("Connection failed", out.getvalue())
        self.assertNotIn(TOKEN, out.getvalue())


class HostGateTests(SyncCase):
    """F1(b): the token only ever goes to the configured Moodle site."""

    def test_make_download_url_accepts_the_moodle_site(self):
        for url in (f"{BASE_URL}/pluginfile.php/5/mod_resource/content/1/a.pdf",
                    f"{BASE_URL}/webservice/pluginfile.php/5/mod_resource/content/1/a.pdf",
                    f"{BASE_URL.upper().replace('HTTPS', 'https')}/pluginfile.php/5/x/1/a.pdf"):
            out = cs.make_download_url(url, TOKEN, BASE_URL)
            self.assertIn("/webservice/pluginfile.php/", out)
            self.assertNotIn("/webservice/webservice/", out)
            self.assertTrue(out.endswith("?token=" + TOKEN))

    def test_make_download_url_keeps_an_existing_query(self):
        url = f"{BASE_URL}/pluginfile.php/5/x/1/a.pdf?forcedownload=1"
        self.assertTrue(cs.make_download_url(url, TOKEN, BASE_URL).endswith("&token=" + TOKEN))

    def test_make_download_url_refuses_other_origins(self):
        for url in ("https://other.example.test/pluginfile.php/5/x/1/a.pdf",
                    f"{BASE_URL}.evil.example.test/pluginfile.php/5/x/1/a.pdf",
                    "https://evil.example.test/?u=moodle.example.test/pluginfile.php/",
                    "http://moodle.example.test/pluginfile.php/5/x/1/a.pdf",
                    "https://moodle.example.test:8443/pluginfile.php/5/x/1/a.pdf",
                    "/pluginfile.php/5/x/1/a.pdf",
                    f"{BASE_URL}/course/view.php?id=2"):
            with self.assertRaises(ValueError, msg=url) as ctx:
                cs.make_download_url(url, TOKEN, BASE_URL)
            self.assertNotIn(TOKEN, str(ctx.exception))

    def build_links(self):
        foreign = "https://other-moodle.example.test/pluginfile.php/9/mod_label/intro/1/Foreign.pdf"
        self.server.add_file(foreign, b"foreign bytes")
        own = self.server.add_file(pf_url("Own.pdf", 2, area="mod_label/intro"), b"own bytes")
        label = {"id": 1, "name": "Mixed links", "modname": "label",
                 "description": "<p>Resources for this week, read before class starts.</p>"
                                f'<a href="{foreign}">Foreign</a> <a href="{own}">Own</a>'}
        url_mod = {"id": 2, "name": "Other uni file", "modname": "url", "contents": [
            {"type": "url", "fileurl": foreign.replace("Foreign", "Foreign2")}]}
        self.server.add_course(101, [self.server.section("Week 1", [label, url_mod])])
        return foreign

    def test_foreign_pluginfile_link_is_an_external_link_never_fetched(self):
        foreign = self.build_links()
        code, out, err = self.run_main()
        self.assertEqual(code, 0)
        self.assertNotIn("other-moodle.example.test", self.server.hosts_contacted())
        self.assertEqual((self.out / "UNIT/Week1/Own.pdf").read_bytes(), b"own bytes")
        self.assertFalse((self.out / "UNIT/Week1/Foreign.pdf").exists())
        links = (self.out / "UNIT/Week1/_links.md").read_text(encoding="utf-8")
        self.assertIn(foreign, links)
        self.assertIn("Foreign2.pdf", links)
        self.assert_no_token_anywhere(out, err)

    def test_no_request_to_another_host_ever_carries_the_token(self):
        self.build_links()
        self.run_main()
        base_host = "moodle.example.test"
        for url, params, _ in self.server.calls:
            if base_host not in url:
                self.assertNotIn(TOKEN, url)


class RedirectTests(SyncCase):
    """F1(c): a tokenised fetch must not follow a redirect off the Moodle site."""

    def setUp(self):
        super().setUp()
        self.url = pf_url("moved.pdf", 7)

    def test_cross_host_redirect_is_refused_and_never_requested(self):
        self.server.redirects[FakeMoodle.key(self.url)] = "https://evil.example.test/steal.pdf"
        self.server.files["evil.example.test/steal.pdf"] = b"evil"
        with self.assertRaises(RuntimeError) as ctx:
            cs.fetch_moodle_file(BASE_URL, TOKEN, self.url, timeout=5)
        self.assertIn("evil.example.test", str(ctx.exception))
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn("evil.example.test", self.server.hosts_contacted())

    def test_every_hop_is_requested_without_auto_redirects(self):
        target = pf_url("real.pdf", 8)
        self.server.add_file(target, b"real bytes")
        self.server.redirects[FakeMoodle.key(self.url)] = target
        r = cs.fetch_moodle_file(BASE_URL, TOKEN, self.url, timeout=5)
        self.assertEqual(r.content, b"real bytes")
        self.assertEqual(len(self.server.calls), 2)
        for call_url, _params, allow in self.server.calls:
            self.assertFalse(allow)
            self.assertIn("token=" + TOKEN, call_url)

    def test_relative_same_site_redirect_is_followed(self):
        self.server.add_file(pf_url("real.pdf", 8), b"real bytes")
        self.server.redirects[FakeMoodle.key(self.url)] = "/pluginfile.php/55/mod_resource/content/8/real.pdf"
        r = cs.fetch_moodle_file(BASE_URL, TOKEN, self.url, timeout=5)
        self.assertEqual(r.content, b"real bytes")

    def test_https_to_http_downgrade_on_the_same_host_is_refused(self):
        self.server.redirects[FakeMoodle.key(self.url)] = (
            "http://moodle.example.test/pluginfile.php/55/mod_resource/content/8/real.pdf")
        with self.assertRaises(RuntimeError):
            cs.fetch_moodle_file(BASE_URL, TOKEN, self.url, timeout=5)
        self.assertEqual(len(self.server.calls), 1)

    def test_redirect_to_a_non_file_page_on_the_site_is_refused(self):
        self.server.redirects[FakeMoodle.key(self.url)] = f"{BASE_URL}/login/index.php"
        with self.assertRaises(RuntimeError):
            cs.fetch_moodle_file(BASE_URL, TOKEN, self.url, timeout=5)

    def test_redirect_loop_stops_at_the_hop_limit(self):
        self.server.redirects[FakeMoodle.key(self.url)] = self.url
        with self.assertRaises(RuntimeError) as ctx:
            cs.fetch_moodle_file(BASE_URL, TOKEN, self.url, timeout=5)
        self.assertIn("redirects", str(ctx.exception))
        self.assertEqual(len(self.server.calls), cs.MAX_REDIRECTS + 1)

    def test_full_run_never_contacts_the_redirect_target(self):
        self.server.add_course(101, [self.server.section("Week 1", [
            {"id": 1, "name": "Slides", "modname": "resource", "contents": [
                {"type": "file", "filename": "moved.pdf", "fileurl": self.url}]}])])
        self.server.redirects[FakeMoodle.key(self.url)] = "https://evil.example.test/steal.pdf"
        self.server.files["evil.example.test/steal.pdf"] = b"evil"
        code, out, err = self.run_main()
        self.assertNotIn("evil.example.test", self.server.hosts_contacted())
        self.assertFalse((self.out / "UNIT/Week1/moved.pdf").exists())
        self.assert_no_token_anywhere(out, err)


if __name__ == "__main__":
    unittest.main()
