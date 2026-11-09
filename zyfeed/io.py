"""Bounded downloads, atomic files and a process lock for scheduled updates."""

from __future__ import annotations

import fcntl
import http.client
import math
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from zyfeed import ZyFeedError, __version__

MAX_SOURCE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Download:
    text: str | None
    etag: str | None = None
    last_modified: str | None = None


def validate_source_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise ZyFeedError(f"invalid source URL: {error}") from error
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or not value.isascii()
        or any(character.isspace() for character in value)
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ZyFeedError(
            "source must be an ASCII HTTP(S) URL without credentials or a fragment"
        )
    return value


def decode_text(data: bytes, *, source: str) -> str:
    if len(data) > MAX_SOURCE_BYTES:
        raise ZyFeedError(f"{source}: source exceeds the 10 MiB size limit")
    try:
        return data.decode("utf-8-sig")
    except UnicodeError as error:
        raise ZyFeedError(f"{source}: source must be UTF-8 text") from error


def read_text(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
    except OSError as error:
        raise ZyFeedError(f"cannot read {path}: {error}") from error
    return decode_text(data, source=str(path))


def download(
    url: str,
    *,
    timeout: float = 30,
    etag: str | None = None,
    last_modified: str | None = None,
) -> Download:
    validate_source_url(url)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ZyFeedError("download timeout must be a positive, finite number")
    if any(
        value and ("\r" in value or "\n" in value) for value in (etag, last_modified)
    ):
        raise ZyFeedError("HTTP cache validators must not contain line breaks")
    headers = {
        "User-Agent": f"ZyFeed/{__version__}",
        "Accept": "text/plain, application/octet-stream;q=0.9, */*;q=0.1",
        "Accept-Encoding": "identity",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = Request(url, headers=headers)
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            validate_source_url(response.geturl())
            if response.status != 200:
                raise ZyFeedError(f"unexpected HTTP status {response.status}")
            encoding = response.headers.get("Content-Encoding", "identity").lower()
            if encoding not in {"identity", ""}:
                raise ZyFeedError(f"server ignored identity encoding: {encoding}")
            data = response.read(MAX_SOURCE_BYTES + 1)
            text = decode_text(data, source="download")
            if not text.strip():
                raise ZyFeedError("server returned an empty blocklist")
            return Download(
                text,
                response.headers.get("ETag"),
                response.headers.get("Last-Modified"),
            )
    except HTTPError as error:
        try:
            if error.code == 304 and (etag or last_modified):
                return Download(
                    None,
                    error.headers.get("ETag", etag),
                    error.headers.get("Last-Modified", last_modified),
                )
            raise ZyFeedError(
                f"blocklist download failed: HTTP {error.code}"
            ) from error
        finally:
            error.close()
    except (URLError, OSError, http.client.HTTPException) as error:
        raise ZyFeedError(f"blocklist download failed: {error}") from error


def atomic_write(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        raise ZyFeedError(f"cannot atomically publish {path}: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def update_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_dir / ".lock").open("a+b") as stream:
        os.fchmod(stream.fileno(), 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ZyFeedError(
                "another update is already using this state directory"
            ) from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
