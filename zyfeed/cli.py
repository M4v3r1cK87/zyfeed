"""Command-line interface; all diagnostics go to stderr, never into the TXT."""

from __future__ import annotations

import argparse
import logging
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path

from zyfeed import ZyFeedError, __version__
from zyfeed.config import load_config
from zyfeed.io import atomic_write, download, read_text
from zyfeed.rules import (
    InputFormat,
    convert_text,
    parse_duration,
    parse_overrides,
)
from zyfeed.scheduler import (
    disable_schedule,
    enable_schedule,
    schedule_status,
    unit_directory,
)
from zyfeed.updater import expiry_time, metadata_bytes, update_all, utc_now

LOGGER = logging.getLogger(__name__)


class Arguments(argparse.Namespace):
    command: str = ""
    source: str = ""
    output: Path | None = None
    metadata: Path | None = None
    overrides: Path | None = None
    input_format: InputFormat = InputFormat.AUTO
    comments: bool = False
    allow_empty: bool = False
    default_expires: str = "24 hours"
    timeout: float = 30
    config: Path = Path("zyfeed.toml")
    force: bool = False
    verbose: bool = False
    schedule_action: str = ""
    yes: bool = False


def _arguments(argv: Sequence[str] | None) -> Arguments:
    frozen = getattr(sys, "frozen", False) is True
    program = (
        shlex.quote(str(Path(sys.executable).absolute()))
        if frozen
        else "./run"
        if Path(sys.argv[0]).name == "run"
        else "zyfeed"
    )

    def examples(text: str) -> str:
        return text.replace("./run", program) if frozen else text

    parser = argparse.ArgumentParser(
        prog=program,
        description="Convert and refresh blocklists in Zyxel DNS/URL format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=examples(
            "One conversion, with an optional override file (no TOML needed):\n"
            "  ./run convert input.txt --overrides overrides.txt -o blocklist.txt\n"
            "\n"
            "Repeated updates using saved settings:\n"
            "  ./run update --config zyfeed.toml\n"
            '  Set overrides = "overrides.txt" inside each [[feeds]] block.\n'
            "\n"
            "Optional automatic updates (asks for confirmation):\n"
            "  ./run schedule enable --config zyfeed.toml\n"
            "  ./run schedule status\n"
            "  ./run schedule disable\n"
            "\n"
            "TOML is a plain-text settings file: source URLs, output folders,\n"
            "and optional override files. pyproject.toml is for developers,\n"
            "not your feed configuration.\n"
            "\n"
            "More details:\n"
            "  ./run convert --help\n"
            "  ./run update --help\n"
            "  ./run schedule --help\n"
            "\n"
            "Bare convert, update and schedule commands show their guides.\n"
            "\n"
            "Unsupported browser rules are errors.\n"
            "Existing exports are not replaced on conversion errors."
        ),
    )
    parser.add_argument("--version", action="version", version=f"ZyFeed {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable debug logging"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    convert = commands.add_parser(
        "convert",
        help="convert one HTTP(S) URL or local UTF-8 file",
        description=(
            "Write one entry per line; validate everything before publishing. "
            "No TOML configuration is required for this command."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=examples(
            "Examples:\n"
            "  # Convert the local synthetic example and print TXT to the terminal:\n"
            "  ./run convert examples/blocklist.adblock.txt\n"
            "\n"
            "  # Convert your local list with additions/removals and save it:\n"
            "  ./run convert input.txt --overrides overrides.txt -o blocklist.txt\n"
            "\n"
            "  # Download a source and save it (replace this placeholder URL):\n"
            "  ./run convert 'https://example.org/blocklist.txt' -o blocklist.txt\n"
            "\n"
            "  # Select the input syntax explicitly:\n"
            "  ./run convert input.txt --format adblock -o blocklist.txt\n"
            "\n"
            "Replace example filenames with your own files.\n"
            "Saving blocklist.txt also writes metadata to blocklist.json.\n"
            "Without -o, only TXT goes to stdout; diagnostics go to stderr.\n"
            "convert runs once: it does not cache or schedule future updates.\n"
            "\n"
            "Overrides: '+ hostname' adds an entry; '- pattern' removes entry hosts.\n"
            "Removals win. They are NOT allow rules inside broader wildcard entries."
        ),
    )
    convert.add_argument("source", help="HTTP(S) URL or local source file")
    convert.add_argument(
        "-o", "--output", type=Path, help="output .txt file; default: stdout"
    )
    convert.add_argument(
        "--metadata", type=Path, help="metadata JSON; default: next to an output file"
    )
    convert.add_argument(
        "--overrides",
        type=Path,
        help="UTF-8 file with + / - hostname or wildcard rules",
    )
    convert.add_argument(
        "--format",
        dest="input_format",
        type=InputFormat,
        choices=list(InputFormat),
        default=InputFormat.AUTO,
        help="source syntax (default: auto)",
    )
    convert.add_argument(
        "--comments", action="store_true", help="include # metadata headers in TXT"
    )
    convert.add_argument(
        "--allow-empty",
        action="store_true",
        help="explicitly allow an empty converted export",
    )
    convert.add_argument(
        "--default-expires",
        default="24 hours",
        help="fallback when Expires is absent (default: 24 hours)",
    )
    convert.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="HTTP timeout in seconds (default: 30)",
    )
    update = commands.add_parser(
        "update",
        help="refresh due feeds or apply changed overrides from cache",
        description=(
            "Run this command with a timer/cron. "
            "Expires controls source checks per feed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=examples(
            "Examples:\n"
            "  # Show this guide without updating anything:\n"
            "  ./run update\n"
            "\n"
            "  # Use an explicitly selected settings file:\n"
            "  ./run update --config zyfeed.toml\n"
            "\n"
            "  # Check sources now, even before their Expires deadline:\n"
            "  ./run update --config zyfeed.toml --force\n"
            "\n"
            "Pass --config to execute an update. --force without --config\n"
            "explicitly checks sources using zyfeed.toml in the current directory.\n"
            "\n"
            "Create and customize your settings before running these commands:\n"
            "  cp examples/zyfeed.example.toml zyfeed.toml\n"
            "  cp examples/overrides.example.txt overrides.txt\n"
            "\n"
            "Without --force, only due sources are checked; changed overrides\n"
            "can rebuild from cache. --force still uses HTTP cache validators.\n"
            "This command runs once and exits; use schedule for automatic checks.\n"
            "\n"
            "TOML stores settings so you do not repeat them on the command line.\n"
            "Each [[feeds]] block selects a source and its optional override file:\n"
            "\n"
            '  output_dir = "var/output"\n'
            '  state_dir = "var/state"\n'
            "\n"
            "  [[feeds]]\n"
            '  name = "example"\n'
            '  url = "https://example.org/blocklist.txt"\n'
            '  overrides = "overrides.txt"\n'
            "\n"
            "The example.org URL is a placeholder: replace it with your source.\n"
            "Paths inside TOML are relative to that TOML file.\n"
            "Omit overrides to use no local additions/removals.\n"
            "Template: examples/zyfeed.example.toml\n"
            "Do not edit pyproject.toml to configure feeds."
        ),
    )
    update.add_argument(
        "--config",
        type=Path,
        default=Path("zyfeed.toml"),
        help="TOML configuration (default: zyfeed.toml)",
    )
    update.add_argument(
        "--force",
        action="store_true",
        help="check sources now, still using HTTP cache validators",
    )
    schedule = commands.add_parser(
        "schedule",
        help="enable, inspect or disable confirmed systemd user updates",
        description=(
            "Manage a timer for the current user's systemd manager. "
            "This does not change system-wide services."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=examples(
            "Examples:\n"
            "  # After configuring and testing your feeds, enable automatic checks:\n"
            "  ./run schedule enable --config zyfeed.toml\n"
            "\n"
            "  # Inspect the timer without changing anything:\n"
            "  ./run schedule status\n"
            "\n"
            "  # Stop future automatic checks:\n"
            "  ./run schedule disable\n"
            "\n"
            "enable and disable ask you to type 'yes'. Only add --yes when you\n"
            "explicitly want to authorize a non-interactive change.\n"
            "The user timer runs update every 15 minutes using the same TOML\n"
            "sources and override files. A running systemd user manager is required.\n"
            "\n"
            "What actually happens with multiple lists:\n"
            "  One user timer and one service cover the entire configuration.\n"
            "  Five feeds produce five TXT exports, "
            "not five timers or one merged list.\n"
            "  update visits feeds sequentially and checks each stored deadline.\n"
            "  Expires in each source sets its interval; missing Expires uses\n"
            "  default_expires (24 hours by default). Unchanged, not-due feeds\n"
            "  make no request. Due feeds use conditional HTTP downloads.\n"
            "  HTTP 304 reuses the cached source; HTTP 200 supplies new source text.\n"
            "  Convert, apply overrides and validate before publishing.\n"
            "  Changed TXT is replaced atomically at the same path; unchanged TXT\n"
            "  is not rewritten. Metadata/cache record the next source check.\n"
            "  Download/conversion errors retain the last valid TXT; other feeds\n"
            "  continue. ZyFeed writes local TXT files; importing or sharing them\n"
            "  is handled outside ZyFeed.\n"
            "\n"
            "For an immediate manual refresh, run update separately.\n"
            "A persistent timer may also catch up a missed check after activation.\n"
            "For action-specific examples: ./run schedule enable --help"
        ),
    )
    actions = schedule.add_subparsers(dest="schedule_action", required=True)
    enable = actions.add_parser(
        "enable",
        help="confirm and enable 15-minute checks",
        description="Enable a systemd user timer after explicit confirmation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=examples(
            "Examples:\n"
            "  ./run schedule enable --config zyfeed.toml\n"
            "  ./run schedule enable --config /path/to/zyfeed.toml --yes\n"
            "\n"
            "First configure your sources and test ./run update --config zyfeed.toml.\n"
            "Without --yes, type 'yes' at the confirmation prompt.\n"
            "The timer checks every 15 minutes; Expires controls source refreshes.\n"
            "All configured feeds share this one timer, with independent deadlines.\n"
            "It reuses the TOML override settings and requires "
            "a systemd user manager.\n"
            "Check the result with ./run schedule status."
        ),
    )
    enable.add_argument(
        "--config",
        type=Path,
        default=Path("zyfeed.toml"),
        help="your TOML configuration (default: zyfeed.toml)",
    )
    disable = actions.add_parser(
        "disable",
        help="confirm and stop future checks",
        description="Stop future checks after explicit confirmation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=examples(
            "Examples:\n"
            "  ./run schedule disable\n"
            "  ./run schedule disable --yes\n"
            "\n"
            "Without --yes, type 'yes' at the confirmation prompt.\n"
            "Unit files and exported lists are retained; an active update may finish."
        ),
    )
    for action in (enable, disable):
        action.add_argument(
            "--yes", action="store_true", help="explicitly confirm without a prompt"
        )
    actions.add_parser(
        "status",
        help="show current user timer state without changes",
        description="Inspect the current user's timer without changing it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=examples(
            "Example:\n"
            "  ./run schedule status\n"
            "\n"
            "Shows whether the timer is installed/active and its next check.\n"
            "It does not download sources, regenerate exports or require confirmation."
        ),
    )
    result = Arguments()
    supplied = list(sys.argv[1:] if argv is None else argv)
    if (
        supplied
        and supplied[-1] in {"convert", "update", "schedule"}
        and all(token in {"-v", "--verbose"} for token in supplied[:-1])
    ):
        supplied.append("--help")
    parser.parse_args(supplied or ["--help"], namespace=result)
    return result


