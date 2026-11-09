"""Strict, file-based blocklist publishing for Zyxel firewalls."""

__version__ = "0.1.0"


class ZyFeedError(Exception):
    """An input, configuration or publishing error safe to report to the user."""
