"""Tests for gromacs-mcp.

Pure-logic tests always run. Tests that actually invoke ``gmx`` are skipped
automatically when GROMACS is not installed (e.g. on CI).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

# Redirect the project root to a temp dir BEFORE importing the server, so the
# import-time mkdir does not touch the real home directory.
os.environ.setdefault("GROMACS_MCP_ROOT", tempfile.mkdtemp(prefix="gromacs-mcp-test-"))

import pytest  # noqa: E402

from gromacs_mcp import server  # noqa: E402

EXPECTED_TOOLS = {
    "gmx_info",
    "list_files",
    "read_text_file",
    "write_mdp",
    "pdb2gmx",
    "editconf",
    "solvate",
    "grompp",
    "genion",
    "trjconv",
    "mdrun_start",
    "mdrun_status",
    "mdrun_list",
    "mdrun_stop",
    "run_gmx",
}

gmx_required = pytest.mark.skipif(
    shutil.which("gmx") is None and not os.environ.get("GMX_BIN"),
    reason="GROMACS (gmx) not installed",
)


def test_all_tools_registered():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert names >= EXPECTED_TOOLS, f"missing: {EXPECTED_TOOLS - names}"


def test_clip_truncates_long_output():
    long = "x" * (server.MAX_STREAM_CHARS + 500)
    out = server._clip(long)
    assert len(out) < len(long)
    assert "truncated" in out


def test_clip_passes_short_output():
    assert server._clip("hello") == "hello"


def test_extract_fatal_error():
    stderr = (
        "-------------------------------------------------------\n"
        "Program:     gmx pdb2gmx\n"
        "Fatal error:\n"
        "Residue 'CBX' not found in residue topology database\n"
        "For more information and tips for troubleshooting, please check ...\n"
        "-------------------------------------------------------\n"
    )
    assert server._extract_fatal_error(stderr) == (
        "Residue 'CBX' not found in residue topology database"
    )


def test_extract_fatal_error_none_when_absent():
    assert server._extract_fatal_error("all good, no errors here") == ""


def test_jobs_registry_roundtrip():
    jobs = {"mdrun-1-abc": {"pid": 1, "workdir": "/tmp"}}
    server._save_jobs(jobs)
    assert server._load_jobs() == jobs


@gmx_required
def test_gmx_info_runs():
    info = server.gmx_info()
    assert info["ok"] is True
    assert "GROMACS" in (info["version_output"] or "")


@gmx_required
def test_write_and_list(tmp_path):
    wd = "unit_wd"
    res = server.write_mdp("probe.mdp", "integrator = steep\n", workdir=wd)
    assert res["ok"]
    listing = server.list_files(wd)
    assert any(f["name"] == "probe.mdp" for f in listing["files"])
