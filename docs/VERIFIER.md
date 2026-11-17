# Laya content verifier

Laya reads the content of an agent turn to help a person judge whether work remains. Process-state multiplexers report whether a process is running; this card measures the content judgement. It does not certify task completion.

Measured 2026-11-14T19:13:05Z; public golden suite: 200 cases; live warning cut: 0.700.
Operator/Sonnet agreement on the uniform sample: κ 0.822 ± 0.377 (95% CI 0.445, 0.941; n=24).

## The headline

Area under the ROC curve, with a bootstrap 95% interval. Every voter answered the same rows: `bench/results/2026-11-14.json`, protocol `agent-open-work.q2.g7fada07abf88.h164-6dfc51a5`.[^earlier]

| Set | Rows | Served fine-tune | Base model | TF-IDF floor |
|---|---|---|---|---|
| Held-out: operator-graded turn ends no fine-tune trained on | 164 (96 open / 68 done) | 0.850 [0.788, 0.909] | 0.674 [0.588, 0.752] | 0.844 [0.782, 0.902] |
| Public golden suite | 200 (100 open / 100 done) | 0.857 [0.805, 0.906] | 0.824 [0.761, 0.883] | 0.750 [0.680, 0.816] |

## Live sample, at each recorded cut
One newest decision per uniformly sampled graded turn; precision and recall are reported separately for each landing class. Small denominators remain unmeasured.

- held: precision unmeasured [95% CI unmeasured, unmeasured], n=0; recall unmeasured [95% CI unmeasured, unmeasured], n=0.
- new: precision unmeasured [95% CI unmeasured, unmeasured], n=0; recall unmeasured [95% CI unmeasured, unmeasured], n=0.
- repeat: precision unmeasured [95% CI unmeasured, unmeasured], n=0; recall unmeasured [95% CI unmeasured, unmeasured], n=0.
- retired: precision unmeasured [95% CI unmeasured, unmeasured], n=0; recall unmeasured [95% CI unmeasured, unmeasured], n=0.
- unknown: precision unmeasured [95% CI unmeasured, unmeasured], n=1; recall unmeasured [95% CI unmeasured, unmeasured], n=2.

False shown warnings per 100 graded sampled turn ends: 0.000 (false=0, n=4). This is a sample estimate, not a census.
Latency: p50 unmeasured ms; p90 unmeasured ms; n=0; measured never.
Challenger shadow agreement: unmeasured; n=0 across 0 days.

## Reproduce the public golden suite
The suite is `lab/golden/agent-open-work.jsonl` in this repository. The command below is the bench on an empty data directory: only the base model can answer there, and its `golden` row must match the base model's golden AUC above (`REPRODUCE.md` has every number it prints). Fine-tuned weights and private golden cases are not published. Every case is invented, none from a transcript: 34 hand-written; 166 agent-written (batch-2), accepted on a stratified random sample of 40 that the operator graded, all 40 agreed: at 95% confidence at most 10 of them carry a wrong label. The question's wording was chosen on the first 34 cases of the public suite, so their numbers flatter the base model.

```sh
TLDRACK_DATA_DIR=$(mktemp -d) uv run --project lab laya-lab bench agent-open-work
```

The operator and live measurements above come from private local grades and cannot be reproduced from the public suite. No private message and no private suite is in this repository. Every voter, challengers included: `bench/results/`. What the model is and is not for: `MODEL_CARD.md`.

[^earlier]: An earlier protocol, from before this bench, put the fine-tune served at the time at AUC 0.879: question v1, on the 84-row evaluation split of its own training record, measured 2026-10-29. It asked another question of other rows, so it does not compare with the table above. The table is the number to quote.
