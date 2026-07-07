# Examples

A minimal end-to-end walkthrough you can drive by talking to your MCP client.
These mirror the classic GROMACS protein-in-water protocol.

## 1. Energy minimisation of a solvated protein

Ask your agent something like:

> In workdir `lyso`, from `1aki.pdb`:
> 1. `pdb2gmx` with amber99sb-ildn / tip3p (ignore hydrogens)
> 2. `editconf` into a 1.0 nm cubic box, centered
> 3. `solvate` with spc216
> 4. `grompp` using `ions.mdp` → `ions.tpr`, then `genion` to neutralise
> 5. `grompp` using `em.mdp` → `em.tpr`
> 6. `mdrun_start` (deffnm `em`) in the background, then poll `mdrun_status`

The agent will call the tools in order and report when minimisation converges.

## Sample MDP files

- [`em.mdp`](em.mdp) — steepest-descent energy minimisation
- [`ions.mdp`](ions.mdp) — tiny preprocessing input used only to build a `.tpr` for `genion`

Copy them into your work directory with `write_mdp`, or ask the agent to write them for you.

## Verifying without a protein

A capping-group-free smoke test that always works — a pure water box:

> In workdir `water`, write a topology that includes
> `amber99sb-ildn.ff/forcefield.itp`, `spc.itp`, and `ions.itp` with an empty
> `[ molecules ]` section; `solvate` an empty 2.2 nm box with spc216; `grompp`
> with `em.mdp`; then `mdrun_start` a short minimisation in the background.
