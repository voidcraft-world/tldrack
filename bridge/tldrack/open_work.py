"""Does a reply leave planned work open? Laya adds ⚠️ to the rack's reply mark, or ✅ in its place.

The rack's reply mark records an unread turn, never task completion. Some turns plan phases
A, B and C, ship A and B, and hand off with C never started. This
module asks Laya, on this Mac, whether the closing message leaves asked-for or promised work
open. No per-call cloud charge, private, sub-second, and it never spends Claude quota on a classifier's job.

── The question and its owner ──────────────────────────────────────────────
`OPEN_WORK_QUESTION` is THE wording: the bridge asks it, and `laya-lab golden agent-open-work`
scores it against the golden cases in `lab/golden/`. Edit the text, or what `decide_state`
builds → bump `QUESTION_VERSION` and re-run the golden suite; `tests/test_open_work.py` pins both.

The coordinator asks a sibling question over one hand-off LINE (`coordinator/laya_rank.py`).
Plugins cannot import the core, so the two stay separate. The served version judges the closing
message alone; version 3 (`decide_state`, measured as a challenger) also reads the request that
opened the turn. Neither certifies that a research decision is settled.

── Three bands, two cuts ────────────────────────────────────────────────────
One p, two cuts: p ≥ the OPEN cut → ⚠️ (work open); p < the DONE cut → ✅ (the work looks done);
between them the row keeps its plain reply mark 💬, because "not sure it is open" is not "done".
The bands are asymmetric on purpose. A false ✅ invites a kill with planned work still open, so
the DONE cut sits well below the OPEN one (`DONE_AT` owns the measurement); a missed ✅ only costs
a glance at the pane. The operator grades both marks on the REVIEW deck's RECENT list,
and those answers feed both cuts' sweeps.

── An answer to read ───────────────────────────────────────────────────────
A turn that ANSWERS a question is done by the question above: the agent owes nothing, so the row
draws ✅. But the operator still owes the answer a read, and ✅ alone says "nothing to look at".
So a ✅ whose turn was opened by a question carries a second, separate signal: `answer_to_read`.
It is not a label and not a band; Laya's question, cuts, golden set and labels do not move.
  · The rule (`asks_question`): the prompt that opened the turn (`turn_request`) is a question,
    its last non-empty line ending in `?` (the `ends_on_question` rule the landing trace already
    applies to the closing). A turn a hook, a task or a child woke has no prompt: no mark.
  · Only on ✅. An open verdict's ⚠️ already asks for a look; 💬 is already an unread reply.
  · It clears like a parked ⚠️ (attention.py § 3): a busy turn voids the verdict and the mark with
    it, and the operator opening that session's pane posts `POST /open-work/read`, which stamps
    the verdict `read`. Both stamps ride the kept copy (verdicts.py), so a restart neither drops
    an unread answer nor brings back a read one.

── How a verdict reaches a row ─────────────────────────────────────────────
A `Stop` hook opens a turn token on the store and submits the turn here. One worker thread asks
Laya; per session only the LATEST turn is kept, so a burst of Stops costs one decision. The
verdict is written back compare-and-set on that token (`StatusStore.set_open_work`), so a prompt
typed while Laya was thinking voids it instead of painting a stale ⚠️ on a busy row.

Laya is reached IN PROCESS through the mounted `laya` plugin object — the same `/decide` and
`/status` handlers an HTTP caller hits, without a loopback round trip. No plugin, Laya not
installed, or a refusal → no verdict, and the row keeps its ordinary reply mark. Silence, never an error: a
operator who never enabled Laya should not read about it on every turn.

── The ledger ──────────────────────────────────────────────────────────────
Every `/decide` is tagged `agent_open_work` with the session as its `subject` and the hook's
conversation id as its `conversation`, so the plugin's decision ledger keeps the closing text for
the REVIEW deck (what `conversation` is for: owned
by schemas/src/decide.ts `LAYA_LEDGER_CONVERSATION` — see the note there). `OpenWorkReactions` then
tells it what became of each ⚠️ the rack SHOWED: `shown`, then `dismissed` (the operator said no) or
`expired` (a new turn, a kill), with `accepted` between when the operator said the work IS open.
Telemetry only: a ledger that is off, absent or refusing never costs the operator the ⚠️.

── The operator's grade ─────────────────────────────────────────────────────
The ⚠️'s menu on the rack is also a grading door: ✗ NOT OPEN (`/open-work/dismiss`) and ✓ STILL
OPEN (`/open-work/confirm`) each record the operator's answer to THAT verdict as a label, through the
plugin's in-process `rack_grade` (`grade_verdict`). A reaction counts presses; the label is what
precision, the sweeps and a retrain read. What it records and why it is a flip or an agree: owned
by plugins/laya/laya_bridge/review.py `rack` — see the note there. ⏸ NOT NOW grades nothing.
The press's own HTTP headers go with it (`rack_grader`), and the plugin decides from them whether
this caller may write a label: owned by plugins/laya/laya_bridge/plugin.py — see the note there.

── The landing record ──────────────────────────────────────────────────────
Where a verdict LANDED is known only after `/decide` answered: its class (held, new, repeat,
retired; attention.py), the audience (`held`: background, children or free), what was still
running and the turn's trace. So it is never a tag field: main.py builds it with `landing_body`,
from the `decision_id` this module threads through the verdict, and `post_landing` sends it to the
plugin's `/landing`. Why a sidecar and what reads it: owned by
plugins/laya/laya_bridge/landing.py — see the note there.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from tldrack.ledger import claude_fold
from tldrack.profiles import AGENT_SESSION_ID_RE
from tldrack.transcript import _is_turn_start, ends_on_question

#: Bump on ANY wording change to `OPEN_WORK_QUESTION` or to what `decide_state` builds; the golden
#: report records it. 2: long hand-offs fitted to Laya's window keeping their end (`fit_hand_off`).
#: 3 (built, not served): the turn's request ahead of the closing (`decide_state`).
QUESTION_VERSION = 2

OPEN_WORK_QUESTION: dict[str, Any] = {
    "type": "noul",
    # Short, positive, and over the hand-off ALONE: measured on the golden suite (base checkpoint),
    # the long version with a request field and an "answer no when…" exception list scored 0.28 —
    # anti-correlated — while this scores 0.75–0.78. The base model reads negated instructions
    # badly; the reflex exceptions are a fine-tune's job (laya-lab), not prose's.
    "instructions": ("Does this message say that some work is still not done, not started, blocked, or waiting on an answer?"),
}

#: How much worse a false ⚠️ is than a missed one: THE rule for where the cut sits (laya-lab's
#: sweep imports this constant; docs and READMEs point here). A false ⚠️ outlives the unread
#: state, so it costs about twice a missed one. The sweep picks the cut minimising
#: `FALSE_WARNING_COST·fp + fn`, ties to the higher cut. Of the three cuts measured 2026-10-16 for
#: the `FINE_TUNE_MODEL` checkpoint on held-out turn ends it picks 0.7: the lower cuts (0.6, 0.5)
#: catch a few more open turns and pay for it in false ⚠️. A RE-SWEEP over the finer grid
#: and operator labels may move it, and `/status.thresholds` then carries the result.
FALSE_WARNING_COST = 2.0

#: `noul` ≥ this → ⚠️, unless `/status.thresholds` carries a cut swept for the pinned fine-tune
#: (`open_work_cut`). Chosen by `FALSE_WARNING_COST`'s rule, and the golden suite's best cut too
#: (4 false of 16 deliberately hard done cases, 15 of 16 open). Re-sweep after every retrain
#: (laya-lab README).
OPEN_WORK_AT = 0.7

#: `noul` < this → ✅ (the work looks done), unless `/status.thresholds` carries a `done_at` swept
#: for the pinned fine-tune (`done_work_cut`). Never above the live OPEN cut. Measured 2026-10-30 on
#: the served checkpoint's golden reports (34 cases, 17 open): no OPEN case scored below 0.5, so 0.4
#: keeps a margin and still turns about a third of live turn ends into ✅. A false ✅ costs
#: `FALSE_DONE_COST` missed ones: the sweep's rule for this cut (laya-lab `heldout.py`).
DONE_AT = 0.4

#: How much worse a false ✅ (open work drawn as done) is than a missed one (done work left at 💬).
#: A false ✅ invites killing the session with its plan half-shipped; a missed one costs a glance.
FALSE_DONE_COST = 3.0

#: The laya-lab fine-tune this surface pins when one is registered (`fine_tune_name` convention,
#: owned by schemas/src/decide.ts).
FINE_TUNE_MODEL = "voidcraft-agent-open-work"

#: The ledger surface every verdict is tagged with (`DECIDE_SURFACES`, schemas/src/decide.ts).
LEDGER_SURFACE = "agent_open_work"
#: Copies of `LAYA_LEDGER_SUBJECT` and `LAYA_DECISION_ID`: owned by schemas/src/decide.ts.
LEDGER_SUBJECT = re.compile(r"[A-Za-z0-9_.:#|-]{1,128}")
DECISION_ID = re.compile(r"[0-9]{8}T[0-9]{6}-[0-9a-f]{12}")
#: `model_id` is `laya-local/<name ≤64>@<version ≤64>` (decide.ts `is_laya_local_model_id`).
MAX_MODEL_ID_CHARS = 140

#: A cold Laya start is tens of seconds; past this the turn's verdict is simply skipped.
WARM_WAIT_SECONDS = 90
WARM_POLL_SECONDS = 1.0


#: Every question version `decide_state` builds: the served one, and a challenger the lab measures
#: against it (laya-lab `challenger-state`). 3 puts the turn's request ahead of the closing; it is
#: served only once it beats 2 on held-out turn ends by more than the interval (lab/README.md
#: "Question v3").
STATE_VERSIONS = (2, 3)


def decide_state(hand_off: str, request: str | None = None, *, version: int = QUESTION_VERSION) -> str:
    """The state Laya judges — THE one builder, shared with the golden suite and laya-lab's training
    rows. Every caller passes the turn's request when it has one, so a version change is this one
    function and `QUESTION_VERSION`.

    v2: the closing message alone, fitted to Laya's window by `fit_hand_off`. v3: the request that
    opened the turn (its head, `fit_request`), then the closing fitted to what is left, keeping its
    end. A turn nobody typed (a hook, a finished task or a child woke it: `turn_request`) has no
    request, and its v3 state is its v2 state."""
    if version not in STATE_VERSIONS:
        raise ValueError(f"question version {version} has no state builder")
    head = fit_request(request) if version >= 3 and request else ""
    if not head:
        return fit_hand_off(hand_off)
    prefix = f"{REQUEST_LABEL}{head}{PART_BREAK}{REPLY_LABEL}"
    return prefix + fit_hand_off(hand_off, STATE_MAX_CHARS - len(prefix))


#: The longest state, in characters, that clears Laya's 512-token window beside the question.
#: Measured 2026-10-30 on turn ends: many overflowed at full length, and Laya keeps the HEAD,
#: so the hand-off's last lines (where the unstarted work is named) were the part it never saw.
#: At 1,300 code-collapsed characters none of them still overflow.
STATE_MAX_CHARS = 1300
#: Of a long hand-off, the opening kept for context; the rest of the budget goes to its END.
STATE_HEAD_CHARS = 450
CODE_FENCE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)
CODE_PLACEHOLDER = "[code block]"
ELISION = " … "
#: v3: of `STATE_MAX_CHARS`, the most the request takes. Its HEAD is kept: a request names its ask
#: first, and what follows is usually pasted context. The closing gets the rest, so a v3 state is
#: never longer than a v2 one and clears the same window.
REQUEST_MAX_CHARS = 300
REQUEST_LABEL = "Request: "
REPLY_LABEL = "Reply: "
PART_BREAK = "\n\n"
WHITESPACE = re.compile(r"\s+")


def fit_hand_off(text: str, max_chars: int = STATE_MAX_CHARS) -> str:
    """`text` unchanged when it fits in `max_chars`; else its fenced code collapsed (pasted content,
    never status); else its opening and its end joined by `ELISION`, `max_chars` in all. The opening
    is `STATE_HEAD_CHARS` of a whole budget and shrinks with a smaller one (v3's closing)."""
    if len(text) <= max_chars:
        return text
    text = CODE_FENCE.sub(CODE_PLACEHOLDER, text)
    if len(text) <= max_chars:
        return text
    head = STATE_HEAD_CHARS * max_chars // STATE_MAX_CHARS
    tail = max_chars - head - len(ELISION)
    return text[:head].rstrip() + ELISION + text[-tail:].lstrip()


def turn_request(entry: dict) -> str | None:
    """The request a turn-opening transcript entry carries: what the operator typed, or the slash
    command they ran with its arguments. None when a hook, a finished task, a child or a peer
    opened the turn: nobody asked for anything there. Who wrote a prompt is the session ledger's
    reading (`ledger/claude_fold.py` `origin_of`), so the two never disagree."""
    message = entry.get("message")
    text = claude_fold.block_text(message.get("content") if isinstance(message, dict) else None).strip()
    if not text or text.startswith((claude_fold.INTERRUPT_PREFIX, *claude_fold._LOCAL_OUTPUT)):
        return None
    command = claude_fold._COMMAND_NAME.search(text[:500])
    if command:
        args = claude_fold._COMMAND_ARGS.search(text)
        return f"{command.group(1)} {args.group(1) if args else ''}".strip()
    return text if claude_fold.origin_of(entry, text, "main") == "human" else None


def fold_turn_request(prior: str | None, entries: Iterable[dict]) -> str | None:
    """The CURRENT turn's request after these entries (`turn_request` of its opening prompt). Pure.
    A turn starts where the rack's turn trace starts one (`transcript._is_turn_start`): THE one fold
    the bridge serves with and `laya-lab export-open-work` exports with."""
    for entry in entries:
        if _is_turn_start(entry):
            prior = turn_request(entry)
    return prior


def flat_request(text: str) -> str:
    """A request flattened: fenced code collapsed, every run of whitespace one space. Idempotent, so a
    stored head of it (laya-lab's export keeps one) fits to the same state as the whole request."""
    return WHITESPACE.sub(" ", CODE_FENCE.sub(CODE_PLACEHOLDER, text)).strip()


def fit_request(text: str) -> str:
    """The head of a turn's request as a v3 state carries it: `flat_request`, at most
    `REQUEST_MAX_CHARS` (its end cut, marked `…`)."""
    flat = flat_request(text)
    if len(flat) <= REQUEST_MAX_CHARS:
        return flat
    return flat[: REQUEST_MAX_CHARS - 1].rstrip() + "…"


def state_sha(state: str) -> str:
    """The SHA-256 of a judged state: how a kept verdict proves it still describes the turn on
    screen after a bridge restart (verdicts.py owns why). Hex, so it never carries the text."""
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def is_open(p: float, at: float = OPEN_WORK_AT) -> bool:
    return p >= at


def is_done(p: float, done_at: float = DONE_AT) -> bool:
    return p < done_at


#: The verdict's row keys, filled by `verdict_row_fields` and carried onto `/snapshot` rows by
#: `snapshot.merge_snapshot`. The rack reads only the snapshot, so a key missing there is a mark
#: the rack never draws: until 2026-11-05 none of the three reached it, and no ⚠️ or ✅ ever did.
VERDICT_ROW_KEYS = ("open_work", "work_done", "open_work_p", "answer_to_read")


def asks_question(request: str | None) -> bool:
    """Was the turn opened by a question (§ An answer to read)? Pure. `request` is `turn_request`'s
    reading of the opening prompt; None (nobody typed one) is never a question."""
    return ends_on_question(request)


def with_request(verdict: dict, request: str | None) -> dict:
    """The verdict stamped with whether its turn asked a question (`asked`), before it lands on the
    row and in the kept copy. A new dict; the judge's own copy is left alone."""
    return {**verdict, "asked": asks_question(request)}


def verdict_row_fields(verdict: dict | None) -> dict[str, object]:
    """The additive `/status` and `/snapshot` row fields: `open_work` (bool | None), `work_done`
    (bool | None), `open_work_p`, and `answer_to_read` (bool | None: a ✅ on a turn that asked a
    question, not read yet; § An answer to read). A verdict from before the done band (no
    `done_at`) is never ✅: the rack drew 💬."""
    if not verdict:
        return dict.fromkeys(VERDICT_ROW_KEYS)
    p = verdict["p"]
    done_at = verdict.get("done_at")
    done = is_done(p, done_at) if isinstance(done_at, (int, float)) else False
    return {
        "open_work": is_open(p, verdict.get("at", OPEN_WORK_AT)),
        "work_done": done,
        "open_work_p": round(p, 3),
        "answer_to_read": done and verdict.get("asked") is True and verdict.get("read") is not True,
    }


def open_work_cut(thresholds: object, model: str | None) -> float:
    """The cut for this verdict: the swept `thresholds.agent_open_work.at` off `/status` when it was
    swept for the PINNED fine-tune and lies strictly inside (0, 1), else `OPEN_WORK_AT`. A cut is
    scoped to the checkpoint it was swept on, so the base model never borrows one."""
    entry = thresholds.get(LEDGER_SURFACE) if isinstance(thresholds, dict) else None
    if model is None or not isinstance(entry, dict) or entry.get("model") != model:
        return OPEN_WORK_AT
    at = entry.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not 0 < at < 1:
        return OPEN_WORK_AT
    return float(at)


def done_work_cut(thresholds: object, model: str | None, open_at: float) -> float:
    """The ✅ cut: a swept `thresholds.agent_open_work.done_at` under the same scoping rule as
    `open_work_cut`, else `DONE_AT`; never above `open_at`, so the three bands never overlap."""
    entry = thresholds.get(LEDGER_SURFACE) if isinstance(thresholds, dict) else None
    cut = DONE_AT
    if model is not None and isinstance(entry, dict) and entry.get("model") == model:
        done_at = entry.get("done_at")
        if not isinstance(done_at, bool) and isinstance(done_at, (int, float)) and 0 < done_at < 1:
            cut = float(done_at)
    return min(cut, open_at)


def ledger_tag(subject: str | None, at: float, done_at: float | None = None, conversation: object = None) -> dict[str, Any]:
    """The `/decide` request's `ledger` field (`layaLedgerTagSchema`). A session name the subject
    pattern refuses is left out rather than sent, so the plugin never 400s the verdict over it.
    `done_threshold` records the ✅ cut, so the RECENT list grades the mark the rack actually drew.
    `conversation` is the hook's `session_id`: hook text, so anything but a UUID is left out too."""
    tag: dict[str, Any] = {"surface": LEDGER_SURFACE, "question_version": QUESTION_VERSION, "threshold": at}
    if done_at is not None:
        tag["done_threshold"] = done_at
    if isinstance(subject, str) and LEDGER_SUBJECT.fullmatch(subject):
        tag["subject"] = subject
    if isinstance(conversation, str) and AGENT_SESSION_ID_RE.fullmatch(conversation):
        tag["conversation"] = conversation
    return tag


Caller = Callable[[str, dict | None], tuple[int, dict]]


def plugin_caller(plugin: object, on_decide: Callable[[], None] | None = None) -> Caller:
    """`(path, body) → (status, payload)` against a mounted plugin object, never raising.

    `body is None` is a GET. A handler exception is a 500, exactly what the host would answer.
    `on_decide` runs before and after each `/decide`: production marks the resource lease with it
    (`live_marks.py`), so scheduled GPU work steps aside from a live verdict; it never takes a ticket.
    """

    def call(path: str, body: dict | None) -> tuple[int, dict]:
        if on_decide is not None and path == "/decide":
            on_decide()
            try:
                return _call(path, body)
            finally:
                on_decide()
        return _call(path, body)

    def _call(path: str, body: dict | None) -> tuple[int, dict]:
        if body is None:
            return _answer_of(lambda: plugin.handle_get(path, {}))  # type: ignore[attr-defined]
        return _answer_of(lambda: plugin.handle_post(path, {}, body))  # type: ignore[attr-defined]

    return call


def _answer_of(handler: Callable[[], tuple]) -> tuple[int, dict]:
    """One in-process plugin call as `(status, payload)`, never raising: an exception is a 500."""
    try:
        status, payload = handler()
    except Exception:  # noqa: BLE001 — a plugin bug must not take the hook path down
        return 500, {}
    return int(status), payload if isinstance(payload, dict) else {}


Grader = Callable[[dict], tuple[int, dict]]


def rack_grader(plugin: object, headers: Mapping[str, object]) -> Grader:
    """`body → (status, payload)` against the plugin's `rack_grade`, never raising, for ONE operator press.

    `headers` are those of the HTTP request that carried the press, names lowercased as the plugin host does it.
    A plugin without `rack_grade` (one from before the door) is a 500, so nothing is recorded."""
    lowered = {str(name).lower(): value for name, value in headers.items()}
    return lambda body: _answer_of(lambda: plugin.rack_grade(body, lowered))  # type: ignore[attr-defined]


def ledger_judged(plugin: object, conversation: object, state: str) -> bool | None:
    """Has Laya's ledger already judged THIS turn: a decision row of this conversation whose state hashes
    to `state`'s? True or False only when the plugin can say; None for anything it cannot, never raising.

    A MISSED TURN END. The judge runs on the `Stop` hook, and a `Stop` that lands while the bridge is
    down is gone: Claude Code reports the refused POST and never sends it again. The bridge restarts
    often (verdicts.py tells of one such day), so turns end unjudged every day, and the REVIEW page has
    nothing to show for them. One session's first turn (2026-11-11) ended into an ECONNREFUSED,
    three seconds before the bridge was back. So when Claude's index rebuilds a lost row turn-over and
    no kept verdict describes it (main.py `recover_turn_end`), the bridge judges the closing message it
    finds then, unless this says the ledger already holds that exact turn. Only a definite False judges:
    a turn judged and then dismissed has no kept verdict either, and re-judging it would raise the ⚠️ the
    operator already took down, so an unsure answer (no ledger, an older plugin) judges nothing."""
    if not isinstance(conversation, str) or not AGENT_SESSION_ID_RE.fullmatch(conversation):
        return None
    method = getattr(plugin, "judged_before", None)
    if not callable(method):
        return None
    try:
        answer = method(conversation, state_sha(state))
    except Exception:  # noqa: BLE001 — a plugin bug must not take the arbiter's thread down
        return None
    return answer if isinstance(answer, bool) else None


def ask_laya(
    call: Caller,
    state: dict,
    *,
    subject: str | None = None,
    conversation: object = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict | None:
    """One judgement: `{"p", "v", "model", "at", "decision_id", "model_id", "checkpoint"}`, or
    None when Laya cannot answer this turn. `subject` (the session) and `conversation` (the
    hook's `session_id`) tag the ledger row.

    `/decide` starts a cold Laya itself and answers `503 warming`; this waits that out (the
    worker thread is the only thing waiting) and asks once more.
    """
    status, info = call("/status", None)
    if status != 200:
        return None
    models = info.get("models") if isinstance(info.get("models"), list) else []
    model = FINE_TUNE_MODEL if FINE_TUNE_MODEL in models else None
    at = open_work_cut(info.get("thresholds"), model)
    done_at = done_work_cut(info.get("thresholds"), model, at)
    body: dict[str, Any] = {
        "state": state,
        "questions": {"q": OPEN_WORK_QUESTION},
        "ledger": ledger_tag(subject, at, done_at, conversation),
    }
    if model:
        body["model"] = model
    deadline = clock() + WARM_WAIT_SECONDS
    while True:
        status, payload = call("/decide", body)
        if status == 200:
            return _verdict(payload, model, at, done_at)
        refused = _refused_optional_field(payload, body.get("ledger")) if status == 400 else None
        if refused is not None:
            # A plugin older than the field: keep the rest of the tag, so the row keeps its text.
            _report_refused_field(refused, payload["detail"])
            ledger = {key: value for key, value in body["ledger"].items() if key != refused}
            body = {**body, "ledger": ledger}
            continue
        if status == 400 and "ledger" in body and _refused_tag(payload):
            # The tag is telemetry; the ⚠️ is the product. Ask again untagged.
            _report_refused_tag(payload["detail"])
            body = {key: value for key, value in body.items() if key != "ledger"}
            continue
        if status != 503 or payload.get("error") != "warming" or clock() >= deadline:
            return None
        sleep(WARM_POLL_SECONDS)


def _refused_tag(payload: dict) -> bool:
    """The plugin's 400 for a bad `ledger` field names it in `detail` (`ledger.<field>`)."""
    detail = payload.get("detail")
    return payload.get("error") == "bad_request" and isinstance(detail, str) and detail.startswith("ledger")


#: The tag fields the rack may DROP when a plugin refuses one by name, with the words its console line
#: uses. Every other field is the tag itself: refused, the ask goes untagged.
#:
#: A NEW decide-tag field takes four steps, all in one change, because the tag is a strict wire on
#: both sides and an older plugin refuses a whole tag over one field it does not know: its Zod line
#: in schemas/src/decide.ts `ledgerTagFields`; `ledger.py` `TAG_FIELDS` / `COPIED_TAG_FIELDS` and a
#: `clean_tag` branch whose 400 detail starts `ledger.<field>`; the enum pin in
#: plugins/laya/tests/test_contract.py; and an entry HERE, so a skewed pair costs the row that
#: field and never its tag. A fact about a verdict that only the bridge learns after `/decide` (where
#: it LANDED) is no tag field at all: it is the landing record (`landing_body`).
OPTIONAL_TAG_FIELDS: dict[str, str] = {"conversation": "conversation id"}


def _refused_optional_field(payload: dict, ledger: object) -> str | None:
    """The one `OPTIONAL_TAG_FIELDS` field a 400 refuses by name, when the tag still carries it: a
    plugin from before the field ("not a ledger field"), or one that disagrees on its shape. Dropping
    the whole tag instead would record the verdict `untagged`, hash-only, and it would vanish from
    the RECENT list. None for any other refusal."""
    if not _refused_tag(payload) or not isinstance(ledger, dict):
        return None
    for field in OPTIONAL_TAG_FIELDS:
        if field in ledger and payload["detail"].startswith(f"ledger.{field}"):
            return field
    return None


#: What this process already said the plugin refuses: the tag, or one optional field of it. Once per
#: process: a refusal repeats on every turn end, and the operator does not read the console that often.
_tag_refusal_reported = False
_field_refusals_reported: set[str] = set()


def _report_refused_tag(detail: str) -> None:
    """One console line the first time the tag is refused. Without it a cross-lane mismatch
    (a field name, the native-caller check) turns every row hash-only and nothing says so."""
    global _tag_refusal_reported
    if _tag_refusal_reported:
        return
    _tag_refusal_reported = True
    print(f"tldrack: laya refused the open-work ledger tag ({detail[:200]}); asking untagged")


def _report_refused_field(field: str, detail: str) -> None:
    """`_report_refused_tag`'s line for one optional field: the rows stay tagged, but without it
    until the plugin is restarted onto a version that records it."""
    if field in _field_refusals_reported:
        return
    _field_refusals_reported.add(field)
    print(f"tldrack: laya refused the open-work {OPTIONAL_TAG_FIELDS[field]} ({detail[:200]}); tagging without it")


def _verdict(payload: dict, model: str | None, at: float, done_at: float = DONE_AT) -> dict | None:
    answer = (payload.get("answers") or {}).get("q")
    value = answer.get("noul") if isinstance(answer, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        return None
    decision_id = payload.get("decision_id")
    model_id = payload.get("model_id")
    checkpoint = payload.get("checkpoint")
    return {
        "p": float(value),
        "v": QUESTION_VERSION,
        "model": model or "english",
        "at": at,
        "done_at": done_at,
        # The ledger's handle on this verdict, for `OpenWorkReactions`; None when the ledger is off.
        "decision_id": decision_id if isinstance(decision_id, str) and DECISION_ID.fullmatch(decision_id) else None,
        "model_id": model_id if isinstance(model_id, str) and 0 < len(model_id) <= MAX_MODEL_ID_CHARS else None,
        # A basename only (never a params path over the wire); anything path-shaped is dropped.
        "checkpoint": checkpoint if _is_basename(checkpoint) else None,
    }


def _is_basename(value: object) -> bool:
    return (
        isinstance(value, str) and 0 < len(value) <= 120 and value not in (".", "..") and "/" not in value and "\\" not in value
    )


# ── the landing record (module docstring § The landing record) ────────────────

#: The plugin route, and the body's keys in order. Copies of plugins/laya/laya_bridge/landing.py
#: (a bridge module may not import a plugin); lab/tests/test_landing_wire.py pins both sides.
LANDING_ROUTE = "/landing"
LANDING_RUNNING_KEYS = ("descendants", "agents", "workflows", "shells")
LANDING_TRACE_KEYS = ("ran_tests", "edited_files", "opened_pr", "ended_on_question")


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def landing_body(verdict: dict, landing_class: str | None, held: str | None, running: dict, trace: dict | None) -> dict | None:
    """The `/landing` body for one landed verdict, or None when it carries no `decision_id` (the
    ledger was off: there is no row to join). Counts default to 0; a trace with a missing key is
    dropped whole rather than half-told."""
    decision_id = verdict.get("decision_id")
    if not isinstance(decision_id, str) or not DECISION_ID.fullmatch(decision_id):
        return None
    if trace is not None and not all(key in trace for key in LANDING_TRACE_KEYS):
        trace = None
    return {
        "decision_id": decision_id,
        "surface": LEDGER_SURFACE,
        "class": landing_class,
        "held": held,
        "running": {key: _count(running.get(key)) for key in LANDING_RUNNING_KEYS},
        "trace": None if trace is None else {key: trace[key] for key in LANDING_TRACE_KEYS},
    }


#: `grade_verdict`'s answers. `recorded` and `kept` both mean the text now has an operator label (`kept`: it already
#: had one, which stands, whichever way it says); `none` is a verdict with no ledger id or not a ⚠️; `off` is
#: everything else (no plugin, no params, no kept text, a refused caller, a refused write).
GRADE_RECORDED, GRADE_KEPT, GRADE_NONE, GRADE_OFF = "recorded", "kept", "none", "off"


def grade_verdict(grade: Grader | None, verdict: dict | None, *, is_open_now: bool) -> str:
    """Record the operator's answer to one ⚠️ as a label (module docstring § The operator's grade). Never raises.

    Only a verdict that drew a ⚠️ and carries the ledger's `decision_id` is gradable: the menu opens on a ⚠️
    alone, and without the id there is no text to label.

    `kept` is believed only when the 409 names the label that stands (`answer.open` a bool). A bare
    `already_answered`, or one whose answer is a skip, is `off`: a skip is no label, and saying "graded" over
    one told the rack a label stood where none did."""
    decision_id = (verdict or {}).get("decision_id")
    if (
        not isinstance(decision_id, str)
        or not DECISION_ID.fullmatch(decision_id)
        or not is_open(verdict["p"], verdict.get("at", OPEN_WORK_AT))
    ):
        return GRADE_NONE
    if grade is None:
        return GRADE_OFF
    status, payload = grade({"decision_id": decision_id, "open": is_open_now})
    if status == 200:
        return GRADE_RECORDED
    standing = payload.get("answer")
    labelled = isinstance(standing, dict) and isinstance(standing.get("open"), bool)
    return GRADE_KEPT if status == 409 and payload.get("code") == "already_answered" and labelled else GRADE_OFF


def post_landing(call: Caller, body: dict) -> int:
    """Send one landing row; the plugin's status. 404 is a plugin from before the route, 409 a
    decision it does not hold, 503 the ledger off: every caller drops a failed landing (telemetry)."""
    status, _payload = call(LANDING_ROUTE, body)
    return status


#: How long after a Stop the closing text is read. The judgement re-tails the transcript then
#: rather than trusting what the hook saw: nothing documents that Claude Code flushes the final
#: message before the Stop hook runs, and judging the PREVIOUS message would be a wrong verdict.
SETTLE_SECONDS = 1.0


class OpenWorkJudge:
    """One worker thread, a latest-wins slot per session. `submit` never blocks a hook."""

    def __init__(
        self,
        laya: Callable[[], Caller | None],
        on_verdict: Callable[[str, int, dict], None],
        *,
        ask: Callable[..., dict | None] = ask_laya,
        settle_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._laya = laya
        self._on_verdict = on_verdict
        self._ask = ask
        self._settle = SETTLE_SECONDS if settle_seconds is None else settle_seconds
        self._clock = clock
        self._lock = threading.Condition()
        # session -> (token, conversation, read_state, due_at): the conversation rides beside its
        # token, so latest-wins keeps the id of the turn it actually judges
        self._pending: dict[str, tuple[int, object, Callable[[], Any], float]] = {}
        self._thread: threading.Thread | None = None

    def submit(self, session: str, token: int, read_state: Callable[[], Any], *, conversation: object = None) -> bool:
        """Queue a turn; `read_state` returns the state to judge, or None for "nothing to
        judge". `conversation` is the Stop hook's `session_id`, as it came (`ledger_tag` checks
        it). False (and nothing queued) when this bridge has no Laya."""
        if self._laya() is None:
            return False
        with self._lock:
            self._pending[session] = (token, conversation, read_state, self._clock() + self._settle)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="open-work-judge", daemon=True)
                self._thread.start()
            self._lock.notify()
        return True

    def drain(self) -> int:
        """Judge everything queued, due or not, on THIS thread; how many verdicts landed. Tests."""
        landed = 0
        while (item := self._take(block=False)) is not None:
            landed += self._judge(*item)
        return landed

    def _take(self, *, block: bool) -> tuple[str, int, Callable[[], Any], object] | None:
        with self._lock:
            while True:
                if not self._pending:
                    if not block:
                        return None
                    self._lock.wait()
                    continue
                session, (token, conversation, read_state, due) = min(self._pending.items(), key=lambda kv: kv[1][3])
                wait = due - self._clock()
                if block and wait > 0:
                    self._lock.wait(wait)  # a newer submit may replace it meanwhile
                    continue
                del self._pending[session]
                return session, token, read_state, conversation

    def _run(self) -> None:
        while True:
            item = self._take(block=True)
            if item is not None:
                try:
                    self._judge(*item)
                except Exception:  # noqa: BLE001 — one bad turn must not kill the worker
                    pass

    def _judge(self, session: str, token: int, read_state: Callable[[], Any], conversation: object = None) -> int:
        call = self._laya()
        state = read_state() if call is not None else None
        verdict = self._ask(call, state, subject=session, conversation=conversation) if state else None
        if verdict is None:
            return 0
        self._on_verdict(session, token, {**verdict, "state_sha": state_sha(state)})
        return 1


#: Reactions waiting for the poster thread. Past this the OLDEST are dropped: a ledger that
#: stopped answering must not grow the bridge's memory, and a lost reaction is only telemetry.
MAX_PENDING_REACTIONS = 256


class OpenWorkReactions:
    """What became of each ⚠️ the rack SHOWED, posted to the plugin's `/react` as ledger reactions.

    One outstanding decision per session: the `decision_id` of the verdict on screen. A landing
    first expires the one before it; a dismiss, a new turn or a kill resolves it once and forgets
    it. Only a verdict that drew a ⚠️ (p ≥ its own cut) and carries a `decision_id` is tracked,
    so a below-cut verdict or a bridge whose ledger is off posts nothing.

    The bookkeeping is under one lock; the POSTS go out on one FIFO daemon thread, so a hook
    never waits on the ledger and `shown` always precedes its own `expired`. Every error is
    swallowed.
    """

    def __init__(self, laya: Callable[[], Caller | None], *, worker: bool = True) -> None:
        self._laya = laya
        self._worker = worker
        self._lock = threading.Condition()
        self._shown: dict[str, str] = {}
        self._outbox: deque[tuple[str, str]] = deque(maxlen=MAX_PENDING_REACTIONS)
        self._thread: threading.Thread | None = None

    def landed(self, session: str, verdict: dict, *, live: Callable[[], bool] | None = None) -> None:
        """A verdict landed on the row. `live` (when given) re-checks, under this lock, that the
        verdict is still on the row: a prompt that voided it in between was never shown, and one
        that voids it after finds the id here and expires it. No interleaving loses either, so
        long as every clear site calls `cleared` AFTER the store's own clear (main.py does)."""
        with self._lock:
            self._resolve_locked(session, "expired")
            decision_id = verdict.get("decision_id")
            if not isinstance(decision_id, str) or not is_open(verdict["p"], verdict.get("at", OPEN_WORK_AT)):
                return
            if live is not None and not live():
                return
            self._shown[session] = decision_id
            self._post_locked(decision_id, "shown")

    def restored(self, session: str, verdict: dict) -> None:
        """A kept verdict came back after a bridge restart (verdicts.py). Its ⚠️ was already
        reported `shown` before the restart, so this re-tracks the id WITHOUT posting: a second
        `shown` would repeat a reaction the ledger already holds. Its `dismissed` / `expired` still
        reaches the ledger, which a restart used to orphan."""
        with self._lock:
            self._resolve_locked(session, "expired")
            decision_id = verdict.get("decision_id")
            if isinstance(decision_id, str) and is_open(verdict["p"], verdict.get("at", OPEN_WORK_AT)):
                self._shown[session] = decision_id

    def cleared(self, session: str, action: str) -> None:
        """The row's ⚠️ went away: `dismissed` by the operator, or `expired`. A no-op (no post)
        when nothing is outstanding, which is every busy hook of an ordinary turn."""
        with self._lock:
            self._resolve_locked(session, action)

    def accepted(self, session: str) -> None:
        """The operator said the ⚠️ is right (✓ STILL OPEN). The ⚠️ stays on the row, so its id stays
        outstanding and still expires or is dismissed later. A second press posts again; the ledger
        keeps one row per (decision, action)."""
        with self._lock:
            decision_id = self._shown.get(session)
            if decision_id is not None:
                self._post_locked(decision_id, "accepted")

    def rename(self, old: str, new: str) -> None:
        """Follow `/rename-session`. Whatever was outstanding under `new` belonged to a session
        that no longer exists, so it expires."""
        with self._lock:
            decision_id = self._shown.pop(old, None)
            self._resolve_locked(new, "expired")
            if decision_id is not None:
                self._shown[new] = decision_id

    def forget(self, session: str) -> None:
        """Drop the session's outstanding id without a reaction."""
        with self._lock:
            self._shown.pop(session, None)

    def outstanding(self, session: str) -> str | None:
        with self._lock:
            return self._shown.get(session)

    def drain(self) -> int:
        """Post everything queued on THIS thread; how many were sent. Tests (`worker=False`)."""
        sent = 0
        while True:
            with self._lock:
                if not self._outbox:
                    return sent
                decision_id, action = self._outbox.popleft()
            self._send(decision_id, action)
            sent += 1

    def _resolve_locked(self, session: str, action: str) -> None:
        decision_id = self._shown.pop(session, None)
        if decision_id is not None:
            self._post_locked(decision_id, action)

    def _post_locked(self, decision_id: str, action: str) -> None:
        self._outbox.append((decision_id, action))
        if self._worker and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="open-work-reactions", daemon=True)
            self._thread.start()
        self._lock.notify()

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._outbox:
                    self._lock.wait()
                decision_id, action = self._outbox.popleft()
            self._send(decision_id, action)

    def _send(self, decision_id: str, action: str) -> None:
        try:
            call = self._laya()
            if call is not None:
                call("/react", {"decision_id": decision_id, "action": action})
        except Exception:  # noqa: BLE001 — telemetry must never take the poster thread down
            pass
