set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

# Install/sync dependencies
sync:
    uv sync

# Format code
fmt:
    uv run ruff format .

# Check formatting without modifying files
fmt-check:
    uv run ruff format --check .

# Lint with ruff (pass --fix to auto-fix; used by the pre-commit hook)
lint *args:
    uv run ruff check {{args}} .

# Type-check with ty
typecheck:
    uv run ty check

# Lint, type-check, and verify formatting (no modifications)
check: lint typecheck fmt-check

# Install git hooks so checks run automatically on commit
hooks-install:
    uv run prek install

# Tag the next version (major/minor/patch, default patch), e.g. `just bump minor`
# Version comes from the latest vX.Y.Z git tag (uv-dynamic-versioning); push the tag yourself
bump level="patch":
    #!/usr/bin/env bash
    set -euo pipefail
    latest="$(git tag --list 'v*' --sort=-v:refname | head -n1)"
    IFS=. read -r major minor patch <<< "${latest#v}"
    case "{{level}}" in
        major) major=$((major + 1)); minor=0; patch=0 ;;
        minor) minor=$((minor + 1)); patch=0 ;;
        patch) patch=$((patch + 1)) ;;
        *) echo "Unknown level: {{level}}" >&2; exit 1 ;;
    esac
    new="v${major}.${minor}.${patch}"
    git tag -a "$new" -m "$new"
    echo "Tagged $new"
    # uv's build cache is keyed by source mtimes, not by git tag, so a bump on an
    # otherwise-unchanged tree needs a touch to be picked up; drop stale dist artifacts too
    touch pyproject.toml
    rm -rf dist
    uv sync

# Build and install the `krun` OCI runtime (libkrunfw + libkrun + crun --with-libkrun) on Debian.
# Not packaged for Debian, so this builds from source; needs sudo. See README's microVM section.
# Pinned to known-compatible release tags: crun's krun handler targets libkrun's 1.x API, and
# libkrun's `main` branch is an unreleased, API-incompatible 2.0 line.
krun-build:
    #!/usr/bin/env bash
    set -euo pipefail
    libkrunfw_ref=v5.6.2
    libkrun_ref=v1.19.8
    crun_ref=1.30.1
    if command -v krun >/dev/null; then
        echo "krun already installed: $(command -v krun)" >&2
        exit 0
    fi
    command -v cargo >/dev/null || { echo "Rust/cargo not found; install via https://rustup.rs first" >&2; exit 1; }
    command -v rustup >/dev/null || { echo "rustup not found; libkrun's init binary needs a musl target, install Rust via https://rustup.rs first" >&2; exit 1; }
    rustup target add "$(uname -m)-unknown-linux-musl"

    sudo apt-get update
    sudo apt-get install -y \
        build-essential clang libclang-dev pkg-config patchelf python3-pyelftools \
        flex bison libelf-dev \
        autoconf automake libtool pkgconf libcap-dev libseccomp-dev libyajl-dev libjson-c-dev \
        libsystemd-dev go-md2man git

    # Persistent cache dir, not a mktemp one: a failed run can resume from here instead of
    # re-cloning and recompiling (cargo, autoconf, make) from scratch every retry.
    workdir="${XDG_CACHE_HOME:-$HOME/.cache}/agent-sandbox/krun-build"
    mkdir -p "$workdir"
    clone_or_reuse() {
        local dir=$1 url=$2 ref=$3
        if [ -d "$dir/.git" ]; then
            echo "Reusing existing checkout at $dir" >&2
        else
            git clone --depth 1 --branch "$ref" "$url" "$dir"
        fi
    }

    clone_or_reuse "$workdir/libkrunfw" https://github.com/containers/libkrunfw "$libkrunfw_ref"
    make -C "$workdir/libkrunfw" -j"$(nproc)"
    sudo make -C "$workdir/libkrunfw" install
    sudo ldconfig

    clone_or_reuse "$workdir/libkrun" https://github.com/containers/libkrun "$libkrun_ref"
    make -C "$workdir/libkrun" -j"$(nproc)" BLK=1 NET=1 VHOST_USER=1
    sudo make -C "$workdir/libkrun" BLK=1 NET=1 VHOST_USER=1 install
    # libkrun's Makefile installs into $PREFIX/lib64, which Debian's dynamic linker doesn't
    # search by default (only $PREFIX/lib and the multiarch dir are configured)
    echo /usr/local/lib64 | sudo tee /etc/ld.so.conf.d/libkrun.conf >/dev/null
    sudo ldconfig

    clone_or_reuse "$workdir/crun" https://github.com/containers/crun "$crun_ref"
    [ -f "$workdir/crun/Makefile" ] || (cd "$workdir/crun" && ./autogen.sh && ./configure --with-libkrun)
    make -C "$workdir/crun" -j"$(nproc)"
    sudo make -C "$workdir/crun" install

    sudo ln -sf /usr/local/bin/crun /usr/local/bin/krun
    echo "Installed $(krun --version | head -n1) as krun"

# Run tests
test:
    uv run pytest

# Serve docs locally with live reload
docs:
    uv run zensical serve

# Build static docs site
docs-build:
    uv run zensical build

# Build the sdist and wheel
build:
    uv build

# Run the app, e.g. `just run hello`
run *args:
    uv run agent-sandbox {{args}}

# Full check: what CI runs
ci: sync check test docs-build build
