"""Tests for gromacs-mcp.

Pure-logic tests always run. Tests that actually invoke ``gmx`` are skipped
automatically when GROMACS is not installed (e.g. on CI).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import pytest  # noqa: E402

from gromacs_mcp import server  # noqa: E402

EXPECTED_TOOLS = {
    "gmx_info",
    "list_files",
    "read_text_file",
    "write_mdp",
    "write_em_mdp",
    "write_nvt_mdp",
    "write_npt_mdp",
    "write_md_mdp",
    "validate_mdp",
    "pdb2gmx",
    "editconf",
    "solvate",
    "grompp",
    "genion",
    "trjconv",
    "make_ndx",
    "energy",
    "rms",
    "rmsf",
    "gyrate",
    "hbond",
    "sasa",
    "check",
    "mdrun_start",
    "mdrun_status",
    "mdrun_list",
    "mdrun_stop",
    "mdrun_logs",
    "mdrun_cleanup",
    "mdrun_forget",
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


def test_tool_annotations_and_structured_output_registered():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert tools["gmx_info"].annotations.readOnlyHint is True
    assert tools["run_gmx"].annotations.openWorldHint is True
    assert tools["mdrun_stop"].annotations.destructiveHint is True
    assert tools["gmx_info"].outputSchema["type"] == "object"


def test_resources_and_prompts_registered():
    resources = {r.name for r in asyncio.run(server.mcp.list_resources())}
    templates = {r.name for r in asyncio.run(server.mcp.list_resource_templates())}
    prompts = {p.name for p in asyncio.run(server.mcp.list_prompts())}
    assert {"projects", "jobs"} <= resources
    assert {"project_files", "job_log", "mdp_template"} <= templates
    assert {
        "prepare_protein_md",
        "debug_grompp_failure",
        "basic_trajectory_analysis",
    } <= prompts


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
    jobs = {"mdrun-1-abc": {"pid": 1, "workdir": "/tmp", "capture_log": "/tmp/x"}}
    server._save_jobs(jobs)
    assert server._load_jobs() == jobs


def test_workdir_traversal_and_absolute_paths_are_rejected(monkeypatch):
    monkeypatch.delenv("GROMACS_MCP_ALLOW_ABSOLUTE_WORKDIRS", raising=False)
    assert server.list_files("../escape")["ok"] is False
    assert server.list_files(str(Path(tempfile.gettempdir()).resolve()))["ok"] is False


def test_read_text_file_blocks_absolute_traversal_hidden_and_binary(tmp_path):
    wd = server._resolve_workdir("safe_read")
    (wd / "ok.mdp").write_text("integrator = steep\n")
    (wd / ".env").write_text("TOKEN=secret\n")
    (wd / "state.tpr").write_bytes(b"\x00binary")

    assert server.read_text_file("ok.mdp", workdir="safe_read")["ok"] is True
    assert server.read_text_file("/etc/hosts", workdir="safe_read")["ok"] is False
    assert server.read_text_file("../outside.txt", workdir="safe_read")["ok"] is False
    assert server.read_text_file(".env", workdir="safe_read")["ok"] is False
    assert server.read_text_file("state.tpr", workdir="safe_read")["ok"] is False


def test_read_text_file_blocks_symlink_escape(tmp_path):
    wd = server._resolve_workdir("safe_symlink")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    link = wd / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")

    res = server.read_text_file("link.txt", workdir="safe_symlink")
    assert res["ok"] is False
    assert "escapes workdir" in res["error"]


def test_write_mdp_blocks_unsafe_paths_and_sensitive_names():
    assert server.write_mdp("nested/probe.mdp", "integrator = steep\n", workdir="safe_write")["ok"]
    assert server.write_mdp("../probe.mdp", "x", workdir="safe_write")["ok"] is False
    assert server.write_mdp(".env", "TOKEN=secret\n", workdir="safe_write")["ok"] is False
    assert server.write_mdp("state.tpr", "not text\n", workdir="safe_write")["ok"] is False


def test_numeric_validation_and_blocking_mdrun_guard():
    assert server.editconf("missing.gro", "num_validation", distance=-1)["ok"] is False
    assert server.solvate("missing.top", "num_validation", box=[1.0, 2.0])["ok"] is False
    assert (
        server.grompp("missing.mdp", "missing.gro", "missing.top", "num_validation", maxwarn=-1)[
            "ok"
        ]
        is False
    )
    assert server.run_gmx(["--version"], timeout=server.MAX_TIMEOUT_SECONDS + 1)["ok"] is False
    assert server.run_gmx(["mdrun"], timeout=10)["ok"] is False


def test_mdp_templates_and_validation():
    res = server.write_em_mdp("template_test", nsteps=10)
    assert res["ok"] is True
    valid = server.validate_mdp("em.mdp", workdir="template_test")
    assert valid["ok"] is True
    assert valid["valid"] is True
    invalid_write = server.write_em_mdp("template_test", nsteps=0)
    assert invalid_write["ok"] is False


def test_mdrun_log_cleanup_and_forget_helpers():
    wd = server._resolve_workdir("job_helpers")
    capture = wd / "mdrun-test.out"
    capture.write_text("\n".join(f"line {i}" for i in range(5)))
    server._save_jobs(
        {
            "mdrun-test": {
                "pid": 99999999,
                "pgid": None,
                "workdir": str(wd),
                "deffnm": "md",
                "capture_log": str(capture),
                "started_at": "2026-07-08 00:00:00",
            }
        }
    )

    logs = server.mdrun_logs("mdrun-test", max_lines=2)
    assert logs["ok"] is True
    assert "line 4" in logs["log_tail"]

    cleanup = server.mdrun_cleanup()
    assert cleanup["ok"] is True
    assert cleanup["removed"] == ["mdrun-test"]
    assert server.mdrun_forget("missing")["ok"] is False


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
