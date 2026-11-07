"""The agent's working copy, and the folders for exchanging files with it.

By default the agent works in a clone of the current git repository instead
of the directory itself, so nothing it writes there (hooks, git config,
ignored files) ends up in the user's repository. Its commits are brought back
with `git fetch`, which copies objects and refs but runs none of the clone's
hooks. Outside a git repository the current directory is mounted read-only
instead, unless read-write access is asked for explicitly.

Each workspace also has an inbox (read-only for the agent) and an outbox
(writable), for untracked files such as binaries. Workspaces outlive the
session, so their contents can be fetched afterwards, and are removed with
`discard`.
"""

import json
import logging
import os
import posixpath
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

Runner = Callable[..., subprocess.CompletedProcess[Any]]

DEFAULT_WORKDIR = "/workspace"
INBOX_PATH = "/exchange/in"
OUTBOX_PATH = "/exchange/out"
REPO_DIR = "repo"
INBOX_DIR = "inbox"
OUTBOX_DIR = "outbox"
META_FILE = "workspace.json"
# Branch the agent works on in the clone
AGENT_BRANCH = "agent"
# Fetched branches of workspace ID land in refs/remotes/sandbox/ID/
REMOTE_NAMESPACE = "sandbox"

CLONE, READ_ONLY, READ_WRITE = "clone", "ro", "rw"


class WorkspaceError(Exception):
    """Raised when a workspace cannot be created, found or used."""


def git(*args: str, cwd: Path, runner: Runner = subprocess.run, **kwargs: Any) -> str:
    """Run git in `cwd` and return its stdout, raising WorkspaceError on failure."""
    cmd = ["git", "-C", str(cwd), *args]
    log.debug("Running %s", cmd)
    try:
        result = runner(cmd, check=False, capture_output=True, **{"text": True, **kwargs})
    except FileNotFoundError as e:
        raise WorkspaceError("git not found in PATH") from e
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace") if isinstance(result.stderr, bytes) else result.stderr
        raise WorkspaceError((stderr or "").strip() or f"git {args[0]} failed with exit code {result.returncode}")
    return result.stdout


@dataclass(frozen=True)
class Mount:
    """What the agent service needs to see a workspace."""

    volumes: tuple[str, ...]
    working_dir: str
    env: dict[str, str] = field(default_factory=dict)
    # Host directory the agent must not get write access to by other means
    protected: Path | None = None