def _convert(args: Arguments) -> None:
    output = (
        args.output if args.output is not None and args.output != Path("-") else None
    )
    metadata = args.metadata or (output.with_suffix(".json") if output else None)
    if output and output.suffix.lower() != ".txt":
        raise ZyFeedError("the firewall output file must have a .txt extension")
    targets = [path.resolve() for path in (output, metadata) if path is not None]
    if len(targets) != len(set(targets)):
        raise ZyFeedError("TXT and metadata outputs must use different paths")
    remote = "://" in args.source
    source_path = None if remote else Path(args.source).resolve()
    for protected in (
        source_path,
        args.overrides.resolve() if args.overrides else None,
    ):
        if protected is not None and protected in targets:
            raise ZyFeedError(
                "an output must not overwrite the source or override file"
            )
    fallback = parse_duration(args.default_expires)
    overrides = parse_overrides(
        read_text(args.overrides) if args.overrides else "",
        source=str(args.overrides) if args.overrides else "<overrides>",
    )
    if remote:
        fetched = download(args.source, timeout=args.timeout)
        if fetched.text is None:
            raise ZyFeedError("unexpected HTTP 304 without a cached source")
        text = fetched.text
    else:
        if source_path is None:
            raise ZyFeedError("missing source path")
        text = read_text(source_path)
    result = convert_text(
        text,
        source="<remote source>" if remote else args.source,
        input_format=args.input_format,
        overrides=overrides,
        allow_empty=args.allow_empty,
    )
    now = utc_now()
    expires = result.expires_seconds(fallback)
    public_metadata = metadata_bytes(
        result,
        generated_at=now,
        checked_at=now,
        next_check=expiry_time(now, expires),
        expires_seconds=expires,
    )
    rendered = result.render(comments=args.comments)
    if output:
        atomic_write(output, rendered)
    else:
        sys.stdout.write(rendered.decode("utf-8"))
    if metadata:
        atomic_write(metadata, public_metadata)
    LOGGER.info(
        "%d entries; added %d, removed %d",
        result.statistics.output_entries,
        result.statistics.added_entries,
        result.statistics.removed_entries,
    )


def _schedule(args: Arguments) -> None:
    if args.schedule_action == "status":
        print(schedule_status())
        return
    LOGGER.info(
        "%s current-user timer in %s; system-wide services are unchanged",
        args.schedule_action,
        unit_directory(),
    )
    if args.schedule_action == "enable":
        LOGGER.info(
            "configuration: %s; source checks every 15 minutes",
            args.config.resolve(),
        )
    if not args.yes:
        try:
            answer = input("Confirm this scheduler change? Type 'yes' to proceed: ")
        except EOFError as error:
            raise ZyFeedError(
                "confirmation required; use --yes only "
                "to explicitly authorize the change"
            ) from error
        if answer.strip().lower() != "yes":
            LOGGER.info("cancelled; no scheduler changes made")
            return
    if args.schedule_action == "enable":
        enable_schedule(args.config)
    else:
        disable_schedule()


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    try:
        if args.command == "convert":
            _convert(args)
            return 0
        if args.command == "schedule":
            _schedule(args)
            return 0
        results = update_all(load_config(args.config), force=args.force)
        return 1 if any(result.status == "failed" for result in results) else 0
    except (ZyFeedError, OSError) as error:
        LOGGER.error("%s", error)
        return 1
