"""Start a repo's FastAPI app from a snapshot so documented examples can run against it.

The snapshot is written to a temp dir; dependencies go into a venv cached under
`.driftless_cache/venvs/<hash of requirements + python version>` (built with `uv` when it is on
PATH, else `venv` + pip). Each `serve()` starts uvicorn on a free port and always kills the
whole process tree afterwards (on Windows a venv's python.exe is a launcher with a child
process, so killing the parent alone would leave the server running).

This runs the repository's code on this machine: trust.py decides whose code that may be, and
isolation.py keeps it away from Driftless's secrets (clean env, wheels-only installs, and on
Linux an unprivileged SANDBOX_USER with process limits, where the host allows the switch).
"""

import ast
import hashlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import httpx

from driftless.isolation import IsolationError, SandboxUser, active, clean_env, resolve_sandbox_user, safe_requirements

if TYPE_CHECKING:
    from driftless.config import Settings

IS_WINDOWS = os.name == "nt"
_PREFERRED_APP_FILES = ("main.py", "app.py", "server.py", "api.py", "asgi.py")


class SandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class SandboxLimits:
    """Where venvs are cached and how long each step may take (small hosts want tighter ones)."""

    cache_dir: Path = Path(".driftless_cache")
    startup_timeout: float = 30.0  # until the app answers HTTP
    install_timeout: float = 600.0  # building a venv for new requirements
    request_timeout: float = 10.0  # each documented request
    user: str = ""  # SANDBOX_USER: run installs and the app as this user (Linux, server as root)

    @classmethod
    def from_settings(cls, settings: "Settings") -> "SandboxLimits":
        return cls(
            Path(settings.sandbox_cache_dir),
            settings.sandbox_startup_timeout,
            settings.sandbox_install_timeout,
            settings.example_request_timeout,
            settings.sandbox_user,
        )


@dataclass(frozen=True)
class AppTarget:
    module: str  # "app.main"
    attribute: str  # "app"
    app_dir: str  # directory (relative to the repo root) that must be on sys.path

    @property
    def spec(self) -> str:
        return f"{self.module}:{self.attribute}"


def find_fastapi_app(files: dict[str, str]) -> AppTarget | None:
    """The module-level `x = FastAPI(...)` object, preferring main.py/app.py and shallow paths."""
    found: list[tuple[tuple[int, int, str], AppTarget]] = []
    for path, content in files.items():
        p = PurePosixPath(path)
        if p.suffix != ".py" or any(part in ("tests", "test", "docs", "examples") for part in p.parts):
            continue
        if "FastAPI" not in content:
            continue
        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue
        attribute = _fastapi_assignment(tree)
        if attribute is None:
            continue
        parts = list(p.with_suffix("").parts)
        app_dir = ""
        if parts[0] == "src" and len(parts) > 1:
            app_dir, parts = "src", parts[1:]
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        rank = (0 if p.name in _PREFERRED_APP_FILES else 1, len(p.parts), path)
        found.append((rank, AppTarget(".".join(parts), attribute, app_dir)))
    return min(found, key=lambda item: item[0])[1] if found else None


def _fastapi_assignment(tree: ast.Module) -> str | None:
    for node in tree.body:
        if isinstance(node, ast.Assign | ast.AnnAssign) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if name == "FastAPI" and isinstance(targets[0], ast.Name):
                return targets[0].id
    return None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def sandbox_env(values: dict[str, str], unset: Collection[str] = (), home: Path | None = None) -> dict[str, str]:
    """An allow-listed environment (never our credentials) minus `unset`, plus `values`."""
    return clean_env(values, unset, home)


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


_BUILD_LOCKS: dict[str, threading.Lock] = {}
_BUILD_LOCKS_GUARD = threading.Lock()


