<div align="center">

# 🧬 gromacs-mcp

**Drive [GROMACS](https://www.gromacs.org/) from any MCP client, in natural language.**

Build topologies, solvate, add ions, preprocess, run simulations **in the background**,
and post-process trajectories — all through the [Model Context Protocol](https://modelcontextprotocol.io).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-server-6E56CF.svg)](https://modelcontextprotocol.io)
[![CI](https://github.com/Alierkn/gromacs-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Alierkn/gromacs-mcp/actions/workflows/ci.yml)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

</div>

---

## Why

Setting up and running a molecular-dynamics simulation is a long, error-prone chain of
`gmx` sub-commands. `gromacs-mcp` exposes that pipeline as clean MCP tools so an LLM agent
(Claude Code, Claude Desktop, or any MCP client) can plan and run the whole workflow while
you supervise — **without the agent ever blocking on a multi-hour `mdrun`.**

## Features

- **Hybrid tool design** — typed helpers for every common pipeline step **plus** a generic
  `run_gmx` escape hatch for any other `gmx` sub-command.
- **Non-blocking simulations** — `mdrun_start` launches in the background and returns a job id;
  poll progress (step / time / ns·day⁻¹ / ETA) with `mdrun_status`.
- **Actionable errors** — GROMACS `Fatal error:` blocks are extracted into a dedicated
  `error_summary` field, so the real cause survives output truncation.
- **Project-scoped work dirs** — each system lives in its own directory under a configurable root.
- **Zero-config discovery** — finds `gmx` on `PATH` automatically (override with `GMX_BIN`).

## Tools

| Category | Tool | Purpose |
|----------|------|---------|
| **Introspect** | `gmx_info` | GROMACS version / build / binary path |
| | `list_files` | List files in a work directory |
| | `read_text_file` | Read an `.mdp` / `.top` / `.log` (clipped) |
| | `write_mdp` | Write an `.mdp` (or any text) file |
| **Pipeline** | `pdb2gmx` | Structure → topology + coordinates |
| | `editconf` | Define the simulation box |
| | `solvate` | Fill the box with solvent |
| | `grompp` | Preprocess → run input (`.tpr`) |
| | `genion` | Add neutralising / salt ions |
| | `trjconv` | Trajectory PBC / centering / conversion |
| **Simulation** | `mdrun_start` | Start a simulation **in the background** |
| | `mdrun_status` | Poll progress + log tail |
| | `mdrun_list` | List all background jobs |
| | `mdrun_stop` | Terminate a job (writes checkpoint) |
| **Escape hatch** | `run_gmx` | Any other `gmx` sub-command |

## Requirements

- **GROMACS** installed and `gmx` runnable (`brew install gromacs`, conda, or a source build).
- **Python ≥ 3.10**.
- An MCP client (e.g. [Claude Code](https://claude.com/claude-code) or Claude Desktop).

## Install & run

The recommended way is [`uv`](https://docs.astral.sh/uv/) — no global install needed:

```bash
# Run straight from GitHub (or a local checkout)
uvx --from git+https://github.com/Alierkn/gromacs-mcp gromacs-mcp
```

<details>
<summary>Alternative: pip / pipx</summary>

```bash
pipx install git+https://github.com/Alierkn/gromacs-mcp     # isolated, on PATH
# or
pip install git+https://github.com/Alierkn/gromacs-mcp
gromacs-mcp        # starts the stdio server
```
</details>

## Connect it to your MCP client

### Claude Code

```bash
claude mcp add gromacs --scope user -- \
  uvx --from git+https://github.com/Alierkn/gromacs-mcp gromacs-mcp
```

Then check: `claude mcp list` → `gromacs: … ✔ Connected`.

### Claude Desktop

Add to `claude_desktop_config.json`:

```jsonc
{
  "mcpServers": {
    "gromacs": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Alierkn/gromacs-mcp", "gromacs-mcp"],
      "env": { "GMX_BIN": "/opt/homebrew/bin/gmx" }
    }
  }
}
```

## Configuration

| Env var | Default | Meaning |
|---------|---------|---------|
| `GMX_BIN` | auto (`which gmx`) | Path to the `gmx` executable |
| `GROMACS_MCP_ROOT` | `~/gromacs-mcp/projects` | Base dir for work directories |

Relative `workdir` arguments resolve under `GROMACS_MCP_ROOT`; each subdirectory is one
"project" (system) holding its `.gro` / `.top` / `.tpr` / `.log` files.

## Example prompt

> *"In workdir `lyso`, build a topology from `1aki.pdb` with amber99sb-ildn / tip3p, put it
> in a 1.0 nm cubic box, solvate it, neutralise with ions, then run a short energy
> minimisation in the background and tell me when it's done."*

The agent chains:
`pdb2gmx → editconf → solvate → grompp (ions) → genion → grompp (em) → mdrun_start → mdrun_status`.

## How it works

Each tool shells out to the local `gmx` binary via `subprocess`, captures stdout/stderr
(GROMACS writes almost everything to stderr), and returns a structured result. Long
simulations are detached with `start_new_session=True`; their metadata is persisted to a
JSON registry so `mdrun_status` can report progress parsed from the live `md.log`.

## Development

```bash
git clone https://github.com/Alierkn/gromacs-mcp && cd gromacs-mcp
uv sync --extra dev
uv run pytest        # tests that need gmx auto-skip if it is absent
uv run ruff check .
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Related

- [**vmd-mcp**](https://github.com/Alierkn/vmd-mcp) — companion MCP server for headless
  VMD analysis & rendering. Pair them: simulate with GROMACS, visualise with VMD.

## License

[MIT](LICENSE) © Ali Erkan Ocaklı
