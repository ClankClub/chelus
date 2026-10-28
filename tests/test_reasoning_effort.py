"""Thinking toggle and explicit DeepSeek effort are independent controls."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chelus.config import ConfigError, Settings, load_settings
from chelus.provider import build_payload


class ReasoningEffortTests(unittest.TestCase):
    def test_low_effort_keeps_thinking_enabled(self):
        settings = Settings(thinking=True, reasoning_effort="low")
        payload = build_payload(settings, [{"role": "user", "content": "hello"}], [])
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload.get("reasoning_effort"), "low")

    def test_disabled_thinking_does_not_send_effort(self):
        payload = build_payload(Settings(thinking=False, reasoning_effort="low"), [], [])
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", payload)

    def test_existing_thinking_configuration_keeps_default_high(self):
        payload = build_payload(Settings(thinking=True), [], [])
        self.assertEqual(payload.get("reasoning_effort"), "high")

    def test_invalid_effort_is_rejected_when_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "config.toml").write_text('thinking = true\nreasoning_effort = "banana"\n')
            with patch.dict(os.environ, {"CHELUS_CONFIG_DIR": str(path)}):
                with self.assertRaisesRegex(ConfigError, "reasoning_effort"):
                    load_settings()

    def test_load_low_and_thinking_together(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "config.toml").write_text('thinking = true\nreasoning_effort = "low"\n')
            with patch.dict(os.environ, {"CHELUS_CONFIG_DIR": str(path)}):
                settings = load_settings()
            self.assertTrue(settings.thinking)
            self.assertEqual(settings.reasoning_effort, "low")


if __name__ == "__main__":
    unittest.main()
