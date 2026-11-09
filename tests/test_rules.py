from __future__ import annotations

import unittest

from zyfeed import ZyFeedError
from zyfeed.rules import (
    InputFormat,
    convert_text,
    normalize_domain,
    parse_duration,
    parse_entry,
    parse_overrides,
)


class RuleTests(unittest.TestCase):
    def test_explicit_input_formats(self) -> None:
        for input_format in (
            InputFormat.ADBLOCK,
            InputFormat.ADGUARD,
            InputFormat.UBLOCK,
        ):
            with self.subTest(input_format=input_format):
                result = convert_text("||example.org^\n", input_format=input_format)
                self.assertEqual(result.render(), b"*.example.org\nexample.org\n")
        for input_format, text in (
            (InputFormat.DOMAINS, "*.example.org\n"),
            (InputFormat.HOSTS, "0.0.0.0 *.example.org\n"),
            (InputFormat.ZYXEL, "http://*.example.org/path\n"),
        ):
            with self.subTest(input_format=input_format):
                if input_format == InputFormat.HOSTS:
                    with self.assertRaises(ZyFeedError):
                        convert_text(text, input_format=input_format)
                else:
                    self.assertEqual(
                        len(convert_text(text, input_format=input_format).entries), 1
                    )

    def test_comments_are_optional(self) -> None:
        result = convert_text("! Title: Example\n||example.org^\n")
        self.assertNotIn(b"#", result.render())
        self.assertIn(b"# Title: Example\n", result.render(comments=True))
        self.assertIn(b"# ZyFeed entries: 2\n", result.render(comments=True))

    def test_adblock_metadata_and_subdomains(self) -> None:
        result = convert_text(
            "[Adblock Plus]\n! Title: Example\n! Expires: 8 hours\n"
            "||TRACKER.example.org^\n||tracker.example.org^\n"
        )
        self.assertEqual(
            result.render(), b"*.tracker.example.org\ntracker.example.org\n"
        )
        self.assertEqual(result.metadata["title"], "Example")
        self.assertEqual(result.expires_seconds(), 28800)
        self.assertEqual(result.statistics.duplicate_entries, 2)

    def test_domains_wildcards_idna_and_line_endings(self) -> None:
        result = convert_text(
            "\ufeff# Title: Example\r\nEXAMPLE.ORG.\r\n*.zyxel.*\r\n"
            "b\u00fccher.example\r\n"
        )
        self.assertEqual(
            result.render(), b"*.zyxel.*\nexample.org\nxn--bcher-kva.example\n"
        )

    def test_hosts_aliases_and_local_boilerplate(self) -> None:
        result = convert_text(
            "127.0.0.1 localhost localhost.localdomain\n"
            "ff02::1 ip6-allnodes\n"
            "0.0.0.0 ads.example.org metric.example.org # comment\n"
            ":: tracker.example.org\n",
            input_format=InputFormat.HOSTS,
        )
        self.assertEqual(len(result.entries), 3)
        self.assertNotIn("localhost", result.render().decode())

    def test_urls_preserve_scheme_path_and_query(self) -> None:
        entry = parse_entry("HTTP://*.Example.ORG:8080/path?q=value")
        self.assertEqual(entry.value, "http://*.example.org:8080/path?q=value")
        self.assertEqual(entry.host, "*.example.org")
        self.assertEqual(
            parse_entry("https://example.org").value, "https://example.org/"
        )

    def test_overrides_removal_precedence_and_no_allowlisting(self) -> None:
        overrides = parse_overrides(
            "+ extra.example.org\n- exact.example.org\n+ exact.example.org\n"
            "- allowed.example.org\n"
        )
        result = convert_text("exact.example.org\n*.example.org\n", overrides=overrides)
        self.assertEqual(result.render(), b"*.example.org\nextra.example.org\n")
        self.assertEqual(result.statistics.added_entries, 1)
        self.assertEqual(result.statistics.removed_entries, 1)

    def test_removal_wildcards_match_entries_and_url_hosts(self) -> None:
        result = convert_text(
            "example.org\n*.example.org\nsub.example.org\n"
            "http://other.example.org/path\n",
            overrides=parse_overrides("- *.example.org\n"),
        )
        self.assertEqual(result.render(), b"example.org\n")

    def test_helpful_line_number_for_unsupported_rules(self) -> None:
        bad_rules = (
            "@@||allowed.example.org^",
            "||ads.example.org^$third-party",
            "example.org##.advert",
            "/regular-expression/",
            "!#if env_chromium",
            "||*.example.org^",
            "|https://example.org/path|",
        )
        for line in bad_rules:
            with self.subTest(line=line):
                with self.assertRaisesRegex(ZyFeedError, "feed:2:"):
                    convert_text(f"! comment\n{line}\n", source="feed")

    def test_adblock_header_does_not_make_substring_rules_domains(self) -> None:
        with self.assertRaises(ZyFeedError):
            convert_text("[Adblock Plus 2.0]\nexample.org\n")

    def test_invalid_domains_and_urls_fail(self) -> None:
        for value in (
            "localhost",
            "bad_domain.example",
            "-bad.example",
            "example..org",
            "example.org..",
            "0.0.0.0",
            "https://user:password@example.org/",
            "https://example.org/#fragment",
            "https://example.org:99999/",
            "https://example.org/%zz",
            "https://example.org/a b",
        ):
            with self.subTest(value=value), self.assertRaises(ZyFeedError):
                parse_entry(value)

    def test_hosts_reject_real_address_mappings(self) -> None:
        with self.assertRaises(ZyFeedError):
            convert_text("192.0.2.1 ads.example.org\n", input_format=InputFormat.HOSTS)

    def test_overrides_require_an_operator_and_valid_domain(self) -> None:
        for text in ("example.org", "+", "- https://example.org/", "+ bad_domain.org"):
            with self.subTest(text=text), self.assertRaises(ZyFeedError):
                parse_overrides(text)

    def test_durations(self) -> None:
        for value, seconds in (
            ("8 hours", 28800),
            ("1 day", 86400),
            ("30 minutes", 1800),
            ("2h", 7200),
            ("1 week", 604800),
        ):
            self.assertEqual(parse_duration(value), seconds)
        for value in ("0 hours", "-1 day", "tomorrow", "1.5 hours", "9999999999 days"):
            with self.subTest(value=value), self.assertRaises(ZyFeedError):
                parse_duration(value)
        with self.assertRaises(ZyFeedError):
            convert_text("! Expires: tomorrow\n||example.org^\n")

    def test_empty_export_requires_explicit_opt_in(self) -> None:
        overrides = parse_overrides("- example.org")
        with self.assertRaises(ZyFeedError):
            convert_text("example.org", overrides=overrides)
        result = convert_text("example.org", overrides=overrides, allow_empty=True)
        self.assertEqual(result.render(), b"")

    def test_exact_fifty_thousand_entry_limit(self) -> None:
        source = "\n".join(f"d{index}.example.org" for index in range(50_000))
        self.assertEqual(len(convert_text(source).entries), 50_000)
        with self.assertRaisesRegex(ZyFeedError, "50,001"):
            convert_text(source + "\none-more.example.org")

    def test_limit_is_after_overrides_and_deduplication(self) -> None:
        source = "\n".join(f"d{index}.example.org" for index in range(50_001))
        result = convert_text(
            source + "\nd0.example.org",
            overrides=parse_overrides("- d50000.example.org"),
        )
        self.assertEqual(len(result.entries), 50_000)

    def test_long_dns_labels_fail(self) -> None:
        with self.assertRaises(ZyFeedError):
            normalize_domain("a" * 64 + ".example.org")

    def test_lossy_idna_mapping_is_not_a_different_domain_block(self) -> None:
        with self.assertRaisesRegex(ZyFeedError, "lossy IDNA"):
            normalize_domain("fa\u00df.example")
        self.assertEqual(normalize_domain("xn--fa-hia.example"), "xn--fa-hia.example")
        self.assertEqual(
            normalize_domain("bu\u0308cher.example"), "xn--bcher-kva.example"
        )


if __name__ == "__main__":
    unittest.main()
