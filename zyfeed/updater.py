"""Refresh due sources and rebuild changed overrides from the last valid source."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from zyfeed import ZyFeedError, __version__
from zyfeed.config import AppConfig, FeedConfig, mapping, string
from zyfeed.io import MAX_SOURCE_BYTES, atomic_write, download, read_text, update_lock
from zyfeed.rules import Conversion, convert_text, parse_overrides

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class State:
    source_url: str
    text: str
    etag: str | None
    last_modified: str | None
    checked_at: datetime
    next_check: datetime
    settings_sha256: str
    output_sha256: str
    metadata_sha256: str


@dataclass(frozen=True, slots=True)
class UpdateResult:
    name: str
    status: Literal["updated", "not-due", "failed"]
    error: str | None = None


def utc_now() -> datetime:
    return datetime.now(UTC)


def timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def expiry_time(checked_at: datetime, seconds: int) -> datetime:
    try:
        return checked_at + timedelta(seconds=seconds)
    except OverflowError as error:
        raise ZyFeedError(
            "Expires duration exceeds the supported date range"
        ) from error


def json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def metadata_bytes(
    result: Conversion,
    *,
    generated_at: datetime,
    checked_at: datetime,
    next_check: datetime,
    expires_seconds: int,
    feed_name: str | None = None,
) -> bytes:
    return json_bytes(
        {
            "schema_version": 1,
            "generator": {"name": "ZyFeed", "version": __version__},
            "feed": feed_name,
            "generated_at": timestamp(generated_at),
            "checked_at": timestamp(checked_at),
            "next_check": timestamp(next_check),
            "expires_seconds": expires_seconds,
            "upstream": result.metadata,
            "statistics": asdict(result.statistics),
        }
    )


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_digest(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except FileNotFoundError:
        return None
    return digest


def _date(value: object, context: str) -> datetime:
    try:
        result = datetime.fromisoformat(string(value, context))
    except ValueError as error:
        raise ZyFeedError(f"{context}: invalid timestamp") from error
    if result.tzinfo is None:
        raise ZyFeedError(f"{context}: timestamp needs a timezone")
    return result.astimezone(UTC)


def _optional_string(value: object, context: str) -> str | None:
    return None if value is None else string(value, context)


def _sha(value: object, context: str) -> str:
    result = string(value, context)
    if re.fullmatch(r"[a-f0-9]{64}", result) is None:
        raise ZyFeedError(f"{context}: invalid SHA-256 digest")
    return result


def _load_state(path: Path) -> State | None:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES * 3 + 100_001)
    except FileNotFoundError:
        return None
    if len(data) > MAX_SOURCE_BYTES * 3 + 100_000:
        raise ZyFeedError(f"{path}: cached state is too large")
    try:
        row = mapping(json.loads(data), f"state {path}")
    except (ValueError, UnicodeError) as error:
        raise ZyFeedError(
            f"{path}: invalid cached JSON; previous export retained"
        ) from error
    if type(row.get("schema_version")) is not int or row["schema_version"] != 1:
        raise ZyFeedError(f"{path}: unsupported cached state schema")
    checked = _date(row.get("checked_at"), "state.checked_at")
    next_check = _date(row.get("next_check"), "state.next_check")
    if next_check <= checked:
        raise ZyFeedError(f"{path}: invalid cached expiry")
    return State(
        source_url=string(row.get("source_url"), "state.source_url"),
        text=string(row.get("text"), "state.text"),
        etag=_optional_string(row.get("etag"), "state.etag"),
        last_modified=_optional_string(row.get("last_modified"), "state.last_modified"),
        checked_at=checked,
        next_check=next_check,
        settings_sha256=_sha(row.get("settings_sha256"), "state.settings_sha256"),
        output_sha256=_sha(row.get("output_sha256"), "state.output_sha256"),
        metadata_sha256=_sha(row.get("metadata_sha256"), "state.metadata_sha256"),
    )


def _save_state(path: Path, state: State) -> None:
    row = asdict(state)
    row["checked_at"] = timestamp(state.checked_at)
    row["next_check"] = timestamp(state.next_check)
    row["schema_version"] = 1
    atomic_write(path, json_bytes(row), mode=0o600)


def _settings_hash(config: AppConfig, feed: FeedConfig, overrides: str) -> str:
    return _digest(
        json_bytes(
            {
                "version": __version__,
                "format": feed.input_format,
                "comments": feed.comments,
                "allow_empty": feed.allow_empty,
                "default_expires": config.default_expires,
                "overrides": overrides,
            }
        )
    )


def _fetch(
    config: AppConfig,
    feed: FeedConfig,
    cached: State | None,
    now: datetime,
) -> tuple[str, str | None, str | None, datetime]:
    result = download(
        feed.url,
        timeout=config.timeout,
        etag=cached.etag if cached else None,
        last_modified=cached.last_modified if cached else None,
    )
    if result.text is None:
        if cached is None:
            raise ZyFeedError("HTTP 304 received without a valid cached source")
        text = cached.text
    else:
        text = result.text
    return text, result.etag, result.last_modified, now


def update_feed(
    config: AppConfig, feed: FeedConfig, *, now: datetime, force: bool = False
) -> UpdateResult:
    state_path = config.state_dir / f"{feed.name}.json"
    output_path = config.output_dir / f"{feed.name}.txt"
    metadata_path = config.output_dir / f"{feed.name}.json"
    loaded = _load_state(state_path)
    cached = loaded if loaded and loaded.source_url == feed.url else None
    override_text = read_text(feed.overrides) if feed.overrides else ""
    overrides = parse_overrides(
        override_text, source=str(feed.overrides) if feed.overrides else "<overrides>"
    )
    settings_hash = _settings_hash(config, feed, override_text)
    output_hash = _file_digest(output_path)
    metadata_hash = _file_digest(metadata_path)
    due = force or cached is None or now >= cached.next_check
    intact = (
        cached is not None
        and output_hash == cached.output_sha256
        and metadata_hash == cached.metadata_sha256
    )
    if cached and not due and intact and cached.settings_sha256 == settings_hash:
        LOGGER.info(
            "%s: not due; next source check %s", feed.name, timestamp(cached.next_check)
        )
        return UpdateResult(feed.name, "not-due")

    changed_output = cached is not None and output_hash not in (
        None,
        cached.output_sha256,
    )
    if due or changed_output:
        text, etag, last_modified, checked_at = _fetch(config, feed, cached, now)
    else:
        if cached is None:
            raise ZyFeedError("no cached source available")
        text, etag, last_modified, checked_at = (
            cached.text,
            cached.etag,
            cached.last_modified,
            cached.checked_at,
        )
    result = convert_text(
        text,
        source=feed.name,
        input_format=feed.input_format,
        overrides=overrides,
        allow_empty=feed.allow_empty,
    )
    expires = result.expires_seconds(config.default_expires)
    next_check = expiry_time(checked_at, expires)
    if checked_at != now and next_check <= now:
        text, etag, last_modified, checked_at = _fetch(config, feed, cached, now)
        result = convert_text(
            text,
            source=feed.name,
            input_format=feed.input_format,
            overrides=overrides,
            allow_empty=feed.allow_empty,
        )
        expires = result.expires_seconds(config.default_expires)
        next_check = expiry_time(checked_at, expires)

    output = result.render(comments=feed.comments)
    public_metadata = metadata_bytes(
        result,
        generated_at=now,
        checked_at=checked_at,
        next_check=next_check,
        expires_seconds=expires,
        feed_name=feed.name,
    )
    new_output_hash = _digest(output)
    new_metadata_hash = _digest(public_metadata)
    if output_hash != new_output_hash:
        atomic_write(output_path, output)
    if metadata_hash != new_metadata_hash:
        atomic_write(metadata_path, public_metadata)
    _save_state(
        state_path,
        State(
            feed.url,
            text,
            etag,
            last_modified,
            checked_at,
            next_check,
            settings_hash,
            new_output_hash,
            new_metadata_hash,
        ),
    )
    LOGGER.info(
        "%s: published %d entries; added %d, removed %d; next source check %s",
        feed.name,
        result.statistics.output_entries,
        result.statistics.added_entries,
        result.statistics.removed_entries,
        timestamp(next_check),
    )
    return UpdateResult(feed.name, "updated")


def update_all(
    config: AppConfig, *, force: bool = False, now: datetime | None = None
) -> list[UpdateResult]:
    now = now or utc_now()
    if now.tzinfo is None:
        raise ZyFeedError("update time must include a timezone")
    results: list[UpdateResult] = []
    with update_lock(config.state_dir):
        for feed in config.feeds:
            try:
                results.append(update_feed(config, feed, now=now, force=force))
            except (ZyFeedError, OSError) as error:
                LOGGER.error("%s: update failed: %s", feed.name, error)
                results.append(UpdateResult(feed.name, "failed", str(error)))
    return results
