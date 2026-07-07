# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/Alierkn/gromacs-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Alierkn/gromacs-mcp/releases/tag/v0.1.0
