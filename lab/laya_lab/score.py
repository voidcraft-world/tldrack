"""`laya-lab review-open-work`: the served model's p on every text the REVIEW deck can show.

    laya-lab review-open-work                  # the SERVED model (what AGENT RACK › LAYA › SCORE runs)
    laya-lab review-open-work --model english
    laya-lab review-open-work --full           # score every key again, not only the unscored ones

Scores every Sonnet-labelled text and every text the ledger kept, one `predict` per text with the
served question (`open_work.OPEN_WORK_QUESTION`), and rewrites
`data/agent-open-work-laya-p.jsonl` whole and atomically (0600), one `lab_files.scores_row` per
key. That file feeds the deck's `disagree` and `uncertain` strata and κ-vs-Laya; the deck works without
it, and ignores rows another checkpoint scored (`review_data.py` owns the strata).

Each Sonnet key's row also carries its `teacher` pair, from `teacher_confidence`: a text-only surrogate
(TF-IDF over words and word pairs, then L2 logistic regression), trained five times, each time without
one fold, and asked only about the fold it never saw. What it gives Sonnet's own label is that label's
self-confidence, which the deck's `suspect` stratum ranks (`review_data.suspect_ranked`): cleanlab's
confident-learning idea with no new dependency, in a few seconds of pure Python on the CPU.

INCREMENTAL: a row is keyed by (checkpoint digest, device, key) plus the model, checkpoint and question version
(`reusable`); a key with such a row keeps its p and only the unscored keys are predicted. A row's `threshold` is the
cut at the time of writing, so a moved cut updates every row without a prediction. `--full` re-scores everything.

Exit codes are the bridge's contract (`laya_bridge/lab_jobs.py` runs this): 0 written · 1 an error.
"""

from __future__ import annotations

import math
import random
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from laya_lab import siblings  # noqa: F401  (puts laya_bridge on sys.path)
from laya_lab import infer
from laya_lab.data import write_jsonl
from laya_lab.golden import SUITES
from laya_lab.registry import resolve_model, served_threshold
from laya_lab.open_work_data import require_texts
from laya_bridge import lab_files, paths, review_data, rows
from laya_bridge.review_surfaces import REVIEW_SURFACES

SURFACE = "agent_open_work"
#: The surrogate: folds, the vocabulary's floor (a term in fewer texts carries no signal across folds), and SGD's
#: passes and step. Fixed, so a rerun on the same labels writes the same confidences.
FOLDS = 5
MIN_DF = 2
EPOCHS = 20
ETA0 = 0.5
#: Fewer labels than this per class and the surrogate is noise: no teacher pair is written at all.
MIN_PER_CLASS = FOLDS
_TOKEN = re.compile(r"[a-z0-9]+")


def keys_to_score(base: Path, surface: str) -> dict[str, str]:
    """`{key: text}` for every Sonnet-labelled key and every ledger key that has text."""
    spec = REVIEW_SURFACES[surface]
    found = review_data.texts(base, surface)
    _code, sonnet = review_data.sonnet_labels(base, spec)
    wanted = sonnet.keys() | review_data.ledger_keys(base, surface)
    return {key: found[key] for key in sorted(wanted) if key in found}


def _terms(text: str) -> list[str]:
    words = _TOKEN.findall(text.lower())
    return words + [f"{a} {b}" for a, b in zip(words, words[1:])]


def _vectors(texts: list[str]) -> list[list[tuple[int, float]]]:
    """Sublinear TF × smoothed IDF over terms in at least `MIN_DF` texts, each row L2-normalised. Fitted on every
    labelled text: IDF reads no label, so no fold sees another's answer through it."""
    counts = [Counter(_terms(text)) for text in texts]
    df = Counter(term for count in counts for term in count)
    vocab = {term: i for i, term in enumerate(sorted(term for term, n in df.items() if n >= MIN_DF))}
    n = len(texts)
    idf = {term: math.log((1 + n) / (1 + df[term])) + 1.0 for term in vocab}
    out = []
    for count in counts:
        row = [(vocab[term], (1.0 + math.log(tf)) * idf[term]) for term, tf in count.items() if term in vocab]
        norm = math.sqrt(sum(v * v for _i, v in row)) or 1.0
        out.append(sorted((i, v / norm) for i, v in row))
    return out


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z)) if z >= 0 else math.exp(z) / (1.0 + math.exp(z))


def _fit(xs: list[list[tuple[int, float]]], ys: list[bool], seed: str) -> tuple[dict[int, float], float]:
    """L2 logistic regression by SGD (Bottou's step, `l2` = 1/n: C = 1). The weights are kept as `scale × w` so the
    decay each step costs one multiply, not a pass over the vocabulary."""
    l2 = 1.0 / len(xs)
    w: dict[int, float] = {}
    bias, scale, step = 0.0, 1.0, 0
    order = list(range(len(xs)))
    for epoch in range(EPOCHS):
        random.Random(f"{seed}:{epoch}").shuffle(order)
        for i in order:
            step += 1
            eta = ETA0 / (1.0 + l2 * ETA0 * step)
            x = xs[i]
            g = _sigmoid(scale * sum(w.get(j, 0.0) * v for j, v in x) + bias) - (1.0 if ys[i] else 0.0)
            scale *= 1.0 - eta * l2
            for j, v in x:
                w[j] = w.get(j, 0.0) - eta * g * v / scale
            bias -= eta * g
            if scale < 1e-6:  # fold the scale back in before the floats lose their digits
                w = {j: value * scale for j, value in w.items()}
                scale = 1.0
    return {j: value * scale for j, value in w.items()}, bias


