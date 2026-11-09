"""Manage only explicitly requested, ZyFeed-owned systemd user units."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from zyfeed import ZyFeedError
from zyfeed.config import load_config
from zyfeed.io import atomic_write, read_text
from zyfeed.rules import parse_overrides

LOGGER = logging.getLogger(__name__)
MARKER = "# Managed by ZyFeed.\n"
SERVICE = "zyfeed.service"
TIMER = "zyfeed.timer"


def unit_directory() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    root = Path(configured) if configured else Path.home() / ".config"
    if not root.is_absolute():
        raise ZyFeedError("XDG_CONFIG_HOME must be an absolute path")
    return root / "systemd" / "user"


def _unit_value(value: str, *, command: bool = False) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ZyFeedError("systemd paths must not contain control characters")
    escaped = value.replace("%", "%%")
    if not command:
        if value != value.strip():
            raise ZyFeedError("systemd working directory must not end with whitespace")
        return escaped
    escaped = escaped.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
    return f'"{escaped}"'


def render_units(config_path: Path) -> tuple[bytes, bytes]:
    config_path = config_path.resolve()
    executable = str(Path(sys.executable).absolute())
    arguments: tuple[str, ...]
    if getattr(sys, "frozen", False) is True:
        working_directory = config_path.parent
        arguments = (executable, "update", "--config", str(config_path))
    else:
        working_directory = Path(__file__).resolve().parent.parent
        arguments = (
            executable,
            "-B",
            "-m",
            "zyfeed",
            "update",
            "--config",
            str(config_path),
        )
    command = " ".join(_unit_value(argument, command=True) for argument in arguments)
    service = (
        MARKER + "[Unit]\n"
        "Description=Refresh and publish ZyFeed blocklists\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={_unit_value(str(working_directory))}\n"
        f"ExecStart={command}\n"
        "Environment=PYTHONDONTWRITEBYTECODE=1\n"
        "UMask=0027\n"
        "NoNewPrivileges=true\n"
    )
    timer = (
        MARKER + "[Unit]\n"
        "Description=Check ZyFeed refresh deadlines every 15 minutes\n\n"
        "[Timer]\n"
        "OnCalendar=*:0/15\n"
        "Persistent=true\n"
        f"Unit={SERVICE}\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service.encode("utf-8"), timer.encode("utf-8")


def _systemctl(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "--no-pager", *arguments],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as error:
        raise ZyFeedError("automatic updates require systemd and systemctl") from error
    except subprocess.TimeoutExpired as error:
        raise ZyFeedError("systemctl --user timed out") from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ZyFeedError(
            f"systemctl --user {' '.join(arguments)} failed "
            f"(exit {result.returncode}): {detail}. "
            "A running systemd user manager is required."
        )
    return result.stdout.strip()


def _check_owned(path: Path) -> None:
    if path.is_symlink():
        raise ZyFeedError(f"refusing to replace or manage a symlink unit: {path}")
    if path.exists() and not read_text(path).startswith(MARKER):
        raise ZyFeedError(f"refusing to manage a unit not created by ZyFeed: {path}")


def enable_schedule(config_path: Path) -> None:
    config_path = config_path.resolve()
    config = load_config(config_path)
    for feed in config.feeds:
        if feed.overrides is not None:
            parse_overrides(read_text(feed.overrides), source=str(feed.overrides))
    directory = unit_directory()
    service_path, timer_path = directory / SERVICE, directory / TIMER
    _check_owned(service_path)
    _check_owned(timer_path)
    service, timer = render_units(config_path)
    _systemctl("show", "--property=Version")
    atomic_write(service_path, service)
    atomic_write(timer_path, timer)
    try:
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", TIMER)
    except ZyFeedError as error:
        raise ZyFeedError(
            f"timer activation did not complete: {error} "
            f"Unit files remain in {directory}; inspect 'schedule status' "
            "and use 'schedule disable' if necessary."
        ) from error
    LOGGER.info(
        "enabled user timer %s; configuration %s; checks every 15 minutes",
        TIMER,
        config_path,
    )


def disable_schedule() -> None:
    directory = unit_directory()
    service_path, timer_path = directory / SERVICE, directory / TIMER
    _check_owned(service_path)
    _check_owned(timer_path)
    if not service_path.exists() and not timer_path.exists():
        LOGGER.info("no ZyFeed-managed user timer is installed in %s", directory)
        return
    if not service_path.exists() or not timer_path.exists():
        raise ZyFeedError(
            f"incomplete ZyFeed user units in {directory}; "
            "inspect them before disabling"
        )
    _systemctl("disable", "--now", TIMER)
    LOGGER.info(
        "disabled user timer %s; unit files retained; an active update may finish",
        TIMER,
    )


def schedule_status() -> str:
    return _systemctl(
        "show",
        TIMER,
        "--property=LoadState,ActiveState,SubState,UnitFileState,"
        "NextElapseUSecRealtime,LastTriggerUSec",
    )
