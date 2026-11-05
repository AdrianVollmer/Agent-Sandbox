"""Finding denied requests in Squid's access log."""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# Default port per scheme for URLs that don't name one
_SCHEME_PORTS = {"http": 80, "https": 443, "ftp": 21}


@dataclass(frozen=True)
class Denial:
    """A destination the proxy refused, with how often and when it was last tried."""

    host: str
    port: int | None
    count: int
    last_seen: datetime


def _destination(method: str, url: str) -> tuple[str, int | None] | None:
    # CONNECT logs "host:port", everything else a full URL
    parts = urlsplit(f"//{url}" if method == "CONNECT" else url)
    try:
        port = parts.port or _SCHEME_PORTS.get(parts.scheme)
    except ValueError:
        return None
    return (parts.hostname, port) if parts.hostname else None


def denied_destinations(lines: Iterable[str]) -> list[Denial]:
    """Group the denied requests in a native-format access log by destination, newest first.

    Lines look like `<epoch> <elapsed> <client> TCP_DENIED/403 <bytes> <method> <url> ...`.
    """
    seen: dict[tuple[str, int | None], Denial] = {}
    for line in lines:
        fields = line.split()
        if len(fields) < 7 or not fields[3].startswith("TCP_DENIED/"):
            continue
        destination = _destination(fields[5], fields[6])
        try:
            stamp = datetime.fromtimestamp(float(fields[0])).astimezone()
        except ValueError:
            destination = None
        if destination is None:
            log.debug("Cannot parse access log line: %s", line)
            continue
        previous = seen.get(destination)
        count = previous.count + 1 if previous else 1
        seen[destination] = Denial(*destination, count=count, last_seen=stamp)
    return sorted(seen.values(), key=lambda d: d.last_seen, reverse=True)
