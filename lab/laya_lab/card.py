"""`laya-lab card`: the verifier card, count-only, previewed in the store and written into this checkout's `docs/`.

    laya-lab card                              # build the preview, print {pairs, ready, digest, waiting}
    laya-lab card --publish --digest <digest>  # write the previewed files into docs/, print {"written": [...]}

THE PREVIEW is `<data>/laya/verifier-card.json`: `{v, repository, at, pairs, ready, digest, files}`. Its reader, and
the bounds a preview must keep to be shown: owned by `plugins/laya/laya_bridge/card_files.py` — see the note there.
`files` holds four texts. Three are the card (`WRITTEN`); the fourth is the public golden suite as it stood when the
numbers were measured, so the digest binds the suite the numbers were read against. It is never written anywhere:
the suite already lives in this checkout under `lab/golden/`.

ONE PROTOCOL. Every model number the card prints comes from the newest `bench/results/<date>.json` in this checkout,
`bench.py`'s one protocol: the headline (`HEADLINE_KINDS`, held-out and golden) in `VERIFIER.md`, and the base model's
golden row in `REPRODUCE.md` as what its command must print, since that command IS the bench's (`bench.REPRODUCE`) run
on an empty store. A results file counts only while its golden set is the public suite as it stands (`bench_numbers`
compares the protocol id); an older one leaves the card unready rather than quoting a suite nobody can rerun. A figure
measured under another protocol is not a second headline: `EARLIER` is the card's one footnote, so whoever meets it in
an older document finds it explained here, beside the number that replaced it.

THE GATE. `--publish` writes only a card the operator has seen: the card is regenerated at the preview's own
timestamp, and it must equal the stored preview, carry the digest handed in and be `ready` (at least `READY_PAIRS`
operator/teacher pairs with a κ, and bench results of the current public suite). A new grade, a suite edit, a new
results file or another served model changes the regenerated card, and the write is refused (exit 3, nothing written)
until the preview is rebuilt and reviewed again.

WHAT LEAVES THE STORE. Counts, rates and intervals; the public golden suite; the fixed prose below. Never a state, a
key, a decision or a private suite. The retrain cycle's numbers reach `MODEL_CARD.md` through an allowlist of scalar
journal fields (`cycle_numbers`), never by attaching a file from the store.

This command opens no pull request, clones nothing and runs no other program: whether the written files are committed
is the operator's next step, by hand.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from laya_lab import siblings  # noqa: F401  (puts laya_bridge on sys.path)
from laya_lab import bench, registry
from laya_lab import metrics as scoring
from laya_lab.data import atomic_write_text
from laya_lab.golden import SUITES
from laya_bridge import agreement, card_files, files, lab_files, paths, review_data, review_log, rows
from laya_bridge.ops import latency_row, shadow_summary

REFUSED = 3
FILENAME = "verifier-card.json"
SURFACE = "agent_open_work"
SUITE = "agent-open-work"
#: The bench run on an empty store: the base model alone answers, on the public suite alone (module note: ONE PROTOCOL).
COMMAND = bench.REPRODUCE.format(suite=SUITE)
READY_PAIRS = 20
#: A dated results file; `fixture.json` beside them holds invented numbers and is never read here.
RESULTS_NAME = re.compile(r"\d{4}-\d{2}-\d{2}\.json")
RESULTS_MAX_BYTES = 4 * 1024 * 1024
#: The headline's columns: one voter of each kind, the served fine-tune first. A challenger is measured, not served,
#: so it stays in the results file and off the card.
HEADLINE_KINDS = (("served", "Served fine-tune"), ("base", "Base model"), ("floor", "TF-IDF floor"))
SET_TITLES = {"heldout": "Held-out: operator-graded turn ends no fine-tune trained on", "golden": "Public golden suite"}
#: The served fine-tune under an earlier protocol, before `bench.py` existed: question v1, on the 84-row evaluation
#: split of its own training record. Kept as the card's one footnote (module note: ONE PROTOCOL).
EARLIER = {"auc": 0.879, "question_version": 1, "rows": 84, "measured": "2026-10-29"}
#: Why the base model's public number is not a clean held-out one: owned by lab/README.md — see "Golden suites".
WORDING_CAVEAT = (
    "The question's wording was chosen on the first 34 cases of the public suite, so their numbers flatter the base model."
)

VERIFIER, REPRODUCE, MODEL_CARD = "VERIFIER.md", "REPRODUCE.md", "MODEL_CARD.md"
GOLDEN = f"golden/{SUITE}.jsonl"
#: The preview's files that `--publish` writes, each to `docs/<name>` (the module note owns why GOLDEN is not one).
WRITTEN = (VERIFIER, REPRODUCE, MODEL_CARD)
DOCS_DIRNAME = "docs"

#: The retrain journal fields a model card may carry: names, states and numbers, each checked by `is_plain`.
CYCLE_FIELDS = ("checkpoint", "surface", "question_version", "device", "id", "started_at", "model_digest", "dataset_version")
CYCLE_STEP_FIELDS = {
    "label": ("teacher", "labelled"),
    "build": (
        "at",
        "unique",
        "open",
        "done",
        "rows",
        "copies",
        "train_open",
        "train_done",
        "operator",
        "reserved",
        "curated",
        "golden",
    ),
    "train": ("base_revision", "epochs", "train_rows", "held_out_rows"),
    "gate": ("report", "gate"),
    "register": ("state",),
    "sweep": ("state", "best", "done_best", "rows", "reserve", "kappa"),
}
CYCLE_METRICS = ("loss", "accuracy", "brier", "ece")
CYCLE_CHECKS = ("heldout", "warning_cost", "done_cost", "private_golden", "floor")
CYCLE_CHECK_STATES = ("pass", "fail", "skipped", "unmeasured", "pending")
#: The sections of the cycle's own card (`retrain.model_card`) that the public card repeats: numbers only.
CYCLE_SECTIONS = ("Training data", "Metrics")


class Held(ValueError):
    """The card is not the one the operator reviewed, or is not ready: nothing is written."""


def number(value: Any, digits: int = 3) -> str:
    return "unmeasured" if not isinstance(value, (int, float)) or isinstance(value, bool) else f"{value:.{digits}f}"


def rate(block: dict[str, Any]) -> str:
    return f"{number(block['value'])} [95% CI {number(block['lo'])}, {number(block['hi'])}], n={block['n']}"


def metrics(base: Path) -> dict[str, Any]:
    """The card's measurements, from the operator's grades and the decision ledger: numbers only."""
    snap = review_data.snapshot(base, SURFACE)
    stats = review_data.stats(snap, base)
    pairs: dict[str, list[tuple[bool, bool]]] = {name: [] for name in review_data.LANDING_SPLIT}
    for key, ids in snap.items.decisions_of.items():
        answer = snap.fold.labels.get(key)
        if answer is None or not review_data.is_live_member(snap.spec, key) or not ids:
            continue
        verdict = snap.items.verdicts[ids[0]]
        pairs[verdict.landing_class].append((verdict.flagged, answer["open"]))
    live = {
        name: {metric: agreement.rate_summary(values, metric) for metric in ("precision", "recall")}
        for name, values in pairs.items()
    }
    shadow, _code = lab_files.read_shadow(base, SURFACE)
    target = registry.alias_target(base, SURFACE, "challenger")
    return {
        "kappa": stats["operator_vs_sonnet"],
        "live": live,
        "felt": scoring.felt(scoring.felt_turns(snap)),
        "latency": latency_row(base, snap.spec),
        "shadow": shadow_summary(shadow, target[0]) if target else None,
        "cut": registry.served_threshold(base, SURFACE, registry.served_model(base, SURFACE))[0],
    }


def provenance(public: list[dict[str, Any]], labels: dict[str, Any], skipped: Any = frozenset()) -> str:
    """Where the public suite's cases came from, and what the operator's sample sign-off promises (lab_files.BATCH_TAG
    owns the rule): counts and the bound only, never a case."""
    batches = lab_files.batch_standing(public, labels, skipped)
    batched = sum(standing["size"] for standing in batches.values())
    parts = [f"{len(public) - batched} hand-written"]
    for batch, standing in batches.items():
        sampled = len(standing["sample"])
        if standing["state"] == "accepted":
            parts.append(
                f"{standing['size']} agent-written ({batch}), accepted on a stratified random sample of {sampled} "
                f"that the operator graded, all {sampled} agreed: at {lab_files.BATCH_CONFIDENCE:.0%} confidence at "
                f"most {standing['bound']} of them carry a wrong label"
            )
        else:
            parts.append(
                f"{standing['size']} agent-written ({batch}), not yet signed off ({standing['agreed']}/{sampled} "
                f"of its sample agreed{', a sampled case disputed' if standing['state'] == 'rejected' else ''}): "
                "they count only once agreed"
            )
    return "Every case is invented, none from a transcript: " + "; ".join(parts) + "."


# ── the bench's numbers (module note: ONE PROTOCOL) ───────────────────────────


def results_dir() -> Path:
    """Where `bench.py` writes its files: this checkout's `bench/results/`."""
    return paths.REPO_ROOT.joinpath(*bench.RESULTS_DIR)


