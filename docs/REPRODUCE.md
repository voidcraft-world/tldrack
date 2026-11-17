# Reproduce the public golden numbers

The public suite is `lab/golden/agent-open-work.jsonl`: 200 cases, 100 open and 100 done. The command scores it with the base model, `convaiinnovations/laya`, which the first run downloads from Hugging Face. Its data directory is a new empty one, so nothing of yours is read, and the base model is the only voter that can answer.

Run it from the repository root. `uv run` installs the lab's dependencies into `lab/.venv` first.

```sh
TLDRACK_DATA_DIR=$(mktemp -d) uv run --project lab laya-lab bench agent-open-work
```

## What it prints

A `golden` block with one row, `laya-base`. The published run, `bench/results/2026-11-14.json` (golden protocol `agent-open-work.q2.g7fada07abf88`), printed:

| AUC [95% CI] | Brier | ECE | Cut | False ⚠️ | Missed open | Cost |
|---|---|---|---|---|---|---|
| 0.824 [0.761, 0.883] | 0.203 | 0.156 | 0.70 | 4 | 78 | 86.0 |

Cost is 2 × false ⚠️ + missed open, at the cut the model serves with. The interval is a percentile bootstrap with a fixed seed (2,000 resamples, seed 20261113), so the same probabilities give the same interval. The published run used `mps`, the default device; `--device cpu` can move the last digit.

The run also writes `bench/results/<today>.json` into the checkout. `git status bench/` shows it: delete it, or `git restore` it where it replaced a published day.