def teacher_confidence(texts: Mapping[str, str], labels: Mapping[str, bool]) -> dict[str, float]:
    """`{key: P(its own label)}` for every labelled key with text, each from the fold that never trained on it.

    Folds are `bucket(key, 'suspect-fold') % FOLDS`, so membership never moves between runs. {} when a class has
    fewer than `MIN_PER_CLASS` labels; a fold whose training half holds one class only scores nothing."""
    keys = sorted(key for key in labels if key in texts)
    if min(sum(1 for key in keys if labels[key]), sum(1 for key in keys if not labels[key])) < MIN_PER_CLASS:
        return {}
    xs = _vectors([texts[key] for key in keys])
    fold_of = [rows.bucket(key, "suspect-fold") % FOLDS for key in keys]
    out: dict[str, float] = {}
    for fold in range(FOLDS):
        train = [i for i, f in enumerate(fold_of) if f != fold]
        ys = [labels[keys[i]] for i in train]
        if all(ys) or not any(ys):
            continue
        w, bias = _fit([xs[i] for i in train], ys, f"fold-{fold}")
        for i, f in enumerate(fold_of):
            if f == fold:
                p_open = _sigmoid(sum(w.get(j, 0.0) * v for j, v in xs[i]) + bias)
                out[keys[i]] = round(p_open if labels[keys[i]] else 1.0 - p_open, 4)
    return out


def reusable(
    base: Path, stem: str, *, model: str, checkpoint: str | None, question_version: int, device: str, digest: str | None
) -> dict[str, dict]:
    """`{key: row}` of the scores file that a run of this model would write identically: same model, checkpoint,
    question version, device and digest. Nothing is reusable while the digest is unknown."""
    if digest is None:
        return {}
    found, _code = lab_files.read_scores(base, stem)
    return {
        row["key"]: row
        for row in found
        if row["model"] == model
        and row["checkpoint"] == checkpoint
        and row["question_version"] == question_version
        and row.get("device") == device
        and row.get("digest") == digest
    }


def run(base: Path, *, model: str = "served", device: str = "mps", router: Any = None, full: bool = False) -> int:
    spec = REVIEW_SURFACES[SURFACE]
    suite = SUITES[spec.golden_suite]
    require_texts(base, SURFACE)
    target, path = resolve_model(base, SURFACE, model)
    wanted = keys_to_score(base, SURFACE)
    if not wanted:
        print("[laya-lab] review-open-work: nothing to score (no Sonnet labels or ledger texts yet)")
        return 0
    checkpoint = path.name if path is not None else None
    digest = infer.checkpoint_digest(target, path)
    kept = (
        {}
        if full
        else reusable(
            base,
            spec.stem,
            model=target,
            checkpoint=checkpoint,
            question_version=suite.question_version,
            device=device,
            digest=digest,
        )
    )
    todo = {key: text for key, text in wanted.items() if key not in kept}
    scored: dict[str, float] = {key: row["p"] for key, row in kept.items() if key in wanted}
    if todo:
        router = router if router is not None else infer.open_router(base, device, {target: path})
        probabilities = infer.predict_noul(
            router, target, [suite.build_state({"hand_off": text}) for text in todo.values()], suite.question
        )
        scored.update({key: round(p, 4) for key, p in zip(todo, probabilities)})
    cut, source = served_threshold(base, SURFACE, target, code=suite.threshold)
    at = rows.utc_stamp()
    _code, sonnet = review_data.sonnet_labels(base, spec)
    # The surrogate reads every wanted key on every run (seconds on the CPU), so a kept p never carries a stale pair.
    teacher_open = {key: sonnet[key]["open"] for key in wanted if key in sonnet}
    confidence = teacher_confidence(wanted, teacher_open)
    out_rows = [
        lab_files.scores_row(
            key,
            scored[key],
            target,
            checkpoint,
            suite.question_version,
            cut,
            kept[key]["at"] if key in kept else at,
            device=device,
            digest=digest,
            teacher=(teacher_open[key], confidence[key]) if key in confidence else None,
        )
        for key in wanted
    ]
    out = paths.data_file(base, spec.stem, "laya-p")
    write_jsonl(out, out_rows)
    flagged = sum(1 for p in scored.values() if p >= cut)
    suspect = review_data.suspect_ranked({key: (teacher_open[key], conf) for key, conf in confidence.items()})
    print(
        f"[laya-lab] review-open-work · {target}{f' ({checkpoint})' if checkpoint else ''} · cut {cut} ({source}): "
        f"{len(todo)} texts scored, {len(wanted) - len(todo)} kept from the last run, {flagged} at or over the cut, "
        f"{len(suspect)} of {len(confidence)} Sonnet labels suspect → {out}"
    )
    return 0
