"""GROMACS MCP server.

Wraps the local ``gmx`` binary so an MCP client can drive a full
molecular-dynamics workflow: build topology, define a box, solvate, add ions,
preprocess, run simulations in the background, and analyse trajectories.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

_fcntl: Any | None
try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows fallback
    _fcntl = None

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

GMX_BIN = os.environ.get("GMX_BIN") or shutil.which("gmx") or "/opt/homebrew/bin/gmx"

ROOT = Path(os.environ.get("GROMACS_MCP_ROOT", Path.home() / "gromacs-mcp" / "projects"))
ROOT = ROOT.expanduser().resolve()
ROOT.mkdir(parents=True, exist_ok=True)

JOBS_FILE = ROOT.parent / ".jobs.json"
JOBS_LOCK_FILE = ROOT.parent / ".jobs.lock"
JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)

MAX_STREAM_CHARS = 4000
MAX_TEXT_READ_CHARS = 20000
MAX_TEXT_WRITE_CHARS = 500000
MAX_TIMEOUT_SECONDS = 3600
ROOT_VERSION = "sandbox-v1"

TextMaxChars = Annotated[int, Field(ge=1, le=MAX_TEXT_READ_CHARS)]
TimeoutSeconds = Annotated[int, Field(ge=1, le=MAX_TIMEOUT_SECONDS)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
MaxWarn = Annotated[int, Field(ge=0, le=10)]
BoxType = Literal["cubic", "triclinic", "dodecahedron", "octahedron"]
PbcMode = Literal["mol", "res", "atom", "nojump", "cluster", "whole"]
EmIntegrator = Literal["steep", "cg"]
Thermostat = Literal["V-rescale", "Berendsen", "Nose-Hoover", "no"]
Barostat = Literal["Parrinello-Rahman", "Berendsen", "C-rescale", "no"]

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
WRITE_ONLY = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
RUN_LOCAL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)

TEXT_SUFFIXES = {
    "",
    ".csv",
    ".dat",
    ".err",
    ".gro",
    ".itp",
    ".log",
    ".mdp",
    ".ndx",
    ".out",
    ".pdb",
    ".top",
    ".txt",
    ".xvg",
}
BINARY_SUFFIXES = {
    ".cpt",
    ".edr",
    ".png",
    ".p12",
    ".pfx",
    ".tng",
    ".tpr",
    ".trr",
    ".xtc",
}
SENSITIVE_NAMES = {
    ".env",
    ".env.local",
    ".netrc",
    "authorized_keys",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "known_hosts",
}
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}

JobRegistry = dict[str, dict[str, Any]]

mcp = FastMCP("gromacs")


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}


def _ok(**extra: Any) -> dict[str, Any]:
    return {"ok": True, **extra}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _resolve_workdir(workdir: str) -> Path:
    """Resolve a work directory and keep it under ROOT by default."""
    if not str(workdir).strip():
        raise ValueError("workdir must not be empty")

    raw = Path(workdir)
    if raw.is_absolute():
        if not _truthy_env("GROMACS_MCP_ALLOW_ABSOLUTE_WORKDIRS"):
            raise ValueError(
                "absolute workdir values are disabled; use a relative workdir under "
                "GROMACS_MCP_ROOT or set GROMACS_MCP_ALLOW_ABSOLUTE_WORKDIRS=1"
            )
        candidate = raw.expanduser().resolve()
    else:
        if str(workdir).startswith("~"):
            raise ValueError("workdir must not start with '~'")
        if ".." in raw.parts:
            raise ValueError("workdir must not contain '..'")
        candidate = (ROOT / raw).resolve()
        if not _is_relative_to(candidate, ROOT):
            raise ValueError(f"workdir escapes project root: {workdir}")

    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _workdir_or_error(workdir: str) -> tuple[Path | None, dict[str, Any] | None]:
    try:
        return _resolve_workdir(workdir), None
    except ValueError as exc:
        return None, _error(str(exc))


def _normalize_relative_path(raw: str, field: str, *, allow_hidden: bool = False) -> Path:
    value = str(raw)
    if not value.strip():
        raise ValueError(f"{field} must not be empty")
    if "\x00" in value:
        raise ValueError(f"{field} must not contain NUL bytes")
    if value.startswith("~"):
        raise ValueError(f"{field} must not start with '~'")

    path = Path(value)
    if path.is_absolute():
        raise ValueError(f"{field} must be relative to the workdir")
    if path in {Path("."), Path("")}:
        raise ValueError(f"{field} must name a file")
    if ".." in path.parts:
        raise ValueError(f"{field} must not contain '..'")
    if not allow_hidden and any(
        part.startswith(".") for part in path.parts if part not in {".", ""}
    ):
        raise ValueError(f"{field} must not contain hidden path components")
    return path


def _validate_token(token: str, field: str) -> None:
    value = str(token)
    if not value.strip():
        raise ValueError(f"{field} contains an empty argument")
    if "\x00" in value:
        raise ValueError(f"{field} contains a NUL byte")
    if value.startswith("~") or Path(value).is_absolute():
        raise ValueError(f"{field} contains a path outside the workdir: {value}")
    normalized = value.replace("\\", "/")
    if (
        normalized == ".."
        or normalized.startswith("../")
        or normalized.endswith("/..")
        or "/../" in normalized
        or ".." in Path(normalized).parts
    ):
        raise ValueError(f"{field} contains path traversal: {value}")


def _validate_arg_tokens(args: list[str] | None, field: str) -> list[str]:
    if not args:
        return []
    for arg in args:
        _validate_token(arg, field)
    return args


def _safe_deffnm(value: str, field: str = "deffnm") -> str:
    rel = _normalize_relative_path(value, field)
    if len(rel.parts) != 1:
        raise ValueError(f"{field} must be a simple file stem, not a path")
    suffix = rel.suffix.lower()
    if suffix in BINARY_SUFFIXES or suffix in SENSITIVE_SUFFIXES:
        raise ValueError(f"{field} has a blocked suffix: {suffix}")
    return rel.as_posix()


def _blocked_text_reason(path: Path) -> str:
    name = path.name
    suffix = path.suffix.lower()
    if name in SENSITIVE_NAMES or suffix in SENSITIVE_SUFFIXES:
        return f"sensitive file is blocked: {name}"
    if suffix in BINARY_SUFFIXES:
        return f"binary file type is blocked: {suffix}"
    if suffix not in TEXT_SUFFIXES:
        return f"unsupported text file suffix: {suffix or '<none>'}"
    return ""


def _safe_path(
    wd: Path,
    raw: str,
    field: str,
    *,
    must_exist: bool = False,
    for_write: bool = False,
    text_file: bool = False,
    allow_hidden: bool = False,
) -> Path:
    rel = _normalize_relative_path(raw, field, allow_hidden=allow_hidden)
    candidate = (wd / rel).resolve(strict=False)
    root = wd.resolve()
    if not _is_relative_to(candidate, root):
        raise ValueError(f"{field} escapes workdir: {raw}")
    if must_exist and not candidate.exists():
        raise ValueError(f"{field} not found: {rel.as_posix()}")
    if text_file:
        reason = _blocked_text_reason(candidate)
        if reason:
            raise ValueError(reason)
    if for_write:
        parent = candidate.parent.resolve(strict=False)
        if not _is_relative_to(parent, root):
            raise ValueError(f"{field} parent escapes workdir: {raw}")
        parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _safe_file_arg(
    wd: Path,
    raw: str,
    field: str,
    *,
    must_exist: bool = False,
    for_write: bool = False,
    text_file: bool = False,
) -> str:
    path = _safe_path(
        wd,
        raw,
        field,
        must_exist=must_exist,
        for_write=for_write,
        text_file=text_file,
    )
    return path.relative_to(wd.resolve()).as_posix()


def _validate_gromacs_library_arg(raw: str, field: str) -> str:
    _validate_token(raw, field)
    if any(part.startswith(".") for part in Path(raw).parts if part not in {".", ""}):
        raise ValueError(f"{field} must not contain hidden path components")
    return raw


def _guarded_tool_call(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return fn()
    except ValueError as exc:
        return _error(str(exc))
    except OSError as exc:
        return _error(str(exc))


def _clip(text: str | None) -> str:
    """Truncate a stream to head+tail so long GROMACS logs do not flood context."""
    if text is None:
        return ""
    if len(text) <= MAX_STREAM_CHARS:
        return text
    half = MAX_STREAM_CHARS // 2
    omitted = len(text) - 2 * half
    return f"{text[:half]}\n... [{omitted} characters truncated] ...\n{text[-half:]}"


def _run(
    args: list[str],
    workdir: Path,
    stdin: str | None = None,
    timeout: int | None = 600,
) -> dict[str, Any]:
    """Run ``gmx <args>`` synchronously inside *workdir*."""
    cmd = [GMX_BIN, *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workdir),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return _error(
            f"gmx not found at '{GMX_BIN}'. Set the GMX_BIN env var.",
            command=" ".join(shlex.quote(c) for c in cmd),
        )
    except subprocess.TimeoutExpired:
        return _error(
            (
                f"Command timed out after {timeout}s. For long simulations use "
                "mdrun_start (background) instead of a blocking call."
            ),
            command=" ".join(shlex.quote(c) for c in cmd),
        )

    result: dict[str, Any] = {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "command": " ".join(shlex.quote(c) for c in cmd),
        "workdir": str(workdir),
        "stdout": _clip(proc.stdout),
        "stderr": _clip(proc.stderr),
    }
    if proc.returncode != 0:
        summary = _extract_fatal_error(proc.stderr) or _extract_fatal_error(proc.stdout)
        if summary:
            result["error_summary"] = summary
    return result


def _extract_fatal_error(stream: str | None) -> str:
    """Pull the GROMACS 'Fatal error' block out of an output stream."""
    if not stream or "Fatal error:" not in stream:
        return ""
    lines = stream.splitlines()
    start = next(i for i, ln in enumerate(lines) if "Fatal error:" in ln)
    msg = []
    for ln in lines[start + 1 :]:
        if ln.startswith("For more information") or set(ln.strip()) == {"-"}:
            break
        msg.append(ln)
    return " ".join(part.strip() for part in msg if part.strip())


# --------------------------------------------------------------------------- #
# Job registry helpers
# --------------------------------------------------------------------------- #


@contextmanager
def _jobs_file_lock() -> Iterator[None]:
    JOBS_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with JOBS_LOCK_FILE.open("a+") as lock_fh:
        if _fcntl is not None:
            _fcntl.flock(lock_fh.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            if _fcntl is not None:
                _fcntl.flock(lock_fh.fileno(), _fcntl.LOCK_UN)


def _load_jobs_unlocked() -> JobRegistry:
    if JOBS_FILE.exists():
        try:
            data = json.loads(JOBS_FILE.read_text())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_jobs_unlocked(jobs: JobRegistry) -> None:
    tmp = JOBS_FILE.with_name(f"{JOBS_FILE.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(jobs, indent=2, sort_keys=True))
    os.replace(tmp, JOBS_FILE)


def _load_jobs() -> JobRegistry:
    with _jobs_file_lock():
        return _load_jobs_unlocked()


def _save_jobs(jobs: JobRegistry) -> None:
    with _jobs_file_lock():
        _save_jobs_unlocked(jobs)


def _update_jobs(mutator: Callable[[JobRegistry], Any]) -> Any:
    with _jobs_file_lock():
        jobs = _load_jobs_unlocked()
        result = mutator(jobs)
        _save_jobs_unlocked(jobs)
        return result


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _job_process_alive(job: dict[str, Any]) -> bool:
    pid = int(job.get("pid") or 0)
    if pid <= 0 or not _pid_alive(pid):
        return False
    pgid = job.get("pgid")
    if pgid is None:
        return True
    try:
        return os.getpgid(pid) == int(pgid)
    except (ProcessLookupError, PermissionError):
        return False


def _refresh_job(job: dict[str, Any]) -> str:
    running = _job_process_alive(job)
    if running and job.get("stopped_at"):
        return "stopping"
    if running:
        return "running"
    if not job.get("finished_at"):
        job["finished_at"] = _now()
        job.setdefault("exit_status", "unknown")
    return "stopped" if job.get("stopped_at") else "finished"


def _tail(path: Path, n_lines: int = 40) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n_lines:])


def _job_path(job: dict[str, Any], key: str) -> Path:
    wd = Path(str(job.get("workdir", ""))).expanduser().resolve()
    path = Path(str(job.get(key, ""))).expanduser().resolve(strict=False)
    if not _is_relative_to(path, wd):
        raise ValueError(f"job {key} escapes recorded workdir")
    return path


def _parse_progress(workdir: Path, deffnm: str, capture_log: Path) -> dict[str, str]:
    progress: dict[str, str] = {}

    md_log = workdir / f"{deffnm}.log"
    if md_log.exists():
        try:
            lines = md_log.read_text(errors="replace").splitlines()
        except OSError:
            lines = []
        for i in range(len(lines) - 1, 0, -1):
            if lines[i - 1].split()[:2] == ["Step", "Time"]:
                parts = lines[i].split()
                if len(parts) >= 2:
                    progress["current_step"] = parts[0]
                    progress["current_time_ps"] = parts[1]
                break
        for i, ln in enumerate(lines):
            if ln.strip().startswith("Performance:"):
                vals = lines[i].split()
                if len(vals) >= 2:
                    progress["performance_ns_per_day"] = vals[1]

    cap = capture_log.read_text(errors="replace") if capture_log.exists() else ""
    for ln in reversed(cap.splitlines()):
        if "remaining wall clock time" in ln:
            progress["eta"] = ln.strip()
            break

    return progress


# --------------------------------------------------------------------------- #
# MDP templates
# --------------------------------------------------------------------------- #


def _mdp_lines(values: dict[str, Any]) -> str:
    return "\n".join(f"{key:<24}= {value}" for key, value in values.items()) + "\n"


def _em_mdp(
    integrator: EmIntegrator = "steep",
    emtol: float = 1000.0,
    emstep: float = 0.01,
    nsteps: int = 50000,
) -> str:
    return _mdp_lines(
        {
            "integrator": integrator,
            "emtol": emtol,
            "emstep": emstep,
            "nsteps": nsteps,
            "cutoff-scheme": "Verlet",
            "nstlist": 10,
            "rcoulomb": 1.0,
            "rvdw": 1.0,
            "pbc": "xyz",
        }
    )


def _nvt_mdp(
    nsteps: int = 50000,
    dt: float = 0.002,
    ref_t: float = 300.0,
    tau_t: float = 0.1,
    tcoupl: Thermostat = "V-rescale",
) -> str:
    return _mdp_lines(
        {
            "define": "-DPOSRES",
            "integrator": "md",
            "dt": dt,
            "nsteps": nsteps,
            "nstxout-compressed": 500,
            "nstenergy": 500,
            "nstlog": 500,
            "continuation": "no",
            "constraint_algorithm": "lincs",
            "constraints": "h-bonds",
            "cutoff-scheme": "Verlet",
            "rcoulomb": 1.0,
            "rvdw": 1.0,
            "tcoupl": tcoupl,
            "tc-grps": "Protein Non-Protein",
            "tau_t": f"{tau_t} {tau_t}",
            "ref_t": f"{ref_t} {ref_t}",
            "pcoupl": "no",
            "pbc": "xyz",
            "gen_vel": "yes",
            "gen_temp": ref_t,
            "gen_seed": -1,
        }
    )


def _npt_mdp(
    nsteps: int = 50000,
    dt: float = 0.002,
    ref_t: float = 300.0,
    ref_p: float = 1.0,
    tcoupl: Thermostat = "V-rescale",
    pcoupl: Barostat = "Parrinello-Rahman",
) -> str:
    return _mdp_lines(
        {
            "define": "-DPOSRES",
            "integrator": "md",
            "dt": dt,
            "nsteps": nsteps,
            "nstxout-compressed": 500,
            "nstenergy": 500,
            "nstlog": 500,
            "continuation": "yes",
            "constraint_algorithm": "lincs",
            "constraints": "h-bonds",
            "cutoff-scheme": "Verlet",
            "rcoulomb": 1.0,
            "rvdw": 1.0,
            "tcoupl": tcoupl,
            "tc-grps": "Protein Non-Protein",
            "tau_t": "0.1 0.1",
            "ref_t": f"{ref_t} {ref_t}",
            "pcoupl": pcoupl,
            "pcoupltype": "isotropic",
            "tau_p": 2.0,
            "ref_p": ref_p,
            "compressibility": "4.5e-5",
            "pbc": "xyz",
            "gen_vel": "no",
        }
    )


def _md_mdp(
    nsteps: int = 500000,
    dt: float = 0.002,
    ref_t: float = 300.0,
    ref_p: float = 1.0,
    tcoupl: Thermostat = "V-rescale",
    pcoupl: Barostat = "Parrinello-Rahman",
) -> str:
    return _mdp_lines(
        {
            "integrator": "md",
            "dt": dt,
            "nsteps": nsteps,
            "nstxout-compressed": 5000,
            "nstenergy": 1000,
            "nstlog": 1000,
            "continuation": "yes",
            "constraint_algorithm": "lincs",
            "constraints": "h-bonds",
            "cutoff-scheme": "Verlet",
            "rcoulomb": 1.0,
            "rvdw": 1.0,
            "tcoupl": tcoupl,
            "tc-grps": "Protein Non-Protein",
            "tau_t": "0.1 0.1",
            "ref_t": f"{ref_t} {ref_t}",
            "pcoupl": pcoupl,
            "pcoupltype": "isotropic",
            "tau_p": 2.0,
            "ref_p": ref_p,
            "compressibility": "4.5e-5",
            "pbc": "xyz",
            "gen_vel": "no",
        }
    )


TEMPLATES: dict[str, Callable[[], str]] = {
    "em.mdp": _em_mdp,
    "nvt.mdp": _nvt_mdp,
    "npt.mdp": _npt_mdp,
    "md.mdp": _md_mdp,
}


def _write_text_file(wd: Path, filename: str, content: str) -> dict[str, Any]:
    if len(content) > MAX_TEXT_WRITE_CHARS:
        return _error(f"content exceeds {MAX_TEXT_WRITE_CHARS} characters")
    path = _safe_path(wd, filename, "filename", for_write=True, text_file=True)
    path.write_text(content)
    return _ok(path=str(path), bytes=len(content))


def _parse_mdp(content: str) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    errors: list[str] = []
    for lineno, raw in enumerate(content.splitlines(), start=1):
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            errors.append(f"line {lineno}: expected key = value")
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            errors.append(f"line {lineno}: empty key")
            continue
        values[key] = value
    return values, errors


# --------------------------------------------------------------------------- #
# Introspection tools
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def gmx_info() -> dict[str, Any]:
    """Return the GROMACS version, build config, binary path, and project root."""
    res = _run(["--version"], ROOT, timeout=30)
    return {
        "gmx_bin": GMX_BIN,
        "project_root": str(ROOT),
        "absolute_workdirs_allowed": _truthy_env("GROMACS_MCP_ALLOW_ABSOLUTE_WORKDIRS"),
        "version_output": res.get("stdout", "") + res.get("stderr", ""),
        "ok": res.get("ok", False),
    }


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def list_files(workdir: str = ".") -> dict[str, Any]:
    """List files in a project work directory."""
    wd, err = _workdir_or_error(workdir)
    if err:
        return err
    assert wd is not None
    entries = []
    for file_path in sorted(wd.iterdir()):
        try:
            st = file_path.stat()
            entries.append(
                {
                    "name": file_path.name,
                    "size": st.st_size,
                    "is_dir": file_path.is_dir(),
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                }
            )
        except OSError:
            continue
    return {"ok": True, "workdir": str(wd), "files": entries}


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def read_text_file(path: str, workdir: str = ".", max_chars: TextMaxChars = 6000) -> dict[str, Any]:
    """Read a safe text file from a work directory; output is clipped."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        file_path = _safe_path(wd, path, "path", must_exist=True, text_file=True)
        with file_path.open("rb") as fh:
            sample = fh.read(8192)
        if b"\x00" in sample:
            return _error("binary-looking file is blocked")
        text = file_path.read_text(errors="replace")
        limit = min(max_chars, MAX_TEXT_READ_CHARS)
        clipped = text if len(text) <= limit else text[:limit] + "\n... [truncated] ..."
        return _ok(path=str(file_path), content=clipped, truncated=len(text) > limit)

    return _guarded_tool_call(run)


