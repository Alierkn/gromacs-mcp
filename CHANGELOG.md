# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-07-08

### Added
- Safe MDP template tools: `write_em_mdp`, `write_nvt_mdp`, `write_npt_mdp`,
  `write_md_mdp`, and `validate_mdp`.
- Typed analysis helpers: `make_ndx`, `energy`, `rms`, `rmsf`, `gyrate`,
  `hbond`, `sasa`, and `check`.
- MCP resources for projects, jobs, job logs, and built-in MDP templates.
- MCP prompts for protein MD setup, `grompp` debugging, and basic trajectory
  analysis.
- Background-job helpers: `mdrun_logs`, `mdrun_cleanup`, and `mdrun_forget`.
- Real GROMACS integration smoke test job for CI.

### Changed
- Workdir and file path handling is now sandboxed under `GROMACS_MCP_ROOT` by
  default. Absolute workdirs require `GROMACS_MCP_ALLOW_ABSOLUTE_WORKDIRS=1`,
  and tool file arguments cannot escape the selected workdir.
- Job registry writes are now file-locked and atomic, with richer process-group
  and command metadata.
- Tools now expose MCP annotations and structured output schemas.

### Security
- Block path traversal, hidden/sensitive text file reads/writes, binary text
  helper access, direct blocking `run_gmx(["mdrun", ...])`, and unsafe path
  tokens in `extra_args` / `run_gmx.args`.

## [0.1.0] — 2026-07-07

### Added
- Initial release with 15 tools across four groups:
  - **Introspection:** `gmx_info`, `list_files`, `read_text_file`, `write_mdp`
  - **Pipeline:** `pdb2gmx`, `editconf`, `solvate`, `grompp`, `genion`, `trjconv`
  - **Simulation:** `mdrun_start`, `mdrun_status`, `mdrun_list`, `mdrun_stop`
  - **Escape hatch:** `run_gmx`
- Background `mdrun` execution with a persistent job registry and live progress
  parsing (step / time / ns·day⁻¹ / ETA) from `md.log`.
- `error_summary` extraction of GROMACS `Fatal error:` blocks.
- Automatic `gmx` discovery via `PATH` with `GMX_BIN` override.
- `src/` package layout, MIT license, CI (Python 3.10–3.13), ruff, pre-commit, tests.

[Unreleased]: https://github.com/Alierkn/gromacs-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Alierkn/gromacs-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Alierkn/gromacs-mcp/releases/tag/v0.1.0
