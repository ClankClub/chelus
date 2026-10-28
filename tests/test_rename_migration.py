"""Chelys-era folders move to their Chelus names once, without losing anything."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chelus import config, skill_guard


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CHELUS_", "CHELYS_"))}
        self.patches = [patch.dict(os.environ, env, clear=True),
                        patch.object(Path, "home", return_value=self.home)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    def old(self, *parts):
        d = self.home.joinpath(*parts, "chelys")
        d.mkdir(parents=True)
        (d / "deepseek.key").write_text("k", encoding="utf-8")
        return d

    def test_old_folder_moves_and_old_path_still_reaches_it(self):
        old = self.old(".config")
        new = config._config_dir()
        self.assertEqual(new, self.home / ".config" / "chelus")
        self.assertEqual((new / "deepseek.key").read_text(encoding="utf-8"), "k")
        if old.is_symlink():  # symlinks may be unavailable (Windows without the privilege)
            self.assertEqual((old / "deepseek.key").read_text(encoding="utf-8"), "k")
        self.assertEqual(config._config_dir(), new)  # second start: nothing more happens

    def test_data_folder_moves_too(self):
        self.old(".local", "share")
        self.assertTrue((config._data_dir() / "deepseek.key").exists())

    def test_existing_new_folder_wins_and_old_is_left_alone(self):
        old = self.old(".config")
        (self.home / ".config" / "chelus").mkdir()
        self.assertEqual(config._config_dir(), self.home / ".config" / "chelus")
        self.assertTrue((old / "deepseek.key").exists())

    def test_failed_move_keeps_using_old_folder(self):
        old = self.old(".config")
        with patch("os.rename", side_effect=OSError("cross-device")):
            self.assertEqual(config._config_dir(), old)

    def test_fresh_install_uses_new_name(self):
        self.assertEqual(config._data_dir(), self.home / ".local" / "share" / "chelus")
        self.assertFalse((self.home / ".local" / "share" / "chelys").exists())

    def test_env_vars_old_and_new(self):
        with patch.dict(os.environ, {"CHELYS_CONFIG_DIR": "/x/old"}):
            self.assertEqual(config._config_dir(), Path("/x/old"))
            with patch.dict(os.environ, {"CHELUS_CONFIG_DIR": "/x/new"}):
                self.assertEqual(config._config_dir(), Path("/x/new"))

    def test_scanner_blocks_both_names(self):
        for path in ("~/.config/chelys/config.toml", "~/.config/chelus/config.toml",
                     "~/.local/share/chelys/memories", "~/.local/share/chelus/memories"):
            self.assertTrue(skill_guard.blocking(skill_guard.scan(f"cat {path}")), path)


if __name__ == "__main__":
    unittest.main()
