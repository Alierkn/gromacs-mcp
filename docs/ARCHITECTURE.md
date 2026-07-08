# Architecture

`gromacs-mcp` is a local stdio MCP server that wraps a local `gmx` binary. It is
designed for a trusted operator using an MCP client, not for remote multi-user
execution.

## Runtime Model

- `GMX_BIN` selects the executable; by default the server discovers `gmx` on
  `PATH`.
- `GROMACS_MCP_ROOT` is the project root for relative work directories.
- Tool file arguments are resolved relative to the selected workdir and must
  remain inside that directory.
- Long `mdrun` jobs are detached with `start_new_session=True`; metadata is
  stored next to the project root in `.jobs.json`.

## Tool Surface

- Curated workflow tools cover common setup, simulation, and analysis commands.
- `run_gmx` remains as an escape hatch for non-blocking GROMACS subcommands, but
  it rejects direct `mdrun` calls and unsafe path tokens.
- Tools expose MCP annotations so clients can distinguish read-only, local-run,
  write-only, and destructive operations.
- MCP resources expose projects, jobs, job logs, and built-in MDP templates.
- MCP prompts provide common workflow entry points for setup, debugging, and
  basic analysis.

## Security Boundaries

The main boundary is the selected workdir:

- Relative workdirs are sandboxed under `GROMACS_MCP_ROOT`.
- Absolute workdirs require `GROMACS_MCP_ALLOW_ABSOLUTE_WORKDIRS=1`.
- File arguments cannot use absolute paths, `~`, or `..`.
- Text helpers block hidden files, common secret names, and binary run/trajectory
  formats.

Annotations are client hints, not enforcement. The server enforces path and
argument policy itself before invoking `gmx`.

## Testing Strategy

- Unit tests cover tool registration, MCP annotations/resources/prompts, path
  sandboxing, job registry helpers, and MDP templates.
- Integration tests are opt-in with `RUN_GROMACS_INTEGRATION=1` and execute a
  small pure-water GROMACS workflow.
- CI runs fast checks on the Python matrix and a separate Ubuntu GROMACS smoke
  job.