@dataclass(frozen=True)
class Workspace:
    """One workspace: a clone or a mount of `source`, plus the exchange folders."""

    id: str
    directory: Path
    source: Path
    mode: str
    created: datetime
    # The clone only: cwd relative to the repository root, and the commit it started from
    subdir: str = ""
    base: str | None = None

    @property
    def repo(self) -> Path:
        """Return the clone."""
        return self.directory / REPO_DIR

    @property
    def inbox(self) -> Path:
        """Return the folder for files to the agent."""
        return self.directory / INBOX_DIR

    @property
    def outbox(self) -> Path:
        """Return the folder for files from the agent."""
        return self.directory / OUTBOX_DIR

    @property
    def remote_prefix(self) -> str:
        """Return the ref namespace that `fetch` writes to in the source repository."""
        return f"refs/remotes/{REMOTE_NAMESPACE}/{self.id}/"

    @property
    def agent_ref(self) -> str:
        """Return the short name of the agent's branch once fetched."""
        return f"{REMOTE_NAMESPACE}/{self.id}/{AGENT_BRANCH}"

    def mount(self, workdir: str) -> Mount:
        """Return the volumes and working directory for the agent service."""
        exchange = (f"{self.inbox}:{INBOX_PATH}:ro,Z", f"{self.outbox}:{OUTBOX_PATH}:Z")
        env = {"SANDBOX_INBOX": INBOX_PATH, "SANDBOX_OUTBOX": OUTBOX_PATH}
        if self.mode == CLONE:
            return Mount(
                volumes=(f"{self.repo}:{workdir}:Z", *exchange),
                working_dir=posixpath.join(workdir, self.subdir) if self.subdir else workdir,
                env=env,
                protected=self.source,
            )
        options = "Z" if self.mode == READ_WRITE else "ro,Z"
        return Mount(
            volumes=(f"{self.source}:{workdir}:{options}", *exchange),
            working_dir=workdir,
            env=env,
            protected=self.source if self.mode == READ_ONLY else None,
        )

    def outbox_entries(self) -> list[str]:
        """Return the names in the outbox."""
        return sorted(p.name for p in self.outbox.iterdir()) if self.outbox.is_dir() else []

    def to_json(self) -> str:
        """Serialize the metadata."""
        data = {
            "id": self.id,
            "source": str(self.source),
            "mode": self.mode,
            "created": self.created.isoformat(),
            "subdir": self.subdir,
            "base": self.base,
        }
        return json.dumps(data, indent=2) + "\n"

    @classmethod
    def from_json(cls, directory: Path, text: str) -> "Workspace":
        """Deserialize metadata, raising ValueError, KeyError or TypeError if it is malformed."""
        data = json.loads(text)
        mode = data["mode"]
        if mode not in (CLONE, READ_ONLY, READ_WRITE):
            raise ValueError(f"unknown mode {mode!r}")
        return cls(
            id=data["id"],
            directory=directory,
            source=Path(data["source"]),
            mode=mode,
            created=datetime.fromisoformat(data["created"]),
            subdir=data.get("subdir", ""),
            base=data.get("base"),
        )


def _clone(source: Path, target: Path, runner: Runner) -> tuple[Path, str, str]:
    """Clone the repository containing `source` to `target` and check out AGENT_BRANCH.

    Uncommitted changes to tracked files are carried over, untracked files are
    not. Return the repository root, `source` relative to it, and the commit
    the clone starts from.
    """
    top = Path(git("rev-parse", "--show-toplevel", cwd=source, runner=runner).strip())
    base = git("rev-parse", "--verify", "--quiet", "HEAD", cwd=top, runner=runner).strip()
    git("clone", "--quiet", "--no-hardlinks", "--", str(top), str(target), cwd=top, runner=runner)
    git("switch", "--quiet", "-C", AGENT_BRANCH, base, cwd=target, runner=runner)
    # Only the agent's branch is local, so `fetch` brings back nothing else.
    # The source's branches stay visible as origin/*.
    local = git("for-each-ref", "--format=%(refname:short)", "refs/heads/", cwd=target, runner=runner).split()
    if others := [b for b in local if b != AGENT_BRANCH]:
        git("branch", "--quiet", "-D", *others, cwd=target, runner=runner)
    # No external diff or textconv drivers, the output must be a plain patch
    diff = git("diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", cwd=top, runner=runner, text=False)
    if diff:
        try:
            git("apply", "--index", "--whitespace=nowarn", cwd=target, runner=runner, input=diff, text=False)
            log.info("Carried uncommitted changes over to the clone")
        except WorkspaceError as e:
            log.warning("Could not carry uncommitted changes over to the clone: %s", e)
    untracked = git("ls-files", "--others", "--exclude-standard", "-z", cwd=top, runner=runner).split("\0")
    if count := sum(1 for name in untracked if name):
        log.warning("%d untracked file(s) are not in the clone, use `agent-sandbox put` to hand them over", count)
    subdir = source.resolve().relative_to(top.resolve()).as_posix()
    return top, "" if subdir == "." else subdir, base


