"""Resolving allowed names on the host, for networks only the host can resolve.

With split DNS (e.g. systemd-resolved sending `*.intern` to a VPN resolver),
the container's DNS forwarding may not reach the right server. Resolving on
the host and pinning the result into the proxy's /etc/hosts sidesteps that.
"""

import logging
import socket
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agent_sandbox.config import ProxyConfig
from agent_sandbox.squid import normalize_domains

log = logging.getLogger(__name__)

Resolver = Callable[..., list[tuple[Any, ...]]]


def pinnable_names(proxy: ProxyConfig) -> list[str]:
    """Return the allowed names that can be pinned; wildcards cannot."""
    return [d for d in normalize_domains(proxy.domains()) if not d.startswith(".")]


def resolve_on_host(names: Iterable[str], resolve: Resolver | None = None) -> dict[str, list[str]]:
    """Resolve `names` with the host's resolver, return name -> IPv4 addresses.

    Names that fail to resolve are skipped with a warning; the proxy then
    falls back to its own DNS for them.
    """
    # Looked up at call time so tests can patch socket.getaddrinfo
    resolve = resolve or socket.getaddrinfo

    def lookup(name: str) -> tuple[str, list[str]]:
        try:
            # The container networks are IPv4 only
            infos = resolve(name, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as e:
            log.warning("Cannot resolve %s on the host, the proxy will use its own DNS: %s", name, e)
            return name, []
        # sockaddr is (host, port) for AF_INET
        return name, sorted({str(info[4][0]) for info in infos})

    names = list(names)
    with ThreadPoolExecutor(max_workers=min(16, len(names) or 1)) as pool:
        results = dict(pool.map(lookup, names))
    pinned = {name: ips for name, ips in results.items() if ips}
    for name, ips in pinned.items():
        log.debug("Pinning %s to %s", name, ", ".join(ips))
    return pinned
