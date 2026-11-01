"""The test bench's own files: every model profile is a valid config and every scenario
parses and uses checks the simulator knows. No model calls."""
import tempfile
import unittest
from pathlib import Path

from chelus.config import Settings

try:
    from bench import sim
except ImportError:  # the phone gets chelus/, tests/ and skills/ only
    sim = None

KNOWN_CHECKS = {"tool_used", "tool_not_used", "skill_viewed", "reply_regex", "reply_not_regex",
                "args_not_regex", "file_regex", "max_calls", "no_errors", "judge"}


@unittest.skipIf(sim is None, "bench/ isn't deployed here")
class BenchFilesTests(unittest.TestCase):
    def test_profiles_are_valid_settings(self):
        profiles = sorted(sim.MODELS.glob("*.toml"))
        self.assertTrue(profiles)
        for p in profiles:
            with self.subTest(p.stem):
                s = Settings(**sim.load_profile(p.stem))
                self.assertIn(s.provider, ("deepseek", "openai"))
                self.assertEqual(Path(s.api_key_file).name, s.api_key_file)

    def test_scenarios_parse_and_use_known_checks(self):
        scenarios = sim.load_scenarios("all")
        self.assertTrue(scenarios)
        for sc in scenarios:
            with self.subTest(sc["name"]):
                self.assertTrue(sc.get("turns") or sc.get("owner"))
                self.assertTrue(sc.get("check"))
                for c in sc["check"]:
                    self.assertIn(c["kind"], KNOWN_CHECKS)
                if sc.get("role") not in (None, "owner", "guest"):
                    self.fail(f"bad role {sc['role']}")

    def test_checks_score_a_recorded_run(self):
        with tempfile.TemporaryDirectory() as d:
            s = Settings(workdir=d)
            (Path(d) / "f.txt").write_text("timeout = 45\n", encoding="utf-8")
            run = sim.Run("x", "m", turns=[{"user": "hi", "reply": "see 2401.12345", "error": ""}],
                          calls=[{"turn": 1, "name": "skill_view", "args": {"name": "arxiv"}}])
            verdict = lambda **c: sim.check(c, run, s)["pass"]
            self.assertTrue(verdict(kind="skill_viewed", skill="arxiv"))
            self.assertFalse(verdict(kind="tool_used", tool="web_fetch"))
            self.assertTrue(verdict(kind="reply_regex", pattern=r"\d{4}\.\d{5}"))
            self.assertTrue(verdict(kind="file_regex", path="f.txt", pattern="^timeout = 45$"))
            self.assertTrue(verdict(kind="args_not_regex", pattern=r"\.ssh"))
            self.assertTrue(verdict(kind="no_errors"))


if __name__ == "__main__":
    unittest.main()
