# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security vulnerabilities.

Instead, use GitHub's [private vulnerability reporting](https://github.com/Alierkn/gromacs-mcp/security/advisories/new)
or email **alierkn.ocakli@gmail.com**. You will get an acknowledgement within a
few days, and we will keep you updated as the issue is resolved.

## Scope & threat model

`gromacs-mcp` executes the local `gmx` binary via `subprocess` on behalf of an
MCP client. It does **not** run a shell (`shell=False`), so command arguments are
passed as an explicit list. Be aware that:

- Tools accept file paths and a `run_gmx` escape hatch that forwards arbitrary
  arguments to `gmx`. Treat the MCP client as a trusted operator — do not expose
  this server to untrusted input.
- Work directories are created under `GROMACS_MCP_ROOT`; relative paths resolve
  there. Absolute `workdir` values are rejected unless
  `GROMACS_MCP_ALLOW_ABSOLUTE_WORKDIRS=1` is explicitly set.
- Tool file arguments must stay inside the selected work directory. Path
  traversal, hidden path components, common secret names, and binary trajectory /
  run-input files are blocked for text read/write helpers.
- `run_gmx` remains an escape hatch for non-blocking GROMACS subcommands, but it
  rejects direct `mdrun` calls, absolute path tokens, and `..` traversal tokens.

## Supported versions

The latest release on the `main` branch receives security fixes.