@mcp.tool(annotations=WRITE_ONLY, structured_output=True)
def write_mdp(filename: str, content: str, workdir: str = ".") -> dict[str, Any]:
    """Write a safe text MDP/topology-style file into a work directory."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        return _write_text_file(wd, filename, content)

    return _guarded_tool_call(run)


# --------------------------------------------------------------------------- #
# Safe MDP template tools
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=WRITE_ONLY, structured_output=True)
def write_em_mdp(
    workdir: str,
    filename: str = "em.mdp",
    integrator: EmIntegrator = "steep",
    emtol: PositiveFloat = 1000.0,
    emstep: PositiveFloat = 0.01,
    nsteps: PositiveInt = 50000,
) -> dict[str, Any]:
    """Write a conservative energy-minimisation MDP template."""

    def run() -> dict[str, Any]:
        if emtol <= 0 or emstep <= 0 or nsteps <= 0:
            return _error("emtol, emstep, and nsteps must be positive")
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        return _write_text_file(wd, filename, _em_mdp(integrator, emtol, emstep, nsteps))

    return _guarded_tool_call(run)


@mcp.tool(annotations=WRITE_ONLY, structured_output=True)
def write_nvt_mdp(
    workdir: str,
    filename: str = "nvt.mdp",
    nsteps: PositiveInt = 50000,
    dt: PositiveFloat = 0.002,
    ref_t: PositiveFloat = 300.0,
    tau_t: PositiveFloat = 0.1,
    tcoupl: Thermostat = "V-rescale",
) -> dict[str, Any]:
    """Write a conservative NVT equilibration MDP template."""

    def run() -> dict[str, Any]:
        if nsteps <= 0 or dt <= 0 or ref_t <= 0 or tau_t <= 0:
            return _error("nsteps, dt, ref_t, and tau_t must be positive")
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        return _write_text_file(wd, filename, _nvt_mdp(nsteps, dt, ref_t, tau_t, tcoupl))

    return _guarded_tool_call(run)


@mcp.tool(annotations=WRITE_ONLY, structured_output=True)
def write_npt_mdp(
    workdir: str,
    filename: str = "npt.mdp",
    nsteps: PositiveInt = 50000,
    dt: PositiveFloat = 0.002,
    ref_t: PositiveFloat = 300.0,
    ref_p: PositiveFloat = 1.0,
    tcoupl: Thermostat = "V-rescale",
    pcoupl: Barostat = "Parrinello-Rahman",
) -> dict[str, Any]:
    """Write a conservative NPT equilibration MDP template."""

    def run() -> dict[str, Any]:
        if nsteps <= 0 or dt <= 0 or ref_t <= 0 or ref_p <= 0:
            return _error("nsteps, dt, ref_t, and ref_p must be positive")
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        return _write_text_file(wd, filename, _npt_mdp(nsteps, dt, ref_t, ref_p, tcoupl, pcoupl))

    return _guarded_tool_call(run)


@mcp.tool(annotations=WRITE_ONLY, structured_output=True)
def write_md_mdp(
    workdir: str,
    filename: str = "md.mdp",
    nsteps: PositiveInt = 500000,
    dt: PositiveFloat = 0.002,
    ref_t: PositiveFloat = 300.0,
    ref_p: PositiveFloat = 1.0,
    tcoupl: Thermostat = "V-rescale",
    pcoupl: Barostat = "Parrinello-Rahman",
) -> dict[str, Any]:
    """Write a conservative production-MD MDP template."""

    def run() -> dict[str, Any]:
        if nsteps <= 0 or dt <= 0 or ref_t <= 0 or ref_p <= 0:
            return _error("nsteps, dt, ref_t, and ref_p must be positive")
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        return _write_text_file(wd, filename, _md_mdp(nsteps, dt, ref_t, ref_p, tcoupl, pcoupl))

    return _guarded_tool_call(run)


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def validate_mdp(mdp: str, workdir: str = ".") -> dict[str, Any]:
    """Validate a text MDP file for syntax and common safety issues."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        path = _safe_path(wd, mdp, "mdp", must_exist=True, text_file=True)
        content = path.read_text(errors="replace")
        values, errors = _parse_mdp(content)
        warnings: list[str] = []

        integrator = values.get("integrator")
        if not integrator:
            errors.append("missing required key: integrator")
        elif integrator not in {"steep", "cg", "md", "sd", "bd"}:
            warnings.append(f"unusual integrator: {integrator}")
        if "nsteps" not in values:
            errors.append("missing required key: nsteps")
        if integrator == "md" and "dt" not in values:
            errors.append("missing required key for md integrator: dt")
        if values.get("constraints") == "none" and integrator == "md":
            warnings.append("md runs normally constrain at least h-bonds for 2 fs timesteps")
        if values.get("pcoupl", "").lower() != "no" and "ref_p" not in values:
            warnings.append("pressure coupling is enabled but ref_p is missing")

        return _ok(
            path=str(path),
            valid=not errors,
            keys=values,
            errors=errors,
            warnings=warnings,
        )

    return _guarded_tool_call(run)


