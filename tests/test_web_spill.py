"""web_fetch, web_search, spilled output and read_file paging. The web tests use a local
HTTP server, so they prove the parsing, limits and refusals, not that real sites work."""

from __future__ import annotations

import http.server
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from chelus import agent as agent_mod
from chelus import compaction, spill, tools, web
from chelus.store import Store
from tests.test_agent import FakeModel, make_settings, tool_call


class SpillTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.s = make_settings(self.tmp, cache_dir=self.tmp / "cache")

    def tearDown(self):
        self._tmp.cleanup()

    def test_short_text_is_unchanged(self):
        self.assertEqual(spill.fit(self.s, "x", "hello", 100), "hello")
        self.assertFalse((self.tmp / "cache").exists())

    def test_long_text_shows_head_and_tail_and_saves_all(self):
        text = "".join(f"line {i}\n" for i in range(1, 2001))
        out = spill.fit(self.s, "run_shell", text, 1000)
        self.assertTrue(out.startswith("line 1\n"))
        self.assertIn("line 2000", out)
        self.assertNotIn("line 1000\n", out)
        self.assertLess(len(out), 1400)
        files = list((self.tmp / "cache" / "spill").iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_text(encoding="utf-8"), text)
        # the footer's offset is the first line the model didn't see
        offset = int(out.split("offset=")[1].split("]")[0])
        head_lines = out.split("\n... [")[0].splitlines()
        self.assertEqual(head_lines[-1], f"line {offset - 1}")
        if os.name == "posix":
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual((self.tmp / "cache" / "spill").stat().st_mode & 0o777, 0o700)

    def test_saved_shell_output_is_redacted_line_for_line(self):
        key = "-----BEGIN RSA PRIVATE KEY-----\nabc\ndef\n-----END RSA PRIVATE KEY-----"
        text = key + "\n" + "after\n" * 2000
        spill.fit(self.s, "run_shell", text, 500)
        saved = next((self.tmp / "cache" / "spill").iterdir()).read_text(encoding="utf-8")
        self.assertNotIn("abc", saved)
        self.assertEqual(saved.count("\n"), text.count("\n"))
        self.assertEqual(compaction.redact("token=abcdefghij"), "token=[REDACTED]")

    def test_old_files_are_pruned(self):
        folder = self.tmp / "cache" / "spill"
        folder.mkdir(parents=True)
        old = folder / "old.txt"
        old.write_text("x")
        os.utime(old, (time.time() - 2 * 86400,) * 2)
        spill.fit(self.s, "x", "y" * 50, 10)
        self.assertFalse(old.exists())

    def test_unwritable_cache_falls_back_to_a_cut(self):
        (self.tmp / "cache").write_text("a file where the folder should be")
        out = spill.fit(self.s, "x", "y" * 50, 10)
        self.assertIn("could not be saved", out)


class ReadFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.s = make_settings(self.tmp, cache_dir=self.tmp / "cache", tool_output_chars=1300)
        self.big = self.tmp / "big.txt"
        self.big.write_bytes("".join(f"row {i:04d}\n" for i in range(1, 1001)).encode())

    def tearDown(self):
        self._tmp.cleanup()

    def read(self, **args):
        return tools.TOOLS["read_file"].handler(self.s, {"path": str(self.big), **args}, None)

    def test_small_file_comes_back_as_is(self):
        (self.tmp / "a.txt").write_bytes(b"one\ntwo\n")
        self.assertEqual(tools.TOOLS["read_file"].handler(self.s, {"path": "a.txt"}, None), "one\ntwo\n")

    def test_long_file_pages_to_the_end(self):
        seen, offset = [], 1
        for _ in range(50):
            out = self.read(offset=offset)
            self.assertLessEqual(len(out), self.s.tool_output_chars)
            body, _, footer = out.rpartition("\n[")
            seen += body.splitlines()
            if "Read on" not in footer:
                break
            self.assertIn("of 1000", footer)
            offset = int(footer.split("offset=")[1].rstrip("]"))
        self.assertEqual(seen, [f"row {i:04d}" for i in range(1, 1001)])

    def test_limit_and_bad_offsets(self):
        out = self.read(offset=10, limit=3)
        self.assertTrue(out.startswith("row 0010\nrow 0011\nrow 0012\n["))
        self.assertIn("offset=13", out)
        with self.assertRaises(tools.ToolError):
            self.read(offset=5000)
        with self.assertRaises(tools.ToolError):
            self.read(limit=0)
        with self.assertRaises(tools.ToolError):
            self.read(offset="x")

    def test_overlong_line_is_cut_and_said(self):
        (self.tmp / "one.txt").write_text("z" * 5000 + "\nnext\n", encoding="utf-8")
        out = tools.TOOLS["read_file"].handler(self.s, {"path": "one.txt"}, None)
        self.assertIn("was cut", out)
        self.assertIn("offset=2", out)

    def test_binary_is_refused(self):
        (self.tmp / "b.bin").write_bytes(b"\x00\x01\x02")
        with self.assertRaises(tools.ToolError):
            tools.TOOLS["read_file"].handler(self.s, {"path": "b.bin"}, None)

    def test_off_limits_still_refused(self):
        self.s.data_dir.mkdir(parents=True)
        (self.s.data_dir / "secret.txt").write_text("x")
        with self.assertRaises(tools.ToolError):
            tools.TOOLS["read_file"].handler(self.s, {"path": str(self.s.data_dir / "secret.txt")}, None)


class AgentSpillTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.s = make_settings(self.tmp, cache_dir=self.tmp / "cache", tool_output_chars=200)
        self.store = Store(self.s.db_path)
        self.sid = self.store.new_session()
        self.long = "".join(f"item {i}\n" for i in range(500))
        tools.register(tools.Tool("test_long", "test", tools.obj({}, []), lambda s, a, c=None: self.long))

    def tearDown(self):
        tools.TOOLS.pop("test_long", None)
        self.store.close()
        self._tmp.cleanup()

    def run_tool(self, guest=False):
        model = FakeModel([
            {"role": "assistant", "content": None, "tool_calls": [tool_call("test_long", {})]},
            {"role": "assistant", "content": "done"},
        ])
        agent = agent_mod.Agent(self.s, "k", self.store, self.sid, lambda r: True, chat_fn=model, guest=guest)
        agent.tool_names.append("test_long")
        agent.run_turn("go")
        return model.calls[1][-1]["content"]

    def test_owner_output_is_spilled(self):
        content = self.run_tool()
        self.assertIn("read_file path=", content)
        self.assertIn("item 499", content)
        self.assertEqual(len(list((self.tmp / "cache" / "spill").iterdir())), 1)

    def test_guest_output_is_only_cut(self):
        content = self.run_tool(guest=True)
        self.assertIn("cut off:", content)
        self.assertNotIn("read_file", content)
        self.assertFalse((self.tmp / "cache").exists())

    def test_web_tools_are_owner_only_and_can_be_switched_off(self):
        self.assertNotIn("web_fetch", tools.available(guest=True))
        self.assertIn("web_fetch", tools.available(guest=False))
        self.s.web = False
        self.assertIn("web_fetch", agent_mod.disabled_tools(self.s))
        with self.assertRaises(tools.ToolError):
            web.fetch(self.s, {"url": "https://example.com"}, type("G", (), {"guest": True})())


class UrlSafetyTests(unittest.TestCase):
    def test_blocked_addresses(self):
        for addr in ["127.0.0.1", "10.1.2.3", "192.168.1.1", "172.16.0.1", "169.254.169.254", "100.64.0.1",
                     "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fd00::1", "::ffff:127.0.0.1",
                     "::ffff:192.168.0.1", "64:ff9b::a00:1", "2002:c0a8:101::1", "not an ip"]:
            self.assertTrue(web.blocked_ip(addr), addr)
        for addr in ["93.184.215.14", "1.1.1.1", "2606:4700:4700::1111", "64:ff9b::101:101"]:
            self.assertFalse(web.blocked_ip(addr), addr)

    def test_url_checks(self):
        self.assertEqual(web.check_url("https://Example.com./a?b=1"), ("https", "example.com", 443, "/a?b=1"))
        for bad in ["ftp://example.com", "file:///etc/passwd", "https://user:pw@example.com/",
                    "http://localhost:8080/", "http://printer.local/", "https://example.com/?token=abc",
                    "https://example.com/?API_KEY=abc", "https://x.com/" + "a" * 3000, "https:///nohost", "",
                    "http://[::1", None]:
            with self.assertRaises(web.WebError, msg=bad):
                web.check_url(bad)
        web.check_url("https://example.com/?token=")  # empty value carries nothing

    def test_name_resolving_to_private_address_is_refused(self):
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]
        with patch("socket.getaddrinfo", return_value=fake), patch("socket.socket") as sock:
            with self.assertRaises(web.WebError) as cm:
                web._connect("rebind.example", 443, 5)
            self.assertIn("10.0.0.5", str(cm.exception))
            sock.assert_not_called()

    def test_connection_goes_to_the_checked_address(self):
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 80))]
        with patch("socket.getaddrinfo", return_value=fake), patch("socket.socket") as sock:
            web._connect("example.com", 80, 5)
            sock.return_value.connect.assert_called_once_with(("93.184.215.14", 80))


