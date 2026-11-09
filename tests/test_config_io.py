from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zyfeed import ZyFeedError
from zyfeed.config import load_config
from zyfeed.io import atomic_write, decode_text, update_lock


class ConfigTests(unittest.TestCase):
    def test_relative_paths_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zyfeed.toml"
            path.write_text(
                'output_dir = "www"\nstate_dir = "state"\n'
                '[[feeds]]\nname = "example"\nurl = "https://example.org/list.txt"\n'
                'overrides = "overrides.txt"\n',
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.output_dir, Path(directory) / "www")
            self.assertEqual(
                config.feeds[0].overrides, Path(directory) / "overrides.txt"
            )
            self.assertEqual(config.default_expires, 86400)

    def test_example_configuration_has_only_a_placeholder_source(self) -> None:
        project = Path(__file__).resolve().parents[1]
        examples = project / "examples"
        config = load_config(examples / "zyfeed.example.toml")
        self.assertEqual(len(config.feeds), 1)
        self.assertEqual(config.feeds[0].name, "example")
        self.assertEqual(config.feeds[0].url, "https://example.org/blocklist.txt")
        self.assertEqual(config.feeds[0].overrides, examples / "overrides.txt")
        self.assertEqual(config.output_dir, examples / "var" / "output")
        self.assertEqual(config.state_dir, examples / "var" / "state")

    def test_documented_template_copy_resolves_the_override_file(self) -> None:
        examples = Path(__file__).resolve().parents[1] / "examples"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "zyfeed.toml"
            path.write_bytes((examples / "zyfeed.example.toml").read_bytes())
            overrides = root / "overrides.txt"
            overrides.write_bytes((examples / "overrides.example.txt").read_bytes())
            config = load_config(path)
            self.assertEqual(config.feeds[0].overrides, overrides)
            self.assertEqual(config.output_dir, root / "var" / "output")
            self.assertEqual(config.state_dir, root / "var" / "state")
            self.assertTrue(overrides.is_file())

    def test_invalid_configuration_is_not_silently_ignored(self) -> None:
        cases = (
            'output_dir="www"\nstate_dir="www/private"\n',
            'output_dir="www"\nstate_dir="state"\nunknown=true\n',
            'output_dir="www"\nstate_dir="state"\ntimeout=0\n',
            'output_dir="www"\nstate_dir="state"\ntimeout=true\n',
            'output_dir="www"\nstate_dir="state"\ndefault_expires="never"\n',
        )
        feed = '[[feeds]]\nname="test"\nurl="https://example.org/list.txt"\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for case in cases:
                with self.subTest(case=case):
                    path.write_text(case + feed, encoding="utf-8")
                    with self.assertRaises(ZyFeedError):
                        load_config(path)
            prefix = 'output_dir="www"\nstate_dir="state"\n'
            for invalid_feed in (
                '[[feeds]]\nname="../escape"\nurl="https://example.org/"\n',
                feed + feed,
                feed + 'format="unknown"\n',
                feed + 'comments="yes"\n',
                feed.replace("https://example.org/list.txt", "file:///etc/hosts"),
                feed.replace(
                    "https://example.org/list.txt", "https://user:pass@example.org/"
                ),
                feed + 'overrides="www/test.txt"\n',
            ):
                with self.subTest(feed=invalid_feed):
                    path.write_text(prefix + invalid_feed, encoding="utf-8")
                    with self.assertRaises(ZyFeedError):
                        load_config(path)


class IOTests(unittest.TestCase):
    def test_atomic_failure_keeps_previous_file_and_cleans_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "list.txt"
            path.write_bytes(b"old.example.org\n")
            with patch("zyfeed.io.os.replace", side_effect=OSError("test failure")):
                with self.assertRaises(ZyFeedError):
                    atomic_write(path, b"new.example.org\n")
            self.assertEqual(path.read_bytes(), b"old.example.org\n")
            self.assertEqual(
                [item.name for item in path.parent.iterdir()], ["list.txt"]
            )
            atomic_write(path, b"new.example.org\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_lock_rejects_overlapping_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state"
            with update_lock(path):
                with self.assertRaisesRegex(ZyFeedError, "already"):
                    with update_lock(path):
                        self.fail("overlapping lock acquired")
            with update_lock(path):
                self.assertEqual((path / ".lock").stat().st_mode & 0o777, 0o600)

    def test_invalid_utf8_fails(self) -> None:
        with self.assertRaises(ZyFeedError):
            decode_text(b"\xff", source="test")


if __name__ == "__main__":
    unittest.main()
