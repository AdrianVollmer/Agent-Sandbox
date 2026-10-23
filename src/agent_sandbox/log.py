"""Colored logging setup."""

import logging
import sys

import click

_LEVEL_COLORS = {
    logging.DEBUG: "bright_black",
    logging.INFO: "blue",
    logging.WARNING: "yellow",
    logging.ERROR: "red",
    logging.CRITICAL: "bright_red",
}


class ColorFormatter(logging.Formatter):
    """Prefix records with a colored level name."""

    def format(self, record: logging.LogRecord) -> str:
        """Format the record, coloring the level name by severity."""
        level = click.style(f"{record.levelname.lower():>7}", fg=_LEVEL_COLORS.get(record.levelno), bold=True)
        return f"{level} {super().format(record)}"


def setup_logging(verbosity: int) -> None:
    """Send log records to stderr; -v enables debug output."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ColorFormatter("%(message)s"))
    root = logging.getLogger("agent_sandbox")
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbosity > 0 else logging.INFO)
