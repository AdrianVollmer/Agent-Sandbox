import stat
from pathlib import Path

import pytest

from agent_sandbox.allowlist import DynamicAllowlist, parse_destination
from agent_sandbox.config import ConfigError


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Example.COM", "example.com"),
        (".example.org", ".example.org"),
        ("10.1.2.3", "10.1.2.3/32"),
        ("10.0.0.0/8", "10.0.0.0/8"),
        ("fd00::1", "fd00::1/128"),
    ],
)
def test_parse_destination(value: str, expected: str) -> None:
    assert parse_destination(value) == expected


@pytest.mark.parametrize("value", ["", "exa mple.com", "example.com:443", "http://example.com", "10.1.2.3/8", "-a.com"])
def test_parse_destination_rejects_garbage(value: str) -> None:
    # A bad entry makes Squid die on reconfigure, so nothing unvalidated may reach the files
    with pytest.raises(ConfigError):
        parse_destination(value)


@pytest.fixture
def allowlist(tmp_path: Path) -> DynamicAllowlist:
    allowlist = DynamicAllowlist(tmp_path / "dynamic")
    allowlist.create()
    return allowlist


def test_create_makes_empty_files_readable_by_the_proxy_user(allowlist: DynamicAllowlist) -> None:
    assert allowlist.entries() == []
    for path in (allowlist.directory, *allowlist.directory.iterdir()):
        assert stat.S_IMODE(path.stat().st_mode) & 0o005 == (0o005 if path.is_dir() else 0o004)
    assert sorted(p.name for p in allowlist.directory.iterdir()) == ["domains.txt", "ips.txt"]


def test_add_splits_domains_and_ips(allowlist: DynamicAllowlist) -> None:
    added = allowlist.add(["pypi.org", "10.0.0.5", "pypi.org"])
    assert added == ["pypi.org", "10.0.0.5/32"]
    assert (allowlist.directory / "domains.txt").read_text() == "pypi.org\n"
    assert (allowlist.directory / "ips.txt").read_text() == "10.0.0.5/32\n"
    assert allowlist.add(["PyPI.org"]) == []
    assert allowlist.entries() == ["pypi.org", "10.0.0.5/32"]


def test_add_wildcard_drops_covered_domains(allowlist: DynamicAllowlist) -> None:
    # Squid refuses a dstdomain ACL holding both a domain and a covering wildcard
    allowlist.add(["files.example.com"])
    allowlist.add([".example.com"])
    assert allowlist.entries() == [".example.com"]


def test_add_is_all_or_nothing(allowlist: DynamicAllowlist) -> None:
    with pytest.raises(ConfigError):
        allowlist.add(["good.com", "bad domain"])
    assert allowlist.entries() == []


def test_remove(allowlist: DynamicAllowlist) -> None:
    allowlist.add(["a.com", "b.com", "192.168.0.0/16"])
    assert allowlist.remove(["a.com", "192.168.0.0/16", "c.com"]) == ["a.com", "192.168.0.0/16"]
    assert allowlist.entries() == ["b.com"]


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("pypi.org", True),
        ("files.pypi.org", False),
        ("example.com", True),
        ("www.example.com", True),
        ("notexample.com", False),
        ("10.1.2.3", True),
        ("10.2.0.1", False),
    ],
)
def test_allows(allowlist: DynamicAllowlist, host: str, allowed: bool) -> None:
    allowlist.add(["pypi.org", ".example.com", "10.1.0.0/16"])
    assert allowlist.allows(host) is allowed
