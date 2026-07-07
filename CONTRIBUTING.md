# Contributing to gromacs-mcp

Thanks for your interest in improving `gromacs-mcp`! 🎉

## Development setup

```bash
git clone https://github.com/Alierkn/gromacs-mcp && cd gromacs-mcp
uv sync --extra dev
uv run pre-commit install
```

## Before opening a PR

```bash
uv run ruff check .          # lint
uv run ruff format .         # format
uv run pytest                # tests (gmx tests auto-skip without GROMACS)
```

CI runs the same checks on Python 3.10–3.13.

## Adding a new tool

1. Add an `@mcp.tool()`-decorated function in `src/gromacs_mcp/server.py`.
2. Give it a clear docstring — the first paragraph becomes the tool description
   the LLM sees, so state **what it does, key parameters, and what it returns**.
3. Reuse the shared `_run()` helper so error handling and output truncation stay
   consistent. Prefer explicit, typed parameters over free-form strings.
4. Add its name to `EXPECTED_TOOLS` in `tests/test_server.py`.

## Design principles

- **Hybrid, not exhaustive.** Curated typed tools for the common pipeline; the
  `run_gmx` escape hatch covers the long tail. Don't wrap every `gmx` flag.
- **Never block.** Anything that can run for minutes/hours must be a background
  job (see `mdrun_start`), not a blocking call.
- **Fail loudly and usefully.** Surface the real GROMACS error, not a generic one.

## Reporting bugs

Open an [issue](https://github.com/Alierkn/gromacs-mcp/issues) with your OS,
GROMACS version (`gmx --version`), the tool call, and the full result.

By contributing you agree your work is licensed under the [MIT License](LICENSE).
