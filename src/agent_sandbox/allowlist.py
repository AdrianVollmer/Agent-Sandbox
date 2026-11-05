"""Exceptions to the proxy allowlist that can be added while a session runs.

The entries live in two files in the session directory, which is mounted
read-only into the proxy and referenced by file ACLs in squid.conf. Squid
rereads them on reconfigure.
"""

import ipaddress
import logging
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from agent_sandbox.config import ConfigError, is_valid_domain, parse_network
from agent_sandbox.squid import DOMAINS_FILE, IPS_FILE, normalize_domains

log = logging.getLogger(__name__)


def parse_destination(value: str) -> str:
    """Return the normalized form of a domain, IP address or network, or raise ConfigError.

    Squid dies on reconfigure if an ACL file holds an invalid entry, so
    everything is validated before it is written.
    """
    try:
        return str(parse_network(value))
    except ValueError:
        pass
    if is_valid_domain(value):
        return value.lower()
    raise ConfigError(f"Invalid destination {value!r}, expected a domain (.example.com for subdomains), IP or network")


def _is_network(entry: str) -> bool:
    return "/" in entry


def _matches(host: str, entry: str) -> bool:
    if _is_network(entry):
        try:
            return ipaddress.ip_address(host) in ipaddress.ip_network(entry)
        except ValueError:
            return False
    if entry.startswith("."):
        return host == entry[1:] or host.endswith(entry)
    return host == entry


@dataclass(frozen=True)
class DynamicAllowlist:
    """The dynamic exceptions of one session, stored in `directory`."""

    directory: Path

    def create(self) -> None:
        """Create the directory with empty lists, readable by Squid's unprivileged user."""
        self.directory.mkdir(mode=0o755)
        # mkdir's mode is subject to the umask
        self.directory.chmod(0o755)
        self._write(DOMAINS_FILE, [])
        self._write(IPS_FILE, [])

    def domains(self) -> list[str]:
        """Return the allowed domains."""
        return self._read(DOMAINS_FILE)

    def networks(self) -> list[str]:
        """Return the allowed IP networks, single addresses as /32 or /128."""
        return self._read(IPS_FILE)

    def entries(self) -> list[str]:
        """Return all entries, domains first."""
        return [*self.domains(), *self.networks()]

    def allows(self, host: str) -> bool:
        """Return whether `host` (a name or an IP address) matches an entry."""
        host = host.lower()
        return any(_matches(host, entry) for entry in self.entries())

    def add(self, values: Iterable[str]) -> list[str]:
        """Add destinations and return the ones that were not there yet.

        All values are validated first, so a typo leaves the lists unchanged.
        """
        current = self.entries()
        added = [e for e in dict.fromkeys(map(parse_destination, values)) if e not in current]
        self._save([*current, *added])
        return added

    def remove(self, values: Iterable[str]) -> list[str]:
        """Remove destinations and return the ones that were present."""
        current = self.entries()
        unwanted = {parse_destination(v) for v in values}
        removed = [e for e in current if e in unwanted]
        self._save([e for e in current if e not in unwanted])
        return removed

    def _save(self, entries: list[str]) -> None:
        self._write(DOMAINS_FILE, normalize_domains(e for e in entries if not _is_network(e)))
        self._write(IPS_FILE, [e for e in entries if _is_network(e)])

    def _read(self, name: str) -> list[str]:
        return [line for line in (self.directory / name).read_text().splitlines() if line]

    def _write(self, name: str, entries: list[str]) -> None:
        # Replace atomically, Squid may read the file at any time
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=f".{name}.")
        try:
            with os.fdopen(fd, "w") as f:
                f.writelines(f"{entry}\n" for entry in entries)
            Path(tmp).chmod(0o644)
            Path(tmp).replace(self.directory / name)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        log.debug("Wrote %d entries to %s", len(entries), self.directory / name)