def ensure_venv(requirements: str, cache_dir: Path, timeout: float = 600, user: SandboxUser | None = None) -> Path:
    """Return the python of a cached venv with `requirements` + uvicorn installed (wheels only).

    With a sandbox `user`, the install runs as that user with a fresh HOME (no shared download
    cache) and the finished venv is locked (root-owned, read-only), so one run can't plant code in
    a venv a later run reuses. Builds of the same requirements are serialized."""
    try:
        requirements = safe_requirements(requirements)
    except IsolationError as exc:
        raise SandboxError(str(exc)) from exc
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    key = hashlib.sha256(f"{version}\n{requirements}".encode()).hexdigest()[:16]
    with _BUILD_LOCKS_GUARD:
        build_lock = _BUILD_LOCKS.setdefault(key, threading.Lock())
    with build_lock:
        return _build_venv(requirements, cache_dir, key, timeout, user)


def _build_venv(requirements: str, cache_dir: Path, key: str, timeout: float, user: SandboxUser | None) -> Path:
    venvs = cache_dir / "venvs"
    venv = venvs / key
    python = venv_python(venv)
    ready = venv / ".driftless-ready"
    if ready.exists() and python.exists() and (user is None or ready.stat().st_uid == os.geteuid()):
        return python
    shutil.rmtree(venv, ignore_errors=True)  # a half-built venv from an interrupted run
    venvs.mkdir(parents=True, exist_ok=True)
    req_file = venvs / f"{key}-requirements.txt"
    req_file.write_text(requirements + "\nuvicorn\n", encoding="utf-8")
    home = Path(tempfile.mkdtemp(prefix="home-", dir=cache_dir))  # HOME (pip/uv caches) of this install only
    extra: dict[str, Any] = {}
    if user is not None:
        for path in (cache_dir, venvs):
            os.chmod(path, 0o755)  # traversable, but only root may create venvs in it
        os.chmod(req_file, 0o644)
        venv.mkdir()
        user.give(venv)
        user.give(home)
        extra = user.popen_kwargs()
    uv = shutil.which("uv")
    if uv:
        steps = [
            [uv, "venv", "--quiet", "--allow-existing", "--python", sys.executable, str(venv)],
            [uv, "pip", "install", "--quiet", "--only-binary", ":all:", "--python", str(python), "-r", str(req_file)],
        ]
    else:
        steps = [
            [sys.executable, "-m", "venv", str(venv)],
            [
                str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-input",
                "--only-binary=:all:", "-r", str(req_file),
            ],
        ]
    env = sandbox_env({"UV_LINK_MODE": "copy"}, home=home)
    deadline = time.monotonic() + timeout
    active.enter()
    try:
        for step in steps:
            try:
                subprocess.run(
                    user.limited(step, serve=False) if user else step,
                    check=True, capture_output=True, text=True, timeout=max(1.0, deadline - time.monotonic()),
                    env=env, stdin=subprocess.DEVNULL, **extra,
                )
            except subprocess.TimeoutExpired as exc:
                raise SandboxError(f"installing requirements timed out after {timeout:.0f}s") from exc
            except subprocess.CalledProcessError as exc:
                raise SandboxError(f"{Path(step[0]).name} failed: {(exc.stderr or exc.stdout).strip()[-800:]}") from exc
            except OSError as exc:  # e.g. the user switch refused after all
                raise SandboxError(f"could not start {Path(step[0]).name}: {exc}") from exc
        if user is not None:
            try:
                user.lock(venv)
            except (IsolationError, OSError) as exc:
                raise SandboxError(f"could not lock the venv: {exc}") from exc
    except BaseException:
        shutil.rmtree(venv, ignore_errors=True)
        raise
    finally:
        active.leave(user)
        shutil.rmtree(home, ignore_errors=True)
    ready.write_text(requirements, encoding="utf-8")
    return python


