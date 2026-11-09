from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from zyfeed.cli import main


class CLITests(unittest.TestCase):
    def test_frozen_help_uses_the_installed_binary_in_every_example(self) -> None:
        for arguments in (
            [],
            ["convert"],
            ["update"],
            ["schedule"],
            ["schedule", "enable", "--help"],
            ["schedule", "disable", "--help"],
            ["schedule", "status", "--help"],
        ):
            with self.subTest(arguments=arguments):
                output = io.StringIO()
                with (
                    patch("sys.frozen", True, create=True),
                    patch("sys.executable", "/opt/zyfeed-bin/zyfeed"),
                    redirect_stdout(output),
                    self.assertRaises(SystemExit) as result,
                ):
                    main(arguments)
                self.assertEqual(result.exception.code, 0)
                self.assertIn("/opt/zyfeed-bin/zyfeed", output.getvalue())
                self.assertNotIn("./run", output.getvalue())

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "zyfeed", *arguments],
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_help_and_version(self) -> None:
        for arguments in (
            ("--help",),
            ("convert", "--help"),
            ("update", "--help"),
            ("--version",),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--overrides", self.run_cli("convert", "--help").stdout)
        self.assertIn("0.1.0", self.run_cli("--version").stdout)

    def test_bare_commands_show_their_full_help(self) -> None:
        for command in ("convert", "update", "schedule"):
            explicit = self.run_cli(command, "--help")
            for prefix in ((), ("--verbose",)):
                with self.subTest(command=command, prefix=prefix):
                    result = self.run_cli(*prefix, command)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, explicit.stdout)
                    self.assertEqual(result.stderr, "")

    def test_incomplete_or_invalid_options_are_still_errors(self) -> None:
        for arguments in (
            ("convert", "--format", "adblock"),
            ("schedule", "--invalid-option"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIn("error:", result.stderr)

    def test_bare_update_does_not_read_an_existing_configuration(self) -> None:
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "zyfeed.toml"
            config.write_text("invalid configuration must not be read\n")
            result = subprocess.run(
                [str(project / "run"), "update"],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage: ./run update", result.stdout)
            self.assertEqual(result.stderr, "")
            self.assertEqual(list(Path(directory).iterdir()), [config])

    def test_update_with_parameters_still_reads_its_configuration(self) -> None:
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "zyfeed.toml"
            config.write_text("invalid configuration must be reported\n")
            for arguments in (("--config", "zyfeed.toml"), ("--force",)):
                with self.subTest(arguments=arguments):
                    result = subprocess.run(
                        [str(project / "run"), "update", *arguments],
                        cwd=directory,
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(
                        "cannot load configuration zyfeed.toml", result.stderr
                    )
                    self.assertEqual(result.stdout, "")
            self.assertEqual(list(Path(directory).iterdir()), [config])

    def test_initial_help_explains_override_modes_and_toml(self) -> None:
        for arguments in ((), ("--help",)):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--overrides overrides.txt", result.stdout)
                self.assertIn('overrides = "overrides.txt"', result.stdout)
                self.assertIn("no TOML needed", result.stdout)
                self.assertIn("pyproject.toml", result.stdout)
        update = self.run_cli("update", "--help")
        self.assertEqual(update.returncode, 0, update.stderr)
        self.assertIn("[[feeds]]", update.stdout)
        self.assertIn('overrides = "overrides.txt"', update.stdout)
        self.assertIn("relative to that TOML file", update.stdout)

    def test_each_command_help_includes_usage_and_practical_examples(self) -> None:
        cases = (
            (
                ("convert",),
                (
                    "./run convert examples/blocklist.adblock.txt",
                    "--overrides overrides.txt -o blocklist.txt",
                    "https://example.org/blocklist.txt",
                ),
            ),
            (
                ("update",),
                (
                    "./run update\n",
                    "./run update --config zyfeed.toml",
                    "./run update --config zyfeed.toml --force",
                ),
            ),
            (
                ("schedule",),
                (
                    "./run schedule enable --config zyfeed.toml",
                    "./run schedule status",
                    "./run schedule disable",
                ),
            ),
            (
                ("schedule", "enable"),
                ("./run schedule enable --config zyfeed.toml", "--yes"),
            ),
            (
                ("schedule", "disable"),
                ("./run schedule disable", "./run schedule disable --yes"),
            ),
            (
                ("schedule", "status"),
                ("./run schedule status", "does not download"),
            ),
        )
        for command, examples in cases:
            with self.subTest(command=command):
                result = self.run_cli(*command, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage: zyfeed " + " ".join(command), result.stdout)
                self.assertIn("Example", result.stdout)
                for example in examples:
                    self.assertIn(example, result.stdout)
        project = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [str(project / "run"), "convert", "--help"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: ./run convert", result.stdout)

    def test_schedule_help_explains_multiple_feeds_and_publication(self) -> None:
        result = self.run_cli("schedule", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout.lower()
        for detail in (
            "one user timer",
            "five feeds",
            "sequentially",
            "expires",
            "default_expires",
            "no request",
            "http 304",
            "atomically",
            "unchanged txt",
            "local txt files",
            "outside zyfeed",
        ):
            with self.subTest(detail=detail):
                self.assertIn(detail, text)

    def test_file_conversion_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.txt"
            source.write_text(
                "! Title: Example\n! Expires: 8 hours\n||tracker.example.org^\n"
            )
            overrides = root / "overrides.txt"
            overrides.write_text("+ extra.example.org\n- tracker.example.org\n")
            output = root / "www" / "output.txt"
            result = self.run_cli(
                "convert", str(source), "-o", str(output), "--overrides", str(overrides)
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(
                output.read_text(), "*.tracker.example.org\nextra.example.org\n"
            )
            metadata = json.loads(output.with_suffix(".json").read_text())
            self.assertEqual(metadata["upstream"]["title"], "Example")
            self.assertEqual(metadata["expires_seconds"], 28800)

    def test_stdout_contains_only_the_blocklist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.txt"
            source.write_text("||example.org^\n")
            result = self.run_cli("convert", str(source))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "*.example.org\nexample.org\n")
            self.assertIn("entries", result.stderr)

    def test_documented_example_uses_only_local_synthetic_input(self) -> None:
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "example.txt"
            result = self.run_cli(
                "convert",
                str(project / "examples" / "blocklist.adblock.txt"),
                "--format",
                "adblock",
                "--overrides",
                str(project / "examples" / "overrides.example.txt"),
                "--output",
                str(output),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                output.read_bytes(),
                b"*.extra.example.org\n*.tracker.example.org\n"
                b"extra.example.org\ntracker.example.org\n",
            )
            metadata = json.loads(output.with_suffix(".json").read_text())
            self.assertEqual(metadata["upstream"]["title"], "ZyFeed example blocklist")
            self.assertEqual(metadata["statistics"]["added_entries"], 2)
            self.assertEqual(metadata["statistics"]["removed_entries"], 2)

    def test_source_and_overrides_are_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.txt"
            original = "||example.org^\n"
            source.write_text(original)
            result = self.run_cli("convert", str(source), "-o", str(source))
            self.assertEqual(result.returncode, 1)
            self.assertEqual(source.read_text(), original)
            result = self.run_cli("convert", str(source), "--metadata", str(source))
            self.assertEqual(result.returncode, 1)
            self.assertEqual(source.read_text(), original)

    def test_invalid_conversion_does_not_replace_a_published_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.txt"
            output = Path(directory) / "output.txt"
            source.write_text("||example.org^$third-party\n")
            output.write_text("old.example.org\n")
            result = self.run_cli("convert", str(source), "-o", str(output))
            self.assertEqual(result.returncode, 1)
            self.assertEqual(output.read_text(), "old.example.org\n")
            self.assertIn("options/conditions", result.stderr)


if __name__ == "__main__":
    unittest.main()
