"""Shared pytest setup for safe local test execution."""

from __future__ import annotations

import os
import tempfile

os.environ["GROMACS_MCP_ROOT"] = tempfile.mkdtemp(prefix="gromacs-mcp-test-")
os.environ.pop("GROMACS_MCP_ALLOW_ABSOLUTE_WORKDIRS", None)