@dataclass(frozen=True)
class WorkspaceStore:
    """All workspaces, one directory each below `root`."""

    root: Path
    runner: Runner = subprocess.run

    def create(self, workspace_id: str, source: Path, *, read_write: bool = False) -> Workspace:
        """Create a workspace for `source`.

        A clone if `source` is in a git repository with at least one commit,
        else a read-only mount, or a read-write mount if `read_write` is set.
        `read_write` skips the clone entirely.
        """
        directory = self.root / workspace_id
        directory.mkdir(parents=True)
        try:
            (directory / INBOX_DIR).mkdir()
            (directory / OUTBOX_DIR).mkdir()
            workspace = self._make(workspace_id, directory, source, read_write=read_write)
            (directory / META_FILE).write_text(workspace.to_json())
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return workspace

    def _make(self, workspace_id: str, directory: Path, source: Path, *, read_write: bool) -> Workspace:
        now = datetime.now().astimezone()
        if read_write:
            log.warning("Mounting %s read-write, the agent can change anything in it", source)
            return Workspace(workspace_id, directory, source, READ_WRITE, now)
        try:
            top, subdir, base = _clone(source, directory / REPO_DIR, self.runner)
        except WorkspaceError as e:
            shutil.rmtree(directory / REPO_DIR, ignore_errors=True)
            log.warning("Cannot clone %s (%s), mounting it read-only. Use --rw for write access", source, e)
            return Workspace(workspace_id, directory, source, READ_ONLY, now)
        log.info("Agent works in a clone of %s on branch %r", top, AGENT_BRANCH)
        return Workspace(workspace_id, directory, top, CLONE, now, subdir=subdir, base=base)

    def get(self, workspace_id: str) -> Workspace:
        """Return the workspace with this ID."""
        directory = self.root / workspace_id
        try:
            return Workspace.from_json(directory, (directory / META_FILE).read_text())
        except FileNotFoundError:
            raise WorkspaceError(f"No workspace {workspace_id!r}") from None
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise WorkspaceError(f"Unreadable workspace {workspace_id!r}: {e}") from e

    def workspaces(self) -> list[Workspace]:
        """Return all readable workspaces, oldest first."""
        if not self.root.is_dir():
            return []
        found = []
        for meta in self.root.glob(f"*/{META_FILE}"):
            try:
                found.append(Workspace.from_json(meta.parent, meta.read_text()))
            except (OSError, ValueError, KeyError, TypeError) as e:
                log.warning("Ignoring unreadable workspace %s: %s", meta.parent, e)
        return sorted(found, key=lambda w: w.created)

    def select(self, workspace_id: str | None, cwd: Path) -> Workspace:
        """Return the workspace with this ID, else the only one, else the only one for `cwd`."""
        if workspace_id is not None:
            return self.get(workspace_id)
        found = self.workspaces()
        if not found:
            raise WorkspaceError("No workspaces")
        if len(found) == 1:
            return found[0]
        cwd = cwd.resolve()
        here = [w for w in found if cwd == w.source.resolve() or cwd.is_relative_to(w.source.resolve())]
        if len(here) == 1:
            return here[0]
        ids = ", ".join(w.id for w in (here or found))
        raise WorkspaceError(f"Several workspaces ({ids}), pick one by ID")

    def discard(self, workspace: Workspace) -> list[str]:
        """Delete the workspace and the refs fetched from it, return the deleted refs."""
        refs: list[str] = []
        if workspace.mode == CLONE and workspace.source.is_dir():
            try:
                refs = fetched_refs(workspace, runner=self.runner)
                if refs:
                    commands = "".join(f"delete {ref}\n" for ref in refs)
                    git("update-ref", "--stdin", cwd=workspace.source, runner=self.runner, input=commands)
            except WorkspaceError as e:
                log.warning("Could not delete the fetched refs in %s: %s", workspace.source, e)
                refs = []
        shutil.rmtree(workspace.directory)
        return refs


def fetched_refs(workspace: Workspace, *, runner: Runner = subprocess.run) -> list[str]:
    """Return the refs fetched from `workspace` into its source repository."""
    out = git("for-each-ref", "--format=%(refname)", workspace.remote_prefix, cwd=workspace.source, runner=runner)
    return out.split()


