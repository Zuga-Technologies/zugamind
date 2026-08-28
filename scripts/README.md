# scripts/ — the reproduce-our-science toolchain

Everything here exists so a stranger can re-run the published experiments
(write-ups in [docs/experiments/](../docs/experiments/), raw data in
[experiments/](../experiments/)) or verify their own setup. Nothing in this
folder is imported by the engine at runtime.

## Your harness

| file | what it's for |
|---|---|
| `verify_harness.py` | Sends a planted test item through *your* harness config and checks the wake actually fires. The "is my wiring right?" tool — run this first. Cited by the README, the harness bug-report template, and every example config. |

## Experiment runners (one per experiment)

| file | what it's for |
|---|---|
| `run_exp001.py` | Runs EXP-001 (workspace vs cron) and, with cadence flags, the EXP-002 sweep. `--smoke` mode is hermetic: no keys, no network, $0. This is the README's "Replay it" command. |
| `run_exp003.py` | Runs EXP-003 (attention-health ablation). Imports its corpus builder directly. |
| `run_exp004.py` | Runs EXP-004 (strong-baseline comparison). Also home of `calibrate_workspace_floor` — the offline calibration the engine's `act/floor_calibration.py` cites as the source of its constants. |

## Corpus builders and frozen inputs

| file | what it's for |
|---|---|
| `build_exp001_corpus.py` | Built the EXP-001/002 event corpus (real scanner events + planted canaries). |
| `build_exp003_corpus.py` | Builds a fresh per-seed EXP-003 corpus; imported by `run_exp003.py`. |
| `build_exp004_corpus.py` | Builds the EXP-004 corpus (the first attempt's flaw is documented in `experiments/exp004-out-invalid/`). |
| `exp001_corpus.jsonl` | The frozen corpus the measured EXP-001/002 runs used — replays consume this exact file. |
| `exp001_claude_config.json` | The pinned harness config (model, prompt, caps) from the measured EXP-001 runs, as cited in the results. |
| `exp003_claude_config.json` | Same, for EXP-003. |

## Analysis helpers

| file | what it's for |
|---|---|
| `score_exp001.py` | Recomputes detection scores from a run folder — how the summary numbers are derived. |
| `analyze_misses.py` | Lists which planted canaries a run missed, for diagnosis. |

**Rule:** file paths in here are cited by the README, docs, tests, and one
engine docstring. Renaming or moving anything in this folder means updating
every citation in the same commit.
