"""Rendering the Squid configuration for the egress proxy."""

import logging
from collections.abc import Iterable

from agent_sandbox.config import ProxyConfig

log = logging.getLogger(__name__)


def normalize_domains(domains: Iterable[str]) -> list[str]:
    """Lowercase, dedupe, and drop domains already covered by a wildcard entry.

    Squid treats a leading dot as "this domain and all subdomains" and refuses
    to load a dstdomain ACL that lists both `.example.com` and `example.com`.
    """
    unique = list(dict.fromkeys(d.lower() for d in domains))
    wildcards = {d for d in unique if d.startswith(".")}
    result = []
    for domain in unique:
        covering = next((w for w in wildcards if w != domain and (f".{domain}".endswith(w))), None)
        if covering:
            log.debug("Dropping %s from allowlist, already covered by %s", domain, covering)
            continue
        result.append(domain)
    return result


def render_squid_conf(proxy: ProxyConfig) -> str:
    """Return a squid.conf that only allows HTTP(S) to the allowlisted domains.

    Logs stay at the image defaults under /var/log/squid. Logging to
    /dev/stdout is not possible since Squid drops privileges before opening
    its log files and the unprivileged user cannot reopen the root-owned stream.
    """
    domains = " ".join(normalize_domains(proxy.domains()))
    return f"""\
http_port {proxy.port}

acl allowed_domains dstdomain {domains}
acl SSL_ports port 443
acl Safe_ports port 80 443
acl CONNECT method CONNECT

http_access deny !Safe_ports
http_access deny CONNECT !SSL_ports
http_access allow allowed_domains
http_access deny all

cache deny all
pid_filename none
# Default waits 30s for clients, longer than podman's 10s stop timeout
shutdown_lifetime 0 seconds
"""
