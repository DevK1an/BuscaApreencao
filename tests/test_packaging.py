import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import app_paths
import desktop


class PackagingTests(unittest.TestCase):
    def test_frozen_worker_calls_same_executable_without_python_flags(self):
        with patch.object(app_paths.sys, "frozen", True, create=True), \
             patch.object(app_paths.sys, "executable", r"C:\Aplicativos\CompraHub.exe"):
            args = app_paths.worker_command(Path(r"C:\Dados\config.json"), 3)
        self.assertEqual(args, [
            r"C:\Aplicativos\CompraHub.exe", "--worker",
            "--config", r"C:\Dados\config.json", "--quantidade", "3",
        ])

    def test_source_worker_keeps_python_entry(self):
        with patch.object(app_paths.sys, "frozen", False, create=True):
            args = app_paths.worker_command("config.json", 2, Path("source"))
        self.assertEqual(args[1:3], ["-X", "utf8"])
        self.assertTrue(args[3].endswith("bot.py"))

    def test_frozen_user_data_is_separate_from_extracted_resources(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {"LOCALAPPDATA":folder}, clear=True), \
             patch.object(app_paths.sys, "frozen", True, create=True):
            self.assertEqual(app_paths.data_root(), Path(folder) / "CompraHub")
            self.assertNotEqual(app_paths.data_root(), app_paths.RESOURCE_ROOT)

    def test_explicit_test_directory_does_not_touch_real_account_data(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {"COMPRAHUB_DATA_DIR":folder}):
            self.assertEqual(app_paths.data_root(), Path(folder).resolve())

    def test_frozen_browser_path_uses_embedded_resources(self):
        with patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH":"missing-browser"}), \
             patch.object(app_paths.sys, "frozen", True, create=True):
            app_paths.browser_environment()
            self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], str(app_paths.RESOURCE_ROOT / ".browsers"))

    def test_worker_dispatch_never_starts_a_second_hub(self):
        with patch("bot.main", return_value=0) as bot, patch("hub.main") as hub:
            self.assertEqual(desktop.main(["--worker","--config","test.json","--quantidade","1"]), 0)
            bot.assert_called_once_with(["--config","test.json","--quantidade","1"])
            hub.assert_not_called()

    def test_regular_launch_dispatches_to_hub(self):
        with patch("hub.main", return_value=0) as hub:
            self.assertEqual(desktop.main(["--no-open"]), 0)
            hub.assert_called_once_with(["--no-open"])


if __name__ == "__main__":
    unittest.main()
