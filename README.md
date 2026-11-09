# ZyFeed

**Convert and refresh blocklists as local TXT files.**

ZyFeed downloads a list, converts supported rules into the documented Zyxel
DNS/URL External Block List format, applies local additions/removals, and
atomically writes TXT files to a directory you choose. Generating these files
does not require a network service or make them reachable through a URL.

```text
Upstream HTTP(S) list + local overrides
                 |
             ZyFeed update
                 |
       Local TXT + metadata JSON
```

You decide how to use the result: keep it locally, import it into a compatible
application, copy it elsewhere, or make it available through a URL yourself.
That distribution/import step is outside ZyFeed.

Available as prebuilt Linux AMD64/ARM64 executables, or as Python source.
Prebuilt executables include Python; running the source requires Python 3.11+
on Linux and has no third-party runtime dependencies. The target format follows
the Zyxel manual, not workarounds for individual firmware versions.

**No real blocklists or active feed configurations are included.** The supplied
input, overrides and source URLs are examples using reserved domains. Installing
or launching ZyFeed without arguments does not download lists, publish files
or enable a scheduler.

## Features

- DNS-style AdBlock, AdGuard and uBlock Origin sources, domains, wildcard lists,
  hosts files and native Zyxel hostname/HTTP(S) URL entries.
- A single `+` / `-` override file, with wildcard support.
- Strict conversion: unsupported browser rules are errors, never silently
  broadened into network-wide hostname blocks.
- Deduplication, deterministic sorting and a hard limit of **50,000 output
  entries**, checked after conversion and overrides. Oversized lists are not
  truncated.
- `Expires`-aware refresh, conditional requests with `ETag`/`Last-Modified`,
  private caching and immediate override processing on the next invocation.
- Atomic replacement of each file; download, conversion and validation failures
  preserve the last valid export. Concurrent updaters cannot share a state directory.
- Direct executable, CLI help, JSON metadata, confirmed systemd user scheduling,
  systemd examples and GitHub Actions CI.

## Install a prebuilt Linux binary

