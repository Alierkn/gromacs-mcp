"""Integration smoke tests that execute a real GROMACS workflow."""

from __future__ import annotations

import os
import shutil
import time

import pytest

from gromacs_mcp import server

integration_required = pytest.mark.skipif(
    os.environ.get("RUN_GROMACS_INTEGRATION") != "1"
    or (shutil.which("gmx") is None and not os.environ.get("GMX_BIN")),
    reason="set RUN_GROMACS_INTEGRATION=1 with GROMACS installed to run integration tests",
)


@pytest.mark.integration
@integration_required
def test_pure_water_minimization_smoke():
    workdir = f"water-smoke-{int(time.time())}"
    topology = """#include "amber99sb-ildn.ff/forcefield.itp"
#include "amber99sb-ildn.ff/spc.itp"
#include "amber99sb-ildn.ff/ions.itp"

[ system ]
Pure water smoke test

[ molecules ]
"""

    assert server.write_mdp("topol.top", topology, workdir=workdir)["ok"] is True
    assert server.write_em_mdp(workdir=workdir, filename="em.mdp", nsteps=10)["ok"] is True

    solvated = server.solvate("topol.top", workdir=workdir, output="solv.gro", box=[2.2, 2.2, 2.2])
    assert solvated["ok"] is True, solvated

    preprocessed = server.grompp(
        "em.mdp",
        "solv.gro",
        "topol.top",
        workdir=workdir,
        output_tpr="em.tpr",
    )
    assert preprocessed["ok"] is True, preprocessed

    started = server.mdrun_start("em.tpr", workdir=workdir, deffnm="em", ntomp=1, nsteps=0)
    assert started["ok"] is True, started

    deadline = time.time() + 90
    last_status = {}
    while time.time() < deadline:
        last_status = server.mdrun_status(started["job_id"])
        assert last_status["ok"] is True, last_status
        if last_status["status"] in {"finished", "stopped"}:
            break
        time.sleep(1)

    if last_status.get("status") == "running":
        server.mdrun_stop(started["job_id"])
    assert last_status.get("status") == "finished", last_status
    listing = server.list_files(workdir)
    names = {entry["name"] for entry in listing["files"]}
    assert {"em.log", "em.edr", "em.gro"} & names
