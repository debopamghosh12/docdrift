"""Start a repo's FastAPI app from a snapshot so documented examples can run against it.

The snapshot is written to a temp dir; dependencies go into a venv cached under
`.driftless_cache/venvs/<hash of requirements + python version>` (built with `uv` when it is on
PATH, else `venv` + pip). Each `serve()` starts uvicorn on a free port and always kills the
whole process tree afterwards (on Windows a venv's python.exe is a launcher with a child
process, so killing the parent alone would leave the server running).

This runs the repository's code on this machine: only use it on repositories you trust.
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
import time
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import httpx

IS_WINDOWS = os.name == "nt"
# Env var names never passed to the app: our own credentials.
_SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PRIVATE_KEY")
_DROPPED_ENV = ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")
_PREFERRED_APP_FILES = ("main.py", "app.py", "server.py", "api.py", "asgi.py")


class SandboxError(RuntimeError):
    pass


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


def sandbox_env(values: dict[str, str], unset: Collection[str] = ()) -> dict[str, str]:
    """The host environment minus credentials and `unset` (variables the app reads), plus `values`."""
    dropped = {name.upper() for name in unset}
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _DROPPED_ENV
        and k.upper() not in dropped
        and not any(marker in k.upper() for marker in _SECRET_MARKERS)
    }
    env.update(values)
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    return env


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def ensure_venv(requirements: str, cache_dir: Path, timeout: float = 600) -> Path:
    """Return the python of a cached venv with `requirements` + uvicorn installed."""
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    key = hashlib.sha256(f"{version}\n{requirements}".encode()).hexdigest()[:16]
    venv = cache_dir / "venvs" / key
    python = venv_python(venv)
    ready = venv / ".driftless-ready"
    if ready.exists() and python.exists():
        return python
    shutil.rmtree(venv, ignore_errors=True)  # a half-built venv from an interrupted run
    venv.parent.mkdir(parents=True, exist_ok=True)
    req_file = venv.parent / f"{key}-requirements.txt"
    req_file.write_text(requirements + "\nuvicorn\n", encoding="utf-8")
    uv = shutil.which("uv")
    if uv:
        steps = [
            [uv, "venv", "--quiet", "--python", sys.executable, str(venv)],
            [uv, "pip", "install", "--quiet", "--python", str(python), "-r", str(req_file)],
        ]
    else:
        steps = [
            [sys.executable, "-m", "venv", str(venv)],
            [str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", str(req_file)],
        ]
    deadline = time.monotonic() + timeout
    for step in steps:
        try:
            subprocess.run(
                step, check=True, capture_output=True, text=True, timeout=max(1.0, deadline - time.monotonic()),
                env=sandbox_env({}),
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"installing requirements timed out after {timeout:.0f}s") from exc
        except subprocess.CalledProcessError as exc:
            raise SandboxError(f"{Path(step[0]).name} failed: {(exc.stderr or exc.stdout).strip()[-800:]}") from exc
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
    ) -> None:
        self._files = files
        self._cache_dir = cache_dir
        self._python = python
        self._startup_timeout = startup_timeout
        self._install_timeout = install_timeout
        self.target = find_fastapi_app(files)
        self._workdir: Path | None = None

    def __enter__(self) -> "Sandbox":
        if self.target is None:
            raise SandboxError("no FastAPI app found (expected a module-level `app = FastAPI(...)`)")
        self._workdir = Path(tempfile.mkdtemp(prefix="driftless-sandbox-"))
        try:
            self._write_files(self._workdir.resolve())
            if self._python is None:
                requirements = self._files.get("requirements.txt", "fastapi\n")
                self._python = str(ensure_venv(requirements, self._cache_dir.resolve(), self._install_timeout))
        except BaseException:
            self.__exit__()
            raise
        return self

    def _write_files(self, root: Path) -> None:
        for path, content in self._files.items():
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
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(
                command,
                cwd=self._workdir,
                env=sandbox_env(env or {}, unset),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                **_new_process_group(),
            )
            try:
                base_url = f"http://127.0.0.1:{port}"
                self._wait_until_up(process, base_url, log_path)
                yield base_url
            finally:
                kill_tree(process)

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
