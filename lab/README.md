# laya-lab — fine-tune Laya for the rack's decision surfaces

The base Laya checkpoint is near chance on typed decisions zero-shot and over-confident out of the
box (its own model card says so). This turns the operator's own labels into a fine-tuned, calibrated
checkpoint per surface, and only lets one reach a caller when it measurably beat the model served.

```bash
cd lab && uv sync                           # torch + laya 0.4.19 (pinned, same as the bridge runtime)

# Coordinator: run a DEEP audit that Claude ranks, then accumulate its labels
uv run laya-lab export-coordinator && uv run laya-lab train coordinator

uv run laya-lab status                      # what AGENT RACK › LAYA shows, in the terminal (no torch)
uv run laya-lab models                      # registered fine-tunes, their held-out numbers, swept cuts
uv run laya-lab golden agent-open-work      # score the SERVED model on the golden cases
uv run laya-lab golden agent-open-work --all-checkpoints   # the base + every fine-tune on disk, one table
uv run laya-lab settle agent-open-work      # a signed-off batch loses `provisional` (dry run without --apply)
uv run laya-lab unregister coordinator      # back to the base checkpoint (and the code's cut)
uv run laya-lab compare agent-open-work --voter laya:served --voter other=http://127.0.0.1:8010
                                            # voters side by side: any loopback /v1/systemone server beside Laya,
                                            # plus their mean and their out-of-fold stack (compare.py)
uv run laya-lab gate agent-open-work        # the promotion gate train runs: the newest unserved checkpoint vs
                                            # the served model, paired (gate.py); --prospective after a registration;
                                            # --fixture PATH also writes its evidence trimmed to numbers
uv run laya-lab teacher-kappa               # gpt-oss:20b as a local teacher: κ vs Sonnet and the operator
uv run laya-lab bench --dry-run             # the plan: which voters can answer here, on which sets, how many rows
uv run laya-lab bench                       # every available voter on the public golden set (and the private
                                            # held-out split when the store has one) → bench/results/<date>.json:
                                            # aggregates only, AUC with a bootstrap 95% interval (bench.py)
uv run laya-lab bench --panel served --panel base   # also the panel voter: many questions of each turn, one fitted
                                            # line over the answers (panel.py); `served` or `base` answers the new ones
uv run laya-lab bench --panel served/3q     # also three questions and no line: the most confident answers
                                            # (`served/3q-mean`: their equal-weight mean)
uv run laya-lab question-challenger agent_open_work --version 3   # the served rows refitted at another question
                                            # version, never registered; `bench --challenger` scores it (question.py)
uv run laya-lab card                        # preview the verifier card; `--publish --digest D` writes the reviewed
                                            # one into docs/ (VERIFIER.md, REPRODUCE.md, MODEL_CARD.md) (card.py)
```

Everything laya-lab reads and writes of the operator's is under `<data>/laya/` (`~/.local/share/tldrack/laya`
unless mapped; the plugin's `laya_bridge/paths.py` owns the order), never in this checkout. What it downloads,
loads or calls, and under which licence: `NOTICE`.

## The open-work review loop (`agent_open_work`)

The REVIEW deck (`/laya-review.html`, AGENT RACK › LAYA › REVIEW ↗) records the operator's answers; these
commands turn them into a better cut, a private yardstick and cleaner training rows. The bridge
starts all four from AGENT RACK › LAYA, one at a time, the rest queued (`plugins/laya/laya_bridge/lab_jobs.py`
owns how), so their names and exit codes are a contract. `golden` and `compare` start the same way;
`laya-lab verdict` prints the verdict of the report a scheduled run wrote. `train` is never a button: it
registers what passes its gate (as @challenger by default).

**The nightly pack** (`laya-lab nightly agent-open-work`, one command for a scheduler to run each night, under one
gpu-slot take when the machine has a lease) runs them together: golden on the base, @champion and every registered version (an
unchanged one reused, by checkpoint digest, device and dataset version), an incremental SCORE, compare with each
challenger and the TF-IDF floor, the challenger shadow pass on the day's live verdicts, the stability pass, and
readiness with a RE-SWEEP dry run at 40/10 and every 10 grades after. While it runs, a lab press waits behind it.
Owned by `laya_lab/nightly.py` — see the note there.