def fetch(workspace: Workspace, *, runner: Runner = subprocess.run) -> list[str]:
    """Fetch all branches of the clone into the source repository, return the resulting refs.

    Fetching from the untrusted clone is safe: git runs upload-pack there,
    which runs no hooks and ignores the dangerous settings in repository
    config, and nothing is checked out.
    """
    if workspace.mode != CLONE:
        raise WorkspaceError(f"Workspace {workspace.id} is a {workspace.mode} mount, there is nothing to fetch")
    if not workspace.source.is_dir():
        raise WorkspaceError(f"The source repository {workspace.source} is gone")
    refspec = f"+refs/heads/*:{workspace.remote_prefix}*"
    git(
        "fetch",
        "--quiet",
        "--no-tags",
        "--prune",
        "--",
        str(workspace.repo),
        refspec,
        cwd=workspace.source,
        runner=runner,
    )
    return fetched_refs(workspace, runner=runner)


def put(workspace: Workspace, paths: Iterable[Path]) -> list[str]:
    """Copy files and directories into the inbox, return their names there."""
    names = []
    for path in paths:
        if not path.exists():
            raise WorkspaceError(f"{path} does not exist")
        target = workspace.inbox / path.resolve().name
        if path.is_dir():
            shutil.copytree(path, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(path, target)
        names.append(target.name)
    return names


def get(workspace: Workspace, names: Sequence[str], dest: Path, *, keep_mode: bool = False) -> list[Path]:
    """Copy `names` (all if empty) from the outbox to `dest`, return the copied files.

    The outbox was written by the agent, so symlinks are skipped (they could
    point at host files) and executable, setuid and setgid bits are dropped
    unless `keep_mode` is set.
    """
    outbox = workspace.outbox.resolve()
    sources = []
    for name in names or workspace.outbox_entries():
        source = workspace.outbox / name
        # The name itself must stay inside, and so must the directory it is in
        # (symlinked directories on the way are the agent's doing). A symlink
        # as the last component is skipped when copying.
        if Path(name).is_absolute() or ".." in Path(name).parts or not source.parent.resolve().is_relative_to(outbox):
            raise WorkspaceError(f"{name} is outside the outbox")
        if not source.exists() and not source.is_symlink():
            raise WorkspaceError(f"{name} is not in the outbox")
        sources.append(source)
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for source in sources:
        copied.extend(_copy_out(source, dest / source.name, keep_mode=keep_mode))
    return copied


def _copy_out(source: Path, target: Path, *, keep_mode: bool) -> list[Path]:
    if source.is_symlink():
        log.warning("Skipping symlink %s", source)
        return []
    if source.is_dir():
        target.mkdir(exist_ok=True)
        copied = []
        for child in sorted(source.iterdir()):
            copied.extend(_copy_out(child, target / child.name, keep_mode=keep_mode))
        return copied
    if not source.is_file():
        log.warning("Skipping %s, not a regular file", source)
        return []
    shutil.copyfile(source, target)
    mode = stat.S_IMODE(source.stat().st_mode)
    target.chmod(mode & ~(stat.S_ISUID | stat.S_ISGID) if keep_mode else mode & 0o666)
    return [target]


def protected_volume_conflicts(volumes: Iterable[str], protected: Path) -> tuple[list[str], list[str]]:
    """Split writable volume specs into ones exposing `protected` (or a part of it) and ones exposing a parent.

    Specs must already have absolute sources, see compose.resolve_volume.
    """
    exact, parents = [], []
    protected = Path(os.path.realpath(protected))
    for spec in volumes:
        source, sep, rest = spec.partition(":")
        if not sep or not source.startswith("/") or "ro" in rest.partition(":")[2].split(","):
            continue
        resolved = Path(os.path.realpath(source))
        if resolved == protected or resolved.is_relative_to(protected):
            exact.append(spec)
        elif protected.is_relative_to(resolved):
            parents.append(spec)
    return exact, parents
