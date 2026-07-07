"""GROMACS MCP server.

Wraps the local ``gmx`` binary so an MCP client (Claude) can drive a full
molecular-dynamics workflow: build topology, define a box, solvate, add ions,
preprocess, run simulations in the background, and convert trajectories.

Design (chosen with the user):
  * Hybrid tools  -> typed helpers for the common pipeline steps
                     (pdb2gmx, editconf, solvate, grompp, genion, trjconv)
                     PLUS a generic ``run_gmx`` escape hatch for anything else.
  * Background mdrun -> ``mdrun_start`` returns immediately with a job id;
                     ``mdrun_status`` / ``mdrun_list`` / ``mdrun_stop`` manage it.
  * Python / FastMCP, stdio transport (runs locally next to gmx).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# The gmx executable. Overridable via env for non-standard installs.
GMX_BIN = os.environ.get("GMX_BIN") or shutil.which("gmx") or "/opt/homebrew/bin/gmx"

# Root under which relative work directories are created. Each "project" is a
# subdirectory holding the .gro/.top/.tpr/.log/... files for one system.
ROOT = Path(os.environ.get("GROMACS_MCP_ROOT", Path.home() / "gromacs-mcp" / "projects"))
ROOT.mkdir(parents=True, exist_ok=True)

# Where background-job metadata is persisted so status survives across calls
# (and, via PID, across a server restart within a boot session).
JOBS_FILE = ROOT.parent / ".jobs.json"

# Keep tool output small enough to stay useful in context.
MAX_STREAM_CHARS = 4000

mcp = FastMCP("gromacs")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _resolve_workdir(workdir: str) -> Path:
    """Resolve a work directory. Relative paths live under ROOT; absolute paths
    are honored as-is. The directory is created if missing."""
    p = Path(workdir).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def _clip(text: str) -> str:
    """Truncate a stream to head+tail so long GROMACS logs don't flood context."""
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
) -> dict:
    """Run ``gmx <args>`` synchronously inside *workdir* and return a result dict.

    ``stdin`` feeds interactive group/selection prompts (genion, trjconv, ...).
    """
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
        return {
            "ok": False,
            "error": f"gmx not found at '{GMX_BIN}'. Set the GMX_BIN env var.",
            "command": " ".join(shlex.quote(c) for c in cmd),
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": (
                f"Command timed out after {timeout}s. For long simulations use "
                "mdrun_start (background) instead of a blocking call."
            ),
            "command": " ".join(shlex.quote(c) for c in cmd),
        }

    # GROMACS writes almost everything (including normal progress) to stderr.
    result = {
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
    """Pull the GROMACS 'Fatal error' block out of an output stream so the
    real cause survives head+tail truncation. GROMACS frames it as:

        -------------------------------------------------------
        Program: ...
        Fatal error:
        <the actual message, possibly multi-line>
        For more information and tips ...
        -------------------------------------------------------
    """
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


def _load_jobs() -> dict:
    if JOBS_FILE.exists():
        try:
            return json.loads(JOBS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_jobs(jobs: dict) -> None:
    JOBS_FILE.write_text(json.dumps(jobs, indent=2))


def _pid_alive(pid: int) -> bool:
    """True if a process with *pid* currently exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


def _tail(path: Path, n_lines: int = 40) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n_lines:])


def _parse_progress(workdir: Path, deffnm: str, capture_log: Path) -> dict:
    """Best-effort progress from GROMACS output.

    Reads the last "Step / Time" pair from ``<deffnm>.log`` and any live
    "remaining wall clock time" / performance (ns/day) lines from the captured
    stdout+stderr. All parsing is defensive: partial info beats an exception.
    """
    progress: dict = {}

    md_log = workdir / f"{deffnm}.log"
    if md_log.exists():
        try:
            lines = md_log.read_text(errors="replace").splitlines()
        except OSError:
            lines = []
        # md.log alternates a "Step Time" header with a values line.
        for i in range(len(lines) - 1, 0, -1):
            if lines[i - 1].split()[:2] == ["Step", "Time"]:
                parts = lines[i].split()
                if len(parts) >= 2:
                    progress["current_step"] = parts[0]
                    progress["current_time_ps"] = parts[1]
                break
        # Final performance summary appears once the run finishes.
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
# Introspection tools
# --------------------------------------------------------------------------- #


@mcp.tool()
def gmx_info() -> dict:
    """Return the GROMACS version, build config, binary path, and project root.
    Use this first to confirm the server can reach a working gmx."""
    res = _run(["--version"], ROOT, timeout=30)
    return {
        "gmx_bin": GMX_BIN,
        "project_root": str(ROOT),
        "version_output": res.get("stdout", "") + res.get("stderr", ""),
        "ok": res.get("ok", False),
    }


@mcp.tool()
def list_files(workdir: str = ".") -> dict:
    """List files (name, size in bytes, mtime) in a project work directory.
    Relative paths are resolved under the project root."""
    wd = _resolve_workdir(workdir)
    entries = []
    for f in sorted(wd.iterdir()):
        try:
            st = f.stat()
            entries.append(
                {
                    "name": f.name,
                    "size": st.st_size,
                    "is_dir": f.is_dir(),
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                }
            )
        except OSError:
            continue
    return {"workdir": str(wd), "files": entries}


@mcp.tool()
def read_text_file(path: str, workdir: str = ".", max_chars: int = 6000) -> dict:
    """Read a small text file (e.g. .mdp, .top, .itp, a .log) from a work
    directory. Relative paths resolve under the work dir; output is clipped."""
    wd = _resolve_workdir(workdir)
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = wd / p
    if not p.exists():
        return {"ok": False, "error": f"File not found: {p}"}
    text = p.read_text(errors="replace")
    clipped = text if len(text) <= max_chars else text[:max_chars] + "\n... [truncated] ..."
    return {"ok": True, "path": str(p), "content": clipped}


@mcp.tool()
def write_mdp(filename: str, content: str, workdir: str = ".") -> dict:
    """Write an MDP (run-parameter) file into a work directory. Handy for
    creating em.mdp / nvt.mdp / md.mdp before grompp. Returns the path."""
    wd = _resolve_workdir(workdir)
    p = wd / filename
    p.write_text(content)
    return {"ok": True, "path": str(p), "bytes": len(content)}


# --------------------------------------------------------------------------- #
# Curated pipeline tools
# --------------------------------------------------------------------------- #


@mcp.tool()
def pdb2gmx(
    structure: str,
    workdir: str,
    forcefield: str = "amber99sb-ildn",
    water: str = "tip3p",
    output: str = "conf.gro",
    topology: str = "topol.top",
    ignh: bool = False,
    extra_args: list[str] | None = None,
) -> dict:
    """Build a topology + processed coordinates from a PDB/GRO structure.

    Passing explicit ``forcefield`` and ``water`` avoids the interactive prompts.
    Set ``ignh=True`` to ignore/rebuild hydrogens. ``extra_args`` are appended
    verbatim (e.g. ["-ter"]). Runs: gmx pdb2gmx.
    """
    args = [
        "pdb2gmx",
        "-f",
        structure,
        "-o",
        output,
        "-p",
        topology,
        "-ff",
        forcefield,
        "-water",
        water,
    ]
    if ignh:
        args.append("-ignh")
    if extra_args:
        args += extra_args
    return _run(args, _resolve_workdir(workdir))


@mcp.tool()
def editconf(
    structure: str,
    workdir: str,
    output: str = "box.gro",
    box_type: str = "cubic",
    distance: float = 1.0,
    center: bool = True,
    extra_args: list[str] | None = None,
) -> dict:
    """Define a simulation box around a structure. ``distance`` (nm) is the
    solute-box margin (-d), ``box_type`` is -bt (cubic/triclinic/dodecahedron).
    Runs: gmx editconf."""
    args = ["editconf", "-f", structure, "-o", output, "-bt", box_type, "-d", str(distance)]
    if center:
        args.append("-c")
    if extra_args:
        args += extra_args
    return _run(args, _resolve_workdir(workdir))


@mcp.tool()
def solvate(
    topology: str,
    workdir: str,
    structure: str | None = None,
    output: str = "solv.gro",
    solvent: str = "spc216.gro",
    box: list[float] | None = None,
    extra_args: list[str] | None = None,
) -> dict:
    """Fill the box with solvent and update the topology.

    Pass ``structure`` to solvate AROUND an existing solute (-cp), or omit it
    and give ``box`` = [x, y, z] (nm) to fill an empty box with pure solvent.
    ``solvent`` is the solvent configuration (spc216.gro suits 3-point water).
    Runs: gmx solvate.
    """
    args = ["solvate", "-cs", solvent, "-o", output, "-p", topology]
    if structure:
        args += ["-cp", structure]
    if box:
        args += ["-box", *[str(v) for v in box]]
    if extra_args:
        args += extra_args
    return _run(args, _resolve_workdir(workdir))


@mcp.tool()
def grompp(
    mdp: str,
    structure: str,
    topology: str,
    workdir: str,
    output_tpr: str = "topol.tpr",
    index: str | None = None,
    maxwarn: int = 0,
    extra_args: list[str] | None = None,
) -> dict:
    """Preprocess: combine an .mdp, a structure, and a topology into a run
    input (.tpr). Increase ``maxwarn`` only to knowingly accept warnings.
    Runs: gmx grompp."""
    args = [
        "grompp",
        "-f",
        mdp,
        "-c",
        structure,
        "-p",
        topology,
        "-o",
        output_tpr,
        "-maxwarn",
        str(maxwarn),
    ]
    if index:
        args += ["-n", index]
    if extra_args:
        args += extra_args
    return _run(args, _resolve_workdir(workdir))


@mcp.tool()
def genion(
    tpr: str,
    topology: str,
    workdir: str,
    output: str = "ions.gro",
    group: str = "SOL",
    neutral: bool = True,
    concentration: float | None = None,
    pname: str = "NA",
    nname: str = "CL",
    extra_args: list[str] | None = None,
) -> dict:
    """Add counter-ions by replacing solvent molecules. ``group`` is the target
    group to substitute into (usually SOL), fed to gmx via stdin. ``neutral``
    neutralizes total charge; ``concentration`` (mol/L) adds extra salt.
    Runs: gmx genion."""
    args = ["genion", "-s", tpr, "-o", output, "-p", topology, "-pname", pname, "-nname", nname]
    if neutral:
        args.append("-neutral")
    if concentration is not None:
        args += ["-conc", str(concentration)]
    if extra_args:
        args += extra_args
    # genion prompts for the group to replace; feed it on stdin.
    return _run(args, _resolve_workdir(workdir), stdin=f"{group}\n")


@mcp.tool()
def trjconv(
    trajectory: str,
    tpr: str,
    workdir: str,
    output: str = "out.xtc",
    output_group: str = "System",
    center_group: str | None = None,
    pbc: str = "mol",
    extra_args: list[str] | None = None,
) -> dict:
    """Post-process a trajectory: PBC treatment, centering, format conversion.
    If ``center_group`` is set, uses -center and feeds two groups on stdin
    (center group, then output group); otherwise feeds only the output group.
    Runs: gmx trjconv."""
    args = ["trjconv", "-f", trajectory, "-s", tpr, "-o", output, "-pbc", pbc]
    if center_group:
        args.append("-center")
        stdin = f"{center_group}\n{output_group}\n"
    else:
        stdin = f"{output_group}\n"
    if extra_args:
        args += extra_args
    return _run(args, _resolve_workdir(workdir), stdin=stdin)


# --------------------------------------------------------------------------- #
# Background mdrun
# --------------------------------------------------------------------------- #


@mcp.tool()
def mdrun_start(
    tpr: str,
    workdir: str,
    deffnm: str = "md",
    ntomp: int | None = None,
    nsteps: int | None = None,
    extra_args: list[str] | None = None,
) -> dict:
    """Start a simulation in the BACKGROUND and return immediately with a job id.

    Poll it with mdrun_status(job_id). ``deffnm`` sets the default output file
    stem (md.log/md.edr/md.xtc/...). ``ntomp`` = OpenMP threads; ``nsteps``
    overrides the step count from the .tpr (use a small value to smoke-test).
    Runs: gmx mdrun -v.
    """
    wd = _resolve_workdir(workdir)
    tpr_path = (wd / tpr) if not Path(tpr).is_absolute() else Path(tpr)
    if not tpr_path.exists():
        return {
            "ok": False,
            "error": f"Run input not found: {tpr_path}. Create it first with grompp.",
        }
    args = [GMX_BIN, "mdrun", "-v", "-s", tpr, "-deffnm", deffnm]
    if ntomp is not None:
        args += ["-ntomp", str(ntomp)]
    if nsteps is not None:
        args += ["-nsteps", str(nsteps)]
    if extra_args:
        args += extra_args

    job_id = f"mdrun-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    capture = wd / f"{job_id}.out"
    # The log file handle is handed to a detached background process as its
    # stdout, so it must outlive this function — a `with` block would close it
    # too early. The child keeps its own dup of the fd; we close the parent's
    # copy immediately after the process is launched.
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
        return {"ok": False, "error": f"gmx not found at '{GMX_BIN}'."}
    finally:
        log_fh.close()

    jobs = _load_jobs()
    jobs[job_id] = {
        "pid": proc.pid,
        "workdir": str(wd),
        "deffnm": deffnm,
        "tpr": tpr,
        "command": " ".join(shlex.quote(c) for c in args),
        "capture_log": str(capture),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_jobs(jobs)
    return {
        "ok": True,
        "job_id": job_id,
        "pid": proc.pid,
        "workdir": str(wd),
        "message": "Simulation started in background. Poll with mdrun_status.",
    }


@mcp.tool()
def mdrun_status(job_id: str) -> dict:
    """Check a background simulation: whether it is still running, current
    step/time, ETA, ns/day (when available), and the tail of its log."""
    jobs = _load_jobs()
    job = jobs.get(job_id)
    if not job:
        return {"ok": False, "error": f"Unknown job_id '{job_id}'. See mdrun_list."}

    running = _pid_alive(job["pid"])
    wd = Path(job["workdir"])
    progress = _parse_progress(wd, job["deffnm"], Path(job["capture_log"]))

    return {
        "ok": True,
        "job_id": job_id,
        "status": "running" if running else "finished",
        "pid": job["pid"],
        "started_at": job.get("started_at"),
        "progress": progress,
        "log_tail": _tail(Path(job["capture_log"]), 30),
    }


@mcp.tool()
def mdrun_list() -> dict:
    """List all known background simulations and whether each is still running."""
    jobs = _load_jobs()
    out = []
    for jid, job in jobs.items():
        out.append(
            {
                "job_id": jid,
                "status": "running" if _pid_alive(job["pid"]) else "finished",
                "workdir": job["workdir"],
                "deffnm": job.get("deffnm"),
                "started_at": job.get("started_at"),
            }
        )
    return {"jobs": out}


@mcp.tool()
def mdrun_stop(job_id: str) -> dict:
    """Stop (SIGTERM) a running background simulation. GROMACS writes a
    checkpoint on termination so the run can be continued later with -cpi."""
    jobs = _load_jobs()
    job = jobs.get(job_id)
    if not job:
        return {"ok": False, "error": f"Unknown job_id '{job_id}'."}
    if not _pid_alive(job["pid"]):
        return {"ok": True, "message": "Job already finished.", "job_id": job_id}
    try:
        os.killpg(os.getpgid(job["pid"]), signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as e:
        return {"ok": False, "error": f"Could not stop job: {e}"}
    return {"ok": True, "job_id": job_id, "message": "Sent SIGTERM to simulation."}


# --------------------------------------------------------------------------- #
# Generic escape hatch
# --------------------------------------------------------------------------- #


@mcp.tool()
def run_gmx(
    args: list[str],
    workdir: str = ".",
    stdin: str | None = None,
    timeout: int = 600,
) -> dict:
    """Run ANY gmx subcommand not covered by a dedicated tool.

    ``args`` is the argument list WITHOUT the leading 'gmx'
    (e.g. ["rms", "-s", "md.tpr", "-f", "md.xtc"]). ``stdin`` feeds interactive
    group selections. Blocking with a timeout — do NOT use for long mdrun jobs
    (use mdrun_start). Example: run_gmx(["make_ndx", "-f", "conf.gro"], stdin="q\\n").
    """
    return _run(args, _resolve_workdir(workdir), stdin=stdin, timeout=timeout)


def main() -> None:
    """Console-script entry point (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
