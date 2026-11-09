"""Exercise a frozen executable without Python or a real service manager on PATH."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from test_updater import Response, serve


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("version")
    parser.add_argument("--https-source")
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="zyfeed-binary-smoke-") as directory:
        root = Path(directory)
        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        calls = root / "systemctl.log"
        systemctl = fake_bin / "systemctl"
        systemctl.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$ZYFEED_SYSTEMCTL_LOG"\n'
            'case "$*" in\n'
            '  *--property=Version*) printf "Version=smoke-test\\n";;\n'
            '  *NextElapseUSecRealtime*) printf "ActiveState=active\\n";;\n'
            "esac\n",
            encoding="utf-8",
        )
        systemctl.chmod(0o755)
        environment = dict(
            os.environ,
            PATH=str(fake_bin),
            XDG_CONFIG_HOME=str(root / "user-config"),
            ZYFEED_SYSTEMCTL_LOG=str(calls),
        )
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)

        def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                [str(binary), *arguments],
                cwd=root,
                env=environment,
                input="",
                capture_output=True,
                text=True,
                timeout=90,
            )
            if result.returncode:
                raise AssertionError(
                    f"{arguments}: exit {result.returncode}\n{result.stderr}"
                )
            return result

        if invoke("--version").stdout.strip() != f"ZyFeed {args.version}":
            raise AssertionError("binary version differs from the release")
        for arguments in (
            (),
            ("convert",),
            ("update",),
            ("schedule",),
            ("schedule", "enable", "--help"),
            ("schedule", "disable", "--help"),
            ("schedule", "status", "--help"),
        ):
            result = invoke(*arguments)
            if "./run" in result.stdout or str(binary) not in result.stdout:
                raise AssertionError("binary help refers to the source launcher")
        source, overrides, output = (
            root / "input.txt",
            root / "overrides.txt",
            root / "converted.txt",
        )
        source.write_text("! Expires: 8 hours\n||tracker.example.org^\n")
        overrides.write_text("+ manual.example.org\n- tracker.example.org\n")
        invoke(
            "convert",
            str(source),
            "--overrides",
            str(overrides),
            "--output",
            str(output),
        )
        if output.read_bytes() != b"*.tracker.example.org\nmanual.example.org\n":
            raise AssertionError("standalone local conversion has incorrect output")
        if args.https_source:
            secure_output = root / "https.txt"
            invoke("convert", args.https_source, "--output", str(secure_output))
            if secure_output.read_bytes() != (
                b"*.metrics.example.net\n*.tracker.example.org\n"
                b"metrics.example.net\ntracker.example.org\n"
            ):
                raise AssertionError("standalone HTTPS download/conversion failed")

        response = Response()
        with serve(response) as url:
            config = root / "zyfeed.toml"
            config.write_text(
                'output_dir="output"\nstate_dir="state"\n'
                f'[[feeds]]\nname="example"\nurl="{url}"\n'
            )
            invoke("update", "--config", str(config))
            exported = root / "output" / "example.txt"
            if exported.read_bytes() != (
                b"*.tracker.example.org\ntracker.example.org\n"
            ):
                raise AssertionError("standalone update did not publish its source")
            modified = exported.stat().st_mtime_ns
            invoke("update", "--config", str(config))
            if len(response.requests) != 1:
                raise AssertionError("not-due binary update unexpectedly downloaded")
            invoke("update", "--config", str(config), "--force")
            if len(response.requests) != 2 or (
                response.requests[-1].get("If-None-Match") != response.etag
            ):
                raise AssertionError("binary conditional download did not use ETag")
            if exported.stat().st_mtime_ns != modified:
                raise AssertionError("unchanged binary export was rewritten")
            metadata = json.loads(exported.with_suffix(".json").read_text())
            if metadata["expires_seconds"] != 28800:
                raise AssertionError("standalone expiry metadata is incorrect")

            invoke("schedule", "enable", "--config", str(config), "--yes")
            units = root / "user-config" / "systemd" / "user"
            unit = (units / "zyfeed.service").read_text()
            line = next(
                line for line in unit.splitlines() if line.startswith("ExecStart=")
            )
            command = shlex.split(line.removeprefix("ExecStart="))
            if command != [str(binary), "update", "--config", str(config)]:
                raise AssertionError(f"invalid frozen unit command: {command}")
            if f"WorkingDirectory={root}\n" not in unit or "_MEI" in unit:
                raise AssertionError("frozen timer depends on an extracted directory")
            scheduled = subprocess.run(
                command,
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=90,
            )
            if scheduled.returncode:
                raise AssertionError(scheduled.stderr)
            invoke("schedule", "status")
            invoke("schedule", "disable", "--yes")
            if "disable --now zyfeed.timer" not in calls.read_text():
                raise AssertionError("binary did not disable the mocked timer")
        print(
            "PASS: standalone help, conversion, HTTP cache and persistent "
            "self-executable scheduling with no Python on PATH"
        )


if __name__ == "__main__":
    main()
