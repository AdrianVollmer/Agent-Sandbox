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
    """Return a squid.conf that only allows HTTP(S) to the allowlisted destinations.

    Logs stay at the image defaults under /var/log/squid. Logging to
    /dev/stdout is not possible since Squid drops privileges before opening
    its log files and the unprivileged user cannot reopen the root-owned stream.
    """
    ports = list(dict.fromkeys((443, *proxy.allowed_ports)))
    # Squid rejects ACLs without values, so only define the ones in use
    acls = []
    if domains := normalize_domains(proxy.domains()):
        acls.append(("allowed_domains", f"dstdomain {' '.join(domains)}"))
    if proxy.allowed_ips:
        acls.append(("allowed_ips", f"dst {' '.join(proxy.allowed_ips)}"))
    acl_lines = "\n".join(f"acl {name} {spec}" for name, spec in acls)
    allow_lines = "\n".join(f"http_access allow {name}" for name, _ in acls)
    return f"""\
http_port {proxy.port}

{acl_lines}
acl SSL_ports port {" ".join(map(str, ports))}
acl Safe_ports port {" ".join(map(str, dict.fromkeys((80, *ports))))}
acl CONNECT method CONNECT

http_access deny !Safe_ports
http_access deny CONNECT !SSL_ports
{allow_lines}
http_access deny all

cache deny all
pid_filename none
# Default waits 30s for clients, longer than podman's 10s stop timeout
shutdown_lifetime 0 seconds
"""
