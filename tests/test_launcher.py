from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class LauncherTests(unittest.TestCase):
    def test_launcher_reports_unsupported_python_before_package_import(self) -> None:
        project = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import runpy, sys; "
                "namespace = runpy.run_path(sys.argv[1]); "
                "sys.version_info = (3, 10); "
                "raise SystemExit(namespace['start']())",
                str(project / "run"),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Python 3.11", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_checkout_runs_without_installation_and_does_not_create_caches(
        self,
    ) -> None:
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout, caller = root / "checkout", root / "caller"
            checkout.mkdir()
            caller.mkdir()
            shutil.copy2(project / "run", checkout / "run")
            shutil.copytree(
                project / "zyfeed",
                checkout / "zyfeed",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            for arguments in (
                [],
                ["--help"],
                ["--version"],
                ["convert"],
                ["convert", "--help"],
                ["update"],
                ["update", "--help"],
                ["schedule"],
                ["schedule", "--help"],
                ["schedule", "enable", "--help"],
            ):
                with self.subTest(arguments=arguments):
                    result = subprocess.run(
                        [str(checkout / "run"), *arguments],
                        cwd=caller,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(result.stdout)
            (caller / "input.txt").write_text("||tracker.example.org^\n")
            result = subprocess.run(
                [
                    str(checkout / "run"),
                    "convert",
                    "input.txt",
                    "--output",
                    "result.txt",
                ],
                cwd=caller,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (caller / "result.txt").read_bytes(),
                b"*.tracker.example.org\ntracker.example.org\n",
            )
            self.assertFalse((checkout / "result.txt").exists())
            self.assertEqual(list(checkout.rglob("__pycache__")), [])
            self.assertFalse((checkout / ".venv").exists())
            self.assertFalse((checkout / "zyfeed.toml").exists())


if __name__ == "__main__":
    unittest.main()
