"""The local profile (profile = "local"): a small model on the phone through llama-server.
FAKE model and mocked HTTP only; bench/scenarios/local-*.toml and docs/LOCAL.md cover real
model runs."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chelus import agent as agent_mod
from chelus import provider, tools
from chelus.config import LOCAL_DEFAULTS, ConfigError, Settings, apply_profile, load_api_key, load_settings
from chelus.store import Store
from tests.test_agent import FakeModel, make_settings, tool_call


def settings_from(toml: str) -> Settings:
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d)
        (cfg / "config.toml").write_text(toml, encoding="utf-8")
        with patch.dict(os.environ, {"CHELUS_CONFIG_DIR": str(cfg)}):
            return load_settings()


def local(tmp: Path, **kw) -> Settings:
    s = make_settings(tmp, profile="local")
    apply_profile(s, set())
    for k, v in kw.items():
        setattr(s, k, v)
    return s


class SettingsTests(unittest.TestCase):
    def test_full_is_the_default_and_unchanged(self):
        s = settings_from("")
        self.assertEqual(s.profile, "full")
        self.assertEqual((s.provider, s.base_url, s.thinking, s.temperature, s.history_chars),
                         ("deepseek", "https://api.deepseek.com", False, -1.0, 40000))

    def test_local_fills_in_its_defaults(self):
        s = settings_from('profile = "local"\n')
        for key, value in LOCAL_DEFAULTS.items():
            self.assertEqual(getattr(s, key), value, key)
        self.assertEqual(s.temperature, 0.6)  # thinking is on by default

    def test_explicit_settings_beat_local_defaults(self):
        s = settings_from('profile = "local"\nbase_url = "http://127.0.0.1:9000/v1"\nhistory_chars = 5000\n'
                          'thinking = false\n')
        self.assertEqual((s.base_url, s.history_chars, s.thinking), ("http://127.0.0.1:9000/v1", 5000, False))
        self.assertEqual(s.temperature, 0.2)  # follows thinking when not set
        self.assertEqual(settings_from('profile = "local"\ntemperature = 0.9\n').temperature, 0.9)

    def test_unknown_profile_is_refused(self):
        with self.assertRaisesRegex(ConfigError, "profile"):
            settings_from('profile = "tiny"\n')

    def test_local_runs_without_a_key_but_full_still_needs_one(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DEEPSEEK_API_KEY", None)
                self.assertEqual(load_api_key(local(Path(d))), "")
                with self.assertRaisesRegex(ConfigError, "no API key"):
                    load_api_key(make_settings(Path(d)))
            (Path(d) / "cfg").mkdir()
            (Path(d) / "cfg" / "local.key").write_text("k\n", encoding="utf-8")
            self.assertEqual(load_api_key(local(Path(d))), "k")


class ProviderTests(unittest.TestCase):
    def sent(self, s: Settings, key: str):
        body = {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}
        response = io.BytesIO(json.dumps(body).encode())
        response.read1 = response.read
        with patch("chelus.provider.urllib.request.urlopen", return_value=response) as urlopen:
            provider.chat(s, key, [{"role": "user", "content": "hi"}], [])
        req = urlopen.call_args[0][0]
        return json.loads(req.data), {k.lower(): v for k, v in req.header_items()}

    def test_local_sends_temperature_and_thinking_switch_and_no_empty_auth(self):
        with tempfile.TemporaryDirectory() as d:
            payload, headers = self.sent(local(Path(d)), "")
        self.assertEqual(payload["temperature"], 0.6)
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": True})
        self.assertNotIn("thinking", payload)
        self.assertNotIn("authorization", headers)

    def test_full_payload_has_no_local_fields(self):
        with tempfile.TemporaryDirectory() as d:
            payload, headers = self.sent(make_settings(Path(d), provider="openai"), "secret")
        self.assertNotIn("temperature", payload)
        self.assertNotIn("chat_template_kwargs", payload)
        self.assertEqual(headers["authorization"], "Bearer secret")


class PromptAndToolsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.s = local(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_only_the_local_tools_are_offered(self):
        names = tools.available(False, agent_mod.disabled_tools(self.s))
        self.assertEqual(set(names), set(tools.LOCAL))
        guest = tools.available(True, agent_mod.disabled_tools(self.s))
        self.assertTrue(set(guest) <= set(tools.LOCAL))

    def test_compact_schemas_are_short_and_keep_risky_purpose(self):
        full = json.dumps(tools.schemas(list(tools.LOCAL)))
        short = tools.schemas(list(tools.LOCAL), compact=True)
        self.assertLess(len(json.dumps(short)), len(full) * 0.7)
        by = {x["function"]["name"]: x["function"]["parameters"] for x in short}
        for name in ("run_shell", "write_file"):
            self.assertTrue(tools.TOOLS[name].risky)
            self.assertIn("purpose", by[name]["required"])
        # names without a short form keep their full schema
        self.assertEqual(tools.schemas(["memory"], compact=True), tools.schemas(["memory"]))

    def test_prompt_is_short_stable_and_states_the_approval_rule(self):
        p = agent_mod.system_prompt(self.s, False, "skills here", "memory here", True, "notes here")
        self.assertEqual(p, agent_mod.system_prompt(self.s, False, "other", "other", False, "other"))
        self.assertIn("approves write_file and run_shell", p)
        self.assertNotIn("skills here", p)
        self.assertNotIn("memory here", p)
        self.assertLess(len(p), 600)
        self.assertIn("guest", agent_mod.system_prompt(self.s, True))

    def test_persona_local_file_overrides_and_full_persona_is_untouched(self):
        self.s.config_dir.mkdir(parents=True)
        self.s.persona_path.write_text("FULL PERSONA", encoding="utf-8")
        self.assertNotIn("FULL PERSONA", agent_mod.system_prompt(self.s))
        self.s.persona_local_path.write_text("TINY PERSONA", encoding="utf-8")
        self.assertTrue(agent_mod.system_prompt(self.s).startswith("TINY PERSONA"))
        full = make_settings(self.tmp)
        self.assertTrue(agent_mod.system_prompt(full).startswith("FULL PERSONA"))

    def test_read_file_header_on_local_only_and_offset_zero_reads_from_start(self):
        (self.tmp / "todo.txt").write_bytes(b"a\nb\n")
        self.assertEqual(tools.TOOLS["read_file"].handler(self.s, {"path": "todo.txt", "offset": 0}),
                         "[todo.txt, 2 lines]\na\nb\n")
        full = make_settings(self.tmp)
        self.assertEqual(tools.TOOLS["read_file"].handler(full, {"path": "todo.txt"}), "a\nb\n")


class AgentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.s = local(self.tmp, review_interval=1, memory_review_interval=1)
        self.store = Store(self.s.db_path)
        self.sid = self.store.new_session()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_turn_sends_compact_schemas_and_short_prompt_and_runs_no_reviews(self):
        seen = []

        def model(s, key, messages, schemas, **kw):
            seen.append((messages[0]["content"], schemas))
            return {"role": "assistant", "content": "hi"}

        a = agent_mod.Agent(self.s, "", self.store, self.sid, lambda r: False, chat_fn=model)
        a.run_turn("hello")
        system, schemas = seen[0]
        self.assertEqual(system, agent_mod.local_prompt(self.s))
        self.assertEqual(schemas, tools.schemas(list(tools.LOCAL), compact=True))
        self.assertFalse(a.review_due or a.memory_review_due)
        self.assertIsNone(self.store.session_field(self.sid, "skills_index"))

    def test_risky_tools_still_need_approval(self):
        asked = []
        model = FakeModel([
            {"role": "assistant", "content": None,
             "tool_calls": [tool_call("run_shell", {"command": "rm old.log", "purpose": "Delete old.log"})]},
            {"role": "assistant", "content": "Not deleted."},
        ])
        (self.tmp / "old.log").write_text("x", encoding="utf-8")
        a = agent_mod.Agent(self.s, "", self.store, self.sid, lambda r: asked.append(r) or False, chat_fn=model)
        a.run_turn("delete old.log")
        self.assertEqual(len(asked), 1)
        self.assertTrue((self.tmp / "old.log").exists())
        self.assertIn("denied", model.calls[1][-1]["content"])

    def test_cli_learn_is_refused_without_a_model_call(self):
        import contextlib
        from chelus import cli, learn
        out = io.StringIO()
        args = cli.build_parser().parse_args(["chat", "--session", str(self.sid)])
        with patch.object(cli, "Agent") as agent, \
                patch("builtins.input", side_effect=["/learn how we check storage", "/quit"]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.cmd_chat(self.s, args), 0)
        self.assertIn(learn.LOCAL_REFUSAL, out.getvalue())
        agent.return_value.run_turn.assert_not_called()

    def test_tools_outside_the_profile_are_refused(self):
        model = FakeModel([
            {"role": "assistant", "content": None, "tool_calls": [tool_call("memory", {"action": "add"})]},
            {"role": "assistant", "content": "ok"},
        ])
        agent_mod.Agent(self.s, "", self.store, self.sid, lambda r: False, chat_fn=model).run_turn("remember x")
        self.assertIn("no tool named 'memory'", model.calls[1][-1]["content"])


if __name__ == "__main__":
    unittest.main()