PAGES: dict[str, tuple[int, dict[str, str], bytes]] = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        status, headers, body = PAGES.get(self.path, (404, {"Content-Type": "text/plain"}, b"missing"))
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class FetchTests(unittest.TestCase):
    """Against a local server: the address check is patched to let 127.0.0.1 through."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.s = make_settings(self.tmp, cache_dir=self.tmp / "cache", web_chars=2000)
        PAGES.clear()
        p = patch.object(web, "blocked_ip", lambda a: False)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def fetch(self, path):
        return web.fetch(self.s, {"url": self.base + path}, None)

    def test_html_becomes_text_with_links(self):
        PAGES["/p"] = (200, {"Content-Type": "text/html; charset=utf-8"}, (
            "<html><head><title>T &amp; C</title><style>.x{}</style></head><body><nav>menu</nav>"
            "<h2>Hello</h2><p>Some&nbsp;text <a href='/next'>next page</a></p><ul><li>one</li></ul>"
            "<pre>  indented\n    more</pre><script>evil()</script></body></html>").encode())
        out = self.fetch("/p")
        self.assertIn("Title: T & C", out)
        self.assertIn("## Hello", out)
        self.assertIn(f"[next page]({self.base}/next)", out)
        self.assertIn("- one", out)
        self.assertIn("  indented\n    more", out)
        self.assertIn(web.UNTRUSTED, out)
        for gone in ("menu", "evil", ".x{}"):
            self.assertNotIn(gone, out)

    def test_redirects_are_followed_and_rechecked(self):
        PAGES["/a"] = (302, {"Location": "/b"}, b"")
        PAGES["/b"] = (200, {"Content-Type": "text/plain"}, b"landed")
        out = self.fetch("/a")
        self.assertIn(f"URL: {self.base}/b", out)
        self.assertIn("landed", out)
        PAGES["/evil"] = (302, {"Location": "http://localhost/admin"}, b"")
        with self.assertRaises(web.WebError):
            self.fetch("/evil")
        PAGES["/loop"] = (302, {"Location": "/loop"}, b"")
        with self.assertRaises(web.WebError) as cm:
            self.fetch("/loop")
        self.assertIn("redirects", str(cm.exception))

    def test_long_page_is_spilled(self):
        PAGES["/long"] = (200, {"Content-Type": "text/plain"}, "".join(f"para {i}\n" for i in range(3000)).encode())
        out = self.fetch("/long")
        self.assertIn("para 0", out)
        self.assertIn("para 2999", out)
        self.assertIn("read_file path=", out)
        self.assertLess(len(out), 2600)

    def test_binary_and_gzip(self):
        PAGES["/img"] = (200, {"Content-Type": "image/png"}, b"\x89PNG")
        with self.assertRaises(web.WebError):
            self.fetch("/img")
        import gzip
        PAGES["/gz"] = (200, {"Content-Type": "application/json", "Content-Encoding": "gzip"},
                        gzip.compress(json.dumps({"a": [1, 2]}).encode()))
        self.assertIn('"a": [', self.fetch("/gz"))

    def test_404_is_reported_not_raised(self):
        self.assertIn("Status: 404", self.fetch("/nope"))

    def test_ddg_results_are_parsed(self):
        html = (
            '<div class="result results_links web-result"><h2 class="result__title"><a rel="nofollow" class="result__a" '
            'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwiki.termux.com%2Fwiki%2FPython&amp;rut=abc">Python - <b>Termux</b> Wiki</a></h2>'
            '<a class="result__snippet" href="x">Python is an <b>interpreted</b> language.</a></div>'
            '<div class="result result--ad"><a class="result__a" href="https://ads.example/">Ad</a></div>')
        with patch.object(web, "request", return_value=("u", 200, "text/html", html.encode(), False)):
            out = web.search(self.s, {"query": "termux python"}, None)
        self.assertIn("1. Python - Termux Wiki\n   https://wiki.termux.com/wiki/Python\n   Python is an interpreted language.", out)
        self.assertNotIn("ads.example", out)
        with patch.object(web, "request", return_value=("u", 200, "text/html", b"<p>anomaly detected</p>", False)):
            with self.assertRaises(web.WebError):
                web.search(self.s, {"query": "x"}, None)

    def test_brave_used_when_keyed(self):
        self.s.config_dir.mkdir(parents=True)
        (self.s.config_dir / "brave.key").write_text("k123\n")
        body = json.dumps({"web": {"results": [{"title": "T", "url": "https://t.example/", "description": "<b>d</b>"}]}})
        with patch.object(web, "request", return_value=("u", 200, "application/json", body.encode(), False)) as req:
            out = web.search(self.s, {"query": "q", "limit": 3}, None)
        self.assertIn("(Brave)", out)
        self.assertIn("1. T\n   https://t.example/\n   d", out)
        self.assertEqual(req.call_args.kwargs["headers"]["X-Subscription-Token"], "k123")


if __name__ == "__main__":
    unittest.main()
