"""The starter skills shipped in skills/ pass the same checks as one the agent writes."""

from __future__ import annotations

import unittest
from pathlib import Path

from chelus import skill_guard, skills

ROOT = Path(__file__).resolve().parent.parent / "skills"


class StarterSkillTests(unittest.TestCase):
    def test_every_starter_skill_is_valid_clean_and_lean(self):
        found = skills.discover(ROOT)
        self.assertGreaterEqual(len(found), 4)
        for skill in found:
            with self.subTest(skill.name):
                text = Path(skill.path).read_text(encoding="utf-8")
                fm, body = skills.validate_skill_md(text, new=True)
                self.assertEqual(fm["name"], skill.dir.name)
                self.assertEqual(skill_guard.scan(text), [])
                self.assertEqual(skill_guard.lint(str(fm["description"]), body), [])


if __name__ == "__main__":
    unittest.main()
