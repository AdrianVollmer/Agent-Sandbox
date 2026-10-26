import socket
from typing import Any

import pytest

from agent_sandbox.config import ProxyConfig
from agent_sandbox.hostdns import pinnable_names, resolve_on_host


def fake_getaddrinfo(table: dict[str, list[str]]) -> Any:
    def resolve(host: str, *_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in table[host]]

    return resolve


def test_pinnable_names_skips_wildcards() -> None:
    proxy = ProxyConfig(allowed_domains=("ai.syss.intern", ".example.com", "Example.org"))
    assert pinnable_names(proxy) == ["ai.syss.intern", "example.org"]


def test_resolve_on_host_dedupes_and_sorts() -> None:
    resolve = fake_getaddrinfo({"a.intern": ["10.0.0.2", "10.0.0.1", "10.0.0.2"], "b.intern": ["10.0.0.3"]})
    assert resolve_on_host(["a.intern", "b.intern"], resolve=resolve) == {
        "a.intern": ["10.0.0.1", "10.0.0.2"],
        "b.intern": ["10.0.0.3"],
    }


def test_resolve_on_host_skips_failures(caplog: pytest.LogCaptureFixture) -> None:
    resolve = fake_getaddrinfo({"a.intern": ["10.0.0.1"]})
    assert resolve_on_host(["a.intern", "missing.intern"], resolve=resolve) == {"a.intern": ["10.0.0.1"]}
    assert "missing.intern" in caplog.text


def test_resolve_on_host_asks_for_ipv4_only() -> None:
    seen: dict[str, Any] = {}

    def resolve(host: str, port: Any, family: int = 0, *_args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        seen["family"] = kwargs.get("family", family)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0))]

    resolve_on_host(["a.intern"], resolve=resolve)
    # The container networks are IPv4 only
    assert seen["family"] == socket.AF_INET
