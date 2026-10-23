"""Named domain lists for common package registries."""

_PYPI = ("pypi.org", "files.pythonhosted.org")
_GITHUB_RELEASES = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")

PRESETS: dict[str, tuple[str, ...]] = {
    "debian": ("deb.debian.org", "security.debian.org"),
    # Leading dot covers country mirrors like de.archive.ubuntu.com
    "ubuntu": (".archive.ubuntu.com", "security.ubuntu.com", "ports.ubuntu.com"),
    "alpine": ("dl-cdn.alpinelinux.org",),
    "pypi": _PYPI,
    # uv fetches Python builds from GitHub releases (python-build-standalone)
    "uv": (*_PYPI, *_GITHUB_RELEASES, "astral.sh"),
    "npm": ("registry.npmjs.org", "registry.yarnpkg.com"),
    "cargo": ("crates.io", "index.crates.io", "static.crates.io", "static.rust-lang.org"),
    "go": ("proxy.golang.org", "sum.golang.org"),
    # Required hosts from https://code.claude.com/docs/en/network-config, without
    # optional telemetry and storage.googleapis.com (plugin metadata)
    "claude-code": (
        "api.anthropic.com",
        "mcp-proxy.anthropic.com",
        "claude.ai",
        "downloads.claude.ai",
        "claude.com",
        "platform.claude.com",
        "sso.claude.com",
        "code.claude.com",
    ),
    "github": ("github.com", "api.github.com", "codeload.github.com", ".githubusercontent.com"),
}