Download from [GitHub Releases](https://github.com/M4v3r1cK87/zyfeed/releases/latest).
Each archive contains the standalone `zyfeed` executable, documentation,
synthetic configuration/override examples and license notices.
**No Python, pip or virtual environment is needed on the target machine.**

| `uname -m` | Archive architecture | Typical target |
| --- | --- | --- |
| `x86_64` | `amd64` | 64-bit Intel/AMD PCs, servers and NAS devices |
| `aarch64` | `arm64` | Raspberry Pi and other ARM systems with a 64-bit OS |
| `armv7l` | Not provided | A 32-bit OS cannot run the ARM64 executable |

Builds target Ubuntu 22.04/glibc 2.35. Use a compatible 64-bit glibc-based Linux
distribution, such as Raspberry Pi OS Bookworm or newer. Older glibc systems,
Alpine/musl, Windows and macOS are not supported by these binary releases.

### Download, verify and install

Run these commands as your normal user in a writable download directory.
For Raspberry Pi **64-bit**, use `ARCH=arm64`; for an Intel/AMD **64-bit** OS,
change that line to `ARCH=amd64`. This example installs release `v0.1.0`:

```sh
VERSION=0.1.0
ARCH=arm64
BASE="https://github.com/M4v3r1cK87/zyfeed/releases/download/v$VERSION"
ARCHIVE="zyfeed-v$VERSION-linux-$ARCH.tar.gz"

curl --fail --location --remote-name "$BASE/$ARCHIVE"
curl --fail --location --remote-name "$BASE/SHA256SUMS"

sha256sum --check --ignore-missing SHA256SUMS &&
tar -xzf "$ARCHIVE" &&
install -Dm755 "zyfeed-v$VERSION-linux-$ARCH/zyfeed" "$HOME/.local/bin/zyfeed"

"$HOME/.local/bin/zyfeed" --version
"$HOME/.local/bin/zyfeed" --help
```

The checksum command must report `OK` for the archive you downloaded. It ignores
the other architecture's archive if that file is absent. Do not continue after
a failed checksum. Install separately from a source checkout: do not overwrite
the checkout's `zyfeed/` Python package directory with the binary.

Use the full installed path as above, or add `$HOME/.local/bin` to your shell's
`PATH` to invoke `zyfeed` directly.

### Configure and use the binary

From the same download directory:

```sh
mkdir -p "$HOME/.config/zyfeed"
cp "zyfeed-v$VERSION-linux-$ARCH/examples/zyfeed.example.toml" \
  "$HOME/.config/zyfeed/zyfeed.toml"
cp "zyfeed-v$VERSION-linux-$ARCH/examples/overrides.example.txt" \
  "$HOME/.config/zyfeed/overrides.txt"
```

Edit those copied files: replace the placeholder source URL, choose your output
and private-state directories, and replace the example override rules.
The [TOML guide](#what-is-toml) explains every setting. Relative paths resolve
against the configuration file, not the binary's installation directory.

```sh
# Convert a local file once, with your own override rules.
"$HOME/.local/bin/zyfeed" convert input.txt \
  --overrides overrides.txt --output blocklist.txt

# Refresh sources from your saved configuration.
"$HOME/.local/bin/zyfeed" update --config "$HOME/.config/zyfeed/zyfeed.toml"

# Optional automatic updates; asks for confirmation.
"$HOME/.local/bin/zyfeed" schedule enable \
  --config "$HOME/.config/zyfeed/zyfeed.toml"
"$HOME/.local/bin/zyfeed" schedule status
"$HOME/.local/bin/zyfeed" schedule disable
```

The binary has the same commands as the source launcher. In the rest of this
README, replace `./run` with the installed binary path when using a release.
Its help examples automatically use the binary's actual path.

Keep the executable at its permanent installation path when enabling a timer.
The generated timer invokes that binary directly; it does not depend on a
system Python installation or the temporary directory used to unpack a bundle.
Automatic updates still require the systemd user manager described below.
No configuration, list or scheduler is activated just by installing the binary.

## Run from source

From the project directory:

```sh
./run
./run convert --help
./run update --help
./run schedule --help
```

With no arguments, `./run` displays the command guide. If a ZIP download loses
the executable permission, restore it once with `chmod +x run`.

`./run convert`, `./run update` and `./run schedule` without arguments display
their complete guides without performing any operation. Supply a source,
update options or a scheduler action to execute the operation instead.
Incomplete/invalid options still report an error.

### Which command needs a configuration file?

| What you want to do | Command | Where to select an override file |
| --- | --- | --- |
| Convert one URL/file now | `./run convert ...` | Add `--overrides path/to/overrides.txt` |
| Refresh saved sources | `./run update --config zyfeed.toml` | Set `overrides = "overrides.txt"` in each TOML feed block |
| Schedule those same sources | `./run schedule enable --config zyfeed.toml` | The timer reuses the same TOML settings |

**A one-off conversion does not need TOML.** The override option belongs to
`convert`, so it is listed in full by `./run convert --help`, not as a global
option applicable to every command.

Try the local, synthetic [example input](examples/blocklist.adblock.txt):

```sh
./run convert \
  examples/blocklist.adblock.txt \
  --format adblock \
  --overrides examples/overrides.example.txt \
  --output var/output/example.txt
```

This example makes no network requests and writes only local `example.txt` and
`example.json` files. Its overrides add `extra.example.org` and its wildcard,
and remove both entries for `metrics.example.net`.

Without `--output`, only the TXT
goes to stdout; diagnostics go to stderr. Local UTF-8 input files are supported
too. Source and override files are never modified, and output paths cannot
overwrite them.

The one-shot `convert` command does not start a scheduler.

For an actual source, supply your own HTTP(S) URL or local file. No source is
selected automatically.

## Command usage and examples

The following commands are illustrations; they are not run automatically.
Replace `input.txt`, `overrides.txt` and placeholder URLs with your own inputs.
For the full option list, append `--help` to any command or scheduler action.

### `convert`: process one source now

```text
./run convert SOURCE [--overrides FILE] [--output FILE.txt] [options]
```

```sh
# Local synthetic input; print the converted list without writing export files.
./run convert examples/blocklist.adblock.txt

# Your local input and override file; write blocklist.txt and blocklist.json.
./run convert input.txt --overrides overrides.txt --output blocklist.txt

# Remote input: this URL is a placeholder, not a supplied feed.
./run convert 'https://example.org/blocklist.txt' --output blocklist.txt

# Explicitly select a source syntax instead of auto-detection.
./run convert input.txt --format adblock --output blocklist.txt
```

This command requires no TOML, processes the source immediately, and exits.
It does not create an update cache or start automatic refreshes. Without
`--output`, TXT goes to stdout and diagnostics go to stderr.

### `update`: refresh sources saved in TOML

```text
./run update [--config FILE.toml] [--force]
```

Without arguments, `./run update` displays its guide and does not read any
configuration. Use `--config` to run an update. An explicit `--force` without
`--config` uses `zyfeed.toml` in the current working directory.

Prepare your settings once, replacing the source URL and example override rules
before updating. See [the TOML guide](#what-is-toml) for each setting.

```sh
cp examples/zyfeed.example.toml zyfeed.toml
cp examples/overrides.example.txt overrides.txt

# Show update usage without reading the configuration or updating.
./run update

# After editing those files, run the configured update.
./run update --config zyfeed.toml

# Or select a different configuration explicitly.
./run update --config /path/to/zyfeed.toml

# Check the upstream sources before their normal deadline.
./run update --config zyfeed.toml --force
```

Without `--force`, only due sources are checked, while changed override files
can rebuild exports from cache. `--force` still honors HTTP cache validators:
an unchanged source may return `304`. Every invocation runs once and exits.

### `schedule`: manage automatic updates

```text
./run schedule enable [--config FILE.toml] [--yes]
./run schedule status
./run schedule disable [--yes]
```

```sh
# After a successful manual update, request a user timer; confirmation is required.
./run schedule enable --config zyfeed.toml

# Inspect the timer without downloading anything or changing settings.
./run schedule status

# Stop future checks; confirmation is required.
./run schedule disable
```

The timer invokes `update` every 15 minutes using the same TOML sources and
overrides. `Expires` decides which lists need a source check. For an immediate
manual refresh, run `update` separately. A persistent timer may also catch up a
missed check after activation.

For intentional non-interactive changes, explicitly authorize with `--yes`:

```sh
./run schedule enable --config /path/to/zyfeed.toml --yes
./run schedule disable --yes
```

A running systemd **user** manager is required. System-wide services
are not modified. Disabling retains unit files and exports; an update already
running may finish. See [automatic-update setup](#optional-automatic-updates)
for logout/reboot behavior and troubleshooting.

## Project layout

The essential checkout is:

```text
run             executable launcher
zyfeed/         application code
examples/       synthetic input, config, overrides and deployment templates
tests/          automated tests
README.md       documentation
pyproject.toml  optional packaging/development configuration
```

`.git` and `.github` are repository/CI metadata. Build output, wheels, virtual
environments and development caches are not needed for normal use.
`./run` does not generate Python bytecode cache directories.

The inner [zyfeed/](zyfeed/) directory is the Python package, not a second
installation:

| Module | Responsibility |
| --- | --- |
| [cli.py](zyfeed/cli.py) | Commands and `--help` |
| [rules.py](zyfeed/rules.py) | Parsing, conversion and overrides |
| [config.py](zyfeed/config.py) | TOML configuration validation |
| [io.py](zyfeed/io.py) | Downloads, atomic writes and locking |
| [updater.py](zyfeed/updater.py) | Caching, deadlines and publishing |
| [scheduler.py](zyfeed/scheduler.py) | Explicitly requested systemd user timers |
| [__init__.py](zyfeed/__init__.py) | Package version and shared error type |
| [__main__.py](zyfeed/__main__.py) | Entry point for `python -m zyfeed` |

This standard package layout keeps application code separate from repository
files. Installing the `zyfeed` command with `pip` remains optional; `./run`
provides the same commands directly and works from other directories without
changing how relative CLI paths are resolved.

## Supported input and conversion semantics

Choose `--format auto|adblock|adguard|ublock|domains|hosts|zyxel`.
The same values are accepted in configuration.

| Input | Export |
| --- | --- |
| `||tracker.example.org^` | `tracker.example.org` **and** `*.tracker.example.org` |
| `tracker.example.org` in a domains list | The exact hostname |
| `*.tracker.example.org`, `*.zyxel.*` | The wildcard pattern unchanged |
| `0.0.0.0 tracker.example.org` | The exact hostname |
| `http://example.org/path?q=value` | A native Zyxel HTTP URL entry |
| `https://example.org/path` | A native Zyxel HTTPS URL entry; the device checks only the hostname |

`adblock`, `adguard` and `ublock` select the shared **DNS-rule subset**, not
complete browser engines. Supported network rules are `||concrete.fqdn^`.
Both the exact domain and its subdomains are exported; a wildcard alone is not
assumed to cover the domain itself.

For generic wildcard hostnames, use a domains/wildcard source or explicit
overrides. Wildcards inside AdBlock network expressions are rejected rather
than assuming browser-pattern matching is identical to Zyxel hostname matching.

Auto mode recognizes list headers and common supported line forms. With an
AdBlock header present, unanchored substring filters are not mistaken for
hostname entries. Hosts sources accept unspecified/loopback sinkhole addresses,
multiple aliases and common localhost boilerplate, but reject real-address mappings.
Hostnames are lowercased and conservatively IDNA-normalized. Lossy Unicode
mappings are rejected: provide an explicit ASCII/punycode name instead of
silently blocking a different hostname. Output uses LF newlines.

**Unsupported rules stop the export**, with source and line number:

- Allow/exception rules such as `@@||example.org^`.
- Options/conditions such as `$third-party`, `$script`, `$domain=...`.
- Cosmetic selectors, scriptlets, regular expressions and substring/path
  filters that cannot be translated faithfully.
- Include/conditional directives requiring a browser engine.

Use the DNS edition of a source where available. There is deliberately no
aggressive extraction or silent skip mode.

### Why not simply call adblock-decoder?

[adblock-decoder](https://github.com/PyFunceble/adblock-decoder) is useful for
domain extraction. Its
[upstream PyFunceble decoder](https://github.com/funilrys/PyFunceble/blob/dev/PyFunceble/converter/adblock_input_line2subject.py)
strips wildcards and can extract hostnames from rules containing conditions.
Those are different semantics from preserving Zyxel patterns and rejecting
unrepresentable rules. ZyFeed therefore uses a small strict parser and does not
pull the complete PyFunceble dependency tree into a Raspberry Pi deployment.

## Local additions and removals

Pass `--overrides overrides.txt`, or set `overrides` on a configured feed.
See [overrides.example.txt](examples/overrides.example.txt).

For one-off conversion, select the file on the command line:

```sh
./run convert input.txt --overrides my-overrides.txt --output blocklist.txt
```

For `update` (including scheduled updates), set `overrides = "my-overrides.txt"`
inside the relevant `[[feeds]]` block in your TOML. Each source can have a
different override file. Omit the option/setting when none is needed.
The override file contains only your `+` / `-` rules, not a copy of the
downloaded blocklist.

```text
# Block these additional entries.
+ extra.example.org
+ *.extra.example.org

# Remove matching entry hosts from the result.
- tracker.example.org
- *.noisy.example.org
```

- Blank lines and `#` comments are ignored.
- `+ hostname` adds an exact entry; `+ pattern` adds a wildcard entry.
- `- pattern` selects exported entry **host strings** using `*` glob matching.
  `*` can span dots. For URL entries, selection uses the hostname, not the path.
- **Removals win over additions**, regardless of line order.
- `- example.org` does not remove `*.example.org`.
- `- *.example.org` removes matching subdomain/wildcard entries, but not the apex
  `example.org`.
- Results are deduplicated and sorted after applying the operations.

**Removals are not firewall allow rules.** Removing `allowed.example.org`
does not carve an exception out of an existing `*.example.org` entry. If a
broader rule remains, the firewall may still block that host. This entry-only
behavior is intentional.

An empty final list is rejected unless explicitly allowed with `--allow-empty`
or `allow_empty = true`. An empty HTTP response is always a download error.

## Refreshing multiple feeds

### What is TOML?

TOML is simply a readable **plain-text format for settings**, not another
program or another blocklist. A line such as `overrides = "overrides.txt"`
means "use this file for local additions and removals". Saving these settings
lets the same `update` command reuse your source URLs, output directory and
override files on every run, including timer runs.

There are two different TOML files to distinguish:

- **Your `zyfeed.toml`**: the settings file you create for your feeds.
- [pyproject.toml](pyproject.toml): development/packaging settings for the
  application itself. **Do not edit it to add sources or override files.**

Basic syntax:

- `key = "text"` stores text, such as a URL or file path.
- `timeout = 30` stores a number; `comments = false` stores a boolean.
- `#` starts a comment, which is explanatory and not executed.
- `[[feeds]]` starts one list's settings. Repeat it for each additional source.
- Shared settings go **before the first `[[feeds]]`**. Settings after that line
  belong to that feed until another block begins; indentation does not change this.

### Create your settings file

The [configuration template](examples/zyfeed.example.toml) uses a reserved placeholder
URL. It is not a working remote feed. Copy the templates, then replace the
source URL and example override rules before running an update:

```sh
cp examples/zyfeed.example.toml zyfeed.toml
cp examples/overrides.example.txt overrides.txt
```

For example, the configuration structure is:

```toml
output_dir = "var/output"
state_dir = "var/state"
default_expires = "24 hours"
timeout = 30

[[feeds]]
name = "example"
url = "https://example.org/blocklist.txt"
format = "adblock"
overrides = "overrides.txt"
comments = false
allow_empty = false
```

| Setting | Meaning |
| --- | --- |
| `output_dir` | Local directory where ZyFeed writes final TXT exports and metadata JSON |
| `state_dir` | Private cache used for source deadlines and conditional downloads; keep it separate from the exported files |
| `default_expires` | How long to wait between source checks when the upstream list has no `Expires` header |
| `timeout` | HTTP socket timeout, in seconds |
| `[[feeds]]` | Start of one source's settings |
| `name` | Output basename: `example` generates `example.txt` and `example.json` |
| `url` | The original list to download, not an address for the generated output |
| `format` | Syntax of that original list; `auto` detects supported forms, or select `adblock`, `domains`, etc. explicitly |
| `overrides` | Optional path to that feed's `+` / `-` file |
| `comments` | Whether to include `#` metadata headers in the final TXT; `false` keeps it header-free |
| `allow_empty` | Whether an empty final list is intentional; leave `false` for protection |

Replace the placeholder URL with a real source you choose and set `overrides`
to your own file. Then run:

```sh
./run update --config zyfeed.toml
./run update --config zyfeed.toml --force
```

The sample output and state paths are local `var/output` and `var/state`.
Writing files there does not expose them over the network.

Add another `[[feeds]]` table for each independent feed. Each generates
`<name>.txt` and `<name>.json`. Relative paths are resolved against the
configuration file, not the current working directory. Unknown configuration
keys, duplicate names and invalid values are errors.

### How deadlines work

1. A list's `! Expires: 8 hours` or `# Expires: 8 hours` sets its source-check interval.
   Positive integer seconds, minutes, hours, days and weeks are supported,
   including short forms such as `8h`.
2. Missing `Expires` uses `default_expires` (24 hours by default).
   Invalid/conflicting `Expires` is an error, not a silent fallback.
3. Before the deadline, an unchanged feed performs no HTTP request.
4. Changed override files/settings or missing output files rebuild from the
   valid cached source on the next run. This does not postpone the source deadline.
5. At the deadline, a conditional HTTP request is made. A `304` reuses the
   cached source and starts the next interval. `--force` checks early, still
   using validators.
6. Failed due refreshes are logged and retain the last valid files; the next
   scheduled invocation retries. A failed early `--force` check does not shorten
   the previous valid deadline. Other feeds can still update; the process
   exits nonzero if any feed fails.

For each feed, `next_check = checked_at + Expires` (or `default_expires` when
absent). `checked_at` is the recorded successful source-check run timestamp;
deadlines are stored in UTC. The interval comes from a comment in the list,
not its filename, `Last modified` text or HTTP cache lifetime.
The [five-list example below](#five-lists-one-timer) separates these deadlines
from the timer's wake-up interval.

Downloads are bounded to 10 MiB of UTF-8 text. HTTP(S) connections retrieve
input sources; `timeout` is the HTTP socket timeout.

The private state holds the valid original source, HTTP validators, UTC
deadlines and output hashes. State files are mode `0600`, and `state_dir` must
be outside the output directory. Exported JSON contains upstream
title/description/homepage/license/version/expiry, refresh times and counts,
not cached source bodies or configured source URLs.

The TXT is header-free by default. `--comments` / `comments = true` explicitly
adds `#` metadata headers; device support must be verified separately.
TXT, JSON and state files are individually atomically replaced, not a
multi-file transaction. Validation completes before any publishing; a later
filesystem error can leave a newly valid TXT alongside older metadata/state
and is reported as an error.

## Local output and optional automatic updates

### Output permissions

Set `output_dir` to your chosen export directory and keep `state_dir` separate
and private. For example, when using the dedicated-account service template:

```toml
output_dir = "/var/lib/zyfeed/output"
state_dir = "/var/lib/zyfeed/state"
```

Create the directories with permissions appropriate for the account running
ZyFeed. That account needs read access to code/config/overrides and write access
to output/state. Decide separately whether other accounts should be able to
read the exported files; cached source state should remain private.
The program does not change system directory ownership or configure file sharing.

### Optional automatic updates

First configure your own sources and verify a manual update. Then:

```sh
./run schedule enable --config zyfeed.toml
./run schedule status
./run schedule disable
```

`enable` and `disable` require typing `yes` before any scheduler changes.
For intentional non-interactive use, `--yes` is explicit authorization:

```sh
./run schedule enable --config /absolute/path/zyfeed.toml --yes
```

This manages `zyfeed.service` and `zyfeed.timer` for the **current user's**
systemd manager, under `$XDG_CONFIG_HOME/systemd/user` or
`~/.config/systemd/user`. It does not modify `/etc`, ports or other users'
services, and refuses to replace units not created by ZyFeed. Keep the checkout
and Python interpreter at their original paths, or re-enable the schedule after
moving them. Only one configuration is scheduled per user; put all feeds in it.

#### Five lists, one timer

**Five `[[feeds]]` blocks create one user timer and one service, not five
timers.** The timer invokes the equivalent of
`./run update --config /absolute/path/zyfeed.toml` every 15 minutes, at the
quarter-hour calendar boundaries. The service processes feeds **sequentially**
in their configuration order, then exits. There is no continuously running
ZyFeed daemon.

The 15-minute cadence is currently fixed in the generated timer. It is not a
TOML `schedule` field, and a source's `Expires` does not create/change a timer.
Instead, each source has its own cached next-check deadline.

For example, assume all five sources were successfully checked at 10:00:

| Feed | Interval declared by its source | Next source check is due |
| --- | --- | --- |
| `list-a` | `Expires: 1 hour` | 11:00 |
| `list-b` | `Expires: 2 hours` | 12:00 |
| `list-c` | `Expires: 8 hours` | 18:00 |
| `list-d` | `Expires: 1 day` | Tomorrow at 10:00 |
| `list-e` | No `Expires` | Tomorrow at 10:00 with the default 24-hour fallback |

With unchanged settings/files, the 10:15 invocation sends no HTTP requests.
At 11:00 it checks only `list-a`. Assuming that succeeds with the same interval,
at 12:00 it checks `list-a` again and `list-b` for the first time.
The other three are inspected locally but not downloaded.

A deadline between timer boundaries is normally processed on the next tick.
For example, 11:07 is handled at 11:15, not exactly at 11:07. Very short source
intervals do not make the timer run more often; downtime or long-running updates
can delay checks further.

Each feed has its own output files, not a merged result:

```text
output_dir/list-a.txt       converted text entries
output_dir/list-a.json      metadata, counts and next_check
...
output_dir/list-e.txt
output_dir/list-e.json

state_dir/list-a.json       private valid source, HTTP validators and deadlines
...
state_dir/list-e.json
```

#### Downloading and replacing files

For each feed on each invocation:

1. Read its private state, override file and output hashes.
2. If it is not due and nothing changed, leave it alone: **no HTTP request and
   no rewrite**.
3. If it is due (or explicitly forced), request the source. When available,
   `ETag`/`Last-Modified` validators allow an HTTP `304`: reuse the cached
   valid source without downloading a new body. Without validator support,
   a successful request downloads the full body.
4. Convert the source, apply that feed's overrides, deduplicate and validate
   the complete result before publishing.
5. If final TXT bytes changed, write a temporary file **in the output
   directory**, flush it, and atomically rename it over the same `<name>.txt`.
   A reader sees a complete old or new file, not a partially overwritten list.
   The file path stays the same; no dated history of exports is created.
6. If final TXT bytes did not change, **do not rewrite the TXT**, even after
   downloading or receiving `304`. Metadata and private state record the
   successful source check and next deadline.

An override edit or missing output can rebuild from the cached source before
its deadline, without postponing that deadline. An externally modified TXT
causes a source check as well. Download/conversion/validation failures preserve
the last valid TXT and are logged; the remaining feeds still get processed.
The entire invocation exits nonzero if any feed fails. A shared state-directory
lock prevents overlapping manual/timer updates.

ZyFeed stops at generating/updating local files. It does not publish an endpoint,
push a list to another application or manage that application's reload schedule.
TXT, metadata and private state are separate atomic files, not one transaction.

#### Timer lifecycle

Override edits are applied on the next invocation. Disabling retains the unit
files and does not interrupt an update already running.

A running systemd user manager is required. To continue after logout and start
after reboot, that user may need **lingering** enabled by an administrator:

```sh
loginctl show-user "$USER" --property=Linger
sudo loginctl enable-linger "$USER"
```

The launcher never runs `sudo` or enables lingering itself. Scheduler errors are
reported; a failed activation can leave unit files for inspection.
Use `./run schedule status` and `journalctl --user -u zyfeed.service --no-pager`
to inspect the result.

For administrators who prefer a system-wide service under a dedicated account,
[zyfeed.service](examples/zyfeed.service) and
[zyfeed.timer](examples/zyfeed.timer) are manual deployment templates.
Adjust/create their user, directories and permissions before installing them.
They are not installed by `schedule`, and should not be enabled alongside the
user timer for the same configuration.

Cron is an alternative, not an additional scheduler:

```cron
*/15 * * * * /opt/zyfeed/run update --config /etc/zyfeed/zyfeed.toml
```

Run it as a user with the same configuration/output/state permissions.

### Using the generated TXT

The TXT is UTF-8 text, with one entry per line and LF line endings. Unless
explicitly enabled, it contains no metadata comment headers.

With `convert --output`, it is saved to the path you supply. Without `--output`,
`convert` prints the text to stdout. Configured `update` runs write one TXT and
one metadata JSON per feed into `output_dir`, plus private cache in `state_dir`.

The output uses the documented Zyxel hostname/URL syntax. Other DNS/filtering
tools can use it only if they accept the actual entry syntax, including any
wildcards or URLs; a `.txt` extension alone does not guarantee compatibility.

Importing, copying or exposing a file through a URL is your choice and is not
configured by ZyFeed. If another application periodically retrieves/reloads
the result, its timing is independent of ZyFeed's source-refresh schedule.
No automatic synchronization with that application is implied.

## Troubleshooting and exit codes

| Exit | Meaning |
| --- | --- |
| `0` | Success, including feeds that are not due |
| `1` | Configuration, download, conversion or publication failure |
| `2` | Invalid command-line usage |

- Unsupported line: use a DNS-compatible source or correct it explicitly;
  conditional/browser rules are not silently skipped.
- Too many entries: choose smaller independent feeds/profiles. No automatic
  truncation or assumption that one wildcard also covers the apex.
- Overlapping execution: another process holds the private state-directory lock.
- Corrupt cache: an explicit error preserves the export. Inspect/restore or
  quarantine that feed's private JSON and run again; `--force` does not hide
  invalid state.
- Update errors: inspect stderr/journal and the exported JSON refresh times.
  Do not equate the firewall's "update completed" log with valid rule import.

## Development

Tests need only system Python:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v
```

For development only, install the optional `.[dev]` tools in an environment
outside the checkout. The following checks avoid persistent lint/type caches:

```sh
ruff format --check --no-cache run zyfeed tests
ruff check --no-cache run zyfeed tests
mypy --cache-dir=/dev/null
```

Packaging/build tools can create `build`, `dist` and `*.egg-info` directories.
They are ignored by Git and are not needed to run the application.

Tests cover strict parsing, IDNA, wildcard overrides, exact 50,000/50,001
thresholds, atomic failure, CLI behavior, real HTTP conditional requests,
expiry scheduling, changed overrides before expiry, last-good preservation,
installation-free execution and confirmed scheduling with mocked systemd calls.
CI targets Python 3.11 and 3.13.

### Building and publishing native releases

[The release workflow](.github/workflows/release.yml) is triggered by a `v*` tag.
It builds separately on native Ubuntu 22.04 AMD64 and ARM64 runners, validates
the source, and exercises the actual bundled executables with no Python on
their `PATH`. Binary smoke tests cover local/HTTPS conversion, conditional
downloads and systemd units that remain usable after the first process exits.

The build dependency is pinned in the `build` optional dependency group.
Build work directories live outside the checkout; normal users do not need
any of the build tools.

For a new release, update the version in [pyproject.toml](pyproject.toml) and
[zyfeed/__init__.py](zyfeed/__init__.py), commit and push it, then create the
matching tag. For example, for version `0.1.0`:

```sh
git tag v0.1.0
git push origin v0.1.0
```

The workflow rejects tag/version mismatches. Only after both architectures
pass does it upload both archives and `SHA256SUMS` to a draft release, verify
the assets, then make the release public. Bundled Python and PyInstaller license
notices are included alongside the application's MIT license.

## License

ZyFeed code is [MIT licensed](LICENSE). Downloaded lists and generated datasets
retain their upstream licensing requirements; the code license does not
relicense third-party data. Source license/homepage metadata is retained.
Generated lists and private state are excluded from Git.
