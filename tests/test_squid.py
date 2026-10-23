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