# --------------------------------------------------------------------------- #
# Curated pipeline tools
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=RUN_LOCAL, structured_output=True)
def pdb2gmx(
    structure: str,
    workdir: str,
    forcefield: str = "amber99sb-ildn",
    water: str = "tip3p",
    output: str = "conf.gro",
    topology: str = "topol.top",
    ignh: bool = False,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Build topology and processed coordinates from a PDB/GRO structure."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        args = [
            "pdb2gmx",
            "-f",
            _safe_file_arg(wd, structure, "structure", must_exist=True),
            "-o",
            _safe_file_arg(wd, output, "output", for_write=True),
            "-p",
            _safe_file_arg(wd, topology, "topology", for_write=True),
            "-ff",
            _validate_gromacs_library_arg(forcefield, "forcefield"),
            "-water",
            _validate_gromacs_library_arg(water, "water"),
        ]
        if ignh:
            args.append("-ignh")
        args += _validate_arg_tokens(extra_args, "extra_args")
        return _run(args, wd)

    return _guarded_tool_call(run)


@mcp.tool(annotations=RUN_LOCAL, structured_output=True)
def editconf(
    structure: str,
    workdir: str,
    output: str = "box.gro",
    box_type: BoxType = "cubic",
    distance: PositiveFloat = 1.0,
    center: bool = True,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Define a simulation box around a structure."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        if distance <= 0:
            return _error("distance must be positive")
        args = [
            "editconf",
            "-f",
            _safe_file_arg(wd, structure, "structure", must_exist=True),
            "-o",
            _safe_file_arg(wd, output, "output", for_write=True),
            "-bt",
            box_type,
            "-d",
            str(distance),
        ]
        if center:
            args.append("-c")
        args += _validate_arg_tokens(extra_args, "extra_args")
        return _run(args, wd)

    return _guarded_tool_call(run)


@mcp.tool(annotations=RUN_LOCAL, structured_output=True)
def solvate(
    topology: str,
    workdir: str,
    structure: str | None = None,
    output: str = "solv.gro",
    solvent: str = "spc216.gro",
    box: list[float] | None = None,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Fill a box with solvent and update the topology."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        if box is not None and (len(box) != 3 or any(v <= 0 for v in box)):
            return _error("box must contain exactly three positive values in nm")
        args = [
            "solvate",
            "-cs",
            _validate_gromacs_library_arg(solvent, "solvent"),
            "-o",
            _safe_file_arg(wd, output, "output", for_write=True),
            "-p",
            _safe_file_arg(wd, topology, "topology", must_exist=True),
        ]
        if structure:
            args += ["-cp", _safe_file_arg(wd, structure, "structure", must_exist=True)]
        if box:
            args += ["-box", *[str(v) for v in box]]
        args += _validate_arg_tokens(extra_args, "extra_args")
        return _run(args, wd)

    return _guarded_tool_call(run)


@mcp.tool(annotations=RUN_LOCAL, structured_output=True)
def grompp(
    mdp: str,
    structure: str,
    topology: str,
    workdir: str,
    output_tpr: str = "topol.tpr",
    index: str | None = None,
    maxwarn: MaxWarn = 0,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Preprocess MDP, structure, and topology into a TPR run input."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        if maxwarn < 0 or maxwarn > 10:
            return _error("maxwarn must be between 0 and 10")
        args = [
            "grompp",
            "-f",
            _safe_file_arg(wd, mdp, "mdp", must_exist=True, text_file=True),
            "-c",
            _safe_file_arg(wd, structure, "structure", must_exist=True),
            "-p",
            _safe_file_arg(wd, topology, "topology", must_exist=True, text_file=True),
            "-o",
            _safe_file_arg(wd, output_tpr, "output_tpr", for_write=True),
            "-maxwarn",
            str(maxwarn),
        ]
        if index:
            args += ["-n", _safe_file_arg(wd, index, "index", must_exist=True, text_file=True)]
        args += _validate_arg_tokens(extra_args, "extra_args")
        return _run(args, wd)

    return _guarded_tool_call(run)


@mcp.tool(annotations=RUN_LOCAL, structured_output=True)
def genion(
    tpr: str,
    topology: str,
    workdir: str,
    output: str = "ions.gro",
    group: str = "SOL",
    neutral: bool = True,
    concentration: PositiveFloat | None = None,
    pname: str = "NA",
    nname: str = "CL",
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Add counter-ions by replacing solvent molecules."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        if concentration is not None and concentration <= 0:
            return _error("concentration must be positive when provided")
        _validate_token(group, "group")
        args = [
            "genion",
            "-s",
            _safe_file_arg(wd, tpr, "tpr", must_exist=True),
            "-o",
            _safe_file_arg(wd, output, "output", for_write=True),
            "-p",
            _safe_file_arg(wd, topology, "topology", must_exist=True, text_file=True),
            "-pname",
            _validate_gromacs_library_arg(pname, "pname"),
            "-nname",
            _validate_gromacs_library_arg(nname, "nname"),
        ]
        if neutral:
            args.append("-neutral")
        if concentration is not None:
            args += ["-conc", str(concentration)]
        args += _validate_arg_tokens(extra_args, "extra_args")
        return _run(args, wd, stdin=f"{group}\n")

    return _guarded_tool_call(run)


@mcp.tool(annotations=RUN_LOCAL, structured_output=True)
def trjconv(
    trajectory: str,
    tpr: str,
    workdir: str,
    output: str = "out.xtc",
    output_group: str = "System",
    center_group: str | None = None,
    pbc: PbcMode = "mol",
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Post-process a trajectory: PBC treatment, centering, and conversion."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        _validate_token(output_group, "output_group")
        args = [
            "trjconv",
            "-f",
            _safe_file_arg(wd, trajectory, "trajectory", must_exist=True),
            "-s",
            _safe_file_arg(wd, tpr, "tpr", must_exist=True),
            "-o",
            _safe_file_arg(wd, output, "output", for_write=True),
            "-pbc",
            pbc,
        ]
        if center_group:
            _validate_token(center_group, "center_group")
            args.append("-center")
            stdin = f"{center_group}\n{output_group}\n"
        else:
            stdin = f"{output_group}\n"
        args += _validate_arg_tokens(extra_args, "extra_args")
        return _run(args, wd, stdin=stdin)

    return _guarded_tool_call(run)


# --------------------------------------------------------------------------- #
# Analysis tools
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=RUN_LOCAL, structured_output=True)
def make_ndx(
    structure: str,
    workdir: str,
    output: str = "index.ndx",
    stdin: str = "q\n",
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Create or edit a GROMACS index file with ``gmx make_ndx``."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        args = [
            "make_ndx",
            "-f",
            _safe_file_arg(wd, structure, "structure", must_exist=True),
            "-o",
            _safe_file_arg(wd, output, "output", for_write=True, text_file=True),
        ]
        args += _validate_arg_tokens(extra_args, "extra_args")
        return _run(args, wd, stdin=stdin)

    return _guarded_tool_call(run)


@mcp.tool(annotations=RUN_LOCAL, structured_output=True)
def energy(
    energy_file: str,
    workdir: str,
    output: str = "energy.xvg",
    selection: str = "Potential",
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Extract an energy term from an EDR file with ``gmx energy``."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        _validate_token(selection, "selection")
        args = [
            "energy",
            "-f",
            _safe_file_arg(wd, energy_file, "energy_file", must_exist=True),
            "-o",
            _safe_file_arg(wd, output, "output", for_write=True, text_file=True),
        ]
        args += _validate_arg_tokens(extra_args, "extra_args")
        return _run(args, wd, stdin=f"{selection}\n0\n")

    return _guarded_tool_call(run)


@mcp.tool(annotations=RUN_LOCAL, structured_output=True)
def rms(
    trajectory: str,
    tpr: str,
    workdir: str,
    output: str = "rmsd.xvg",
    fit_group: str = "Backbone",
    output_group: str = "Backbone",
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Calculate RMSD with ``gmx rms``."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        _validate_token(fit_group, "fit_group")
        _validate_token(output_group, "output_group")
        args = [
            "rms",
            "-f",
            _safe_file_arg(wd, trajectory, "trajectory", must_exist=True),
            "-s",
            _safe_file_arg(wd, tpr, "tpr", must_exist=True),
            "-o",
            _safe_file_arg(wd, output, "output", for_write=True, text_file=True),
        ]
        args += _validate_arg_tokens(extra_args, "extra_args")
        return _run(args, wd, stdin=f"{fit_group}\n{output_group}\n")

    return _guarded_tool_call(run)


@mcp.tool(annotations=RUN_LOCAL, structured_output=True)
def rmsf(
    trajectory: str,
    tpr: str,
    workdir: str,
    output: str = "rmsf.xvg",
    group: str = "Protein",
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Calculate residue/atom RMSF with ``gmx rmsf``."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        _validate_token(group, "group")
        args = [
            "rmsf",
            "-f",
            _safe_file_arg(wd, trajectory, "trajectory", must_exist=True),
            "-s",
            _safe_file_arg(wd, tpr, "tpr", must_exist=True),
            "-o",
            _safe_file_arg(wd, output, "output", for_write=True, text_file=True),
        ]
        args += _validate_arg_tokens(extra_args, "extra_args")
        return _run(args, wd, stdin=f"{group}\n")

    return _guarded_tool_call(run)


@mcp.tool(annotations=RUN_LOCAL, structured_output=True)
def gyrate(
    trajectory: str,
    tpr: str,
    workdir: str,
    output: str = "gyrate.xvg",
    group: str = "Protein",
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Calculate radius of gyration with ``gmx gyrate``."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        _validate_token(group, "group")
        args = [
            "gyrate",
            "-f",
            _safe_file_arg(wd, trajectory, "trajectory", must_exist=True),
            "-s",
            _safe_file_arg(wd, tpr, "tpr", must_exist=True),
            "-o",
            _safe_file_arg(wd, output, "output", for_write=True, text_file=True),
        ]
        args += _validate_arg_tokens(extra_args, "extra_args")
        return _run(args, wd, stdin=f"{group}\n")

    return _guarded_tool_call(run)


@mcp.tool(annotations=RUN_LOCAL, structured_output=True)
def hbond(
    trajectory: str,
    tpr: str,
    workdir: str,
    output: str = "hbond.xvg",
    group_a: str = "Protein",
    group_b: str = "Water",
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Calculate hydrogen bonds with ``gmx hbond``."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        _validate_token(group_a, "group_a")
        _validate_token(group_b, "group_b")
        args = [
            "hbond",
            "-f",
            _safe_file_arg(wd, trajectory, "trajectory", must_exist=True),
            "-s",
            _safe_file_arg(wd, tpr, "tpr", must_exist=True),
            "-num",
            _safe_file_arg(wd, output, "output", for_write=True, text_file=True),
        ]
        args += _validate_arg_tokens(extra_args, "extra_args")
        return _run(args, wd, stdin=f"{group_a}\n{group_b}\n")

    return _guarded_tool_call(run)


@mcp.tool(annotations=RUN_LOCAL, structured_output=True)
def sasa(
    trajectory: str,
    tpr: str,
    workdir: str,
    output: str = "sasa.xvg",
    surface_group: str = "Protein",
    output_group: str = "Protein",
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Calculate solvent-accessible surface area with ``gmx sasa``."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        _validate_token(surface_group, "surface_group")
        _validate_token(output_group, "output_group")
        args = [
            "sasa",
            "-f",
            _safe_file_arg(wd, trajectory, "trajectory", must_exist=True),
            "-s",
            _safe_file_arg(wd, tpr, "tpr", must_exist=True),
            "-o",
            _safe_file_arg(wd, output, "output", for_write=True, text_file=True),
        ]
        args += _validate_arg_tokens(extra_args, "extra_args")
        return _run(args, wd, stdin=f"{surface_group}\n{output_group}\n")

    return _guarded_tool_call(run)


@mcp.tool(annotations=RUN_LOCAL, structured_output=True)
def check(
    input_file: str,
    workdir: str,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Check a GROMACS file with ``gmx check``."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        args = ["check", "-f", _safe_file_arg(wd, input_file, "input_file", must_exist=True)]
        args += _validate_arg_tokens(extra_args, "extra_args")
        return _run(args, wd, timeout=120)

    return _guarded_tool_call(run)


# --------------------------------------------------------------------------- #
# Background mdrun
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=RUN_LOCAL, structured_output=True)
def mdrun_start(
    tpr: str,
    workdir: str,
    deffnm: str = "md",
    ntomp: PositiveInt | None = None,
    nsteps: NonNegativeInt | None = None,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Start a simulation in the background and return immediately with a job id."""

    def run() -> dict[str, Any]:
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        if ntomp is not None and ntomp <= 0:
            return _error("ntomp must be positive when provided")
        if nsteps is not None and nsteps < 0:
            return _error("nsteps must be non-negative when provided")
        safe_tpr = _safe_file_arg(wd, tpr, "tpr", must_exist=True)
        safe_deffnm = _safe_deffnm(deffnm)
        args = [GMX_BIN, "mdrun", "-v", "-s", safe_tpr, "-deffnm", safe_deffnm]
        if ntomp is not None:
            args += ["-ntomp", str(ntomp)]
        if nsteps is not None:
            args += ["-nsteps", str(nsteps)]
        args += _validate_arg_tokens(extra_args, "extra_args")

        job_id = f"mdrun-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        capture = wd / f"{job_id}.out"
        log_fh = open(capture, "w")  # noqa: SIM115
        try:
            proc = subprocess.Popen(
                args,
                cwd=str(wd),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            return _error(f"gmx not found at '{GMX_BIN}'.")
        except OSError as exc:
            return _error(f"Could not start mdrun: {exc}")
        finally:
            log_fh.close()

        try:
            pgid: int | None = os.getpgid(proc.pid)
        except OSError:
            pgid = None
        command = " ".join(shlex.quote(c) for c in args)
        job: dict[str, Any] = {
            "pid": proc.pid,
            "pgid": pgid,
            "workdir": str(wd),
            "deffnm": safe_deffnm,
            "tpr": safe_tpr,
            "command": command,
            "command_hash": hashlib.sha256(command.encode()).hexdigest()[:16],
            "capture_log": str(capture),
            "created_at": _now(),
            "started_at": _now(),
            "finished_at": None,
            "stopped_at": None,
            "exit_status": None,
            "root_version": ROOT_VERSION,
        }

        def add_job(jobs: JobRegistry) -> None:
            jobs[job_id] = job

        _update_jobs(add_job)
        return _ok(
            job_id=job_id,
            pid=proc.pid,
            pgid=pgid,
            workdir=str(wd),
            message="Simulation started in background. Poll with mdrun_status.",
        )

    return _guarded_tool_call(run)


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def mdrun_status(job_id: str) -> dict[str, Any]:
    """Check a background simulation and return progress plus log tail."""

    def mutate(jobs: JobRegistry) -> dict[str, Any]:
        job = jobs.get(job_id)
        if not job:
            return _error(f"Unknown job_id '{job_id}'. See mdrun_list.")
        status = _refresh_job(job)
        wd = Path(str(job["workdir"])).resolve()
        capture = _job_path(job, "capture_log")
        progress = _parse_progress(wd, str(job["deffnm"]), capture)
        return _ok(
            job_id=job_id,
            status=status,
            pid=job["pid"],
            pgid=job.get("pgid"),
            started_at=job.get("started_at"),
            finished_at=job.get("finished_at"),
            stopped_at=job.get("stopped_at"),
            progress=progress,
            log_tail=_tail(capture, 30),
        )

    return _guarded_tool_call(lambda: _update_jobs(mutate))


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def mdrun_list() -> dict[str, Any]:
    """List all known background simulations and their current status."""

    def mutate(jobs: JobRegistry) -> dict[str, Any]:
        out = []
        for jid, job in jobs.items():
            out.append(
                {
                    "job_id": jid,
                    "status": _refresh_job(job),
                    "workdir": job.get("workdir"),
                    "deffnm": job.get("deffnm"),
                    "started_at": job.get("started_at"),
                    "finished_at": job.get("finished_at"),
                    "stopped_at": job.get("stopped_at"),
                }
            )
        return {"ok": True, "jobs": out}

    return _update_jobs(mutate)


@mcp.tool(annotations=DESTRUCTIVE, structured_output=True)
def mdrun_stop(job_id: str) -> dict[str, Any]:
    """Stop a running background simulation with SIGTERM."""

    def run() -> dict[str, Any]:
        jobs = _load_jobs()
        job = jobs.get(job_id)
        if not job:
            return _error(f"Unknown job_id '{job_id}'.")
        if not _job_process_alive(job):
            job["finished_at"] = job.get("finished_at") or _now()
            _save_jobs(jobs)
            return _ok(message="Job already finished.", job_id=job_id)
        try:
            pgid = int(job.get("pgid") or os.getpgid(int(job["pid"])))
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            return _error(f"Could not stop job: {exc}")
        job["stopped_at"] = _now()
        _save_jobs(jobs)
        return _ok(job_id=job_id, message="Sent SIGTERM to simulation.")

    return _guarded_tool_call(run)


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def mdrun_logs(job_id: str, max_lines: Annotated[int, Field(ge=1, le=1000)] = 80) -> dict[str, Any]:
    """Read the captured stdout/stderr log for a background simulation."""

    def run() -> dict[str, Any]:
        jobs = _load_jobs()
        job = jobs.get(job_id)
        if not job:
            return _error(f"Unknown job_id '{job_id}'.")
        capture = _job_path(job, "capture_log")
        return _ok(job_id=job_id, log_tail=_tail(capture, max_lines))

    return _guarded_tool_call(run)


@mcp.tool(annotations=DESTRUCTIVE, structured_output=True)
def mdrun_cleanup(finished_only: bool = True) -> dict[str, Any]:
    """Remove finished job records from the registry."""

    def mutate(jobs: JobRegistry) -> dict[str, Any]:
        removed: list[str] = []
        for jid, job in list(jobs.items()):
            status = _refresh_job(job)
            if finished_only and status in {"running", "stopping"}:
                continue
            removed.append(jid)
            del jobs[jid]
        return _ok(removed=removed, remaining=len(jobs))

    return _update_jobs(mutate)


@mcp.tool(annotations=DESTRUCTIVE, structured_output=True)
def mdrun_forget(job_id: str, force: bool = False) -> dict[str, Any]:
    """Remove one job record; running jobs require ``force=True``."""

    def mutate(jobs: JobRegistry) -> dict[str, Any]:
        job = jobs.get(job_id)
        if not job:
            return _error(f"Unknown job_id '{job_id}'.")
        status = _refresh_job(job)
        if status in {"running", "stopping"} and not force:
            return _error("job is still running; stop it first or pass force=True")
        del jobs[job_id]
        return _ok(job_id=job_id, removed=True)

    return _update_jobs(mutate)


# --------------------------------------------------------------------------- #
# Generic escape hatch
# --------------------------------------------------------------------------- #


@mcp.tool(annotations=RUN_LOCAL, structured_output=True)
def run_gmx(
    args: list[str],
    workdir: str = ".",
    stdin: str | None = None,
    timeout: TimeoutSeconds = 600,
) -> dict[str, Any]:
    """Run any non-blocking gmx subcommand not covered by a dedicated tool."""

    def run() -> dict[str, Any]:
        if not args:
            return _error("args must include a gmx subcommand")
        if timeout < 1 or timeout > MAX_TIMEOUT_SECONDS:
            return _error(f"timeout must be between 1 and {MAX_TIMEOUT_SECONDS} seconds")
        if args[0] == "mdrun":
            return _error("blocking mdrun is disabled; use mdrun_start instead")
        safe_args = _validate_arg_tokens(args, "args")
        wd, err = _workdir_or_error(workdir)
        if err:
            return err
        assert wd is not None
        return _run(safe_args, wd, stdin=stdin, timeout=timeout)

    return _guarded_tool_call(run)


# --------------------------------------------------------------------------- #
# MCP resources and prompts
# --------------------------------------------------------------------------- #


@mcp.resource(
    "gromacs://projects",
    name="projects",
    description="List work directories under GROMACS_MCP_ROOT.",
    mime_type="application/json",
)
def projects_resource() -> str:
    projects = []
    for path in sorted(ROOT.iterdir()):
        if path.is_dir() and not path.name.startswith("."):
            projects.append({"name": path.name, "path": str(path)})
    return json.dumps({"project_root": str(ROOT), "projects": projects}, indent=2)


@mcp.resource(
    "gromacs://projects/{workdir}/files",
    name="project_files",
    description="List files in a specific GROMACS work directory.",
    mime_type="application/json",
)
def project_files_resource(workdir: str) -> str:
    listing = list_files(workdir)
    return json.dumps(listing, indent=2)


@mcp.resource(
    "gromacs://jobs",
    name="jobs",
    description="List background mdrun jobs.",
    mime_type="application/json",
)
def jobs_resource() -> str:
    return json.dumps(mdrun_list(), indent=2)


@mcp.resource(
    "gromacs://jobs/{job_id}/log",
    name="job_log",
    description="Read a background mdrun job log tail.",
    mime_type="text/plain",
)
def job_log_resource(job_id: str) -> str:
    result = mdrun_logs(job_id)
    if result.get("ok"):
        return str(result.get("log_tail", ""))
    return json.dumps(result, indent=2)


@mcp.resource(
    "gromacs://templates/{name}",
    name="mdp_template",
    description="Read built-in safe MDP templates.",
    mime_type="text/plain",
)
def template_resource(name: str) -> str:
    if name not in TEMPLATES:
        return f"Unknown template '{name}'. Available: {', '.join(sorted(TEMPLATES))}"
    return TEMPLATES[name]()


@mcp.prompt(name="prepare_protein_md", description="Plan a standard solvated-protein MD setup.")
def prepare_protein_md_prompt(workdir: str, structure: str = "input.pdb") -> str:
    return (
        f"Prepare a protein-in-water GROMACS workflow in workdir '{workdir}' from "
        f"'{structure}'. Use pdb2gmx, editconf, solvate, write_em_mdp, grompp, "
        "genion when needed, then mdrun_start for a short energy-minimisation smoke test. "
        "Do not use paths outside the workdir."
    )


@mcp.prompt(name="debug_grompp_failure", description="Guide debugging of a failed grompp call.")
def debug_grompp_failure_prompt(workdir: str, mdp: str, structure: str, topology: str) -> str:
    return (
        f"Debug grompp in workdir '{workdir}' using mdp '{mdp}', structure '{structure}', "
        f"and topology '{topology}'. Read the files with read_text_file, run validate_mdp, "
        "inspect error_summary from grompp, and propose the smallest correction."
    )


@mcp.prompt(name="basic_trajectory_analysis", description="Run a basic trajectory analysis set.")
def basic_trajectory_analysis_prompt(workdir: str, trajectory: str, tpr: str) -> str:
    return (
        f"Analyse trajectory '{trajectory}' with tpr '{tpr}' in workdir '{workdir}'. "
        "Use trjconv for PBC cleanup if needed, then run rms, rmsf, gyrate, hbond, "
        "sasa, and energy where the required files are available. Summarise generated XVG files."
    )


def main() -> None:
    """Console-script entry point (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
