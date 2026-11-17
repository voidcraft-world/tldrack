"""PURE detection: parsed records in, Finding dicts out. The tested core.

Nothing in this module opens a file, spawns a process or reads the clock —
`now` is passed in. That is what makes the detection testable against a handful
of hand-built dicts instead of a directory of transcripts, and it is the line to
keep: all I/O lives in `sources.py` / `tail.py`, all judgement lives here.

── The six signals, and why each is shaped the way it is ──────────────────
The near-misses are as load-bearing as the hits, so they are spelled out (and
each has a test):

A `blocked`                  a live session in ~/.claude/sessions is sitting at
                             status "waiting". No transcript parsing at all.
B `stated-next-step`         the `Next:` clause of the final `away_summary`
                             system record. Most sessions that run long enough
                             get one, and most of those name a next step — by
                             far the richest source.
C `rejected-plan`            the FINAL ExitPlanMode's tool_result says the user
                             refused. Claude Code always writes a result, so a
                             dangling tool call never happens and the CONTENT of
                             the result is the only usable evidence.
D `dismissed-question`       same treatment for AskUserQuestion.
E `open-offer`               a soft-ask phrase in the FINAL assistant message,
                             and only when the assistant spoke last.
F `unactioned-recommendation` a recommendation phrase, same conditions.

C and D are recency-gated (`RECENT_CONVERSATIONAL_RECORDS`): a plan rejected
200 turns before the end means the user *steered*, not abandoned. Without the
gate the signal reports ancient course-corrections as open work. The window is
counted in TURNS a human could have steered with — not in records, which are
mostly the agent's own tool_use/tool_result pairs.

`/exit` is deliberately NOT treated as evidence of resolution: a session that
ends with one can still close on an offer nobody accepted.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from coordinator.tail import age_hours, iso_utc, last_timestamp, parse_timestamp

KIND_BLOCKED = "blocked"
KIND_REJECTED_PLAN = "rejected-plan"
KIND_DISMISSED_QUESTION = "dismissed-question"
KIND_STATED_NEXT_STEP = "stated-next-step"
KIND_OPEN_OFFER = "open-offer"
KIND_UNACTIONED_RECOMMENDATION = "unactioned-recommendation"

WORK_CLASS_OPEN = "open-work"
WORK_CLASS_ROUTINE = "routine"

#: Base rung of the severity ladder. `stated-next-step` is the one kind whose
#: rung MOVES: it only earns 2 once the deep pass says it is real work.
BASE_SEVERITY = {
    KIND_BLOCKED: 0,
    KIND_REJECTED_PLAN: 1,
    KIND_DISMISSED_QUESTION: 1,
    KIND_STATED_NEXT_STEP: 4,
    KIND_OPEN_OFFER: 3,
    KIND_UNACTIONED_RECOMMENDATION: 4,
}
UNKNOWN_SEVERITY = 4

#: Only a refusal near the END of the conversation is an open item. Counted in
#: CONVERSATIONAL TURNS — a real user prompt, or an assistant message that
#: actually said something — never in raw records.
#:
#: Counting raw assistant/user records makes this signal fire on nothing. Every
#: tool call is an assistant record plus a user record carrying the tool_result,
#: so a window of 30 records is ~15 tool calls of the agent's own loop: a plan
#: refused just before the agent worked on alone for a while sits far outside
#: such a window even when no human has said a word since. The gate is about the
#: user STEERING, so only turns a human could have steered with are counted.
RECENT_CONVERSATIONAL_RECORDS = 30

# ── tool_result content markers ─────────────────────────────────────────────
REFUSED_MARKERS = (
    "The user doesn't want to proceed with this tool use",
    "User rejected tool use",
)
PLAN_APPROVED_MARKERS = ("User has approved your plan",)
QUESTION_ANSWERED_MARKERS = ("Your questions have been answered", "The user answered")

# ── phrase sets ─────────────────────────────────────────────────────────────
# Each phrase earns its place by showing up in closing hand-offs. `should I`,
# `shall I` and `would you prefer` read like obvious additions but do not appear
# there, so they would buy nothing and cost precision; they stay out.
SOFT_ASK_PHRASES = (
    "want me to",
    "say the word",
    "your call",
    "if you want",
    "let me know",
    "do you want",
    "tell me which",
    "which one",
)
RECOMMENDATION_PHRASES = (
    "code-review",
    "before you deploy",
    "then deploy",
    "deploy-prod",
    "worth running",
    "happy to run",
)

# ── away_summary parsing ────────────────────────────────────────────────────
# The marker must open a sentence. A bare \bnext\b also matches "after your
# next build", which appears mid-clause in summaries and would truncate the
# actual next step away.
#
# The trailing copula is part of the MARKER, not of the clause: "next step is
# your visual check" is a common phrasing, and leaving the verb in makes the
# headline open on a dangling "is …". The headline is the panel's one line and
# the only text the deep pass classifier sees, so a truncated sentence degrades
# both. Swallowing the copula never loses a clause; it only drops the verb.
NEXT_CLAUSE_RE = re.compile(
    r"(?:^|(?<=[.;!?]))\s*next(?:\s+steps?|\s+actions?)?\b\s*[:,—-]?\s*"
    r"(?:(?:is|are|was|will\s+be|would\s+be)\b\s*)?",
    re.IGNORECASE,
)
RECAP_FOOTER_RE = re.compile(r"\(disable recaps in /config\)\s*$", re.IGNORECASE)
#: "there is no open item", in the phrasings summaries use. Checked against
#: the WHOLE summary, not just the extracted clause: a summary that opens with
#: "Nothing else is in progress" and then closes with a `Next` that hands the
#: ball back ("…is whatever task you'd like to start") is the summary telling us
#: it has nothing, in two sentences. A summary that declares itself empty does
#: not also name work, so this costs no real next step.
NOTHING_PENDING_RE = re.compile(
    r"\bnothing\s+(?:\w+\s+){0,2}?(?:is\s+)?(?:pending|outstanding|left|remaining|in\s+progress)\b"
    r"|\bno\s+(?:outstanding|pending|remaining)\b",
    re.IGNORECASE,
)

HEADLINE_MAX = 160
DETAIL_MAX = 600
#: Only the tail of the closing message is searched for phrases — a soft ask
#: 4000 chars up is part of the body of the work, not the hand-off.
CLOSING_TEXT_WINDOW = 800

_SENTENCE_BREAKS = ".!?\n"


def finding_key(session_id: str, kind: str) -> str:
    """STABLE across re-scans, because dismissals are keyed on it.

    Eight hex characters of the session uuid is plenty (a collision needs tens
    of thousands of sessions inside one audit window) and keeps the key short
    enough to read in a log line. One finding per kind per session is the
    deliberate ceiling: two open offers in one session are one thing to go look
    at.
    """
    return f"{(session_id or '')[:8]}:{kind}"


def severity_for(kind: str, work_class: str | None) -> int:
    """The severity ladder. An unknown kind degrades to the bottom rung rather
    than being dropped — a future signal must never vanish from the list."""
    if kind == KIND_STATED_NEXT_STEP:
        return 2 if work_class == WORK_CLASS_OPEN else 4
    return BASE_SEVERITY.get(kind, UNKNOWN_SEVERITY)


def strip_recap_footer(away_summary: object) -> str:
    """An away_summary without its `(disable recaps in /config)` boilerplate.

    It has to be stripped ONCE, here — the footer is carried into `detail` as
    well as the headline, where it burns budget in a 600-char field and renders
    as noise in the panel.
    """
    if not isinstance(away_summary, str):
        return ""
    return RECAP_FOOTER_RE.sub("", away_summary).strip()


def next_step_clause(away_summary: str) -> str | None:
    """The actionable clause of an away_summary, or None.

    None means one of: no summary, no `Next` sentence, or a summary that
    explicitly says nothing is pending — all three of which are the summary
    telling us there is no open item, which is exactly what it is for.
    """
    text = strip_recap_footer(away_summary)
    if not text or NOTHING_PENDING_RE.search(text):
        return None
    match = None
    for candidate in NEXT_CLAUSE_RE.finditer(text):
        match = candidate  # the LAST sentence-opening `Next` wins
    if match is None:
        return None
    clause = text[match.end() :].strip().lstrip(",;:- ")
    return clause or None


def sort_findings(findings: list[dict]) -> list[dict]:
    """The wire order: severity ascending, then last_active descending. A
    finding with no timestamp sorts LAST within its rung rather than first —
    unknown recency is weaker evidence, not stronger."""
    return sorted(findings, key=lambda f: (f.get("severity", UNKNOWN_SEVERITY), _sort_epoch(f.get("last_active"))))


def _sort_epoch(value: object) -> float:
    parsed = parse_timestamp(value)
    return -parsed.timestamp() if parsed is not None else float("inf")


def phrase_hits(text: str, phrases: tuple[str, ...]) -> list[str]:
    """Which of `phrases` occur in `text` (which must already be lowercase)."""
    return [phrase for phrase in phrases if phrase in text]


def scan_session_entry(entry: dict, now: datetime, alive: bool) -> dict | None:
    """Signal A — a live session sitting at `status: "waiting"`.

    `alive` is passed in rather than probed so this stays pure; `sources.py`
    checks the pid. A stale entry for a dead process is the one way this signal
    can lie, and severity 0 is too loud a rung to let it.
    """
    if not isinstance(entry, dict) or entry.get("status") != "waiting" or not alive:
        return None
    session_id = entry.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return None

    waiting_for = entry.get("waitingFor")
    reason = waiting_for if isinstance(waiting_for, str) and waiting_for else "your input"
    updated = epoch_millis_to_datetime(entry.get("updatedAt"))
    name = entry.get("name")
    cwd = entry.get("cwd") if isinstance(entry.get("cwd"), str) else None

    return _build_finding(
        session_id=session_id,
        kind=KIND_BLOCKED,
        label=name if isinstance(name, str) and name else session_id[:8],
        cwd=cwd,
        project=None,
        branch=None,
        last_active=updated,
        now=now,
        headline=f"waiting on {reason}",
        detail=f"This session is running right now and blocked on {reason}." + (f" Working directory: {cwd}." if cwd else ""),
    )


def scan_transcript(session_id: str, project: str | None, records: list[dict], now: datetime) -> list[dict]:
    """Signals B–F over one transcript's tail records."""
    if not records:
        return []
    reading = _read_transcript(records)
    findings: list[dict] = []

    def add(kind: str, headline: str, detail: str) -> None:
        findings.append(
            _build_finding(
                session_id=session_id,
                kind=kind,
                label=reading["label"] or (session_id or "")[:8],
                cwd=reading["cwd"],
                project=project,
                branch=reading["branch"],
                last_active=reading["last_active"],
                now=now,
                headline=headline,
                detail=detail,
            )
        )

    # `detail` is whichever source actually explains the finding: the summary
    # for a summary-derived one, the closing message for a phrase-derived one.
    # Footer-stripped, or a summary-derived detail ends on "(disable recaps in
    # /config)".
    summary_detail = strip_recap_footer(reading["away_summary"]) or reading["closing_text"]

    # B — the away_summary's Next clause.
    clause = next_step_clause(reading["away_summary"] or "")
    if clause:
        add(KIND_STATED_NEXT_STEP, clause, summary_detail)

    # C / D — a structured prompt refused near the end of the conversation.
    if _final_call_refusal(reading, "ExitPlanMode", PLAN_APPROVED_MARKERS):
        add(KIND_REJECTED_PLAN, "the plan you were shown was rejected and never replaced", summary_detail)
    if _final_call_refusal(reading, "AskUserQuestion", QUESTION_ANSWERED_MARKERS):
        add(KIND_DISMISSED_QUESTION, "the last question Claude asked was dismissed unanswered", summary_detail)

    # E / F — the closing message. Only when the assistant genuinely spoke last:
    # an offer the user already replied to is not an open offer.
    if reading["assistant_last"] and reading["closing_text"]:
        window = reading["closing_text"][-CLOSING_TEXT_WINDOW:]
        lowered = window.lower()
        offers = phrase_hits(lowered, SOFT_ASK_PHRASES)
        if offers:
            add(KIND_OPEN_OFFER, _sentence_around(window, lowered.find(offers[0])), window)
        recommendations = phrase_hits(lowered, RECOMMENDATION_PHRASES)
        if recommendations:
            add(KIND_UNACTIONED_RECOMMENDATION, _sentence_around(window, lowered.find(recommendations[0])), window)

    return findings


