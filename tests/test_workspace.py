import subprocess
from pathlib import Path

import pytest

from agent_sandbox.workspace import (
    AGENT_BRANCH,
    CLONE,
    INBOX_PATH,
    OUTBOX_PATH,
    READ_ONLY,
    READ_WRITE,
    WorkspaceError,
    WorkspaceStore,
    fetch,
    get,
    protected_volume_conflicts,
    put,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Keep the user's git config out of the tests
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in {"NAME": "Test", "EMAIL": "test@example.com"}.items():
        monkeypatch.setenv(f"GIT_AUTHOR_{key}", value)
        monkeypatch.setenv(f"GIT_COMMITTER_{key}", value)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    (path / "src").mkdir(parents=True)
    _git(path, "init", "--quiet", "-b", "main")
    (path / "README").write_text("hello\n")
    (path / "src" / "main.c").write_text("int main;\n")
    _git(path, "add", ".")
    _git(path, "commit", "--quiet", "-m", "initial")
    return path


@pytest.fixture
def store(tmp_path: Path) -> WorkspaceStore:
    return WorkspaceStore(tmp_path / "state" / "workspaces")


def test_clone_of_git_repository(store: WorkspaceStore, repo: Path) -> None:
    ws = store.create("abcd1234", repo)
    assert ws.mode == CLONE
    assert ws.source == repo
    assert ws.base == _git(repo, "rev-parse", "HEAD").strip()
    assert (ws.repo / "README").read_text() == "hello\n"
    assert _git(ws.repo, "branch", "--show-current").strip() == AGENT_BRANCH
    assert _git(ws.repo, "for-each-ref", "--format=%(refname)", "refs/heads/").split() == ["refs/heads/agent"]
    assert "refs/remotes/origin/main" in _git(ws.repo, "for-each-ref", "--format=%(refname)").split()
    assert ws.inbox.is_dir()
    assert ws.outbox.is_dir()
    # The clone must not share hooks or config with the source
    assert not (ws.repo / ".git" / "objects" / "info" / "alternates").exists()


def test_clone_mount(store: WorkspaceStore, repo: Path) -> None:
    mount = store.create("abcd1234", repo / "src").mount("/workspace")
    ws = store.get("abcd1234")
    assert mount.volumes == (
        f"{ws.repo}:/workspace:Z",
        f"{ws.inbox}:{INBOX_PATH}:ro,Z",
        f"{ws.outbox}:{OUTBOX_PATH}:Z",
    )
    # Started in a subdirectory, the agent starts in the same one
    assert mount.working_dir == "/workspace/src"
    assert mount.protected == repo
    assert mount.env == {"SANDBOX_INBOX": INBOX_PATH, "SANDBOX_OUTBOX": OUTBOX_PATH}


def test_clone_carries_uncommitted_changes(store: WorkspaceStore, repo: Path) -> None:
    (repo / "README").write_text("changed\n")
    (repo / "untracked").write_text("x")
    ws = store.create("abcd1234", repo)
    assert (ws.repo / "README").read_text() == "changed\n"
    assert not (ws.repo / "untracked").exists()


def test_does_not_copy_hooks(store: WorkspaceStore, repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\ntouch hook-ran\n")
    hook.chmod(0o755)
    ws = store.create("abcd1234", repo)
    assert not (ws.repo / ".git" / "hooks" / "post-checkout").exists()


@pytest.mark.parametrize(("read_write", "mode", "options"), [(False, READ_ONLY, "ro,Z"), (True, READ_WRITE, "Z")])
def test_outside_git_mounts_directory(
    store: WorkspaceStore, tmp_path: Path, read_write: bool, mode: str, options: str
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    ws = store.create("abcd1234", plain, read_write=read_write)
    assert ws.mode == mode
    assert not ws.repo.exists()
    mount = ws.mount("/w")
    assert mount.volumes[0] == f"{plain}:/w:{options}"
    assert mount.working_dir == "/w"
    assert mount.protected == (None if read_write else plain)


def test_rw_skips_clone_in_git_repository(store: WorkspaceStore, repo: Path) -> None:
    assert store.create("abcd1234", repo, read_write=True).mode == READ_WRITE


def test_repository_without_commits_falls_back_to_read_only(store: WorkspaceStore, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "--quiet")
    ws = store.create("abcd1234", empty)
    assert ws.mode == READ_ONLY
    assert not ws.repo.exists()


def test_store_lists_and_selects(store: WorkspaceStore, repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    first = store.create("aaaa0000", repo)
    second = store.create("bbbb1111", other)
    assert [w.id for w in store.workspaces()] == ["aaaa0000", "bbbb1111"]
    assert store.get("bbbb1111") == second
    assert store.select(None, repo / "src") == first
    assert store.select("bbbb1111", repo) == second
    with pytest.raises(WorkspaceError, match="Several workspaces"):
        store.select(None, tmp_path)
    with pytest.raises(WorkspaceError, match="No workspace 'nope'"):
        store.get("nope")


def test_fetch_and_discard(store: WorkspaceStore, repo: Path) -> None:
    ws = store.create("abcd1234", repo)
    (ws.repo / "new.txt").write_text("from the agent\n")
    _git(ws.repo, "add", "new.txt")
    _git(ws.repo, "commit", "--quiet", "-m", "agent work")
    _git(ws.repo, "branch", "side")

    refs = fetch(ws)
    assert refs == ["refs/remotes/sandbox/abcd1234/agent", "refs/remotes/sandbox/abcd1234/side"]
    assert _git(repo, "log", "--format=%s", f"main..{ws.agent_ref}").strip() == "agent work"
    # Nothing checked out or merged
    assert not (repo / "new.txt").exists()
    assert _git(repo, "branch", "--show-current").strip() == "main"

    assert store.discard(ws) == refs
    assert not ws.directory.exists()
    assert _git(repo, "for-each-ref", "refs/remotes/sandbox/").strip() == ""


def test_fetch_needs_clone(store: WorkspaceStore, tmp_path: Path) -> None:
    ws = store.create("abcd1234", tmp_path)
    with pytest.raises(WorkspaceError, match="nothing to fetch"):
        fetch(ws)


def test_put_and_get(store: WorkspaceStore, tmp_path: Path) -> None:
    ws = store.create("abcd1234", tmp_path, read_write=True)
    src = tmp_path / "data"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "f").write_text("x")
    single = tmp_path / "model.bin"
    single.write_bytes(b"\0\1")
    assert put(ws, [src, single]) == ["data", "model.bin"]
    assert (ws.inbox / "data" / "sub" / "f").read_text() == "x"
    assert (ws.inbox / "model.bin").read_bytes() == b"\0\1"

    binary = ws.outbox / "app"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o4755)
    (ws.outbox / "dir").mkdir()
    (ws.outbox / "dir" / "result.txt").write_text("ok")
    (ws.outbox / "secret").symlink_to(Path.home() / ".ssh" / "id_ed25519")
    dest = tmp_path / "out"
    copied = get(ws, [], dest)
    assert sorted(p.relative_to(dest).as_posix() for p in copied) == ["app", "dir/result.txt"]
    assert (dest / "app").stat().st_mode & 0o7777 == 0o644
    assert not (dest / "secret").exists()

    get(ws, ["app"], dest, keep_mode=True)
    assert (dest / "app").stat().st_mode & 0o7777 == 0o755


@pytest.mark.parametrize("name", ["../x", "/etc/passwd", "link/x"])
def test_get_stays_in_outbox(store: WorkspaceStore, tmp_path: Path, name: str) -> None:
    ws = store.create("abcd1234", tmp_path, read_write=True)
    (ws.outbox / "link").symlink_to(tmp_path)
    with pytest.raises(WorkspaceError, match="outside the outbox"):
        get(ws, [name], tmp_path / "out")


def test_get_missing(store: WorkspaceStore, tmp_path: Path) -> None:
    ws = store.create("abcd1234", tmp_path, read_write=True)
    with pytest.raises(WorkspaceError, match="not in the outbox"):
        get(ws, ["nope"], tmp_path / "out")


def test_protected_volume_conflicts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    exact, parents = protected_volume_conflicts(
        [
            f"{repo}:/x",
            f"{repo}/sub:/y:Z",
            f"{repo}:/ro:ro,Z",
            f"{tmp_path}:/home",
            "/elsewhere:/e",
            "named:/data",
            "/anonymous",
        ],
        repo,
    )
    assert exact == [f"{repo}:/x", f"{repo}/sub:/y:Z"]
    assert parents == [f"{tmp_path}:/home"]


def test_unreadable_workspace_is_skipped(store: WorkspaceStore, tmp_path: Path) -> None:
    store.create("good0000", tmp_path, read_write=True)
    broken = store.root / "bad00000"
    broken.mkdir()
    (broken / "workspace.json").write_text("{")
    assert [w.id for w in store.workspaces()] == ["good0000"]
    assert broken.is_dir()
