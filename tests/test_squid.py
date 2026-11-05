from agent_sandbox.config import ProxyConfig
from agent_sandbox.squid import normalize_domains, render_squid_conf


def test_normalize_lowercases_and_dedupes() -> None:
    assert normalize_domains(["GitHub.com", "github.com"]) == ["github.com"]


def test_normalize_drops_entries_covered_by_wildcard() -> None:
    # Squid refuses to start when a dstdomain ACL lists both a domain and its wildcard
    domains = ["anthropic.com", "api.anthropic.com", ".anthropic.com", "github.com"]
    assert normalize_domains(domains) == [".anthropic.com", "github.com"]


def test_normalize_keeps_unrelated_suffixes() -> None:
    assert normalize_domains([".hub.com", "github.com"]) == [".hub.com", "github.com"]


def test_render_contains_allowlist_and_denies_rest() -> None:
    conf = render_squid_conf(ProxyConfig(allowed_domains=(".anthropic.com",), presets=("github",), port=8080))
    lines = conf.splitlines()
    assert "http_port 8080" in lines
    domains = "github.com api.github.com codeload.github.com .githubusercontent.com .anthropic.com"
    assert f"acl allowed_domains dstdomain {domains}" in lines
    assert lines.index("http_access allow allowed_domains") < lines.index("http_access deny all")
    assert "http_access deny CONNECT !SSL_ports" in lines


def test_render_does_not_log_to_std_streams() -> None:
    # Squid drops privileges to its proxy user and then cannot reopen
    # /dev/stdout (owned by root), so it dies with "Cannot open '/dev/stdout'"
    conf = render_squid_conf(ProxyConfig(allowed_domains=("example.com",)))
    assert "/dev/stdout" not in conf
    assert "/dev/stderr" not in conf


def test_render_shuts_down_immediately() -> None:
    # The default shutdown_lifetime of 30s outlasts podman's 10s stop timeout,
    # so every teardown ended in SIGKILL
    conf = render_squid_conf(ProxyConfig(allowed_domains=("example.com",)))
    assert "shutdown_lifetime 0 seconds" in conf.splitlines()


def test_render_allowed_ips_and_ports() -> None:
    conf = render_squid_conf(
        ProxyConfig(allowed_domains=("example.com",), allowed_ips=("10.0.0.0/8", "fd00::/8"), allowed_ports=(11434,))
    )
    lines = conf.splitlines()
    assert "acl allowed_ips dst 10.0.0.0/8 fd00::/8" in lines
    assert "http_access allow allowed_ips" in lines
    assert lines.index("http_access allow allowed_ips") < lines.index("http_access deny all")
    # Extra ports work for plain HTTP and for HTTPS via CONNECT
    assert "acl Safe_ports port 80 443 11434" in lines
    assert "acl SSL_ports port 443 11434" in lines


def test_render_ips_only_has_no_empty_domain_acl() -> None:
    # Squid refuses an ACL line without values
    conf = render_squid_conf(ProxyConfig(allowed_ips=("192.168.178.10",)))
    assert "acl allowed_domains" not in conf
    assert "http_access allow allowed_domains" not in conf
    assert "acl allowed_ips dst 192.168.178.10" in conf.splitlines()


def test_render_dedupes_default_ports() -> None:
    conf = render_squid_conf(ProxyConfig(allowed_domains=("a.b",), allowed_ports=(443, 8443, 8443)))
    assert "acl Safe_ports port 80 443 8443" in conf.splitlines()


def test_render_reads_dynamic_exceptions_from_files() -> None:
    conf = render_squid_conf(ProxyConfig(allowed_domains=("example.com",)))
    lines = conf.splitlines()
    assert 'acl dynamic_domains dstdomain "/etc/squid/agent-sandbox/domains.txt"' in lines
    assert 'acl dynamic_ips dst "/etc/squid/agent-sandbox/ips.txt"' in lines
    for acl in ("dynamic_domains", "dynamic_ips"):
        # After the port checks, so exceptions can't open other ports
        assert lines.index("http_access deny CONNECT !SSL_ports") < lines.index(f"http_access allow {acl}")
        assert lines.index(f"http_access allow {acl}") < lines.index("http_access deny all")