# ── internals ──────────────────────────────────────────────────────────────


def _build_finding(
    *,
    session_id: str,
    kind: str,
    label: str,
    cwd: str | None,
    project: str | None,
    branch: str | None,
    last_active: datetime | None,
    now: datetime,
    headline: str,
    detail: str,
) -> dict:
    return {
        "key": finding_key(session_id, kind),
        "session_id": session_id,
        "label": _clip(label, HEADLINE_MAX),
        "cwd": cwd,
        "project": project,
        "branch": branch,
        "last_active": iso_utc(last_active),
        "age_hours": age_hours(last_active, now),
        "kind": kind,
        "severity": severity_for(kind, None),
        "headline": _clip(_flatten(headline), HEADLINE_MAX) or kind,
        "detail": _clip(_flatten(detail), DETAIL_MAX),
        "live_session": None,  # tmux correlation is Step 5
        "work_class": None,  # set only by the deep pass
    }


def _read_transcript(records: list[dict]) -> dict:
    """One backward-friendly pass over the tail, collecting everything the
    signals need. Kept private: the shape is an implementation detail, the
    Finding dicts are the contract."""
    # id -> (name, turns so far, call order). Two counters, because they answer
    # two questions: `order` says which call came LAST (turns can tie — an agent
    # calling three tools in a row says nothing in between), `turns` says how far
    # back that call is in conversation the user could have steered with.
    tool_uses: dict[str, tuple[str, int, int]] = {}
    tool_results: dict[str, str] = {}
    turns = 0
    calls = 0
    closing_text = ""
    assistant_last = False
    away_summary: str | None = None

    for record in records:
        kind = record.get("type")
        if kind == "assistant":
            for block in _content_blocks(record):
                if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                    calls += 1
                    tool_uses[block["id"]] = (str(block.get("name") or ""), turns, calls)
            text = _assistant_text(record)
            if text.strip():
                closing_text = text
                # Only a record that actually SAID something is a turn — a bare
                # tool_use record is half a tool call, not conversation.
                turns += 1
            assistant_last = True
        elif kind == "user":
            for block in _content_blocks(record):
                if block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                    tool_results[block["tool_use_id"]] = _result_text(block)
            if _has_user_prompt(record):
                turns += 1
            assistant_last = False
        elif kind == "system" and record.get("subtype") == "away_summary":
            content = record.get("content")
            if isinstance(content, str) and content.strip():
                away_summary = content.strip()

    return {
        "tool_uses": tool_uses,
        "tool_results": tool_results,
        "turns": turns,
        "closing_text": closing_text,
        "assistant_last": assistant_last,
        "away_summary": away_summary,
        "last_active": last_timestamp(records),
        "label": _label_of(records),
        "cwd": _last_field(records, "cwd"),
        "branch": _last_field(records, "gitBranch"),
    }


