import re

from agent_sandbox.presets import PRESETS

_DOMAIN_RE = re.compile(r"^\.?[a-z0-9.-]+\.[a-z]+$")


def test_preset_domains_are_valid() -> None:
    for name, domains in PRESETS.items():
        assert domains, name
        for domain in domains:
            assert _DOMAIN_RE.match(domain), (name, domain)


def test_uv_includes_pypi_and_python_downloads() -> None:
    assert set(PRESETS["pypi"]) <= set(PRESETS["uv"])
    assert "release-assets.githubusercontent.com" in PRESETS["uv"]


def test_claude_code_includes_api_and_auth_hosts() -> None:
    # Claude Code's startup check needs both, see code.claude.com/docs/en/network-config
    assert {"api.anthropic.com", "platform.claude.com", "claude.ai"} <= set(PRESETS["claude-code"])
