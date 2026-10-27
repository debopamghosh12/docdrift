"""How the app under test is kept away from Driftless's own secrets and state.

Defense in depth behind trust.py (which decides *whose* code may run at all):

- A clean environment: an allow-list of variables (PATH, locale, temp dirs, Windows basics),
  never the host's environment minus a deny-list.
- Requirements are plain `name[extras]<spec>` lines installed from wheels only: no pip options
  (`-r /proc/1/environ` would echo a file into the error), no URLs or paths, no `setup.py` runs.
- On Linux, when the server runs as root with `SANDBOX_USER` set (the Docker image does this),
  every install and app process runs as that unprivileged user: it can't read the server's
  `/proc/<pid>/environ`, its SQLite database (the server's umask is 077) or the cached venvs
  (root-owned, read-only), and gets process/file limits via `prlimit`. Leftover processes of
  that user (double-forked daemons) are killed whenever no install or app is running.

Residual risk (SECURITY.md): network egress is open, and without SANDBOX_USER the app runs as
the server's own user.
"""

import os
import re
import shutil
import signal
import stat
import threading
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = os.name == "nt"
# Passed through from the host: what Python, uv and pip need to start, nothing that identifies us.
_PASSTHROUGH = frozenset({"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "UV_LINK_MODE"})
_WINDOWS_PASSTHROUGH = frozenset({
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
})
# `name`, `name[extra,...]`, then optional version specifiers and an environment marker.
_REQUIREMENT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,\s-]*\])?\s*"
    r"([<>=!~]=?\s*[A-Za-z0-9.*+!_-]+\s*(,\s*[<>=!~]=?\s*[A-Za-z0-9.*+!_-]+\s*)*)?"
    r"(;\s*[A-Za-z0-9_.\s<>=!~'\"()-]+)?$"
)
PROCESS_LIMITS = ("--nproc=256", "--nofile=1024")
SERVE_LIMITS = ("--fsize=268435456",)  # 256 MB per file written by the app


class IsolationError(RuntimeError):
    pass


def clean_env(values: Mapping[str, str], unset: Collection[str] = (), home: Path | None = None) -> dict[str, str]:
    """An allow-listed environment plus `values` (the app's own variables), minus `unset`."""
    keep = _PASSTHROUGH | (_WINDOWS_PASSTHROUGH if IS_WINDOWS else frozenset())
    dropped = {name.upper() for name in unset}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep and k.upper() not in dropped}
    env.update(values)
    if home is not None:
        env.update(HOME=str(home), TMPDIR=str(home), XDG_CACHE_HOME=str(home / ".cache"))
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1")
    return env


def safe_requirements(text: str) -> str:
    """The requirements as plain lines; raises IsolationError on options, URLs, paths or VCS refs."""
    lines = []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if not line:
            continue
        if line.startswith("-") or "://" in line or "@" in line or "/" in line or "\\" in line or not _REQUIREMENT_RE.match(line):
            raise IsolationError(
                f"unsupported line in requirements.txt: {line[:60]!r} (only `package[extras]<version>` lines are installed)"
            )
        lines.append(line)
    return "\n".join(lines)


@dataclass(frozen=True)
class SandboxUser:
    """The unprivileged user the app runs as (Linux, server running as root)."""

    name: str
    uid: int
    gid: int

    def popen_kwargs(self) -> dict[str, object]:
        return {"user": self.uid, "group": self.gid, "extra_groups": []}

    def give(self, path: Path) -> None:
        """Hand `path` (recursively) to the sandbox user."""
        for item in [path, *path.rglob("*")]:
            os.chown(item, self.uid, self.gid, follow_symlinks=False)

    def lock(self, path: Path) -> None:
        """Make `path` root-owned and read-only for everyone else (a finished venv).

        Race-safe against the sandbox user swapping entries while we walk: directories are walked
        by file descriptor (`os.fwalk`, never following symlinks), every entry is opened with
        O_NOFOLLOW relative to its directory's fd, and only entries owned by the sandbox user are
        changed, via fchown/fchmod on that fd. A swap therefore fails the lock (and the install)
        instead of redirecting root's chown/chmod; a foreign file hardlinked in is refused."""
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        root_fd = os.open(path, flags | os.O_DIRECTORY)
        try:
            self._lock_fd(root_fd)
        finally:
            os.close(root_fd)
        for _, dirs, files, dir_fd in os.fwalk(path, follow_symlinks=False):
            for name in [*dirs, *files]:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    if info.st_uid == self.uid:
                        os.chown(name, 0, 0, dir_fd=dir_fd, follow_symlinks=False)
                    continue
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise IsolationError(f"unexpected file type in the venv: {name}")
                fd = os.open(name, flags, dir_fd=dir_fd)
                try:
                    self._lock_fd(fd)
                finally:
                    os.close(fd)

    def _lock_fd(self, fd: int) -> None:
        info = os.fstat(fd)
        if info.st_uid == 0:
            return
        if info.st_uid != self.uid:
            raise IsolationError("a file not created by the install was found in the venv")
        os.fchown(fd, 0, 0)
        mode = stat.S_IMODE(info.st_mode)
        os.fchmod(fd, (mode | 0o444 | (0o111 if mode & 0o100 else 0)) & ~0o7022)

    def limited(self, command: list[str], serve: bool) -> list[str]:
        prlimit = shutil.which("prlimit")
        if prlimit is None:  # util-linux: in every Debian-based image, so this is a broken host
            raise IsolationError("prlimit is required with SANDBOX_USER (install util-linux)")
        return [prlimit, *PROCESS_LIMITS, *(SERVE_LIMITS if serve else ()), "--", *command]


def sandbox_user(name: str) -> SandboxUser | None:
    """The user to drop to, or None (Windows/macOS dev boxes, or not configured)."""
    if not name or IS_WINDOWS:
        return None
    import pwd

    if os.geteuid() != 0:
        raise IsolationError(f"SANDBOX_USER={name} needs the server to run as root (to switch to that user)")
    try:
        entry = pwd.getpwnam(name)
    except KeyError as exc:
        raise IsolationError(f"SANDBOX_USER={name} doesn't exist") from exc
    if entry.pw_uid == 0:
        raise IsolationError("SANDBOX_USER must not be root")
    if shutil.which("prlimit") is None:
        raise IsolationError("SANDBOX_USER needs prlimit (util-linux) to limit the app's processes")
    return SandboxUser(name, entry.pw_uid, entry.pw_gid)


class _Active:
    """Counts running sandboxes so leftovers are reaped only when none is running."""

    def __init__(self) -> None:
        self._count = 0
        self._lock = threading.Lock()

    def enter(self) -> None:
        with self._lock:
            self._count += 1

    def leave(self, user: SandboxUser | None) -> None:
        with self._lock:
            self._count -= 1
            if self._count == 0 and user is not None:
                kill_user_processes(user.uid)


active = _Active()


def kill_user_processes(uid: int) -> int:
    """SIGKILL every process owned by `uid` (escaped daemons of finished runs). Linux only."""
    killed = 0
    proc = Path("/proc")
    if not proc.is_dir():
        return 0
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid == uid:
                os.kill(int(entry.name), signal.SIGKILL)
                killed += 1
        except (OSError, ProcessLookupError):
            continue
    return killed