def _num(value: Any) -> float:
    if not files.is_number(value):
        raise ValueError("not a number")
    return float(value)


def _count(value: Any) -> int:
    if not files.is_count(value):
        raise ValueError("not a count")
    return int(value)


def _estimate(block: Any) -> dict[str, float] | None:
    """`{value, lo, hi}`, or None where the file has none (an AUC of a one-class set)."""
    return None if block is None else {key: _num(block[key]) for key in ("value", "lo", "hi")}


def _name(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", value):
        raise ValueError("not a plain name")
    return value


def parse_results(payload: Any) -> dict[str, Any]:
    """What the card prints of one results file, each field checked: names, counts and estimates of the headline's
    voters (`HEADLINE_KINDS`) on the sets it titles (`SET_TITLES`). Any other shape raises ValueError (or the
    KeyError / TypeError of a missing field)."""
    if not isinstance(payload, dict) or payload.get("v") != bench.RESULTS_VERSION:
        raise ValueError("not a results file of this version")
    protocol, interval = payload["protocol"], payload["protocol"]["interval"]
    roles: dict[str, str] = {}
    runtime = None
    for voter in payload["voters"]:
        # On an empty store the base model is the one served, and it still belongs in the base column.
        kind = voter.get("kind")
        role = "served" if kind == "fine_tune" and voter.get("served") is True else kind
        if role in dict(HEADLINE_KINDS) and role not in roles:
            roles[role] = _name(voter["name"])
            if role == "base":
                runtime = voter.get("runtime") if voter.get("runtime") in bench.RUNTIMES else None
    sets: dict[str, Any] = {}
    for block in payload["sets"]:
        if block["set"] not in SET_TITLES:
            continue
        by_voter = {entry["voter"]: entry for entry in block["scores"]}
        scores: dict[str, Any] = {}
        for role, name in roles.items():
            entry = by_voter.get(name)
            if entry is None:
                continue
            at = entry["operating"]
            if at["source"] not in ("live", "swept"):
                raise ValueError("an operating point of no known source")
            scores[role] = {
                "voter": name,
                "auc": _estimate(entry["auc"]),
                "brier": _num(entry["brier"]["value"]),
                "ece": _num(entry["ece"]["value"]),
                "cut": _num(at["threshold"]),
                "source": at["source"],
                "false": _count(at["false_warnings"]),
                "missed": _count(at["missed_open"]),
                "cost": _num(at["cost"]["value"]),
            }
        sets[block["set"]] = {"open": _count(block["n"]["open"]), "done": _count(block["n"]["done"]), "scores": scores}
    return {
        "protocol": _name(protocol["id"]),
        "seed": _count(interval["seed"]),
        "resamples": _count(interval["resamples"]),
        "false_warning": _num(protocol["costs"]["false_warning"]),
        "runtime": runtime,
        "sets": sets,
    }


def bench_numbers() -> dict[str, Any]:
    """The newest dated results file, parsed (`parse_results`), with `file`, the public suite's own golden protocol
    (`golden_protocol`) and `current`. Not current, with a `reason`, when there is no file, it cannot be read, or its
    golden set is not the public suite in this checkout."""
    suite = SUITES[SUITE]
    expected = bench.protocol_id(suite, {"golden": bench.golden_rows(SUITE, suite)})
    folder = results_dir()
    dated = sorted(path for path in folder.iterdir() if RESULTS_NAME.fullmatch(path.name)) if folder.is_dir() else []
    if not dated:
        return {"current": False, "golden_protocol": expected, "reason": "no bench results: run `laya-lab bench`"}
    file = "/".join((*bench.RESULTS_DIR, dated[-1].name))
    try:
        raw = files.read_capped(dated[-1], RESULTS_MAX_BYTES)
        found = parse_results(files.loads(raw) if raw else None)
    except (files.SourceError, ValueError, KeyError, TypeError):
        return {"current": False, "golden_protocol": expected, "file": file, "reason": f"{file} cannot be read"}
    found.update(
        file=file,
        golden_protocol=expected,
        current=found["protocol"] == expected or found["protocol"].startswith(f"{expected}.h"),
    )
    if not found["current"]:
        found["reason"] = f"{file} scored another golden set than lab/{GOLDEN} holds now: run `laya-lab bench`"
    return found


def auc_cell(score: dict[str, Any] | None) -> str:
    if score is None:
        return "—"
    ranked = score["auc"]
    return "unmeasured" if ranked is None else f"{ranked['value']:.3f} [{ranked['lo']:.3f}, {ranked['hi']:.3f}]"


def headline(found: dict[str, Any]) -> list[str]:
    """`VERIFIER.md`'s headline: one row per set, one AUC column per `HEADLINE_KINDS`, and the earlier protocol's
    footnote mark."""
    if not found["current"]:
        return ["## The headline", "", f"Not measured: {found['reason']}.[^earlier]", ""]
    lines = [
        "## The headline",
        "",
        "Area under the ROC curve, with a bootstrap 95% interval. Every voter answered the same rows: "
        f"`{found['file']}`, protocol `{found['protocol']}`.[^earlier]",
        "",
        "| Set | Rows | " + " | ".join(title for _role, title in HEADLINE_KINDS) + " |",
        "|---|---|" + "---|" * len(HEADLINE_KINDS),
    ]
    for name, title in SET_TITLES.items():
        block = found["sets"].get(name)
        if block is not None:
            cells = " | ".join(auc_cell(block["scores"].get(role)) for role, _title in HEADLINE_KINDS)
            lines.append(f"| {title} | {block['open'] + block['done']} ({block['open']} open / {block['done']} done) | {cells} |")
    return lines + [""]


def earlier_footnote() -> str:
    return (
        f"[^earlier]: An earlier protocol, from before this bench, put the fine-tune served at the time at AUC "
        f"{EARLIER['auc']:.3f}: question v{EARLIER['question_version']}, on the {EARLIER['rows']}-row evaluation "
        f"split of its own training record, measured {EARLIER['measured']}. It asked another question of other "
        "rows, so it does not compare with the table above. The table is the number to quote."
    )


# ── MODEL_CARD.md ─────────────────────────────────────────────────────────────


def is_plain(value: Any) -> bool:
    """A journal value a public card may print: nothing, a number, a flag, or a short name with no path and no prose."""
    if value is None or isinstance(value, (int, float, bool)):
        return True
    return isinstance(value, str) and len(value) <= 128 and all(c.isalnum() or c in "-_.:@+" for c in value)


def cycle_numbers(base: Path) -> dict[str, Any] | None:
    """The last closed retrain cycle as `retrain.model_card` reads it, cut down to `CYCLE_FIELDS` and
    `CYCLE_STEP_FIELDS`. None when no cycle closed, the cycle never wrote its own card beside the weights, or ANY
    kept field is not plain: one sentence in a journal field drops the whole cycle rather than the one field. The
    cycle's card file is only looked for, never read: what it says could have been edited by hand."""
    journal = lab_files.read_retrain_journal(base, SUITE)
    if journal is None or journal.get("state") != "done":
        return None
    name = journal.get("checkpoint")
    if not isinstance(name, str) or not paths.MODEL_NAME.fullmatch(name):
        return None
    own = base / paths.CHECKPOINTS_DIRNAME / name / lab_files.MODEL_CARD_FILENAME
    if not own.is_file() or own.is_symlink():
        return None
    clean: dict[str, Any] = {key: journal.get(key) for key in CYCLE_FIELDS}
    if not all(is_plain(value) for value in clean.values()):
        return None
    steps = journal.get("steps") if isinstance(journal.get("steps"), dict) else {}
    clean["steps"] = {}
    for step, keys in CYCLE_STEP_FIELDS.items():
        source = steps.get(step) if isinstance(steps.get(step), dict) else {}
        kept: dict[str, Any] = {key: source.get(key) for key in keys}
        if not all(is_plain(value) for value in kept.values()):
            return None
        if step == "train":
            for side in ("before", "after"):
                block = source.get(side) if isinstance(source.get(side), dict) else {}
                kept[side] = {
                    key: value
                    for key, value in block.items()
                    if key in CYCLE_METRICS and isinstance(value, (int, float)) and not isinstance(value, bool)
                }
        if step == "gate":
            checks = source.get("checks") if isinstance(source.get("checks"), dict) else {}
            kept["checks"] = {key: value for key, value in checks.items() if key in CYCLE_CHECKS and value in CYCLE_CHECK_STATES}
        clean["steps"][step] = kept
    return clean


def cycle_sections(clean: dict[str, Any]) -> list[str]:
    """`CYCLE_SECTIONS` of the cycle's own card, one heading level down. That card names a checkpoint folder and a
    report file in its other sections; the public card carries neither."""
    from laya_lab import retrain

    out: list[str] = []
    keep = False
    for line in retrain.model_card(clean).splitlines():
        if line.startswith("## "):
            keep = line[3:].strip() in CYCLE_SECTIONS
            if keep:
                out += ["", f"### {line[3:].strip()}"]
        elif keep and line.strip():
            out.append(line)
    return out


def model_card(base: Path) -> str:
    """`MODEL_CARD.md`: what the model is, in fixed prose, then the last closed retrain cycle's numbers when
    `cycle_numbers` has them."""
    lines = [
        "# Model card · Laya on the agent rack",
        "",
        "## Model details",
        "- Base model: [`convaiinnovations/laya`](https://huggingface.co/convaiinnovations/laya), by Convai Innovations, "
        "Apache-2.0. A ModernBERT-large encoder (`answerdotai/ModernBERT-large`, Apache-2.0) with a decision head: "
        "421M parameters. It is not a text generator: it reads a state and a question and returns a probability.",
        "- tldrack downloads the base model from Hugging Face on install and runs it on the operator's machine. "
        "It works with no fine-tune at all; that is the first run.",
        f"- The question it is asked on the `{SURFACE}` surface, and the cuts the rack draws ⚠️ and ✅ at: "
        "`bridge/tldrack/open_work.py`.",
        "",
        "## The fine-tune",
        "- `laya-lab` fine-tunes the base model on one machine, for one operator. The training rows are closing "
        "messages of coding-agent turns from that operator's own sessions, each labelled open or done: most labels "
        "come from a teacher model that read the message alone, and the operator's own grades override the teacher "
        "wherever they exist.",
        "- Half of the operator's grades are held in reserve and never trained on: the rows a cut or a promotion "
        "is measured against.",
        "- **No trained weights ship with tldrack.** A fine-tune, its training rows, the operator's grades and the "
        "decision ledger stay in the operator's data directory. This repository holds the code that trains one, the "
        "public golden suite, and aggregate numbers.",
        "",
        "## Intended use",
        "- Reads the closing message of a coding agent's turn and answers one yes/no question: does this message say "
        "work is still open. The agent rack draws ⚠️ or ✅ from the probability at two cuts.",
        "- It helps a person decide where to look first. It is not a reason to close, kill or auto-answer a session.",
        "- Out of scope: any text but an English turn-ending message, and any decision with no person in the loop.",
        "",
        "## Limits",
        "- It judges the message, not the work: a turn that says it is done and is not reads as done.",
        "- Most training labels are a teacher model's, so the fine-tune inherits the teacher's reading wherever the "
        "operator has not graded.",
        "- A fine-tune is fitted to one operator's agents, projects and habits of phrasing. Its numbers do not carry "
        "to another operator; the base model's numbers on the public suite do.",
        "- The held-out sets are small. Read every number with its row count and its interval.",
        "- A message past the model's window is judged from its head and tail, not in full.",
        f"- The public golden suite is small and invented (`VERIFIER.md` says how it was made). {WORDING_CAVEAT}",
    ]
    clean = cycle_numbers(base)
    if clean is not None:
        sections = cycle_sections(clean)
        if sections:
            lines += [
                "",
                "## The last retrain cycle on the measuring machine",
                "Counts and scores of one cycle, on one operator's rows. They are not reproducible from this repository.",
                *sections,
            ]
    return "\n".join(lines + [""])


# ── the card ──────────────────────────────────────────────────────────────────


def verifier(measured: dict[str, Any], cases: int, at: str, made: str = "", found: dict[str, Any] | None = None) -> str:
    k = measured["kappa"]
    ci = k.get("kappa_ci") or {}
    found = found if found is not None else {"current": False, "reason": "no bench results: run `laya-lab bench`"}
    lines = [
        "# Laya content verifier",
        "",
        "Laya reads the content of an agent turn to help a person judge whether work remains. Process-state "
        "multiplexers report whether a process is running; this card measures the content judgement. It does not "
        "certify task completion.",
        "",
        f"Measured {at}; public golden suite: {cases} cases; live warning cut: {number(measured['cut'])}.",
        f"Operator/Sonnet agreement on the uniform sample: κ {number(k['kappa'])} ± {number(ci.get('half_width'))} "
        f"(95% CI {number(ci.get('lo'))}, {number(ci.get('hi'))}; n={k['n']}).",
        "",
        *headline(found),
        "## Live sample, at each recorded cut",
        "One newest decision per uniformly sampled graded turn; precision and recall are reported separately for each "
        "landing class. Small denominators remain unmeasured.",
        "",
    ]
    for name, block in measured["live"].items():
        lines.append(f"- {name}: precision {rate(block['precision'])}; recall {rate(block['recall'])}.")
    false = measured["felt"]["false_warning"]
    latency = measured["latency"] or {}
    shadow = measured["shadow"] or {}
    lines += [
        "",
        f"False shown warnings per 100 graded sampled turn ends: {number(false['value'], 1)} "
        f"(95% CI {number(false['lo'], 1)}, {number(false['hi'], 1)}; false={false['count']}, n={false['n']}). "
        "This is a sample estimate, not a census.",
        f"Latency: p50 {number(latency.get('p50_ms'), 1)} ms; p90 {number(latency.get('p90_ms'), 1)} ms; "
        f"n={latency.get('decisions', 0)}; measured {latency.get('at') or 'never'}.",
        f"Challenger shadow agreement: {number(shadow.get('agree'))}; n={shadow.get('rows', 0)} across "
        f"{shadow.get('days', 0)} days.",
        "",
        "## Reproduce the public golden suite",
        f"The suite is `lab/{GOLDEN}` in this repository. The command below is the bench on an empty data directory: "
        "only the base model can answer there, and its `golden` row must match the base model's golden AUC above "
        "(`REPRODUCE.md` has every number it prints). Fine-tuned weights and private golden cases are not published. "
        f"{made} {WORDING_CAVEAT}".replace("  ", " "),
        "",
        "```sh",
        COMMAND,
        "```",
        "",
        "The operator and live measurements above come from private local grades and cannot be reproduced from the "
        "public suite. No private message and no private suite is in this repository. Every voter, challengers "
        "included: `bench/results/`. What the model is and is not for: `MODEL_CARD.md`.",
        "",
        earlier_footnote(),
        "",
    ]
    return "\n".join(lines)


def reproduce(found: dict[str, Any] | None = None) -> str:
    """`REPRODUCE.md`: the command, and what it must print, from the published run's base-model golden row."""
    found = found if found is not None else {"current": False, "reason": "no bench results: run `laya-lab bench`"}
    golden = found["sets"].get("golden") if found["current"] else None
    base = golden["scores"].get("base") if golden else None
    lines = [
        "# Reproduce the public golden numbers",
        "",
        f"The public suite is `lab/{GOLDEN}`"
        + (f": {golden['open'] + golden['done']} cases, {golden['open']} open and {golden['done']} done. " if golden else ". ")
        + "The command scores it with the base model, `convaiinnovations/laya`, which the first run downloads from "
        "Hugging Face. Its data directory is a new empty one, so nothing of yours is read, and the base model is the "
        "only voter that can answer.",
        "",
        "Run it from the repository root. `uv run` installs the lab's dependencies into `lab/.venv` first.",
        "",
        "```sh",
        COMMAND,
        "```",
        "",
        "## What it prints",
        "",
    ]
    if base is None:
        reason = found["reason"] if not found["current"] else "the published run has no base-model golden row"
        return "\n".join(lines + [f"No published run to compare with yet: {reason}.", ""])
    lines += [
        f"A `golden` block with one row, `{base['voter']}`. The published run, `{found['file']}` (golden protocol "
        f"`{found['golden_protocol']}`), printed:",
        "",
        "| AUC [95% CI] | Brier | ECE | Cut | False ⚠️ | Missed open | Cost |",
        "|---|---|---|---|---|---|---|",
        f"| {auc_cell(base)} | {base['brier']:.3f} | {base['ece']:.3f} | {base['cut']:.2f} | {base['false']} | "
        f"{base['missed']} | {base['cost']:.1f} |",
        "",
        f"Cost is {found['false_warning']:g} × false ⚠️ + missed open, at the cut the model serves with. The interval is "
        f"a percentile bootstrap with a fixed seed ({found['resamples']:,} resamples, seed {found['seed']}), so the "
        "same probabilities give the same interval"
        + (
            f". The published run used `{found['runtime']}`, the default device; `--device cpu` can move the last digit."
            if found["runtime"]
            else "."
        ),
        "",
        "The run also writes `bench/results/<today>.json` into the checkout. `git status bench/` shows it: delete it, "
        "or `git restore` it where it replaced a published day.",
        "",
    ]
    return "\n".join(lines)


def build(base: Path, *, at: str | None = None) -> tuple[dict[str, Any], list[str]]:
    """The preview (the four texts, their digest, and whether the card may be written), and what it waits on."""
    measured = metrics(base)
    found = bench_numbers()
    public = (paths.PUBLIC_GOLDEN_DIR / f"{SUITE}.jsonl").read_text(encoding="utf-8")
    cases = sum(1 for line in public.splitlines() if line.strip() and not line.lstrip().startswith("#"))
    k = measured["kappa"]
    at = at or rows.utc_stamp()
    snap_fold = review_log.read_fold(base, SURFACE)[1]
    made = provenance(
        lab_files.read_golden_suite(paths.PUBLIC_GOLDEN_DIR / f"{SUITE}.jsonl")[0], snap_fold.labels, snap_fold.skipped
    )
    outgoing = {
        VERIFIER: verifier(measured, cases, at, made, found),
        GOLDEN: public,
        REPRODUCE: reproduce(found),
        MODEL_CARD: model_card(base),
    }
    digest = hashlib.sha256(json.dumps(outgoing, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    waiting = []
    if k["n"] < READY_PAIRS or k["kappa"] is None:
        waiting.append(f"fewer than {READY_PAIRS} measurable κ pairs ({k['n']})")
    if not found["current"]:
        waiting.append(found["reason"])
    return (
        {
            "v": 1,
            "repository": card_files.configured_repo(),
            "at": at,
            "pairs": k["n"],
            "ready": not waiting,
            "digest": digest,
            "files": outgoing,
        },
        waiting,
    )


def generate(base: Path, *, at: str | None = None) -> dict[str, Any]:
    """The preview alone (`build`)."""
    return build(base, at=at)[0]


def save(base: Path) -> tuple[dict[str, Any], list[str]]:
    """Build the preview and store it where the REVIEW page and `--publish` read it; `(card, what it waits on)`."""
    card, waiting = build(base)
    atomic_write_text(base / FILENAME, json.dumps(card, indent=2, ensure_ascii=False) + "\n")
    return card, waiting


def publish(base: Path, digest: str, *, root: Path | None = None) -> list[str]:
    """Write the reviewed card's `WRITTEN` files into `<root>/docs/` (default: this checkout) and return their paths
    from the root. Raises `Held`, having written nothing, when the gate refuses (the module note owns the gate)."""
    try:
        stored = json.loads((base / FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Held("there is no readable preview: run `laya-lab card` and review it") from None
    if not isinstance(stored, dict):
        raise Held("the preview is not a card: run `laya-lab card` and review it")
    card, waiting = build(base, at=stored.get("at") if isinstance(stored.get("at"), str) else None)
    if stored != card:
        raise Held("the card changed since it was previewed: run `laya-lab card` and review it again")
    if digest != card["digest"]:
        raise Held("that digest is not the previewed card's")
    if not card["ready"]:
        raise Held("the card is not ready: " + "; ".join(waiting))
    root = (root if root is not None else paths.REPO_ROOT).resolve()
    docs = root / DOCS_DIRNAME
    docs.mkdir(exist_ok=True)
    if docs.resolve().parent != root:
        raise Held(f"{DOCS_DIRNAME}/ leads out of the checkout")
    staged = []
    try:
        for name in WRITTEN:
            tmp = docs / f".{name}.tmp"
            tmp.write_text(card["files"][name], encoding="utf-8")
            staged.append((tmp, docs / name))
        for tmp, target in staged:
            os.replace(tmp, target)
    finally:
        for tmp, _target in staged:
            tmp.unlink(missing_ok=True)
    return [f"{DOCS_DIRNAME}/{name}" for name in WRITTEN]


def cmd(args: Any, base: Path, *, root: Path | None = None) -> int:
    """One JSON line on stdout: the preview's `{pairs, ready, digest, waiting}` (`waiting`: why it is not ready, empty
    when it is), or `{"written": [...]}` after `--publish`. A refused publish prints its reason on stderr and exits
    REFUSED with nothing on stdout."""
    if not args.publish:
        card, waiting = save(base)
        print(
            json.dumps(
                {"pairs": card["pairs"], "ready": card["ready"], "digest": card["digest"], "waiting": waiting}, ensure_ascii=False
            )
        )
        return 0
    try:
        written = publish(base, args.digest, root=root)
    except Held as held:
        print(f"[laya-lab] card held: {held}", file=sys.stderr)
        return REFUSED
    print(json.dumps({"written": written}))
    return 0