def _final_call_refusal(reading: dict, tool_name: str, accepted_markers: tuple[str, ...]) -> bool:
    """Did the LAST call to `tool_name` come back refused, recently enough to matter?

    Only the last call is considered: an early plan the user rejected and then
    replaced with an approved one is a normal working rhythm, not an open item.
    An unrecognised result is treated as accepted: guessing on an unknown marker
    would fire severity 1 on a shape nobody has seen.
    """
    calls = [(order, at_turn, use_id) for use_id, (name, at_turn, order) in reading["tool_uses"].items() if name == tool_name]
    if not calls:
        return False
    _order, at_turn, use_id = max(calls)
    # In TURNS, not records: the agent's own tool loop after a refusal is not
    # the user steering (see RECENT_CONVERSATIONAL_RECORDS).
    distance = reading["turns"] - at_turn
    if distance > RECENT_CONVERSATIONAL_RECORDS:
        return False  # the user steered, they did not abandon
    result = reading["tool_results"].get(use_id, "")
    if any(marker in result for marker in accepted_markers):
        return False
    return any(marker in result for marker in REFUSED_MARKERS)


def _has_user_prompt(record: dict) -> bool:
    """Did a HUMAN type this `user` record?

    Claude Code writes tool_results as user records, so `type == "user"` alone
    counts the agent's own loop as user turns. A real prompt is either a bare
    string message or a content list carrying something that is not a
    tool_result.
    """
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    return any(not isinstance(block, dict) or block.get("type") != "tool_result" for block in content)


