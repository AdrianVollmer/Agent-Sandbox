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