**What the operator feels.** `laya-lab status` prints three numbers, each from the operator's grades of live verdicts
in the uniform `live_sample` stratum only, each with its n and a 95% interval, and a bare count under 20 rows:
- **false ⚠️ per 100**: turns where the rack showed a ⚠️ and the operator graded the work not open, per 100 graded sampled turns.
- **missed 🙋 per 100**: turns graded NEEDS YOU that drew no shown ⚠️, per 100 graded sampled turns, split by the mark drawn
  instead (✅, the costly miss; 💬; a ⚠️ the rack held back); an OPEN graded without RUNNING or NEEDS YOU is counted apart.
- **💬 share**: the sampled verdicts in the reply band, of every sampled verdict, then how many are graded and their open / done / read split.
`bench --rack` writes the same block into the results file. Owned by `laya_lab/metrics.py` — see "Felt".

**Two meters beside it.** `laya-lab latency agent-open-work` (a read: no model runs, no gpu slot)
reads the decision ledger: `ms` by state length, machine load and cold start, and the token overflow of the states Laya
is asked. `laya-lab stability agent-open-work` (a step of the pack; by hand it takes the gpu slot itself) is
STABLE κ: agreement between consecutive checkpoints' verdicts on a fixed stop set of live states nobody labelled.
Both show on AGENT RACK › LAYA (LATENCY, STABLE). Owned by `laya_lab/latency.py` and `laya_lab/stability.py` — see
the notes there.

| command | rack button | does | exit |
|---|---|---|---|
| `sweep agent-open-work --labels operator --apply` | RE-SWEEP | sweeps the cut on held-out operator labels and writes it to `models.json` | 0 · 3 refused (not ready, or a built-in served) · 1 |
| `sweep agent-open-work --reset` | RESET | back to the code's cut | 0 |
| `review-open-work --model served` | SCORE | the served model's p on every reviewable text not yet scored under its digest and device (the deck's `disagree` and `uncertain` strata), and the surrogate's confidence in each Sonnet label (`suspect`); `--full` re-scores all | 0 · 1 |
| `promote agent-open-work --flips --apply` | PROMOTE | operator flips of the label budget's TRAIN half → the private golden suite, then rebuilds the training rows | 0 · 3 nothing to promote / refused |

