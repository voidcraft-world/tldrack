"""`laya-lab teacher-kappa`: a LOCAL teacher labels the rack's turn ends the way Sonnet does, scored against Sonnet and
against the operator. `open_work_data.teacher_bar` reads the verdict: a teacher that clears the bar labels in Sonnet's place.

    laya-lab teacher-kappa                  label what this teacher has not answered yet, then score; resumable
    laya-lab teacher-kappa --check          one JSON line: did the keys, the teacher's weights or the rubric move since the last report
    laya-lab teacher-kappa --verdict-json   the last report's verdict as one JSON line (a scheduler's probe)

- **Its own selection, not `label()`'s.** Every key Sonnet labelled, plus every key the operator graded in REVIEW, that
  still has a text (`open_work_data.texts`). `label()` skips the operator-answered keys, and those are exactly the rows
  teacher-vs-operator κ needs. Public `golden:` answers are not turn ends, so they are left out.
- **The teacher reads what Sonnet read:** `LABEL_SYSTEM_PROMPT`, `LABEL_SCHEMA` and `batch_prompt` (owned by
  `open_work_data.py`), `TEACHER_BATCH` texts a call, at temperature 0 and one seed. A level-only family (gpt-oss) hears
  `think: low` with the reasoning's own room (`REASONING_TOKENS`): such a family cannot switch thinking off, and a reply
  whose budget the reasoning spent is cut mid-JSON.
- **On this machine only.** The texts are the operator's own closing messages, so an Ollama URL off loopback is refused (exit 3).
- **Resumable, and an error is never stored.** Each batch's answers append to `data/agent-open-work-teacher.jsonl`
  (0600) as it lands, keyed by (key, teacher tag, its weights digest, `rubric_digest`): a re-run asks only what that
  teacher has not answered, and a re-pulled model or an edited rubric starts over. A failed call (Ollama did not
  answer) writes nothing, and `FAILURES_MAX` in a row stop the run: the transport is down. A reply with no label it
  could read is a miss for its keys, never a down transport. A key a batch left out is asked once more alone.
- **The resource lease, when one is configured** (`laya_lab/lease.py`): the gpu slot for the model's listed size
  before the first call. A scheduled run pauses between batches while live work wants the machine
  (`infer.yield_point`). A run with a stop ahead (`lease.run_minutes_left`) starts a call only while the time left
  covers the longest call so far (the call timeout before one has) and `STOP_MARGIN_MIN` for the score; the next run
  resumes it.
- **κ is `laya_bridge.agreement.cohens_kappa`**, with a seeded percentile bootstrap 95% interval over keys, for three
  pairs: teacher~Sonnet (every Sonnet key), teacher~operator (every operator key) and Sonnet~operator (their overlap).
  Below `KAPPA_MIN_PAIRS` a pair shows no κ, as every Laya readout does.
- **The bar:** κ(teacher, Sonnet) ≥ `KAPPA_BAR`, and κ(teacher, operator) at or above the low end of Sonnet's own
  operator interval (above it is better, never a miss). PASS or
  FAIL once `COVERAGE_MIN` of the selection is answered and every pair has its κ; INCONCLUSIVE before that.
- The report (`evals/teacher-kappa-<suite>-<stamp>.json`) holds keys, ids and numbers, never a text or a `why`.

Exit 0 (the report is written; with the selection covered, even when the tail stopped early, so its verdict
reads) · 1 the teacher's transport went down before the selection was covered · 3 refused (a URL off this machine, a
model Ollama does not list) · 75 the lease is held.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import secrets
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Callable, Mapping

from laya_lab import siblings  # noqa: F401  (puts laya_bridge and tldrack on sys.path)
from laya_lab import infer
from laya_lab.data import atomic_write_text, jsonl_line, open_private
from laya_lab.open_work_data import (
    LABEL_SCHEMA,
    LABEL_SYSTEM_PROMPT,
    STEM,
    SUITE,
    SURFACE,
    batch_prompt,
    model_labels,
    read_labels,
    require_sources,
    texts,
)
from laya_bridge import env_names, files, lab_files, paths, rows
from laya_bridge.agreement import KAPPA_MIN_PAIRS, agree_rate, cohens_kappa, confusion
from laya_bridge.paths import EVALS_DIRNAME

REFUSED = 3
FAILED = 1

TEACHER_MODEL = "gpt-oss:20b"
#: Texts a call: a closing text is at most 2,000 chars (`transcript.CLOSING_TEXT_MAX_CHARS`), so eight fill ~6k tokens.
TEACHER_BATCH = 8
TEACHER_SEED = 7
#: The reasoning's own room for a level-only family, on top of the caps (the module note owns why).
REASONING_TOKENS = 1024
#: One fixed context for every call: Ollama reloads a model whose `num_ctx` changes between calls.
TEACHER_NUM_CTX = 12_288 + REASONING_TOKENS
#: An `{id, open, why}` answer is ~60 tokens; 96 an item leaves room for a long `why`. The reasoning gets twice its
#: room: eight verdicts reason longer than one, and a reply cut mid-JSON is a failed call.
TEACHER_NUM_PREDICT = TEACHER_BATCH * 96 + 2 * REASONING_TOKENS
#: Ollama `details.family` values (punctuation dropped) that read `think` as a level and cannot switch it off.
LEVEL_ONLY_FAMILIES = frozenset({"gptoss"})
CALL_TIMEOUT_S = 600.0
FAILURES_MAX = 3
#: Minutes a run with a stop ahead keeps clear of it beyond a call: the score (its bootstrap) and the report's write.
STOP_MARGIN_MIN = 2
KEEP_ALIVE = "15m"

KAPPA_BAR = 0.75
COVERAGE_MIN = 0.95
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20261109
#: The teacher's answers file is the operator's derived data: the bridge's readers' cap keeps it readable.
TEACHER_CAP_BYTES = 16 * 1024 * 1024
#: Keys a report lists per disagreement kind: enough to review, never the whole set.
DISAGREE_LIST_MAX = 200
REPORT_KIND = "teacher_kappa"
PAIR_NAMES = ("teacher_sonnet", "teacher_operator", "sonnet_operator")


def rubric_digest() -> str:
    """What the teacher is asked, as one id: the rubric, the answer schema and the batch framing."""
    text = json.dumps({"system": LABEL_SYSTEM_PROMPT, "schema": LABEL_SCHEMA, "frame": batch_prompt([])}, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def teacher_path(base: Path) -> Path:
    return paths.data_file(base, STEM, "teacher")


def report_dir(base: Path) -> Path:
    return base / EVALS_DIRNAME


# ── the teacher on this machine's Ollama ──────────────────────────────────────


def ollama_url(env: Mapping[str, str] = os.environ) -> str:
    """The lease's `OLLAMA_URL_ENV` (it sizes its models at the same URL), else Ollama's default on this machine."""
    lease = infer.lease_module()
    return (env.get(lease.OLLAMA_URL_ENV) or lease.DEFAULT_OLLAMA_URL).rstrip("/")


@dataclass(frozen=True)
class Teacher:
    model: str
    digest: str
    family: str
    gb: float

    @property
    def level_only(self) -> bool:
        return "".join(ch for ch in self.family.lower() if ch.isalnum()) in LEVEL_ONLY_FAMILIES

    @property
    def model_digest(self) -> str:
        """One id for these weights, `<tag>+<digest, 12 hex>`: what a verdict names its model by."""
        return f"{self.model}+{self.digest[:12]}"


def _get_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as reply:  # noqa: S310 — loopback only (`ollama_url` is checked)
        body = json.loads(reply.read())
    return body if isinstance(body, dict) else {}


def find_teacher(url: str, model: str, get: Callable[[str], dict] = _get_json) -> Teacher:
    """The installed model by Ollama's listed name (`:latest` optional), its digest, family and lease GB. LookupError
    when Ollama does not list it; OSError when Ollama does not answer."""
    tags = get(f"{url}/api/tags")
    for entry in tags.get("models") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        if model not in (entry["name"], entry["name"].removesuffix(":latest")):
            continue
        size = entry.get("size")
        digest = entry.get("digest") if isinstance(entry.get("digest"), str) else ""
        family = ((entry.get("details") or {}).get("family") or "") if isinstance(entry.get("details"), dict) else ""
        if not isinstance(size, (int, float)) or isinstance(size, bool) or not digest:
            raise LookupError(f"Ollama lists {model} without a size or digest")
        lease = infer.lease_module()
        return Teacher(
            model=entry["name"].removesuffix(":latest"),
            digest=digest,
            family=str(family),
            gb=round(size / lease.GIB * lease.OLLAMA_RESIDENT_FACTOR, 1),
        )
    raise LookupError(f"Ollama does not list {model}: pull it first (ollama pull {model})")


def chat_body(teacher: Teacher, batch: list[dict[str, Any]]) -> dict[str, Any]:
    """The `/api/chat` request for one batch: Sonnet's rubric and schema, deterministic decoding."""
    return {
        "model": teacher.model,
        "messages": [{"role": "system", "content": LABEL_SYSTEM_PROMPT}, {"role": "user", "content": batch_prompt(batch)}],
        "stream": False,
        "format": LABEL_SCHEMA,
        "think": "low" if teacher.level_only else False,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0, "seed": TEACHER_SEED, "num_ctx": TEACHER_NUM_CTX, "num_predict": TEACHER_NUM_PREDICT},
    }


