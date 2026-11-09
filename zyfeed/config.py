"""Typed TOML configuration with explicit validation of every supported key."""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from zyfeed import ZyFeedError
from zyfeed.io import validate_source_url
from zyfeed.rules import InputFormat, parse_duration


@dataclass(frozen=True, slots=True)
class FeedConfig:
    name: str
    url: str
    input_format: InputFormat = InputFormat.AUTO
    overrides: Path | None = None
    comments: bool = False
    allow_empty: bool = False


@dataclass(frozen=True, slots=True)
class AppConfig:
    output_dir: Path
    state_dir: Path
    feeds: tuple[FeedConfig, ...]
    default_expires: int = 86400
    timeout: float = 30


def mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ZyFeedError(f"{context} must be a table/object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ZyFeedError(f"{context} keys must be strings")
        result[key] = item
    return result


def string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ZyFeedError(f"{context} must be a non-empty string")
    return value


def boolean(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise ZyFeedError(f"{context} must be true or false")
    return value


def _known_keys(data: dict[str, object], keys: set[str], context: str) -> None:
    unknown = data.keys() - keys
    if unknown:
        raise ZyFeedError(f"{context}: unknown keys: {', '.join(sorted(unknown))}")


def _path(value: object, base: Path, context: str) -> Path:
    path = Path(string(value, context)).expanduser()
    return (base / path).resolve()


def load_config(path: Path) -> AppConfig:
    try:
        with path.open("rb") as stream:
            root = mapping(tomllib.load(stream), "configuration")
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ZyFeedError(f"cannot load configuration {path}: {error}") from error
    _known_keys(
        root,
        {"output_dir", "state_dir", "feeds", "default_expires", "timeout"},
        "configuration",
    )
    base = path.resolve().parent
    output_dir = _path(root.get("output_dir"), base, "output_dir")
    state_dir = _path(root.get("state_dir"), base, "state_dir")
    if state_dir.is_relative_to(output_dir):
        raise ZyFeedError("state_dir must be outside output_dir")
    default_expires = parse_duration(
        string(root.get("default_expires", "24 hours"), "default_expires")
    )
    timeout = root.get("timeout", 30)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ZyFeedError("timeout must be a positive, finite number of seconds")
    rows = root.get("feeds")
    if not isinstance(rows, list) or not rows:
        raise ZyFeedError("configuration needs at least one [[feeds]] table")
    feeds: list[FeedConfig] = []
    names: set[str] = set()
    for number, row in enumerate(rows, 1):
        context = f"feeds[{number}]"
        data = mapping(row, context)
        _known_keys(
            data,
            {"name", "url", "format", "overrides", "comments", "allow_empty"},
            context,
        )
        name = string(data.get("name"), f"{context}.name")
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", name) is None:
            raise ZyFeedError(
                f"{context}.name needs 1-63 lowercase letters/digits, - or _"
            )
        if name in names:
            raise ZyFeedError(f"duplicate feed name: {name}")
        names.add(name)
        url = validate_source_url(string(data.get("url"), f"{context}.url"))
        try:
            input_format = InputFormat(
                string(data.get("format", "auto"), f"{context}.format")
            )
        except ValueError as error:
            raise ZyFeedError(
                f"{context}.format is not a supported input format"
            ) from error
        override_path = (
            _path(data["overrides"], base, f"{context}.overrides")
            if "overrides" in data
            else None
        )
        feeds.append(
            FeedConfig(
                name,
                url,
                input_format,
                override_path,
                boolean(data.get("comments", False), f"{context}.comments"),
                boolean(data.get("allow_empty", False), f"{context}.allow_empty"),
            )
        )
    targets = {
        target
        for feed in feeds
        for target in (
            output_dir / f"{feed.name}.txt",
            output_dir / f"{feed.name}.json",
            state_dir / f"{feed.name}.json",
        )
    }
    if path.resolve() in targets or any(feed.overrides in targets for feed in feeds):
        raise ZyFeedError(
            "configuration/override files must not also be generated outputs"
        )
    return AppConfig(
        output_dir, state_dir, tuple(feeds), default_expires, float(timeout)
    )
