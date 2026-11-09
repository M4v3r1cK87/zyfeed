from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from zyfeed import ZyFeedError
from zyfeed.cli import main
from zyfeed.scheduler import (
    MARKER,
    SERVICE,
    TIMER,
    disable_schedule,
    enable_schedule,
    render_units,
    schedule_status,
    unit_directory,
)


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environment = patch.dict(
            os.environ, {"XDG_CONFIG_HOME": str(self.root / "user-config")}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.config = self.root / "feeds.toml"
        self.config.write_text(
            'output_dir="www"\nstate_dir="state"\n'
            '[[feeds]]\nname="example"\nurl="https://example.org/list.txt"\n',
            encoding="utf-8",
        )

    def test_enable_installs_owned_user_units_and_checks_manager_first(self) -> None:
        with patch("zyfeed.scheduler._systemctl", return_value="Version=257") as ctl:
            enable_schedule(self.config)
        self.assertEqual(
            [call.args for call in ctl.call_args_list],
            [
                ("show", "--property=Version"),
                ("daemon-reload",),
                ("enable", "--now", TIMER),
            ],
        )
        service = (unit_directory() / SERVICE).read_text()
        timer = (unit_directory() / TIMER).read_text()
        self.assertTrue(service.startswith(MARKER))
        self.assertIn('"-B" "-m" "zyfeed" "update"', service)
        self.assertIn(str(self.config.resolve()), service)
        self.assertNotIn("User=root", service)
        self.assertIn("OnCalendar=*:0/15", timer)
        self.assertEqual((unit_directory() / SERVICE).stat().st_mode & 0o777, 0o644)

    def test_five_feeds_still_install_only_one_timer_and_service(self) -> None:
        self.config.write_text(
            'output_dir="www"\nstate_dir="state"\n'
            + "".join(
                f'[[feeds]]\nname="list-{letter}"\n'
                f'url="https://example.org/{letter}.txt"\n'
                for letter in "abcde"
            ),
            encoding="utf-8",
        )
        with patch("zyfeed.scheduler._systemctl", return_value="Version=257") as ctl:
            enable_schedule(self.config)
        self.assertEqual(
            sorted(path.name for path in unit_directory().iterdir()),
            [SERVICE, TIMER],
        )
        self.assertEqual(
            [call.args for call in ctl.call_args_list if call.args[0] == "enable"],
            [("enable", "--now", TIMER)],
        )
        service = (unit_directory() / SERVICE).read_text()
        self.assertEqual(service.count('"--config"'), 1)
        self.assertIn(str(self.config.resolve()), service)

    def test_frozen_units_invoke_the_permanent_binary_not_python(self) -> None:
        binary = self.root / "installed binary" / "zyfeed"
        with (
            patch("sys.frozen", True, create=True),
            patch("sys.executable", str(binary)),
            patch("zyfeed.scheduler.__file__", "/tmp/_MEIexample/zyfeed/scheduler.py"),
        ):
            service, _ = render_units(self.config)
        text = service.decode()
        self.assertIn(f'ExecStart="{binary}" "update" "--config"', text)
        self.assertIn(f"WorkingDirectory={self.config.parent}\n", text)
        self.assertNotIn('"-B"', text)
        self.assertNotIn('"-m"', text)
        self.assertNotIn("_MEIexample", text)

    def test_no_manager_is_an_explicit_failure_without_creating_units(self) -> None:
        with patch(
            "zyfeed.scheduler._systemctl", side_effect=ZyFeedError("no user manager")
        ):
            with self.assertRaisesRegex(ZyFeedError, "no user manager"):
                enable_schedule(self.config)
        self.assertFalse(unit_directory().exists())

    def test_invalid_config_does_not_create_units_or_invoke_systemd(self) -> None:
        self.config.write_text("invalid configuration")
        with patch("zyfeed.scheduler._systemctl") as ctl:
            with self.assertRaises(ZyFeedError):
                enable_schedule(self.config)
        ctl.assert_not_called()
        self.assertFalse(unit_directory().exists())

    def test_unrelated_units_are_never_overwritten_or_disabled(self) -> None:
        directory = unit_directory()
        directory.mkdir(parents=True)
        timer = directory / TIMER
        timer.write_text("# A manually managed timer\n")
        with patch("zyfeed.scheduler._systemctl") as ctl:
            with self.assertRaisesRegex(ZyFeedError, "not created by ZyFeed"):
                enable_schedule(self.config)
            with self.assertRaisesRegex(ZyFeedError, "not created by ZyFeed"):
                disable_schedule()
        ctl.assert_not_called()
        self.assertEqual(timer.read_text(), "# A manually managed timer\n")
        self.assertFalse((directory / SERVICE).exists())

    def test_unit_symlinks_are_not_replaced(self) -> None:
        directory = unit_directory()
        directory.mkdir(parents=True)
        target = self.root / "other.service"
        target.write_text(MARKER)
        (directory / SERVICE).symlink_to(target)
        with patch("zyfeed.scheduler._systemctl") as ctl:
            with self.assertRaisesRegex(ZyFeedError, "symlink"):
                enable_schedule(self.config)
        ctl.assert_not_called()
        self.assertTrue((directory / SERVICE).is_symlink())

    def test_unit_arguments_escape_systemd_specifiers_and_environment_syntax(
        self,
    ) -> None:
        config = self.root / 'config with $name%specifier"quote.toml'
        service, _ = render_units(config)
        self.assertIn(b'$$name%%specifier\\"quote.toml', service)
        with self.assertRaisesRegex(ZyFeedError, "control characters"):
            render_units(self.root / "unsafe\nname.toml")

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze unavailable")
    def test_generated_units_pass_systemd_validation(self) -> None:
        package = self.root / 'checkout with spaces%and"quotes' / "zyfeed"
        package.mkdir(parents=True)
        with patch("zyfeed.scheduler.__file__", str(package / "scheduler.py")):
            service, timer = render_units(self.root / 'config with $name%".toml')
        service_path, timer_path = self.root / SERVICE, self.root / TIMER
        service_path.write_bytes(service)
        timer_path.write_bytes(timer)
        result = subprocess.run(
            ["systemd-analyze", "verify", str(service_path), str(timer_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_activation_reports_remaining_files(self) -> None:
        with patch(
            "zyfeed.scheduler._systemctl",
            side_effect=["Version=257", ZyFeedError("reload failure")],
        ):
            with self.assertRaisesRegex(ZyFeedError, "Unit files remain"):
                enable_schedule(self.config)
        self.assertTrue((unit_directory() / SERVICE).is_file())
        self.assertTrue((unit_directory() / TIMER).is_file())

    def test_disable_stops_timer_without_removing_unit_files(self) -> None:
        with patch("zyfeed.scheduler._systemctl") as ctl:
            enable_schedule(self.config)
            ctl.reset_mock()
            disable_schedule()
        ctl.assert_called_once_with("disable", "--now", TIMER)
        self.assertTrue((unit_directory() / TIMER).exists())

    def test_status_is_read_only(self) -> None:
        status = "LoadState=not-found\nActiveState=inactive"
        with patch("zyfeed.scheduler._systemctl", return_value=status):
            self.assertEqual(schedule_status(), status)
        self.assertFalse(unit_directory().exists())

    def test_enable_requires_confirmation_and_declining_changes_nothing(self) -> None:
        with (
            patch("builtins.input", return_value="no"),
            patch("zyfeed.cli.enable_schedule") as enable,
            self.assertLogs("zyfeed.cli", level="INFO"),
        ):
            self.assertEqual(
                main(["schedule", "enable", "--config", str(self.config)]), 0
            )
        enable.assert_not_called()
        self.assertFalse(unit_directory().exists())

    def test_confirmation_eof_is_not_authorization(self) -> None:
        with (
            patch("builtins.input", side_effect=EOFError),
            patch("zyfeed.cli.enable_schedule") as enable,
            self.assertLogs("zyfeed.cli", level="ERROR"),
        ):
            self.assertEqual(main(["schedule", "enable"]), 1)
        enable.assert_not_called()

    def test_yes_flag_is_explicit_authorization(self) -> None:
        with (
            patch("builtins.input") as prompt,
            patch("zyfeed.cli.enable_schedule") as enable,
        ):
            self.assertEqual(
                main(["schedule", "enable", "--config", str(self.config), "--yes"]),
                0,
            )
        prompt.assert_not_called()
        enable.assert_called_once_with(self.config)

    def test_confirmed_disable_and_read_only_status(self) -> None:
        with (
            patch("builtins.input", return_value="yes"),
            patch("zyfeed.cli.disable_schedule") as disable,
        ):
            self.assertEqual(main(["schedule", "disable"]), 0)
        disable.assert_called_once()
        output = io.StringIO()
        with (
            patch("builtins.input") as prompt,
            patch("zyfeed.cli.schedule_status", return_value="ActiveState=inactive"),
            redirect_stdout(output),
        ):
            self.assertEqual(main(["schedule", "status"]), 0)
        prompt.assert_not_called()
        self.assertIn("ActiveState=inactive", output.getvalue())

    def test_systemctl_failures_and_timeouts_are_reported(self) -> None:
        for outcome in (
            FileNotFoundError(),
            subprocess.TimeoutExpired("systemctl", 30),
            subprocess.CompletedProcess([], 1, "", "Failed to connect to bus"),
        ):
            with self.subTest(outcome=outcome):
                arguments = (
                    {"return_value": outcome}
                    if isinstance(outcome, subprocess.CompletedProcess)
                    else {"side_effect": outcome}
                )
                with patch("zyfeed.scheduler.subprocess.run", **arguments):
                    with self.assertRaises(ZyFeedError):
                        schedule_status()

    def test_relative_xdg_configuration_directory_is_rejected(self) -> None:
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative"}):
            with self.assertRaisesRegex(ZyFeedError, "absolute"):
                unit_directory()


if __name__ == "__main__":
    unittest.main()