def _post_json(url: str, body: dict, timeout: float) -> dict:
    request = urllib.request.Request(url, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as reply:  # noqa: S310 — loopback only
        payload = json.loads(reply.read())
    return payload if isinstance(payload, dict) else {}


def answer_text(payload: Mapping[str, Any]) -> str:
    """The reply's JSON text: the message content, cut to its outermost object (a model may wrap it in prose)."""
    message = payload.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str):
        return ""
    start, end = content.find("{"), content.rfind("}")
    return content[start : end + 1] if 0 <= start < end else content


Post = Callable[[str, dict, float], dict]


def ask_batch(
    url: str,
    teacher: Teacher,
    batch: list[dict[str, Any]],
    post: Post = _post_json,
    clock: Callable[[], float] = time.perf_counter,
) -> tuple[dict[str, dict[str, Any]], float]:
    """`({key: {open, why}}, wall ms)` for one batch; ValueError when the call failed (nothing is stored then)."""
    started = clock()
    try:
        payload = post(f"{url}/api/chat", chat_body(teacher, batch), CALL_TIMEOUT_S)
    except urllib.error.HTTPError as error:
        raise ValueError(f"Ollama answered HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as error:
        raise ValueError(f"Ollama did not answer ({error})") from None
    ms = (clock() - started) * 1000
    if payload.get("error"):
        raise ValueError(f"Ollama refused the call: {str(payload['error'])[:200]}")
    return read_labels(answer_text(payload), {row["key"] for row in batch}), ms


# ── the answers file ──────────────────────────────────────────────────────────


def read_answers(base: Path, teacher: Teacher, rubric: str) -> dict[str, bool]:
    """`{key: open}` this teacher (same tag and weights) gave under this rubric; the last row wins, a torn line is skipped."""
    path = teacher_path(base)
    try:
        found, _bad = files.read_jsonl_capped(path, TEACHER_CAP_BYTES)
    except files.SourceError as error:
        raise SystemExit(
            f"[laya-lab] refusing: {path} is {error} (a link, not a regular file, or past "
            f"{TEACHER_CAP_BYTES // (1024 * 1024)} MiB)"
        ) from None
    out: dict[str, bool] = {}
    for row in found:
        if (
            row.get("by") == teacher.model
            and row.get("digest") == teacher.digest
            and row.get("rubric") == rubric
            and isinstance(row.get("key"), str)
            and lab_files.KEY.fullmatch(row["key"])
            and isinstance(row.get("open"), bool)
        ):
            out[row["key"]] = row["open"]
    return out


def append_answers(base: Path, teacher: Teacher, rubric: str, got: Mapping[str, Mapping[str, Any]]) -> None:
    with open_private(teacher_path(base), "a") as handle:
        for key, value in got.items():
            handle.write(
                jsonl_line(
                    {
                        "key": key,
                        "open": value["open"],
                        "why": value["why"],
                        "by": teacher.model,
                        "digest": teacher.digest,
                        "rubric": rubric,
                    }
                )
            )


# ── what is asked ─────────────────────────────────────────────────────────────


@dataclass
class Selection:
    sonnet: dict[str, bool]
    operator: dict[str, bool]
    found: dict[str, str]
    keys: list[str] = field(default_factory=list)
    no_text: int = 0

    @property
    def dataset_version(self) -> str:
        """`s<n>-f<n>-<sha8>` over every selected key and both labels: a new label or grade moves it."""
        rows_out = [[key, self.sonnet.get(key), self.operator.get(key)] for key in self.keys]
        digest = hashlib.sha256(json.dumps(rows_out, separators=(",", ":")).encode("utf-8")).hexdigest()[:8]
        return f"s{len(self.sonnet)}-f{len(self.operator)}-{digest}"


def select(base: Path) -> Selection:
    """Every Sonnet-labelled key plus every operator-graded key, with a text, in a stable order."""
    fold = require_sources(base)
    sonnet = {
        row["key"]: row["open"] for row in model_labels(base) if isinstance(row.get("open"), bool) and row.get("by") != "operator"
    }
    operator = {
        key: row["open"]
        for key, row in fold.labels.items()
        if not key.startswith("golden:") and isinstance(row.get("open"), bool)
    }
    found = texts(base, SURFACE)
    wanted = sorted(set(sonnet) | set(operator))
    keys = [key for key in wanted if key in found]
    return Selection(sonnet=sonnet, operator=operator, found=found, keys=keys, no_text=len(wanted) - len(keys))


# ── scoring ───────────────────────────────────────────────────────────────────


def kappa_ci(pairs: list[tuple[bool, bool]], resamples: int | None = None, seed: int = BOOTSTRAP_SEED) -> dict[str, Any]:
    """`{n, kappa, agree, ci95, confusion}`: κ and a percentile bootstrap 95% interval over the pairs (`resamples`,
    default BOOTSTRAP_RESAMPLES). Below KAPPA_MIN_PAIRS there is no κ and no interval (every Laya readout shows none
    rather than a noisy one)."""
    resamples = BOOTSTRAP_RESAMPLES if resamples is None else resamples
    n = len(pairs)
    out: dict[str, Any] = {"n": n, "kappa": None, "agree": agree_rate(pairs), "ci95": None, "confusion": confusion(pairs)}
    if n < KAPPA_MIN_PAIRS:
        return out
    out["kappa"] = cohens_kappa(pairs)
    rng = random.Random(seed)
    draws = []
    for _ in range(resamples):
        k = cohens_kappa([pairs[rng.randrange(n)] for _ in range(n)])
        if k is not None:
            draws.append(k)
    if len(draws) * 2 >= resamples:  # most resamples must have a κ, or the interval would describe a few of them
        draws.sort()
        lo = draws[int(math.floor(0.025 * len(draws)))]
        hi = draws[min(len(draws) - 1, int(math.ceil(0.975 * len(draws))) - 1)]
        out["ci95"] = [round(lo, 3), round(hi, 3)]
    return out


def score(selection: Selection, answers: Mapping[str, bool]) -> dict[str, Any]:
    """The three pairs, the disagreement keys, and the verdict on the bar. Pure."""
    t_s = [(answers[key], selection.sonnet[key]) for key in selection.keys if key in answers and key in selection.sonnet]
    t_f = [(answers[key], selection.operator[key]) for key in selection.keys if key in answers and key in selection.operator]
    s_f = [
        (selection.sonnet[key], selection.operator[key])
        for key in selection.keys
        if key in selection.sonnet and key in selection.operator
    ]
    pairs = {"teacher_sonnet": kappa_ci(t_s), "teacher_operator": kappa_ci(t_f), "sonnet_operator": kappa_ci(s_f)}
    answered = sum(1 for key in selection.keys if key in answers)
    coverage = round(answered / len(selection.keys), 4) if selection.keys else 0.0
    disagree = {
        "teacher_sonnet": [
            key for key in selection.keys if key in answers and key in selection.sonnet and answers[key] != selection.sonnet[key]
        ][:DISAGREE_LIST_MAX],
        "teacher_operator": [
            key
            for key in selection.keys
            if key in answers and key in selection.operator and answers[key] != selection.operator[key]
        ][:DISAGREE_LIST_MAX],
    }
    return {
        "answered": answered,
        "coverage": coverage,
        "pairs": pairs,
        "disagree": disagree,
        "verdict": bar_verdict(coverage, pairs),
    }


def bar_verdict(coverage: float, pairs: Mapping[str, Mapping[str, Any]]) -> str:
    """PASS/FAIL on the bar (the module note owns it) once the selection is covered and every pair has its κ; INCONCLUSIVE before."""
    t_s, t_f, s_f = (pairs[name] for name in PAIR_NAMES)
    if coverage < COVERAGE_MIN or t_s["kappa"] is None or t_f["kappa"] is None or s_f["ci95"] is None:
        return "INCONCLUSIVE"
    return "PASS" if t_s["kappa"] >= KAPPA_BAR and t_f["kappa"] >= s_f["ci95"][0] else "FAIL"


def spread_ms(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    at = lambda p: ordered[min(len(ordered) - 1, max(0, math.ceil(p * len(ordered)) - 1))]  # noqa: E731 — nearest rank
    return {"p50": round(statistics.median(ordered), 1), "p95": round(at(0.95), 1), "n": len(ordered)}


# ── the run ───────────────────────────────────────────────────────────────────


@dataclass
class RunState:
    calls: int = 0
    #: Calls Ollama did not answer (the transport), and calls it answered with no label this could read.
    failed_calls: int = 0
    unreadable_calls: int = 0
    call_ms: list[float] = field(default_factory=list)
    key_ms: list[float] = field(default_factory=list)
    #: The longest call's wall time so far, answered or not: what the next call may take.
    longest_s: float | None = None
    stopped: str | None = None


MinutesLeft = Callable[[], "float | None"]


def call_fits(left: float | None, longest_s: float | None) -> bool:
    """Whether a call may start with `left` minutes to the run's stop (None: no stop): the longest call so far, or the
    call timeout before one has, plus STOP_MARGIN_MIN. Pure."""
    return left is None or left >= (CALL_TIMEOUT_S if longest_s is None else longest_s) / 60 + STOP_MARGIN_MIN


def ask_all(
    base: Path,
    url: str,
    teacher: Teacher,
    rubric: str,
    todo: list[dict[str, Any]],
    *,
    max_calls: int | None,
    post: Post = _post_json,
    minutes_left: MinutesLeft | None = None,
    say: Callable[[str], None] = print,
    clock: Callable[[], float] = time.perf_counter,
) -> RunState:
    """Every batch of `todo`, then each key a batch left out once more alone; answers append as each call lands."""
    state = RunState()
    minutes_left = minutes_left or infer.lease_module().run_minutes_left
    failures = 0

    def run(batch: list[dict[str, Any]]) -> set[str] | None:
        """The keys it answered, or None when the run must stop."""
        nonlocal failures
        if max_calls is not None and state.calls >= max_calls:
            state.stopped = "max_calls"
            return None
        infer.yield_point()
        if not call_fits(minutes_left(), state.longest_s):
            state.stopped = "time"
            return None
        state.calls += 1
        began = clock()
        try:
            got, ms = ask_batch(url, teacher, batch, post, clock)
        except ValueError as error:
            state.longest_s = max(state.longest_s or 0.0, clock() - began)
            state.failed_calls += 1
            failures += 1
            say(f"[laya-lab]   call {state.calls}: failed ({error})")
            if failures >= FAILURES_MAX:
                state.stopped = "failures"
                return None
            return set()
        state.longest_s = max(state.longest_s or 0.0, ms / 1000)
        failures = 0  # it answered: the transport is up, whatever the reply held
        if not got:
            state.unreadable_calls += 1
            say(f"[laya-lab]   call {state.calls}: no label it could read")
            return set()
        append_answers(base, teacher, rubric, got)
        state.call_ms.append(ms)
        state.key_ms.extend([ms / len(batch)] * len(batch))
        say(
            f"[laya-lab]   call {state.calls}: {len(got)}/{len(batch)} labelled ({sum(v['open'] for v in got.values())} open) · {ms / 1000:.1f} s"
        )
        return set(got)

    missed: list[dict[str, Any]] = []
    for start in range(0, len(todo), TEACHER_BATCH):
        batch = todo[start : start + TEACHER_BATCH]
        answered = run(batch)
        if answered is None:
            return state
        missed.extend(row for row in batch if row["key"] not in answered)
    for row in missed:
        if run([row]) is None:
            return state
    return state


def report_name(stamp: str) -> str:
    return lab_files.teacher_report_name(SUITE, stamp)


def build_report(
    selection: Selection, scored: Mapping[str, Any], teacher: Teacher, rubric: str, state: RunState, eval_run: str | None
) -> dict[str, Any]:
    return {
        "kind": REPORT_KIND,
        "v": 1,
        "suite": SUITE,
        "surface": SURFACE,
        "at": rows.utc_stamp(),
        "run_id": secrets.token_hex(6),
        "eval_run": eval_run,
        "teacher": {
            "model": teacher.model,
            "digest": teacher.digest,
            "family": teacher.family,
            "think": chat_body(teacher, [])["think"],
            "batch": TEACHER_BATCH,
            "num_ctx": TEACHER_NUM_CTX,
        },
        "rubric": rubric,
        "dataset_version": selection.dataset_version,
        "selection": {
            "keys": len(selection.keys),
            "sonnet": len(selection.sonnet),
            "operator": len(selection.operator),
            "both": len(set(selection.sonnet) & set(selection.operator)),
            "no_text": selection.no_text,
        },
        "answered": scored["answered"],
        "coverage": scored["coverage"],
        "calls": state.calls,
        "failed_calls": state.failed_calls,
        "unreadable_calls": state.unreadable_calls,
        "stopped": state.stopped,
        "latency_ms": {"call": spread_ms(state.call_ms), "key": spread_ms(state.key_ms)},
        "pairs": scored["pairs"],
        "bar": {"kappa_min": KAPPA_BAR, "coverage_min": COVERAGE_MIN, "operator_floor": "sonnet_operator.ci95[0]"},
        "disagree": scored["disagree"],
        "verdict": scored["verdict"],
    }


def run(
    base: Path,
    *,
    model: str = TEACHER_MODEL,
    max_calls: int | None = None,
    post: Post = _post_json,
    get: Callable[[str], dict] = _get_json,
    minutes_left: MinutesLeft | None = None,
) -> int:
    url = ollama_url()
    if not infer.is_loopback_url(url):
        print(f"[laya-lab] teacher-kappa refused: {url} is not this machine: the texts are the operator's own and never leave it")
        return REFUSED
    selection = select(base)
    try:
        teacher = find_teacher(url, model, get)
    except LookupError as error:
        print(f"[laya-lab] teacher-kappa refused: {error}")
        return REFUSED
    except (OSError, ValueError) as error:
        print(f"[laya-lab] teacher-kappa: Ollama at {url} did not answer ({error})")
        return FAILED
    rubric = rubric_digest()
    answers = read_answers(base, teacher, rubric)
    todo = [{"key": key, "hand_off": selection.found[key]} for key in selection.keys if key not in answers]
    print(
        f"[laya-lab] teacher-kappa {teacher.model_digest} · rubric {rubric} · {len(selection.keys)} keys "
        f"({len(selection.sonnet)} Sonnet, {len(selection.operator)} operator, {selection.no_text} without a text) · "
        f"{len(answers)} answered, {len(todo)} to ask"
    )
    state = RunState()
    minutes_left = minutes_left or infer.lease_module().run_minutes_left
    if todo and not call_fits(minutes_left(), None):
        state.stopped = "time"  # never queue for a slot with no time left to use it (a closed window, a near deadline)
    elif todo:
        infer.hold_slot("gpu", "ollama", teacher.gb, [teacher.model])
        state = ask_all(base, url, teacher, rubric, todo, max_calls=max_calls, post=post, minutes_left=minutes_left)
        answers = read_answers(base, teacher, rubric)
    scored = score(selection, answers)
    report = build_report(selection, scored, teacher, rubric, state, env_names.read(os.environ, lab_files.EVAL_RUN_ENV))
    out = report_dir(base) / report_name(lab_files.report_stamp())
    atomic_write_text(out, json.dumps(report, indent=2, allow_nan=False) + "\n")
    for line in report_lines(report):
        print(line)
    print(f"  report → {out}")
    # Covered, the report's verdict stands (`check` calls it fresh from here on): a down tail must not hide it.
    return FAILED if state.stopped == "failures" and scored["coverage"] < COVERAGE_MIN else 0


def report_lines(report: Mapping[str, Any]) -> list[str]:
    fmt = lambda v: "—" if v is None else f"{v:.3f}"  # noqa: E731
    lines = [
        f"[laya-lab] teacher-kappa · {report['teacher']['model']} · {report['answered']}/{report['selection']['keys']} answered "
        f"({report['coverage'] * 100:.1f}%) · {report['calls']} calls, {report['failed_calls']} failed"
        + (f", {report.get('unreadable_calls')} unreadable" if report.get("unreadable_calls") else "")
        + (f" · stopped: {report['stopped']}" if report["stopped"] else "")
    ]
    for name in PAIR_NAMES:
        pair = report["pairs"][name]
        ci = pair["ci95"]
        lines.append(
            f"  {name:<16} κ {fmt(pair['kappa'])}"
            + (f" [{ci[0]:.3f}, {ci[1]:.3f}]" if ci else "")
            + f" · agree {fmt(pair['agree'])} · n {pair['n']}"
        )
    key_ms = (report["latency_ms"] or {}).get("key")
    if key_ms:
        lines.append(f"  latency          {key_ms['p50']:.0f} ms a key (p50) · {key_ms['p95']:.0f} ms p95")
    lines.append(
        f"  bar              κ teacher~sonnet ≥ {KAPPA_BAR} and teacher~operator ≥ the sonnet~operator CI's low end → {report['verdict']}"
    )
    return lines


# ── reads for a scheduler: --check and --verdict-json ─────────────────────────


def newest_report(base: Path | None) -> dict[str, Any] | None:
    """The newest teacher-kappa report (by its stamp), or None."""
    folder = None if base is None else report_dir(base)
    if folder is None or not files.is_real_dir(folder):
        return None
    pattern = lab_files.teacher_report_pattern(SUITE)
    for name in sorted((n for n in os.listdir(folder) if pattern.fullmatch(n)), reverse=True):
        try:
            data = files.read_capped(folder / name, lab_files.REPORT_CAP_BYTES)
            report = files.loads(data) if data is not None else None
        except (files.SourceError, ValueError, RecursionError):
            continue
        if isinstance(report, dict) and report.get("kind") == REPORT_KIND:
            return report
    return None


def check(base: Path | None, *, get: Callable[[str], dict] = _get_json) -> tuple[bool, str]:
    """`(fresh, reason)`: fresh while the newest report covered the selection, on the same keys and labels, rubric
    and teacher weights. The weights are compared only while Ollama answers (the run itself needs it)."""
    report = newest_report(base)
    if report is None:
        return False, "no teacher-kappa report yet"
    selection = select(base)
    if report.get("dataset_version") != selection.dataset_version:
        return False, f"the labels moved: {report.get('dataset_version')} → {selection.dataset_version}"
    if report.get("rubric") != rubric_digest():
        return False, "the labelling rubric changed"
    if (report.get("coverage") or 0) < COVERAGE_MIN:
        return False, f"the last run answered {report.get('answered')}/{report['selection']['keys']}: resume it"
    try:
        teacher = find_teacher(ollama_url(), report["teacher"]["model"], get)
    except (LookupError, OSError, ValueError):
        teacher = None
    if teacher is not None and teacher.digest != report["teacher"]["digest"]:
        return False, f"{teacher.model}'s weights changed ({report['teacher']['digest'][:12]} → {teacher.digest[:12]})"
    return True, f"{report['verdict']} at {report['at']} on {selection.dataset_version}"


def verdict_probe(report: Mapping[str, Any] | None, eval_run: str | None) -> dict[str, Any]:
    """The verdict probe's one JSON object, from the report THIS scheduled run wrote; INCONCLUSIVE when it wrote
    none (the lease was held, or the run never reached its report)."""
    quiet = {"verdict": "INCONCLUSIVE", "headline": {"cases": 0}, "accelerator": "mps", "cost": {"usd": 0, "gb_s": 0}}
    if report is None or (eval_run is not None and report.get("eval_run") != eval_run):
        return quiet
    t_s = report["pairs"]["teacher_sonnet"]
    key_ms = (report.get("latency_ms") or {}).get("key") or {}
    headline = {
        "cases": report["answered"],
        "kappa": t_s["kappa"],
        "accuracy": t_s["agree"],
        "latency_ms_p50": key_ms.get("p50"),
        "latency_ms_p95": key_ms.get("p95"),
    }
    return {
        **quiet,
        "verdict": report["verdict"],
        "headline": {k: v for k, v in headline.items() if isinstance(v, (int, float))},
        "model_digest": f"{report['teacher']['model']}+{report['teacher']['digest'][:12]}",
        "dataset_version": report["dataset_version"],
    }


def cmd(args: Any, base: Path | None) -> int:
    """The CLI block's one entry (`laya-lab teacher-kappa`)."""
    if args.check:
        fresh, reason = check(base)
        print(json.dumps({"fresh": fresh, "reason": reason}))
        return 0 if fresh else 1
    if args.verdict_json:
        print(json.dumps(verdict_probe(newest_report(base), env_names.read(os.environ, lab_files.EVAL_RUN_ENV))))
        return 0
    return run(base, model=args.model, max_calls=args.max_calls)
