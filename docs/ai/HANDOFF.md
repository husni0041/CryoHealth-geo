# HANDOFF — CryoHealth-geo — 2026-11-06
Session: flowpath-exposure-and-confidence  Model: claude-sonnet-5-5  Branch: feat/flowpath-exposure-and-confidence  Goal: API#13  Task: flow-path downstream population + per-score confidence

## State
Not merged, not deployed. This branch brings the D8 flow-path exposure (written for #19 by laiba-107, only ever on the unmerged `AI-advances` branch) onto `main`, fixes what would break CI/production, and adds `components.confidence`. Full detail: `sessions/2026-11-06-flowpath-exposure-and-confidence-handoff.md`.

## Key finding
Live check on Shishper (real DEM + WorldPop, 20 km): routing works, but the hand-estimated outlet is at 6,252 m (a peak) and the result changes about 2x with the start point (2,434 from the outlet, 5,297 from the lake point, old square buffer 21.8). Treat the number as a model estimate until the outlet coordinates are surveyed. Only 1 of 6 lakes and 20 of 50 km were run live (4 GB machine).

## Next action
Review and merge; deploy and run a hazard pass; replace the outlet coordinates in `pipeline/lakes.py`; run the 6 lakes at 50 km on a machine with more memory.

## Open questions for a human
- Who owns the alert policy and can confirm the confidence cut-offs (15 / 90 days)? — blocking: no
- Where do surveyed dam-toe coordinates for the six lakes come from (GLOF-II reports, ICIMOD, imagery)? — blocking: no

## Verification status
tests: 91/91 default suite, ruff clean, live: Shishper 20 km only

## Resume with
/uexel:orient
