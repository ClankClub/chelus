"""edit_file (find and replace with a diff card) and search_files."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chelus import approval, edit, search, tools
from chelus.agent import Agent
from chelus.store import Store
from tests.test_agent import make_settings

OWNER = SimpleNamespace(guest=False)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.s = make_settings(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, name, data: bytes | str) -> Path:
        p = self.tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode() if isinstance(data, str) else data)
        return p

    def edit(self, **args):
        return tools.TOOLS["edit_file"].handler(self.s, args, OWNER)


class EditTests(Base):
    def test_exact_unique_replace_with_diff(self):
        p = self.put("a.py", "def f():\n    return 1\n\ndef g():\n    return 2\n")
        out = self.edit(path="a.py", old_string="    return 1", new_string="    return 10")
        self.assertIn("-    return 1\n+    return 10", out)
        self.assertEqual(p.read_text(), "def f():\n    return 10\n\ndef g():\n    return 2\n")

    def test_ambiguous_needs_replace_all(self):
        p = self.put("a.txt", "x = 1\ny = 2\nx = 1\n")
        with self.assertRaises(tools.ToolError) as cm:
            self.edit(path="a.txt", old_string="x = 1", new_string="x = 3")
        self.assertIn("2 places", str(cm.exception))
        self.assertIn("lines 1, 3", str(cm.exception))
        self.edit(path="a.txt", old_string="x = 1", new_string="x = 3", replace_all=True)
        self.assertEqual(p.read_text(), "x = 3\ny = 2\nx = 3\n")

    def test_crlf_and_bom_are_kept(self):
        p = self.put("w.txt", b"\xef\xbb\xbfone\r\ntwo\r\nthree\r\n")
        self.edit(path="w.txt", old_string="two\nthree", new_string="2\n3")
        self.assertEqual(p.read_bytes(), b"\xef\xbb\xbfone\r\n2\r\n3\r\n")

    def test_already_applied_is_a_no_op(self):
        p = self.put("a.txt", "colour = blue\n")
        out = self.edit(path="a.txt", old_string="color = blue", new_string="colour = blue")
        self.assertIn("already applied", out)
        self.assertEqual(p.read_text(), "colour = blue\n")

    def test_normalizing_strategies(self):
        cases = [
            ("trailing", "a = 1   \nb = 2\n", "a = 1\nb = 2", "a = 9\nb = 2", "a = 9\nb = 2\n"),
            ("runs", "if  x ==   1:\n    go()\n", "if x == 1:", "if x == 2:", "if x == 2:\n    go()\n"),
            ("indent", "class A:\n    def f(self):\n        return 1\n",
             "def f(self):\n    return 1", "def f(self):\n    return 2",
             "class A:\n    def f(self):\n        return 2\n"),
            ("escapes", "line one\nline two\n", "line one\\nline two", "line 1\\nline 2", "line 1\nline 2\n"),
            ("ends", "a\nb\nc\n", "\n\nb\n\n", "B", "a\nB\nc\n"),
            ("quotes", "say \u201chi\u201d \u2014 now\n", 'say "hi" - now', 'say "bye" - now',
             'say "bye" - now\n'),
        ]
        for label, before, old, new, after in cases:
            with self.subTest(label):
                p = self.put(f"{label}.txt", before)
                out = self.edit(path=p.name, old_string=old, new_string=new)
                self.assertEqual(p.read_text(encoding="utf-8"), after)
                self.assertIn("Matched with", out)

    def test_miss_names_closest_lines_and_hints_after_three(self):
        self.put("a.py", "def compute_total(items):\n    return sum(items)\n")
        ctx = SimpleNamespace(guest=False)
        for i in range(3):
            with self.assertRaises(tools.ToolError) as cm:
                tools.TOOLS["edit_file"].handler(
                    self.s, {"path": "a.py", "old_string": "def compute_totals(item):", "new_string": "x"}, ctx)
        self.assertIn("line 1: def compute_total(items):", str(cm.exception))
        self.assertIn("read it again", str(cm.exception))

    def test_refusals(self):
        self.put("bin.dat", b"\x00\x01\x02")
        self.put("latin.txt", "caf\xe9\n".encode("latin-1"))
        for args in ({"path": "bin.dat", "old_string": "a", "new_string": "b"},
                     {"path": "latin.txt", "old_string": "caf", "new_string": "b"},
                     {"path": "nope.txt", "old_string": "a", "new_string": "b"},
                     {"path": "latin.txt", "old_string": "", "new_string": "b"},
                     {"path": str(self.s.data_dir / "x"), "old_string": "a", "new_string": "b"}):
            with self.subTest(args["path"]), self.assertRaises(tools.ToolError):
                self.edit(**args)

    def test_file_changed_while_waiting(self):
        p = self.put("a.txt", "one\ntwo\n")
        args = {"path": "a.txt", "old_string": "two", "new_string": "2"}
        self.assertIsNone(edit.prepare(self.s, args, OWNER))
        p.write_text("one\ntwo\nthree\n")
        with self.assertRaises(tools.ToolError) as cm:
            self.edit(**args)
        self.assertIn("changed while waiting", str(cm.exception))
        self.assertEqual(p.read_text(), "one\ntwo\nthree\n")

    def test_write_file_hint(self):
        body = "".join(f"line {i}\n" for i in range(500))
        self.put("big.txt", body)
        out = tools.TOOLS["write_file"].handler(self.s, {"path": "big.txt", "content": body + "x\n"}, OWNER)
        self.assertIn("edit_file", out)
        out = tools.TOOLS["write_file"].handler(self.s, {"path": "new.txt", "content": body}, OWNER)
        self.assertNotIn("edit_file", out)


class EditApprovalTests(Base):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(":memory:"))
        self.sid = self.store.new_session()
        self.asked = []

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def action(self, args, answer=approval.ONCE):
        def approve(req):
            self.asked.append(req)
            return answer
        return Agent(self.s, "unused", self.store, self.sid, approve)._execute("edit_file", args)

    def test_card_shows_diff_and_miss_asks_nothing(self):
        p = self.put("n.md", "# Notes\nold line\n")
        out, failed = self.action({"path": "n.md", "old_string": "missing", "new_string": "x", "purpose": "p"})
        self.assertTrue(failed)
        self.assertEqual(self.asked, [])
        out, failed = self.action({"path": "n.md", "old_string": "old line", "new_string": "new line",
                                   "purpose": "fix the note"})
        self.assertFalse(failed, out)
        card = self.asked[0].card()
        self.assertIn("Edit a file on the phone", card)
        self.assertIn("-old line\n+new line", card)
        self.assertEqual(p.read_text(), "# Notes\nnew line\n")

    def test_denied_writes_nothing_and_model_cannot_supply_a_diff(self):
        p = self.put("n.md", "a\n")
        out, failed = self.action({"path": "n.md", "old_string": "a", "new_string": "b",
                                   "_diff": "fake"}, approval.DENY)
        self.assertTrue(failed)
        self.assertNotIn("fake", self.asked[0].card())
        self.assertEqual(p.read_text(), "a\n")

    def test_always_covers_the_same_diff_only(self):
        self.put("n.md", "a\n")
        self.action({"path": "n.md", "old_string": "a", "new_string": "b"}, approval.ALWAYS)
        self.put("n.md", "a\n")
        out, failed = self.action({"path": "n.md", "old_string": "a", "new_string": "b"}, approval.DENY)
        self.assertFalse(failed, out)
        self.assertEqual(len(self.asked), 1)
        self.put("n.md", "a\n")
        out, failed = self.action({"path": "n.md", "old_string": "a", "new_string": "c"}, approval.DENY)
        self.assertTrue(failed)

    def test_guest_denied(self):
        self.put("n.md", "a\n")
        a = Agent(self.s, "unused", self.store, self.sid, lambda r: approval.ALWAYS, guest=True)
        out, failed = a._execute("edit_file", {"path": "n.md", "old_string": "a", "new_string": "b"})
        self.assertTrue(out.startswith("denied"))


class SearchTests(Base):
    def setUp(self):
        super().setUp()
        self.put("notes/todo.md", "buy milk\ncall Bob\nbuy bread\n")
        self.put("notes/deep/plan.txt", "step one\nbuy tickets\n")
        self.put("code/app.py", "def main():\n    print('buy')\n")
        self.put(".hidden/secret.md", "buy hidden\n")
        self.put("node_modules/x.js", "buy npm\n")
        self.put("blob.bin", b"buy\x00\x00")
        self.s.data_dir.mkdir(parents=True, exist_ok=True)
        (self.s.data_dir / "history.txt").write_text("buy private\n")

    def run_search(self, ctx=OWNER, **args):
        return tools.TOOLS["search_files"].handler(self.s, args, ctx)

    def both(self, **args):
        """The answer with ripgrep (when installed) and with the Python walk."""
        with_rg = self.run_search(**args)
        with patch.object(shutil, "which", return_value=None):
            plain = self.run_search(**args)
        return with_rg, plain

    def test_content_search_skips_hidden_heavy_binary_and_off_limits(self):
        for out in self.both(pattern="buy"):
            self.assertIn("notes/todo.md:1: buy milk", out)
            self.assertIn("notes/todo.md:3: buy bread", out)
            self.assertIn("notes/deep/plan.txt:2: buy tickets", out)
            for bad in ("hidden", "npm", "private", "blob.bin"):
                self.assertNotIn(bad, out)

    def test_files_only_glob_context_limit(self):
        for out in self.both(pattern="buy", files_only=True, glob="*.md"):
            self.assertIn("notes/todo.md", out)
            self.assertNotIn("plan.txt", out)
        for out in self.both(pattern="Bob", path="notes", context=1):
            self.assertIn("todo.md-1- buy milk", out)
            self.assertIn("todo.md:2: call Bob", out)
        for out in self.both(pattern="buy", limit=1):
            self.assertIn("stopped at 1", out)

    def test_names_mode(self):
        out = self.run_search(pattern="*.txt", mode="names")
        self.assertIn("notes/deep/plan.txt", out)
        self.assertNotIn("todo.md", out)

    def test_search_root_off_limits_and_bad_regex(self):
        with self.assertRaises(tools.ToolError):
            self.run_search(pattern="buy", path=str(self.s.data_dir))
        with self.assertRaises(tools.ToolError):
            self.run_search(pattern="(")
        self.assertIn("No matching lines", self.run_search(pattern="zzzz"))

    def test_guest_stays_in_workdir(self):
        guest = SimpleNamespace(guest=True)
        with self.assertRaises(tools.ToolError):
            self.run_search(pattern="buy", path=str(self.tmp.parent), ctx=guest)
        out = self.run_search(pattern="buy", ctx=guest)
        self.assertIn("todo.md", out)
        self.assertNotIn("hidden", out)


if __name__ == "__main__":
    unittest.main()
