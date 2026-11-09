from __future__ import annotations

import json
import tempfile
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from zyfeed import ZyFeedError
from zyfeed.config import AppConfig, FeedConfig
from zyfeed.io import Download, download
from zyfeed.updater import update_all

START = datetime(2026, 11, 9, 12, tzinfo=UTC)


@dataclass
class Response:
    body: bytes = b"! Expires: 8 hours\n||tracker.example.org^\n"
    etag: str | None = '"v1"'
    last_modified: str | None = None
    status: int = 200
    requests: list[dict[str, str]] = field(default_factory=list)


@contextmanager
def serve(response: Response) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            response.requests.append(dict(self.headers.items()))
            unchanged = (
                response.etag is not None
                and self.headers.get("If-None-Match") == response.etag
            ) or (
                response.etag is None
                and response.last_modified is not None
                and self.headers.get("If-Modified-Since") == response.last_modified
            )
            status = 304 if response.status == 200 and unchanged else response.status
            self.send_response(status)
            if response.etag:
                self.send_header("ETag", response.etag)
            if response.last_modified:
                self.send_header("Last-Modified", response.last_modified)
            if status != 304:
                self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            if status != 304:
                self.wfile.write(response.body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/list.txt"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def configuration(root: Path, url: str, overrides: Path | None = None) -> AppConfig:
    return AppConfig(
        root / "www",
        root / "state",
        (FeedConfig("test", url, overrides=overrides),),
    )


class UpdateTests(unittest.TestCase):
    def test_five_feeds_keep_independent_deadlines_and_stable_txt_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feeds = tuple(
                FeedConfig(f"list-{letter}", f"https://example.org/{letter}.txt")
                for letter in "abcde"
            )
            config = AppConfig(root / "www", root / "state", feeds)
            replies = [
                Download(
                    (f"! Expires: {expires}\n" if expires else "")
                    + f"||{letter}.example.org^\n",
                    f'"{letter}"',
                )
                for letter, expires in zip(
                    "abcde",
                    ("1 hour", "2 hours", "8 hours", "1 day", None),
                    strict=True,
                )
            ]
            replies.extend(
                [Download(None, '"a"'), Download(None, '"a"'), Download(None, '"b"')]
            )
            with patch("zyfeed.updater.download", side_effect=replies) as fetch:
                first = update_all(config, now=START)
                self.assertEqual([item.status for item in first], ["updated"] * 5)
                original_mtimes = {
                    feed.name: (config.output_dir / f"{feed.name}.txt")
                    .stat()
                    .st_mtime_ns
                    for feed in feeds
                }
                skipped = update_all(config, now=START + timedelta(minutes=15))
                self.assertEqual([item.status for item in skipped], ["not-due"] * 5)
                self.assertEqual(fetch.call_count, 5)
                hourly = update_all(config, now=START + timedelta(hours=1))
                self.assertEqual(
                    [item.status for item in hourly], ["updated"] + ["not-due"] * 4
                )
                self.assertEqual(fetch.call_count, 6)
                two_hourly = update_all(config, now=START + timedelta(hours=2))
                self.assertEqual(
                    [item.status for item in two_hourly],
                    ["updated"] * 2 + ["not-due"] * 3,
                )
                self.assertEqual(
                    [call.args[0] for call in fetch.call_args_list],
                    [feed.url for feed in feeds]
                    + [feeds[0].url, feeds[0].url, feeds[1].url],
                )
            self.assertEqual(len(list(config.output_dir.glob("*.txt"))), 5)
            self.assertEqual(len(list(config.output_dir.glob("*.json"))), 5)
            self.assertEqual(len(list(config.state_dir.glob("*.json"))), 5)
            for feed, hours in zip(feeds, (3, 4, 8, 24, 24), strict=True):
                state = json.loads((config.state_dir / f"{feed.name}.json").read_text())
                expected = START + timedelta(hours=hours)
                self.assertEqual(
                    state["next_check"], expected.isoformat().replace("+00:00", "Z")
                )
                self.assertEqual(
                    (config.output_dir / f"{feed.name}.txt").stat().st_mtime_ns,
                    original_mtimes[feed.name],
                )

    def test_expiry_skips_fetch_and_304_reuses_cached_source(self) -> None:
        response = Response()
        with tempfile.TemporaryDirectory() as directory, serve(response) as url:
            config = configuration(Path(directory), url)
            self.assertEqual(update_all(config, now=START)[0].status, "updated")
            output = config.output_dir / "test.txt"
            original_mtime = output.stat().st_mtime_ns
            self.assertEqual(
                update_all(config, now=START + timedelta(hours=1))[0].status, "not-due"
            )
            self.assertEqual(len(response.requests), 1)
            self.assertEqual(
                update_all(config, now=START + timedelta(hours=8))[0].status, "updated"
            )
            self.assertEqual(response.requests[-1]["If-None-Match"], '"v1"')
            self.assertEqual(output.stat().st_mtime_ns, original_mtime)
            state_path = config.state_dir / "test.json"
            state = json.loads(state_path.read_text())
            self.assertEqual(state["next_check"], "2026-11-10T04:00:00Z")
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
            public = json.loads((config.output_dir / "test.json").read_text())
            self.assertNotIn("source_url", public)
            self.assertNotIn("text", public)
            self.assertEqual(public["statistics"]["output_entries"], 2)

    def test_override_edits_rebuild_before_expiry_without_network(self) -> None:
        response = Response()
        with tempfile.TemporaryDirectory() as directory, serve(response) as url:
            root = Path(directory)
            overrides = root / "overrides.txt"
            overrides.write_text("# empty\n")
            config = configuration(root, url, overrides)
            update_all(config, now=START)
            overrides.write_text("+ manual.example.org\n- tracker.example.org\n")
            result = update_all(config, now=START + timedelta(hours=1))
            self.assertEqual(result[0].status, "updated")
            self.assertEqual(len(response.requests), 1)
            self.assertEqual(
                (config.output_dir / "test.txt").read_text(),
                "*.tracker.example.org\nmanual.example.org\n",
            )
            state = json.loads((config.state_dir / "test.json").read_text())
            self.assertEqual(state["checked_at"], "2026-11-09T12:00:00Z")
            self.assertEqual(state["next_check"], "2026-11-09T20:00:00Z")

    def test_invalid_downloads_keep_output_metadata_and_state(self) -> None:
        response = Response()
        with tempfile.TemporaryDirectory() as directory, serve(response) as url:
            config = configuration(Path(directory), url)
            update_all(config, now=START)
            paths = (
                config.output_dir / "test.txt",
                config.output_dir / "test.json",
                config.state_dir / "test.json",
            )
            originals = [path.read_bytes() for path in paths]
            failures = (
                (200, b"||tracker.example.org^$third-party\n"),
                (200, b"<html>not a blocklist</html>"),
                (200, b""),
                (200, b"\xff"),
                (200, b"! Expires: never\n||tracker.example.org^\n"),
                (503, b"temporarily unavailable"),
            )
            for index, (status, body) in enumerate(failures):
                with self.subTest(status=status, body=body):
                    response.status, response.body = status, body
                    response.etag = f'"failure-{index}"'
                    with self.assertLogs("zyfeed.updater", level="ERROR"):
                        result = update_all(config, now=START, force=True)
                    self.assertEqual(result[0].status, "failed")
                    self.assertEqual([path.read_bytes() for path in paths], originals)

    def test_missing_output_rebuilds_from_cache(self) -> None:
        response = Response()
        with tempfile.TemporaryDirectory() as directory, serve(response) as url:
            config = configuration(Path(directory), url)
            update_all(config, now=START)
            output = config.output_dir / "test.txt"
            output.unlink()
            update_all(config, now=START + timedelta(minutes=5))
            self.assertTrue(output.exists())
            self.assertEqual(len(response.requests), 1)

    def test_changed_source_does_not_send_old_cache_validators(self) -> None:
        response = Response()
        with tempfile.TemporaryDirectory() as directory, serve(response) as url:
            config = configuration(Path(directory), url)
            update_all(config, now=START)
            new_feed = replace(config.feeds[0], url=url + "?different-source=1")
            update_all(replace(config, feeds=(new_feed,)), now=START)
            self.assertNotIn("If-None-Match", response.requests[-1])

    def test_shorter_fallback_ttl_triggers_an_immediate_source_check(self) -> None:
        response = Response(body=b"||tracker.example.org^\n")
        with tempfile.TemporaryDirectory() as directory, serve(response) as url:
            config = configuration(Path(directory), url)
            update_all(config, now=START)
            update_all(
                replace(config, default_expires=1800),
                now=START + timedelta(hours=1),
            )
            self.assertEqual(len(response.requests), 2)
            state = json.loads((config.state_dir / "test.json").read_text())
            self.assertEqual(state["next_check"], "2026-11-09T13:30:00Z")

    def test_force_checks_before_expiry_and_last_modified_is_supported(self) -> None:
        response = Response(etag=None, last_modified="Mon, 09 Nov 2026 10:00:00 GMT")
        with tempfile.TemporaryDirectory() as directory, serve(response) as url:
            config = configuration(Path(directory), url)
            update_all(config, now=START)
            update_all(config, now=START + timedelta(hours=1), force=True)
            self.assertEqual(len(response.requests), 2)
            self.assertEqual(
                response.requests[-1]["If-Modified-Since"], response.last_modified
            )

    def test_corrupt_state_is_an_explicit_failure(self) -> None:
        response = Response()
        with tempfile.TemporaryDirectory() as directory, serve(response) as url:
            config = configuration(Path(directory), url)
            update_all(config, now=START)
            original = (config.output_dir / "test.txt").read_bytes()
            (config.state_dir / "test.json").write_text("{invalid")
            with self.assertLogs("zyfeed.updater", level="ERROR"):
                result = update_all(config, now=START, force=True)
            self.assertEqual(result[0].status, "failed")
            self.assertEqual((config.output_dir / "test.txt").read_bytes(), original)
            self.assertEqual(len(response.requests), 1)

    def test_one_failed_feed_does_not_stop_the_others(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(
                root / "www",
                root / "state",
                (
                    FeedConfig("bad", "https://example.org/bad.txt"),
                    FeedConfig("good", "https://example.org/good.txt"),
                ),
            )
            responses = [Download("invalid"), Download("||tracker.example.org^\n")]
            with (
                patch("zyfeed.updater.download", side_effect=responses),
                self.assertLogs("zyfeed.updater", level="ERROR"),
            ):
                results = update_all(config, now=START)
            self.assertEqual([item.status for item in results], ["failed", "updated"])
            self.assertTrue((config.output_dir / "good.txt").is_file())
            self.assertFalse((config.output_dir / "bad.txt").exists())

    def test_invalid_override_preserves_the_previous_export(self) -> None:
        response = Response()
        with tempfile.TemporaryDirectory() as directory, serve(response) as url:
            root = Path(directory)
            overrides = root / "overrides.txt"
            overrides.write_text("# empty\n")
            config = configuration(root, url, overrides)
            update_all(config, now=START)
            original = (config.output_dir / "test.txt").read_bytes()
            overrides.write_text("invalid override")
            with self.assertLogs("zyfeed.updater", level="ERROR"):
                result = update_all(config, now=START, force=True)
            self.assertEqual(result[0].status, "failed")
            self.assertEqual((config.output_dir / "test.txt").read_bytes(), original)
            self.assertEqual(len(response.requests), 1)

    def test_unconditional_304_is_not_a_successful_empty_download(self) -> None:
        with serve(Response(status=304)) as url:
            with self.assertRaisesRegex(ZyFeedError, "HTTP 304"):
                download(url)

    def test_invalid_timeouts_are_rejected_before_fetching(self) -> None:
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ZyFeedError):
                download("https://example.org/list.txt", timeout=timeout)


if __name__ == "__main__":
    unittest.main()