class Sandbox:
    """`with Sandbox(files) as box:` then `with box.serve(env) as base_url:` per fresh app."""

    def __init__(
        self,
        files: dict[str, str],
        cache_dir: Path = Path(".driftless_cache"),
        python: str | None = None,  # override the venv (tests use the current interpreter)
        startup_timeout: float = 30.0,
        install_timeout: float = 600.0,
        user: str = "",  # SANDBOX_USER (see isolation.py)
    ) -> None:
        self._files = files
        self._cache_dir = cache_dir
        self._python = python
        self._startup_timeout = startup_timeout
        self._install_timeout = install_timeout
        self._user_name = user
        self._user: SandboxUser | None = None
        self.isolation_note: str | None = None  # set when SANDBOX_USER couldn't be used on this host
        self.target = find_fastapi_app(files)
        self._workdir: Path | None = None

    def __enter__(self) -> "Sandbox":
        if self.target is None:
            raise SandboxError("no FastAPI app found (expected a module-level `app = FastAPI(...)`)")
        self._user, self.isolation_note = resolve_sandbox_user(self._user_name)
        self._workdir = Path(tempfile.mkdtemp(prefix="driftless-sandbox-"))
        try:
            self._write_files(self._workdir.resolve())
            if self._user is not None:
                try:
                    self._user.give(self._workdir)
                except OSError as exc:
                    raise SandboxError(f"could not hand the sandbox to {self._user.name}: {exc}") from exc
            if self._python is None:
                requirements = self._files.get("requirements.txt", "fastapi\n")
                self._python = str(
                    ensure_venv(requirements, self._cache_dir.resolve(), self._install_timeout, self._user)
                )
        except BaseException:
            self.__exit__()
            raise
        return self

    def _write_files(self, root: Path) -> None:
        for path, content in self._files.items():
            if not safe_relative_path(path):
                continue
            dest = (root / path).resolve()
            if not dest.is_relative_to(root):
                continue  # never write outside the sandbox
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

    def __exit__(self, *exc: object) -> None:
        if self._workdir is not None:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None

    @contextmanager
    def serve(self, env: dict[str, str] | None = None, unset: Collection[str] = ()) -> Iterator[str]:
        """Start a fresh app instance; yield its base URL; always kill it afterwards."""
        assert self._workdir is not None and self.target is not None and self._python is not None
        port = free_port()
        log_path = self._workdir / f".driftless-uvicorn-{port}.log"
        command = [
            self._python, "-m", "uvicorn", self.target.spec,
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
            "--app-dir", str(self._workdir / self.target.app_dir),
        ]
        user = self._user
        extra: dict[str, Any] = {**_new_process_group(), **(user.popen_kwargs() if user else {})}
        active.enter()
        try:
            with open(log_path, "w", encoding="utf-8", errors="replace") as log:
                try:
                    process = subprocess.Popen(
                        user.limited(command, serve=True) if user else command,
                        cwd=self._workdir,
                        env=sandbox_env(env or {}, unset, home=self._workdir),
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        close_fds=True,
                        **extra,
                    )
                except OSError as exc:
                    raise SandboxError(f"could not start the app: {exc}") from exc
                try:
                    base_url = f"http://127.0.0.1:{port}"
                    self._wait_until_up(process, base_url, log_path)
                    yield base_url
                finally:
                    kill_tree(process)
        finally:
            active.leave(user)  # the last one out kills daemons the app left behind

    def _wait_until_up(self, process: subprocess.Popen[bytes], base_url: str, log_path: Path) -> None:
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SandboxError(f"the app exited during startup (code {process.returncode}):\n{_tail(log_path)}")
            try:
                httpx.get(base_url + "/", timeout=1.0)
                return  # any HTTP response means it's serving
            except httpx.HTTPError:
                time.sleep(0.2)
        raise SandboxError(f"the app did not respond within {self._startup_timeout:.0f}s:\n{_tail(log_path)}")


def safe_relative_path(path: str) -> bool:
    """A plain relative POSIX path: not absolute, no `..`, no backslashes or drive letters."""
    p = PurePosixPath(path)
    return bool(path) and not p.is_absolute() and ".." not in p.parts and "\\" not in path and ":" not in path


def _new_process_group() -> dict[str, object]:
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, timeout=10, check=False
            )
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""