- `sweep` without `--apply` is a dry run; `--labels auto` (the default) uses operator labels once
  there are enough, else Sonnet's with the operator's on top. `--no-heldout` includes trained rows
  (IN-SAMPLE: never applied, and never shown as the rack's sweep). Every report names its model and label source.
- **The cut rule:** owned by `bridge/tldrack/open_work.py` `FALSE_WARNING_COST` — see the note there.
- **Rows a sweep may use, and when it is ready:** owned by `laya_bridge/review_data.py`
  (`clean_label_rows`, `sweep_readiness`) — the same rule the rack shows and the bridge gates on.
- **How sure, and how calibrated:** every sweep and golden report carries `trust` (κ ± and precision/recall at
  the live cut, through `laya_bridge/agreement.py`), ECE and reliability bins around both live cuts. A sweep on
  ≥ 40 operator labels adds `calibration`, operator-ECE beside teacher-ECE with one refit temperature: owned by
  `laya_lab/metrics.py` — see "Calibration".
- A swept cut is scoped to the checkpoint it was swept on: `train` (a new registration) and
  `unregister` clear it. Re-sweep after every retrain.
- `promote` without `--apply` prints what it would add and the balance of each suite, open against done;
  `--key K` names one operator-labelled text instead of every flip.
- **The label budget** holds 1 in 2 operator-labelled keys in reserve: `build()` and `train` leave them out, and
  `promote` refuses them, so a new checkpoint still has clean rows to sweep. Owned by
  `plugins/laya/laya_bridge/label_budget.py` — see the note there.
- An operator log, export or labels file the bridge's readers refuse (a link, or past their size cap)
  stops every command that reads it with exit 1 and its path, rather than running as if it were
  empty (`open_work_data.require_sources`).

**Operator labels win** in `build()` and every sweep: owned by `laya_lab/open_work_data.py`
`effective_labels` — see the note there. `label-open-work` never pays to label a text the operator
already answered.

## Golden suites (`golden/`, `laya_lab/golden.py`)

Small, hand-labelled cases — the fixed yardstick a wording change, a threshold move or a fine-tune is
read against. Not training data: `build()` holds every golden text back. Each suite imports its
question, state builder and threshold from the surface's OWNER, so it scores exactly what is served.
`agent-open-work` (the rack's ✅⚠️, owner `bridge/tldrack/open_work.py`): 200 cases, 100 open / 100 done, every
text invented and every noun generic. The first 34 are hand-written; the question's wording was chosen on them, so
their numbers flatter the base model, and a held-out set of turn ends from the operator's own sessions is what a
fine-tune is judged on. The other 166 are agent-written in four categories (failing tests, waiting on the operator,
done with a caveat, done clean) and tagged `batch-2` and `provisional`.

**The batch sign-off.** A provisional case counts (in the headline, compare and the gate) only once signed off. The
operator signs off a batch on a sample of 40, spread over its strata by a published rule, in the REVIEW deck's golden
stratum: all 40 agreed accepts the batch (at 95% at most 10 of the 166 carry a wrong label), one flipped or skipped
rejects it, and the deck then deals all of it. `laya-lab golden` prints where each batch stands; once it is accepted,
`laya-lab settle agent-open-work --apply` drops `provisional` from what counts, so a run on an empty store (the
verifier card's reproduce command, `bench`) counts it too. Owner of the rule: `plugins/laya/laya_bridge/lab_files.py`
(`BATCH_TAG`) — see the note there. The suite rides in the verifier card verbatim, so it stays under
`card_files.FILE_MAX_CHARS`.

Two suites per name, scored together and reported apart:
- **public** `lab/golden/<suite>.jsonl`: hand-written, publishable, checked in. The headline.
- **private** `<data>/laya/golden/<suite>.private.jsonl`: the operator's flips, written by
  `promote`, **never committed**. It carries the operator's own closing text, so it is named apart
  from the public file and lives outside the repo; `tests/test_golden.py` refuses any operator-shaped
  row (`f-` id, `operator` tag, `key`/`labels`) in `golden/` whenever laya-lab's tests run, CI
  included ([Tests](#tests)). A private case the served fine-tune trained on is reported as
  `contaminated` and left out of the private numbers.

Reports land in `<data>/laya/evals/` (names and fields: `laya_bridge/lab_files.py`), with
κ at the cut, the checkpoint that answered, and case ids — never text. A public case the operator
flipped in REVIEW prints as `operator disputes:`; fix the public file by hand if they are right.

`agent_open_work` data: `export-open-work` (merges; `--limit` caps NEW turn ends, kept ones stay,
and it stops at the size the bridge reads, `review_data.UNLABELLED_CAP_BYTES`) →
`label-open-work` (Claude, subscription only, verified per call) → `train agent_open_work`
(`--epochs` defaults to 2). Why 2, not 4: on a few hundred rows four epochs overfit (near-certain
confidence, a worse held-out loss), which the gate refuses; two do not. Measured wall-clock, one loop on an Apple-silicon
laptop under other load: export seconds, labelling a few hundred turn ends a few minutes, a 2-epoch fit of about a
thousand rows about an hour on MPS, the gate 73 s, `bench` with seven voters 2 min.

## The local teacher (`teacher-kappa`)

`uv run laya-lab teacher-kappa` has `gpt-oss:20b` on this machine's Ollama label every Sonnet-labelled and every
operator-graded turn end with Sonnet's own rubric, then reports κ against Sonnet, against the operator and Sonnet's own κ
against the operator, each with a bootstrap 95% CI. The teacher rule reads it: PASS once κ vs Sonnet ≥ 0.75 and κ vs the
operator sits at or above the low end of Sonnet's interval. Resumable (answers append to
`data/agent-open-work-teacher.jsonl`, keyed by the teacher's weights and the rubric), loopback only.
It starts no call its run's stop would cut. Only a down transport fails it,
and never once the selection is covered.
`--check` and `--verdict-json` are a scheduler's probes. Every rule: owned by
`laya_lab/teacher_local.py` — see the note there.

## A Claude model as a voter (`claude-vote`)

```bash
uv run laya-lab claude-vote --dry-run     # how many states it would ask; calls nothing
uv run laya-lab claude-vote               # ask claude-opus-5-5 p(open) of every state the bench scores, both arms
uv run laya-lab bench --voter opus=votes:claude-opus-5-5/rubric --voter opus-q=votes:claude-opus-5-5/question
uv run laya-lab claude-vote --model claude-fable-5-2 --arm question   # another model, one arm
```

**Measured 2026-11-20** (`bench/results/2026-11-20.json`, the `question` arm of both). Held out, `fable` (Claude Fable
5.2) ranks at AUC 0.911 [0.860, 0.953] and `opus` (Claude Opus 5.5) at 0.902 [0.845, 0.949]; on golden 0.995 and 0.993.
A vote costs 263 ms against 530 ms, amortized over a batch of 25. Paired on the file's own draws, and not in the file:
Fable against Opus is +0.009 [−0.014, +0.035] held out, a tie. On the 43 held-out turns the operator graded by hand,
Fable 0.711, Opus 0.680 and the served fine-tune 0.694: Fable against the fine-tune is +0.017 [−0.098, +0.149].

`compare` and `bench` refuse a voter that is not on this machine, and they still do. `claude-vote` is the
one command that sends: each state, exactly as Laya is asked it, to a Claude model on the subscription login,
once. The answers land in `data/agent-open-work-votes.jsonl`, and a `votes:<model>[/<arm>]` voter reads that
file. Two arms: `question` asks Laya's own one-line question, `rubric` the teacher's rubric. The model
labels are Sonnet's and were made on that rubric, so score a rubric vote against the operator's labels
(`--labels operator`). Why a file and what a row holds: owned by `laya_lab/claude_voter.py` — see the note there.

## Do two voters make different mistakes? (`compare`'s `stack`)

With two voters or more, `compare` adds a `stack` row beside `mean`: one logistic regression over the voters'
logits, with the same columns as every voter. It is fitted and scored out of fold on the very set it is measured on
(folds by a stable hash of the row id), so no row is scored by a stack that saw its label. Under each set's table:

- `stack vs <voter>`: the paired ΔAUC against the best single voter and its bootstrap interval
  (`bench.paired_auc_delta`). A **win only when the interval sits above zero**; a positive point inside a straddling
  interval is no win.
- `<a> wrong N → <b> right K`: at each voter's own live cut, of the rows `a` gets wrong, how many `b` gets right.

Measurement only: nothing registers, serves or saves a stack, and its cut is the code's, as `mean`'s is. A set too
small to fold (fewer than ten rows, or a fold that leaves one class) prints `stack skipped` with the reason. Owned
by `laya_lab/stack.py` — see the note there.

## Operator notes (`data/operator-notes.md`)

An optional markdown file in the laya store, beside the labels: what you care about, in your own words. Two
invented lines:

```markdown
- A failing battle test in project zeta-prime is noise: that turn is done.
- I am learning about vector clocks, so an answer about them is open until I have read it.
```

- **Where it goes.** The teacher's rubric, and nowhere else: Sonnet (`label-open-work`), the local teacher
  (`teacher-kappa`) and the `rubric` arm of `claude-vote` read it as a delimited block after the rubric. Their
  labels are what a fine-tune trains on, so your notes reach Laya's weights at the next retrain.
- **What it never does.** It never enters the state Laya judges or Laya's question: an encoder with a 512-token
  window reads no such file. It moves only what the teacher answers, open or done; it adds no class and no field.
  It re-asks no old label (the week's teacher cap is small): a new label carries the notes' digest, and
  `laya-lab status` prints the digest, the size and how many labels were made under it. Because the local teacher's
  rubric changes with it, an edited file means `teacher-kappa` again before that teacher labels.
- **What it must be.** A regular file, never a link, at most 4 KB, UTF-8 text. Anything else stops every teacher
  run with its reason; a missing or blank file leaves every prompt byte-identical to no notes at all.
- **Private.** Your notes stay on your machine like your labels: never in a report, a results file or a log line.
  `bench` refuses to write a results file that holds a line of them. Owned by `laya_lab/open_work_data.py`
  § operator notes — see the note there.

## The resource lease (optional)

With nothing configured the lab just runs: no lease, no night window, no queue. On a machine that runs other
heavy jobs, `TLDRACK_LEASE_MODULE=/abs/path/lease.py` names the operator's own lease, and every command that
loads a model takes a slot from it first (`laya_lab/infer.py` `hold_slot`): `golden`, `review-open-work`,
`sweep` and `compare` claim 1.6 GB of the gpu slot per router target, `train` claims `TRAIN_GB`,
`--device cpu` claims 4 cores, and `label-open-work` claims the one `subscription_claude` holder. A slot with
no room exits **75** naming who holds it (a busy machine, never a failure); a command its scheduler marked
queues up to 10 minutes first, keeps to the lease's night window, and pauses between predictions while a rack
verdict or load wants the machine (`infer.yield_point`). The protocol a lease module provides: owned by
`laya_lab/lease.py` — see the note there.

## What `train` does

1. Splits the dataset by a hash of each STATE (15% held out, stable across re-exports), and keeps the
   split beside the checkpoint (`<checkpoint>.data/train.jsonl`, each row with its label `key`): the
   sweep, the deck and the gate read it to tell held-out rows from trained ones, so a checkpoint without
   it cannot be swept or gated.
2. `laya.train.finetune` on MPS: trains, fits per-type temperatures with the runtime's own fitter,
   and scores base vs fine-tune on the held-out rows.
3. Registers the checkpoint in `<data>/laya/models.json` only when the promotion gate
   passes it against the SERVED model (`laya_lab/gate.py` owns the checks; a surface without a golden suite
   keeps laya's own rule: held-out log-loss improved, accuracy fell at most 0.01). `--force` overrides.
   It registers as the surface's **@challenger** (`--as challenger`, the default): never served, shadow-scored
   by the nightly pack until a promotion moves @champion onto it. `--as champion` serves it at once, the old
   champion kept as @previous. The version records how it was trained (`trained`: epochs, rows, base revision,
   when) and the gate's verdict (`tags`). The full laya report is saved beside the checkpoint as
   `laya-lab-report.json`, the gate's verdict in `evals/gate-*.json`.

The bridge's laya plugin serves each surface's @champion by name, and the callers pin it automatically
(`fine_tune_name` in `plugins/laya/laya_bridge/paths.py`). Restart Laya after a champion moves (AGENT RACK › LAYA › RESTART).
A swept cut lives on its checkpoint's version, so it moves with the alias: a promotion serves the challenger at
its own cut and a rollback restores the old champion's. models.json is v2 (versions + aliases); a v1 file is still
served as it was, and the first write (any armed press, `train`, the nightly pack's first step, or
`laya-lab registry migrate`) rewrites it, keeping the v1 file beside it. Shape and rule: owned by
`plugins/laya/laya_bridge/paths.py` — see the note there.

Everything trained on lives under `<data>/laya/` — the operator's audits and turn ends never enter the
repo. Where `<data>` is (`$TLDRACK_DATA_DIR`, a `paths.json` mapping, else `~/.local/share/tldrack`): owned by
`plugins/laya/laya_bridge/paths.py` — see the note there. A first run creates the folder. File modes: 0600 files, 0700 folders, created so by laya-lab's writers
(`laya_lab/data.py`) and repaired at the start of every command — owned by
`plugins/laya/laya_bridge/files.py` (`tighten_params`).

Owners: the registry's read side, location and folder names — `plugins/laya/laya_bridge/paths.py`;
every file laya-lab writes that the plugin reads — `laya_bridge/lab_files.py`; κ and its interval — `laya_bridge/agreement.py`;
the text key — `laya_bridge/rows.py`; coordinator rows — `laya_lab/data.py`.

## Question v3: the request, then the closing (`question-challenger`)

Question v2 judges the closing message alone, and a closing cannot tell its own request apart: "happy to go deeper"
is done after "explain X" and open after "explain every rule". Version 3 (`open_work.decide_state(..., version=3)`)
puts the head of the request that opened the turn (`REQUEST_MAX_CHARS`) ahead of the closing, fitted to what is left;
a turn a hook, a finished task or a child opened has no request, and reads as v2. A v3 state never overflows Laya's
window where a v2 one would not.

The request comes from the transcript, by the fold the bridge would serve with (`open_work.fold_turn_request`):
`export-open-work` writes it to `data/agent-open-work-requests.jsonl`, keyed like the texts (the export's key and the
ledger's fitted-state key), and never into the export's own rows. `--root DIR` adds a folder of archived transcripts
(`projects/**/*.jsonl.zst`, unpacked with `zstd`): a request is only known while its transcript is.

A version is served only after it is measured, and the measurement isolates it:

```bash
uv run laya-lab export-open-work --limit 0 --root <archive>     # requests for the texts already exported
uv run laya-lab question-challenger agent_open_work --version 3 # the served rows, labels and split, rebuilt at v3
uv run laya-lab question-challenger agent_open_work --version 2 # the control: the same, refitted at v2
uv run laya-lab bench --labels merged --challenger <v3 checkpoint> --challenger <v2 checkpoint>
```

`bench` asks each challenger its own version of the same rows (`laya-fine-tune-q<version>`) and prints its verdict
against the served model: the paired ΔAUC on the bootstrap's own draws, and accuracy at the operating point. **The
rule:** a version wins when its held-out ΔAUC interval sits above zero and its golden accuracy does not fall. A win
moves `QUESTION_VERSION` and registers the fine-tune as @challenger; a loss keeps the served version, and the results
file keeps the row as a measured no.

**Measured 2026-11-13: a no, by the rule** (`bench/results/2026-11-13.json`, `laya-fine-tune-q3`). Held-out ΔAUC
against the served model +0.027 [−0.004, +0.061]; golden accuracy 0.824 → 0.853. The refit control
(`laya-fine-tune-q2`) moves nothing (−0.004 [−0.043, +0.034]), and v3 against it is +0.031 [+0.005, +0.062]: the
request carries signal, and coverage is what holds it under the bar. Only a minority of the
training and held-out rows carry a request: a turn opened by a finished task carries none. `QUESTION_VERSION` stays 2. Re-measure on a record whose turns are newer
(`question-challenger --like <checkpoint>`), where more of them carry their request.

## Question v4: the request, the turn's facts, then the closing

A closing that SAYS it is done reads as done to v2 and v3 alike. Version 4 (`open_work.decide_state(..., facts=…,
version=4)`) puts one line of what the turn actually did between the request and the closing:
`Facts: tests passed, 3 files edited, commit made`. The vocabulary is fixed (`open_work.fact_phrases`; the note above
`FACT_KEYS` owns its wording), the line is capped at `FACTS_MAX_CHARS` out of the closing's budget, and a v4 state is
never longer than `STATE_MAX_CHARS`. A turn with no facts (no transcript to read) reads as v3; one with no request, as
its facts and then its closing.

The facts come from the trace the bridge already folds for the landing record (`transcript.fold_turn_trace`), which
now also reads whether the LAST test run passed (its own tool result: `read_test_result`, unknown when the output
says neither) and whether a `git commit` came back clean. `export-open-work` writes them to
`data/agent-open-work-facts.jsonl`, keyed like the requests, booleans and one count only (`open_work_data.read_facts`).
A golden case may carry a `state.facts` object; a case with none reads as v3. The bridge threads the request and the
facts to `decide_state` already, and v2 reads neither (`bridge/tests/test_open_work.py` pins that byte for byte).
Serving v3 or v4 would also need the ledger's link (`ledger/laya_links.py`), which matches a stored state against the
v2 one.

```bash
uv run laya-lab export-open-work --limit 0 --root <archive>     # requests and facts for the texts already exported
uv run laya-lab question-challenger agent_open_work --version 4 --dry-run   # how many rows carry facts
uv run laya-lab question-challenger agent_open_work --version 4
uv run laya-lab question-challenger agent_open_work --version 2 # the control
uv run laya-lab bench --labels merged --challenger <v4 checkpoint> --challenger <v2 checkpoint>
```

The rule is v3's. One caveat bounds what v4 can learn today: most training labels came from a teacher that read the
closing ALONE, so a label never disagrees with a closing because of what was run. Facts can only teach through
operator-graded rows until rows are relabelled with the facts in view.

**Measured 2026-11-19: a no, by the rule** (`bench/results/2026-11-19.json`, `laya-fine-tune-q4`). Held-out ΔAUC
against the served model −0.002 [−0.035, +0.032], accuracy 0.771 → 0.741; golden ΔAUC +0.011 [−0.029, +0.052],
accuracy 0.770 → 0.765. The refit control (`laya-fine-tune-q2`) is −0.010 [−0.035, +0.012] held out and −0.042
[−0.067, −0.018] on golden (accuracy 0.710). AUC, v4 against the control: 0.828 against 0.821 held out, 0.868 against
0.816 on golden (no golden case carries facts yet, so there v4 reads as v3). Facts reached 289 of the 516 training
rows and 46 of the 84 record held-out rows (requests: 118 and 22); on the bench, 133 of the 170 held-out rows carry
facts and 68 a request. The texts with facts are 2,854 of 3,609. Of the 116 operator-graded texts, 35 carry facts, and
none of the 516 training rows is operator-graded, so no training label could depend on them (24 of the 84
record held-out rows are, 14 with facts).
`QUESTION_VERSION` stays 2. Re-measure once rows are relabelled with their facts in view.

## The panel: many questions, one line (`bench --panel`)

The served verdict is one question over one window of the closing message. The panel asks whether the same model
ranks turns better when the judgement is broken into parts: 7 questions of the request that opened the turn (a
question, a build, a bug, a plan, a document, a phase, more than one ask), 9 of the closing (the served question
unchanged, then: asks for a decision, asks the reader to act, a failure, a wait, finished and checked, an optional
offer, a skipped step, mainly an answer), and 2 re-asked of every window of the whole closing, so the middle of a long
message is read too. Twelve signals need no model: five read off the text, and the turn's facts (question v4's:
whether tests ran, passed or failed, files edited, a commit, a PR) as seven exact inputs, never a question. One L2
logistic line over the 30 numbers gives p(open). The questions, the windows, the signals and why the line is fitted out
of fold on the held-out rows: owned by `laya_lab/panel.py` — see the note there.

```bash
uv run laya-lab bench --labels merged --panel served --panel base
```

`laya-panel` (LAYA · 18 QUESTIONS in a legend) has the served model answer every question; `laya-panel-base` (LAYA ·
18 Q · BASE) has the base model answer the 17 new ones. Each prints its fitted weights (never written to the file) and a verdict against the served voter by the rule
above: the held-out ΔAUC interval sits above zero and golden accuracy does not fall. A panel costs every ask it makes:
its latency in the file is the sum for one turn. A store with no held-out rows has no panel, so the reproduce command
(the public set, the base model) is unchanged.

**Measured 2026-11-20: a no, by the rule** (`bench/results/2026-11-20.json`). `laya-panel`: held-out ΔAUC against the
served model −0.088 [−0.144, −0.035] (AUC 0.743 against 0.831), accuracy 0.772 → 0.690; golden ΔAUC −0.013 [−0.059,
+0.032], accuracy 0.770 → 0.745. `laya-panel-base`: −0.117 [−0.184, −0.058] held out (AUC 0.714) and −0.076 [−0.132,
−0.024] on golden. A turn costs about 750 ms against the served question's 61 ms. The second run that day, which
added the voters below, measured both again and moved neither number.

Why, from the run's own inputs. These were read after the fact on the same 171 rows, so they explain the verdict and
decide nothing; none is in the results file.
- **The new inputs rank near chance against these labels.** Alone, each request question scores AUC 0.51 to 0.56 and
  each fact 0.50 to 0.56. Re-asking over every window adds nothing to the served question (0.832 against 0.831): a
  stored closing is at most 2,000 characters, so there is little middle to read.
- **The fine-tune answers its own question whatever it is asked.** Its answer to "is it waiting on something still
  running" ranks 0.818; the base model's answer to the same question ranks 0.543.
- **On 171 rows the line pays for every input.** Fitted out of fold, the served question alone scores 0.820, the 9
  closing questions 0.801, all 30 inputs 0.743. No regularisation strength recovers it (the best is 0.755).
- **One shape did not lose:** the served verdict kept as it is, with the seven facts as corrections on top, scores
  0.843 against 0.831. That is inside the noise, and it was found by looking.

The limit is question v4's: most labels came from a teacher that read the closing alone, so a request or a fact cannot
predict a label it was never part of. Re-measure once rows are relabelled with both in view, and there are more of them.

### Three questions and no line (`bench --panel served/3q`)

The 18 questions lost to their own fitted line, so the second shape fits nothing: three questions of the closing,
equal weights. The served question; whether the closing asks the reader to decide or approve (a yes says open); whether
it says the work was finished and checked (a yes says done). A no to either of the last two is "cannot tell", never the
opposite verdict. `3q` answers with the most confident of the three votes, `3q-mean` with the equal-weight mean of
their logits. Nothing is fitted, so it scores a store with no held-out rows too. The votes and both rules: owned by
`laya_lab/panel.py` — see the note there.

```bash
uv run laya-lab bench --labels merged --panel served/3q --panel served/3q-mean
```

`laya-panel-3q` (LAYA · 3 QUESTIONS) and `laya-panel-3q-mean` (LAYA · 3 Q · MEAN); `base/3q` has the base model answer
the two new questions. Each prints, per set, what each vote was worth alone, and a verdict by the same rule.

**Measured 2026-11-20: a no, by the rule, and level with the served question** (`bench/results/2026-11-20.json`).
`laya-panel-3q`: held-out ΔAUC against the served model +0.015 [−0.012, +0.046] (AUC 0.846 against 0.831), accuracy
0.772 → 0.784; golden ΔAUC −0.025 [−0.065, +0.010], accuracy 0.770 → 0.750. `laya-panel-3q-mean`: +0.016 [−0.011,
+0.047] held out (AUC 0.847) and −0.008 [−0.036, +0.020] on golden, accuracy 0.770 → 0.745. The rule asks for a held-out
interval above zero and a golden accuracy that holds; neither rule clears either half. A turn costs 188 ms against 61 ms.

Why, from the votes the run printed (read on the same rows, so they explain and decide nothing):
- **The served question is the most confident vote on 92% of the held-out turns**, so on those the panel is the served
  verdict. The two rules differ by 0.001.
- **"Asks for a decision" ranks at chance alone** (AUC 0.47 held out, 0.49 golden), and says yes on 15% of the held-out
  turns. The fine-tune answers its own question whatever it is asked, as it did above.
- **"Finished and checked" carries a little** (0.61 held out, 0.57 golden): the one new vote that ranks above chance.

So three questions cost a quarter of what eighteen do and lose nothing past the interval, and they do not win. The
limit is the one above: one model trained on one question has one opinion.

## Tests

`cd lab && uv run pytest -q` (a few seconds). Every tree is synthetic, every text invented, and the suite
points the data and config roles at temp folders before any test runs, so no test resolves the operator's
store or takes their lease.
