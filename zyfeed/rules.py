"""Parse supported rules without turning conditional filters into global blocks."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from urllib.parse import urlsplit, urlunsplit

from zyfeed import ZyFeedError

MAX_ENTRIES = 50_000
DEFAULT_EXPIRES = 24 * 60 * 60


class InputFormat(StrEnum):
    AUTO = "auto"
    ADBLOCK = "adblock"
    ADGUARD = "adguard"
    UBLOCK = "ublock"
    DOMAINS = "domains"
    HOSTS = "hosts"
    ZYXEL = "zyxel"


ADBLOCK_FORMATS = {
    InputFormat.ADBLOCK,
    InputFormat.ADGUARD,
    InputFormat.UBLOCK,
}
LOCAL_NAMES = {
    "localhost",
    "localhost.localdomain",
    "localhost6",
    "localhost6.localdomain6",
    "ip6-localhost",
    "ip6-loopback",
    "ip6-allnodes",
    "ip6-allrouters",
    "broadcasthost",
}
HEADER = re.compile(
    r"\[(?:adblock(?: plus)?(?: \d+(?:\.\d+)*)?|adguard)\]", re.IGNORECASE
)
DURATION = re.compile(
    r"([1-9]\d{0,8})\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)",
    re.IGNORECASE,
)
METADATA_KEYS = {
    "title",
    "description",
    "homepage",
    "license",
    "issues",
    "disclaimer",
    "expires",
    "version",
    "last modified",
    "number of entries",
}


@dataclass(frozen=True, slots=True)
class Entry:
    value: str
    host: str


@dataclass(frozen=True, slots=True)
class Overrides:
    additions: frozenset[str] = frozenset()
    removals: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Statistics:
    source_rules: int
    converted_entries: int
    duplicate_entries: int
    added_entries: int
    removed_entries: int
    output_entries: int


@dataclass(frozen=True, slots=True)
class Conversion:
    entries: tuple[Entry, ...]
    metadata: dict[str, str]
    statistics: Statistics

    def render(self, *, comments: bool = False) -> bytes:
        lines: list[str] = []
        if comments:
            lines.extend(
                f"# {key.title()}: {value}"
                for key, value in sorted(self.metadata.items())
            )
            lines.append(f"# ZyFeed entries: {len(self.entries)}")
        lines.extend(entry.value for entry in self.entries)
        return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")

    def expires_seconds(self, default: int = DEFAULT_EXPIRES) -> int:
        value = self.metadata.get("expires")
        return parse_duration(value) if value is not None else default


def parse_duration(value: str) -> int:
    match = DURATION.fullmatch(value.strip())
    if match is None:
        raise ZyFeedError(
            f"invalid Expires duration {value!r}; use e.g. '8 hours' or '1 day'"
        )
    factors = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    return int(match[1]) * factors[match[2][0].lower()]


def normalize_domain(value: str, *, wildcards: bool = True) -> str:
    value = unicodedata.normalize("NFC", value.strip().removesuffix(".").lower())
    if not value or any(character.isspace() for character in value):
        raise ZyFeedError(f"invalid hostname {value!r}")
    if not wildcards and "*" in value:
        raise ZyFeedError(
            "wildcards in AdBlock network rules are not safely convertible; "
            "use a domains/wildcard source or an explicit override"
        )
    try:
        labels = [label.encode("idna").decode("ascii") for label in value.split(".")]
        for original, encoded in zip(value.split("."), labels, strict=True):
            if (
                not original.isascii()
                and encoded.encode("ascii").decode("idna") != original
            ):
                raise ZyFeedError(
                    f"lossy IDNA mapping for {value!r}; "
                    "use an explicit ASCII/punycode hostname"
                )
    except UnicodeError as error:
        raise ZyFeedError(f"invalid international hostname {value!r}") from error
    normalized = ".".join(labels)
    if len(labels) < 2 or len(normalized) > 253:
        raise ZyFeedError(
            f"hostname must be a FQDN of at most 253 characters: {value!r}"
        )
    label_pattern = r"[a-z0-9*](?:[a-z0-9*-]{0,61}[a-z0-9*])?"
    if not all(re.fullmatch(label_pattern, label) for label in labels):
        raise ZyFeedError(f"invalid hostname or wildcard pattern {value!r}")
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        return normalized
    raise ZyFeedError("bare IP addresses belong in IP Reputation, not a DNS/URL list")


def parse_entry(value: str) -> Entry:
    if not value.lower().startswith(("http://", "https://")):
        host = normalize_domain(value)
        return Entry(host, host)
    if "\\" in value or any(character.isspace() for character in value):
        raise ZyFeedError(f"invalid URL {value!r}")
    try:
        parts = urlsplit(value)
        host_value = parts.hostname
        port = parts.port
    except ValueError as error:
        raise ZyFeedError(f"invalid URL {value!r}: {error}") from error
    if host_value is None or parts.username is not None or parts.password is not None:
        raise ZyFeedError("URL entries must have a hostname and no credentials")
    if parts.fragment or "#" in value:
        raise ZyFeedError("URL fragments cannot be checked by the firewall")
    if port is not None and not 1 <= port <= 65535:
        raise ZyFeedError("URL port must be between 1 and 65535")
    try:
        host = str(ipaddress.ip_address(host_value))
    except ValueError:
        host = normalize_domain(host_value)
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    normalized = urlunsplit(
        (parts.scheme.lower(), authority, parts.path or "/", parts.query, "")
    )
    if not normalized.isascii() or re.search(r"%(?![0-9a-fA-F]{2})", normalized):
        raise ZyFeedError("URL paths and queries must use valid ASCII percent encoding")
    return Entry(normalized, host)


def parse_overrides(text: str, *, source: str = "<overrides>") -> Overrides:
    additions: set[str] = set()
    removals: set[str] = set()
    for number, original in enumerate(text.removeprefix("\ufeff").splitlines(), 1):
        line = original.partition("#")[0].strip()
        if not line:
            continue
        if line[0] not in "+-":
            raise ZyFeedError(
                f"{source}:{number}: expected '+ hostname' or '- hostname'"
            )
        try:
            host = normalize_domain(line[1:].strip())
        except ZyFeedError as error:
            raise ZyFeedError(f"{source}:{number}: {error}") from error
        (additions if line[0] == "+" else removals).add(host)
    return Overrides(frozenset(additions), frozenset(removals))


def _adblock_entries(line: str) -> list[Entry]:
    if line.startswith("@@"):
        raise ZyFeedError(
            "allow/exception rules cannot be represented faithfully in a Zyxel denylist"
        )
    if any(marker in line for marker in ("##", "#@#", "#?#", "#$#", "#%#")):
        raise ZyFeedError(
            "cosmetic filters and scriptlets are not hostname block rules"
        )
    if "$" in line:
        raise ZyFeedError(
            "AdBlock options/conditions are unsupported; "
            "they cannot be silently removed"
        )
    if not line.startswith("||") or not line.endswith("^"):
        raise ZyFeedError("supported AdBlock rules have the form '||hostname^'")
    host = normalize_domain(line[2:-1], wildcards=False)
    return [Entry(host, host), parse_entry(f"*.{host}")]


def _hosts_entries(line: str) -> list[Entry]:
    fields = line.split()
    if len(fields) < 2:
        raise ZyFeedError("a hosts row needs a sinkhole IP and at least one hostname")
    try:
        address = ipaddress.ip_address(fields[0])
    except ValueError as error:
        raise ZyFeedError("invalid IP address in hosts row") from error
    names = [name for name in fields[1:] if name.lower().rstrip(".") not in LOCAL_NAMES]
    if not names:
        return []
    if not (address.is_loopback or address.is_unspecified):
        raise ZyFeedError("hosts rows must use an unspecified or loopback sinkhole IP")
    return [parse_entry(normalize_domain(name, wildcards=False)) for name in names]


def _looks_like_hosts(line: str) -> bool:
    fields = line.split()
    if len(fields) < 2:
        return False
    try:
        ipaddress.ip_address(fields[0])
    except ValueError:
        return False
    return True


def convert_text(
    text: str,
    *,
    source: str = "<input>",
    input_format: InputFormat = InputFormat.AUTO,
    overrides: Overrides | None = None,
    allow_empty: bool = False,
) -> Conversion:
    if "\x00" in text:
        raise ZyFeedError(f"{source}: NUL bytes are not allowed")
    metadata: dict[str, str] = {}
    entries: set[Entry] = set()
    source_rules = emitted_entries = 0
    adblock_header = False
    for number, original in enumerate(text.removeprefix("\ufeff").splitlines(), 1):
        line = original.strip()
        if not line:
            continue
        try:
            if line.startswith(("!#", "#if", "#endif", "#include")):
                raise ZyFeedError(
                    "conditional/include directives require a browser engine"
                )
            if HEADER.fullmatch(line):
                adblock_header = True
                continue
            is_comment = line.startswith("!") or (
                line.startswith("#")
                and not line.startswith(("##", "#@#", "#?#", "#$#", "#%#"))
            )
            if is_comment:
                key, separator, value = line[1:].partition(":")
                key = key.strip().lower()
                if separator and key in METADATA_KEYS:
                    value = value.strip()
                    if key == "expires":
                        parse_duration(value)
                        if key in metadata and metadata[key] != value:
                            raise ZyFeedError("conflicting Expires headers")
                    metadata[key] = value
                continue
            line = re.sub(r"\s+#.*$", "", line).strip()
            if input_format in ADBLOCK_FORMATS or (
                input_format == InputFormat.AUTO
                and (adblock_header or line.startswith(("||", "@@", "|", "/")))
            ):
                parsed = _adblock_entries(line)
            elif input_format == InputFormat.HOSTS or (
                input_format == InputFormat.AUTO and _looks_like_hosts(line)
            ):
                parsed = _hosts_entries(line)
            elif input_format == InputFormat.DOMAINS:
                host = normalize_domain(line)
                parsed = [Entry(host, host)]
            else:
                parsed = [parse_entry(line)]
            if parsed:
                source_rules += 1
                emitted_entries += len(parsed)
                entries.update(parsed)
        except ZyFeedError as error:
            raise ZyFeedError(f"{source}:{number}: {error}") from error

    base = entries.copy()
    overrides = overrides or Overrides()
    entries.update(Entry(host, host) for host in overrides.additions)
    before_removals = len(entries)
    exact_removals = {pattern for pattern in overrides.removals if "*" not in pattern}
    wildcard_removals = tuple(
        pattern for pattern in overrides.removals if "*" in pattern
    )
    entries = {
        entry
        for entry in entries
        if entry.host not in exact_removals
        and not any(fnmatchcase(entry.host, pattern) for pattern in wildcard_removals)
    }
    if len(entries) > MAX_ENTRIES:
        raise ZyFeedError(
            f"{source}: {len(entries):,} output entries exceed Zyxel's "
            f"{MAX_ENTRIES:,}-entry limit; nothing was published"
        )
    if not entries and not allow_empty:
        raise ZyFeedError(
            f"{source}: refusing an empty export; use --allow-empty only if intentional"
        )
    statistics = Statistics(
        source_rules=source_rules,
        converted_entries=len(base),
        duplicate_entries=emitted_entries - len(base),
        added_entries=len(entries - base),
        removed_entries=before_removals - len(entries),
        output_entries=len(entries),
    )
    return Conversion(
        tuple(sorted(entries, key=lambda entry: entry.value)), metadata, statistics
    )
