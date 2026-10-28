"""Offline regressions for resource limits, API validation and deployment safety."""

import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from chelys import provider, tools
from tests.test_agent import make_settings, tool_call


class GuestListingTests(unittest.TestCase):
    def test_guest_listing_hides_dotfiles_protected_children_and_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            s = make_settings(root)
            s.config_dir.mkdir()
            s.data_dir.mkdir()
            (root / ".hidden").write_text("dummy")
            (root / "visible").write_text("hello")
            if os.name == "posix":
                (root / "blocked-link").symlink_to(s.config_dir, target_is_directory=True)
                (root / "hidden-link").symlink_to(root / ".hidden")
                (root / "outside-link").symlink_to(root.parent, target_is_directory=True)
                (root / ".hidden-alias").symlink_to(root / "visible")
            out = tools._list_dir(s, {"path": "."}, SimpleNamespace(guest=True))
            self.assertEqual(out.splitlines()[1:], ["visible  5 bytes"])

    def test_owner_listing_still_excludes_protected_children(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            s = make_settings(root)
            s.config_dir.mkdir()
            (root / ".visible-to-owner").write_text("x")
            out = tools._list_dir(s, {"path": "."})
            self.assertEqual(out.splitlines()[1:], [".visible-to-owner  1 bytes"])


@unittest.skipUnless(os.name == "posix", "POSIX shell tools")
class ShellBoundsTests(unittest.TestCase):
    def run_shell(self, code, timeout=3):
        with tempfile.TemporaryDirectory() as d:
            s = make_settings(Path(d), shell_timeout=timeout)
            return tools._run_shell(s, {"command": shlex.join([sys.executable, "-c", code])})

    def test_stdout_and_stderr_are_bounded_before_decoding(self):
        for fd in (1, 2):
            with self.subTest(fd=fd):
                out = self.run_shell(f"import os; os.write({fd}, b'x' * 1000000)")
                self.assertLess(len(out), 70000)
                self.assertIn("output exceeded", out)
                self.assertNotIn("exit code 0", out)

    def test_both_pipes_are_drained_without_deadlock(self):
        out = self.run_shell("import os; [(os.write(1,b'a'*4096),os.write(2,b'b'*4096)) for _ in range(4)]")
        self.assertTrue(out.startswith("exit code 0"))
        self.assertIn("a" * 4096, out)
        self.assertIn("b" * 4096, out)

    def test_output_limit_is_shared_between_stdout_and_stderr(self):
        out = self.run_shell("import os; os.write(1,b'a'*40000); os.write(2,b'b'*40000)")
        self.assertLess(len(out), 70000)
        self.assertIn("output exceeded", out)

    def test_output_overflow_is_failure_even_if_process_already_exited(self):
        # kill/wait can observe a successful exit after the pipes have filled.
        kill = tools._kill_group
        def exited(proc):
            kill(proc)
            proc.returncode = 0
        with patch("chelys.tools._kill_group", side_effect=exited):
            out = self.run_shell("import os; os.write(1,b'x'*1000000)")
        self.assertFalse(out.startswith("exit code 0"))
        self.assertIn("output exceeded", out)

    def test_timeout_still_applies_after_process_closes_its_pipes(self):
        started = time.monotonic()
        with self.assertRaisesRegex(tools.ToolError, "timed out"):
            self.run_shell("import os,time; os.close(1); os.close(2); time.sleep(20)", timeout=0.2)
        self.assertLess(time.monotonic() - started, 3)


class ProviderValidationTests(unittest.TestCase):
    def call_with(self, body, retries=0):
        response = io.BytesIO(json.dumps(body).encode())
        response.read1 = response.read
        with tempfile.TemporaryDirectory() as d:
            s = make_settings(Path(d), api_max_retries=retries)
            with patch("chelys.provider.urllib.request.urlopen", return_value=response):
                return provider.chat(s, "dummy-key", [], [])

    def test_malformed_message_and_tool_shapes_raise_provider_error(self):
        cases = [None, [], "wrong", {"content": []}, {"reasoning_content": {}},
                 {"tool_calls": {}}, {"tool_calls": [None]},
                 {"tool_calls": [{"id": "c", "function": None}]},
                 {"tool_calls": [{"id": "c", "function": {"name": "read_file", "arguments": {}}}]},
                 {"tool_calls": [{"id": "", "function": {"name": "read_file", "arguments": "{}"}}]},
                 {"tool_calls": [{"id": "c", "function": {"name": "", "arguments": "{}"}}]}]
        for message in cases:
            with self.subTest(message=message):
                with self.assertRaises(provider.ProviderError) as cm:
                    self.call_with({"choices": [{"message": message}]})
                self.assertTrue(cm.exception.retryable)
                self.assertTrue(cm.exception.transient)

    def test_valid_tool_arguments_stay_strings_for_agent_error_handling(self):
        tc = tool_call("read_file", {})
        tc["function"]["arguments"] = "{broken"
        result = self.call_with({"choices": [{"message": {"content": None, "tool_calls": [tc]}}]})
        self.assertEqual(result["tool_calls"][0]["function"]["arguments"], "{broken")

    def test_body_size_is_bounded_before_json_parse(self):
        response = io.BytesIO(b"x" * (3 * 1024 * 1024))
        with self.assertRaisesRegex(provider.ProviderError, "response exceeded"):
            provider._read_until(response, time.monotonic() + 3, 3)
        self.assertLessEqual(response.tell(), 2 * 1024 * 1024 + 65536)

    def test_malformed_response_retries_and_recovers(self):
        replies = [io.BytesIO(b'{"choices":[{"message":null}]}'),
                   io.BytesIO(b'{"choices":[{"message":{"content":"recovered"}}]}')]
        with tempfile.TemporaryDirectory() as d:
            s = make_settings(Path(d), api_max_retries=1)
            with patch("chelys.provider.urllib.request.urlopen", side_effect=replies):
                result = provider.chat(s, "dummy", [], [], sleep=lambda seconds: None)
        self.assertEqual(result["content"], "recovered")

    def test_deeply_nested_json_retries_and_recovers(self):
        # Small bodies can exceed the JSON decoder's recursion limit.
        replies = [io.BytesIO(b"[" * 10000 + b"]" * 10000),
                   io.BytesIO(b'{"choices":[{"message":{"content":"recovered"}}]}')]
        with tempfile.TemporaryDirectory() as d:
            s = make_settings(Path(d), api_max_retries=1)
            with patch("chelys.provider.urllib.request.urlopen", side_effect=replies):
                result = provider.chat(s, "dummy", [], [], sleep=lambda seconds: None)
        self.assertEqual(result["content"], "recovered")


@unittest.skipUnless(os.name == "posix", "deployment needs sh, tar and SSH")
class DeployTests(unittest.TestCase):
    def run_deploy(self, test_status):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            # Keep the real shell/tar flow; replace only the transport and phone Python.
            (bin_dir / "ssh").write_text('#!/bin/sh\nfor arg do command=$arg; done\nexec sh -c "$command"\n')
            (bin_dir / "python").write_text(f'#!/bin/sh\nprintf "fake test output\\n"\nexit {test_status}\n')
            for p in bin_dir.iterdir():
                p.chmod(0o755)
            install = root / "chelys"
            install.mkdir()
            (install / "chelys.sh").write_text("previous version")
            env = dict(os.environ, HOME=str(root), PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
            script = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"
            result = subprocess.run(["sh", str(script)], env=env, capture_output=True, text=True, timeout=10)
            return result, (install / "chelys.sh").read_text()

    def test_failed_phone_suite_returns_failure_without_replacing_installed_code(self):
        result, launcher = self.run_deploy(7)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(launcher, "previous version")

    def test_successful_phone_suite_installs_the_verified_candidate(self):
        result, launcher = self.run_deploy(0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("exec python -m chelys", launcher)
