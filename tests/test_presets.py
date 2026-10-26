import re

import pytest

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


# Hosts observed in a proxy access log while running the tools (2026-10-23)
@pytest.mark.parametrize(
    ("preset", "hosts"),
    [
        # go install / go get / GOTOOLCHAIN switch; module zips redirect to GCS
        ("go", {"proxy.golang.org", "sum.golang.org", "storage.googleapis.com"}),
        # rustup install, components, targets, cargo fetch
        ("rust", {"sh.rustup.rs", "static.rust-lang.org", "index.crates.io", "static.crates.io"}),
        # installer, version index, sigstore verification, tools from GitHub
        # releases (python, aqua), node and go downloads
        (
            "mise",
            {
                "mise.run",
                "mise.jdx.dev",
                "mise-versions.jdx.dev",
                "tuf-repo-cdn.sigstore.dev",
                "github.com",
                "release-assets.githubusercontent.com",
                "nodejs.org",
                "dl.google.com",
            },
        ),
    ],
)
def test_preset_covers_observed_hosts(preset: str, hosts: set[str]) -> None:
    assert hosts <= set(PRESETS[preset])


def test_cargo_is_part_of_rust() -> None:
    assert set(PRESETS["cargo"]) <= set(PRESETS["rust"])


def test_jsr_covers_deno_and_npm_compat_hosts() -> None:
    # Deno fetches meta.json and modules from jsr.io, npm/pnpm/yarn get
    # metadata and tarballs from npm.jsr.io (checked with curl, 2026-10-26)
    assert set(PRESETS["jsr"]) == {"jsr.io", "npm.jsr.io"}