def _content_blocks(record: dict) -> list[dict]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _assistant_text(record: dict) -> str:
    return "\n".join(
        block.get("text", "")
        for block in _content_blocks(record)
        if block.get("type") == "text" and isinstance(block.get("text"), str)
    )


def _result_text(block: dict) -> str:
    raw = block.get("content")
    if isinstance(raw, list):
        return " ".join(part.get("text", "") for part in raw if isinstance(part, dict) and isinstance(part.get("text"), str))
    if isinstance(raw, str):
        return raw
    return ""


def _label_of(records: list[dict]) -> str:
    """`ai-title` beats `slug` beats nothing — the title is the one a human
    wrote the session down as. Both are read from the LAST such record: a
    resumed session copies its parent's records forward, so the earliest ones
    can belong to a different conversation entirely."""
    title = _last_field(records, "aiTitle")
    if title:
        return title
    return _last_field(records, "slug") or ""


def _last_field(records: list[dict], field: str) -> str:
    for record in reversed(records):
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _sentence_around(text: str, at: int) -> str:
    """The one sentence containing offset `at`. A phrase quoted in context makes
    a far better headline — and far better input for the deep pass — than the
    phrase alone ("closing offer: if you want" tells nobody anything)."""
    if at < 0:
        return _flatten(text)[:HEADLINE_MAX]
    start = 0
    end = len(text)
    for char in _SENTENCE_BREAKS:
        before = text.rfind(char, 0, at)
        if before + 1 > start:
            start = before + 1
        after = text.find(char, at)
        if after != -1 and after + 1 < end:
            end = after + 1
    return _flatten(text[start:end])


def _flatten(text: object) -> str:
    """Collapse markdown whitespace — headlines are single-line by contract."""
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def epoch_millis_to_datetime(value: object) -> datetime | None:
    """`~/.claude/sessions/*.json` timestamps are epoch milliseconds, unlike the
    ISO strings inside transcripts. Public because `sources.live_agent_rows`
    reads the same field for the wire — one reader of that fact, not two."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
