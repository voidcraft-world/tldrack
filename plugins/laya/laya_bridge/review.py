"""The REVIEW deck's six routes and the agent rack's one (`rack`), as schemas/src/laya-review.ts states them.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".

Every handler returns `(status, body)` in the policy envelope (`envelope.py` owns it). Requests are
strict: exactly the documented fields, a bool is a bool, and a refused value is never echoed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from laya_bridge import files, lab_files, ledger, review_data, review_log, rows
from laya_bridge.envelope import Refusal, Response, error_response, ok_body, params_unavailable, refusal_response
from laya_bridge.review_surfaces import REVIEW_SURFACES

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
LIMIT = re.compile(r"[0-9]{1,2}")
RECENT_LIMIT = re.compile(r"[0-9]{1,3}")
#: `since` on /review/recent: whole epoch seconds, from 2001 to 2286 (ten digits).
SINCE = re.compile(r"[1-9][0-9]{9}")
#: `subject` on /review/recent: the session's name now, then up to this many it had before, comma-joined.
MAX_SUBJECTS = 9
#: `offset` on /review/recent: how many of the ordered pool to skip (`review_data.recent_rows` owns why).
#: TS twin: schemas/src/laya-review.ts REVIEW_RECENT_OFFSET_MAX.
RECENT_OFFSET = re.compile(r"[0-9]{1,6}")
RECENT_OFFSET_MAX = 100_000

#: A `reviewResultSchema` refusal still carries its three result fields, each null.
NO_RESULT: dict[str, None] = {"answer": None, "card": None, "stats": None}
#: A `reviewRetestResultSchema` refusal, likewise.
NO_RETEST: dict[str, None] = {"retest": None, "stats": None}


def first_fine_tune(base: Path | None) -> dict:
    """`/status`'s road to a first fine-tune: owned by review_data.py — see the note there."""
    return review_data.first_fine_tune(base)


def bad_request(code: str, message: str) -> Refusal:
    """A refused query or body: the message names the field, never its value."""
    return Refusal(400, code, message)


def params_ready(base: Path | None) -> bool:
    return files.is_real_dir(base)


def unavailable() -> Response:
    return refusal_response(params_unavailable())


# ── requests ──────────────────────────────────────────────────────────────────


def _query_value(query: Mapping[str, Any], name: str) -> str | None:
    values = query.get(name)
    if values is None:
        return None
    if isinstance(values, str):
        return values
    if not isinstance(values, (list, tuple)) or len(values) != 1 or not isinstance(values[0], str):
        raise bad_request("bad_query", f"{name} must be given once")
    return values[0]


def _check_query(query: Mapping[str, Any] | None, allowed: tuple[str, ...]) -> Mapping[str, Any]:
    query = query or {}
    for name in query:
        if name not in allowed:
            raise bad_request("bad_query", f"only {', '.join(allowed)} may be asked")
    return query


def _surface(value: Any, code: str) -> str:
    if not isinstance(value, str) or value not in REVIEW_SURFACES:
        raise bad_request(code, f"surface must be one of {', '.join(REVIEW_SURFACES)}")
    return value


def _key(value: Any, code: str) -> str:
    if not isinstance(value, str) or not review_log.REVIEW_KEY.fullmatch(value):
        raise bad_request(code, "key is not a review key")
    return value


def _body(body: Any, fields: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict:
    if not isinstance(body, dict):
        raise bad_request("bad_body", "the body must be a JSON object")
    if not set(fields) <= set(body) <= set(fields) | set(optional):
        extra = f" (and optionally {', '.join(optional)})" if optional else ""
        raise bad_request("bad_body", f"the body must hold exactly {', '.join(fields)}{extra}")
    return body


# ── handlers ──────────────────────────────────────────────────────────────────


def _stats_all(
    base: Path, loaded: Mapping[str, str] | None, now: datetime | None
) -> list[tuple[str, review_data.Snapshot, dict]]:
    out = []
    for name in REVIEW_SURFACES:
        snap = review_data.snapshot(base, name, loaded=loaded)
        out.append((name, snap, review_data.stats(snap, base, loaded=loaded, now=now)))
    return out


def queue(
    base: Path | None, query: Mapping[str, Any] | None, *, loaded: Mapping[str, str] | None = None, now: datetime | None = None
) -> Response:
    """GET /review/queue: every surface's stats, and up to `limit` cards of one surface. The mix (no `stratum`)
    also carries the blind retests that page deals (`review_data.retest_cards`); a narrowed deal is only its lane."""
    try:
        query = _check_query(query, ("surface", "limit", "stratum"))
        raw_surface = _query_value(query, "surface")
        surface = None if raw_surface is None else _surface(raw_surface, "bad_query")
        raw_limit = _query_value(query, "limit")
        if raw_limit is not None and (not LIMIT.fullmatch(raw_limit) or int(raw_limit) > MAX_LIMIT):
            raise bad_request("bad_query", f"limit must be a whole number from 0 to {MAX_LIMIT}")
        limit = DEFAULT_LIMIT if raw_limit is None else int(raw_limit)
        stratum = _query_value(query, "stratum")
        if stratum is not None and stratum not in review_data.STRATA:
            raise bad_request("bad_query", f"stratum must be one of {', '.join(review_data.STRATA)}")
    except Refusal as refusal:
        return refusal_response(refusal)
    if not params_ready(base):
        return unavailable()
    assert base is not None
    every = _stats_all(base, loaded, now)
    if surface is None:  # the surface with the most to review; the first on a tie
        surface = max(every, key=lambda entry: entry[2]["to_review"])[0]
    snap = next(snap for name, snap, _stats in every if name == surface)
    cards = review_data.queue(snap, limit, stratum) if limit else []
    retests = review_data.retest_cards(snap, limit, now) if stratum is None else []
    head = next(entry_stats for name, _snap, entry_stats in every if name == surface)
    return 200, ok_body(
        f"{head['to_review']} to review",
        surfaces=[entry_stats for _n, _s, entry_stats in every],
        surface=surface,
        cards=cards,
        retests=retests,
        sources=review_data.sources(snap),
        generated_at=rows.utc_stamp(now),
    )


def recent(
    base: Path | None, query: Mapping[str, Any] | None, *, loaded: Mapping[str, str] | None = None, now: datetime | None = None
) -> Response:
    """GET /review/recent: live verdicts of one surface with the operator's answers — the newest, or with
    `order=edge` the ones nearest a cut; `todo=1` leaves answered texts out before the page is cut.
    `subject` + `since` (epoch seconds) narrow it to one rack session, and `after` (the last page's
    `summary.next`) pages on through the same ordering, `offset` rows further when asked
    (`review_data.recent_rows` owns why)."""
    try:
        query = _check_query(query, ("surface", "limit", "order", "todo", "subject", "since", "offset", "after"))
        raw_surface = _query_value(query, "surface")
        surface = next(iter(REVIEW_SURFACES)) if raw_surface is None else _surface(raw_surface, "bad_query")
        raw_limit = _query_value(query, "limit")
        if raw_limit is not None and (not RECENT_LIMIT.fullmatch(raw_limit) or not 1 <= int(raw_limit) <= review_data.RECENT_MAX):
            raise bad_request("bad_query", f"limit must be a whole number from 1 to {review_data.RECENT_MAX}")
        limit = review_data.RECENT_DEFAULT if raw_limit is None else int(raw_limit)
        order = _query_value(query, "order") or "newest"
        if order not in review_data.RECENT_ORDERS:
            raise bad_request("bad_query", f"order must be one of {', '.join(review_data.RECENT_ORDERS)}")
        raw_todo = _query_value(query, "todo")
        if raw_todo not in (None, "0", "1"):
            raise bad_request("bad_query", "todo must be 0 or 1")
        todo = raw_todo == "1"
        raw_subject = _query_value(query, "subject")
        subject = None if raw_subject is None else tuple(raw_subject.split(","))
        if subject is not None and (len(subject) > MAX_SUBJECTS or not all(ledger.SUBJECT.fullmatch(name) for name in subject)):
            raise bad_request("bad_query", f"subject must be up to {MAX_SUBJECTS} rack session names, comma-joined")
        raw_since = _query_value(query, "since")
        if raw_since is not None and not SINCE.fullmatch(raw_since):
            raise bad_request("bad_query", "since must be whole epoch seconds")
        since = None if raw_since is None else rows.utc_stamp(datetime.fromtimestamp(int(raw_since), timezone.utc))
        raw_offset = _query_value(query, "offset")
        if raw_offset is not None and (not RECENT_OFFSET.fullmatch(raw_offset) or int(raw_offset) > RECENT_OFFSET_MAX):
            raise bad_request("bad_query", f"offset must be a whole number from 0 to {RECENT_OFFSET_MAX}")
        offset = 0 if raw_offset is None else int(raw_offset)
        after = _query_value(query, "after")
        if after is not None and review_data.parse_recent_cursor(after, order) is None:
            raise bad_request("bad_query", "after must be the summary.next of a page in the same order")
    except Refusal as refusal:
        return refusal_response(refusal)
    if not params_ready(base):
        return unavailable()
    assert base is not None
    snap = review_data.snapshot(base, surface, loaded=loaded)
    found, summary = review_data.recent_rows(snap, limit, order, todo, subject=subject, since=since, offset=offset, after=after)
    return 200, ok_body(
        f"{len(found)} {'nearest a cut' if order == 'edge' else 'recent'}",
        surface=surface,
        rows=found,
        summary=summary,
        stats=review_data.stats(snap, base, loaded=loaded, now=now),
        sources=review_data.sources(snap),
        generated_at=rows.utc_stamp(now),
    )


def item(base: Path | None, query: Mapping[str, Any] | None, *, loaded: Mapping[str, str] | None = None) -> Response:
    """GET /review/item: one card by key, answered or not."""
    try:
        query = _check_query(query, ("surface", "key"))
        surface = _surface(_query_value(query, "surface"), "bad_query")
        key = _key(_query_value(query, "key"), "bad_query")
    except Refusal as refusal:
        return refusal_response(refusal, card=None)
    if not params_ready(base):
        return unavailable()
    assert base is not None
    found = review_data.card(review_data.snapshot(base, surface, loaded=loaded), key)
    if found is None:
        return error_response(409, "unknown_item", "No current source holds that item", card=None)
    return 200, ok_body("ok", card=found)


def _wave(value: Any) -> tuple[str, ...]:
    """`wave` on /review/answer: the riders the card showed, each a distinct text key; a card holds its lead too."""
    most = review_data.WAVE_MAX - 1
    if not isinstance(value, list) or len(value) > most or len(set(map(str, value))) != len(value):
        raise bad_request("bad_body", f"wave must be a list of up to {most} distinct keys")
    for member in value:
        if not isinstance(member, str) or not lab_files.KEY.fullmatch(member):
            raise bad_request("bad_body", "wave holds a key that is not a text key")
    return tuple(value)


def answer(base: Path | None, body: Any, *, loaded: Mapping[str, str] | None = None, now: datetime | None = None) -> Response:
    """POST /review/answer: record AGREE, FLIP or SKIP against the reference the deck showed, and on an OPEN
    answer optionally whether the turn needed the operator (`needs`, schemas/src/laya-review.ts owns why).

    `wave`: the riders a wave card showed (`review_data.py` owns waves). The press records the verdict it means on
    the lead (`open`, from AGREE or FLIP against the lead's reference) on every rider too, each as agree or flip
    against its OWN reference, under one `wave_id`. It must name exactly the riders the card has now: one answered
    elsewhere, or a turn that joined the run, is `wave_changed` with the fresh card, and nothing is recorded. The
    press is written whole or not at all (`review_log.append_many`)."""
    try:
        body = _body(body, ("surface", "key", "answer", "reference_open"), optional=("needs", "wave"))
        surface = _surface(body["surface"], "bad_body")
        key = _key(body["key"], "bad_body")
        wave = _wave(body["wave"]) if "wave" in body else ()
        if body["answer"] not in REVIEW_SURFACES[surface].answers:
            raise bad_request("bad_body", f"answer must be one of {', '.join(REVIEW_SURFACES[surface].answers)}")
        if not isinstance(body["reference_open"], bool):
            raise bad_request("bad_body", "reference_open must be true or false")
        needs = body.get("needs")
        if needs is not None:
            if needs not in REVIEW_SURFACES[surface].needs:
                raise bad_request("bad_body", "needs is not one this surface takes")
            records_open = body["reference_open"] if body["answer"] == "agree" else not body["reference_open"]
            if body["answer"] == "skip" or not records_open:
                raise bad_request("bad_body", "needs rides only on an answer that records OPEN")
    except Refusal as refusal:
        return refusal_response(refusal, **NO_RESULT)
    if not params_ready(base):
        return unavailable()
    assert base is not None
    try:
        with review_log.writing(base, surface):
            snap = review_data.snapshot(base, surface, loaded=loaded)
            found = review_data.card(snap, key)
            if found is None:
                return error_response(409, "unknown_item", "No current source holds that item: nothing was recorded", **NO_RESULT)
            live = snap.fold.live_answer(key) if snap.fold.answered(key) else None
            if live is not None:
                return error_response(
                    409,
                    "already_answered",
                    "That item is already answered: undo it first",
                    answer=review_log.answer_ref(live),
                    card=None,
                    stats=None,
                )
            if found["reference"]["open"] != body["reference_open"]:
                return error_response(
                    409,
                    "reference_changed",
                    "The reference changed since the card was shown",
                    answer=None,
                    card=found,
                    stats=None,
                )
            if wave and set(wave) != set(review_data.wave_members(snap, key)):
                return error_response(
                    409,
                    "wave_changed",
                    "The wave changed since the card was shown: nothing was recorded",
                    answer=None,
                    card=found,
                    stats=None,
                )
            at = rows.utc_stamp(now)
            wave_id = review_log.new_wave_id() if wave else None
            records_open = (
                None
                if body["answer"] == "skip"
                else (body["reference_open"] if body["answer"] == "agree" else not body["reference_open"])
            )
            written = []
            for member in (key, *review_data.wave_members(snap, key)) if wave else (key,):
                card = found if member == key else review_data.card(snap, member)
                assert card is not None  # a rider is an item of this snapshot
                verdict = (
                    body["answer"]
                    if member == key or records_open is None
                    else ("agree" if card["reference"]["open"] == records_open else "flip")
                )
                against = review_data.against_of(snap, member)
                assert against is not None
                written.append(
                    review_log.answer_row(
                        answer_id=review_log.new_answer_id(),
                        at=at,
                        surface=surface,
                        key=member,
                        stratum=card["stratum"],
                        answer=verdict,
                        against=against,
                        laya=review_data.laya_of(snap, member),
                        decision_id=card["decision"]["decision_id"] if card["decision"] else None,
                        held_out=card["held_out"],
                        needs=needs,
                        wave_id=wave_id,
                        challenger=card["challenger"] if card["stratum"] == "challenger" else None,
                    )
                )
            review_log.append_many(base, surface, written, snap.fold)
            after = review_data.snapshot(base, surface, loaded=loaded)
    except Refusal as refusal:
        return refusal_response(refusal, **NO_RESULT)
    return 200, ok_body(
        f"recorded ×{len(written)}" if wave else "recorded",
        answer=review_log.answer_ref(written[0]),
        card=None,
        stats=review_data.stats(after, base, loaded=loaded, now=now),
    )


def retest(base: Path | None, body: Any, *, loaded: Mapping[str, str] | None = None, now: datetime | None = None) -> Response:
    """POST /review/retest {surface, key, of, open, needs?}: the operator's second, blind look at a text they graded
    (`review_data.py`: THE BLIND RETEST). `open` is true, false, or null for "cannot say"; `needs` rides only on true.

    Written as a `retest` row, never an answer (`review_log.py` owns why), so the label, every count and every other
    κ stay exactly as they were. `of` must still be the text's live label (409 `retest_stale`: it was undone or
    replaced since the card was dealt), not yet retested (409 `already_retested`), and due (409 `not_due`): the
    rule is checked here again, so a page cannot retest a label of its own choosing or one the operator just gave."""
    try:
        body = _body(body, ("surface", "key", "of", "open"), optional=("needs",))
        surface = _surface(body["surface"], "bad_body")
        key = _key(body["key"], "bad_body")
        of = body["of"]
        if not isinstance(of, str) or not review_log.ANSWER_ID.fullmatch(of):
            raise bad_request("bad_body", "of is not an answer id")
        said = body["open"]
        if said is not None and not isinstance(said, bool):
            raise bad_request("bad_body", "open must be true, false or null")
        needs = body.get("needs")
        if needs is not None and (needs not in REVIEW_SURFACES[surface].needs or said is not True):
            raise bad_request("bad_body", "needs rides only on an OPEN retest, on a surface that takes it")
    except Refusal as refusal:
        return refusal_response(refusal, **NO_RETEST)
    if not params_ready(base):
        return unavailable()
    assert base is not None
    try:
        with review_log.writing(base, surface):
            snap = review_data.snapshot(base, surface, loaded=loaded)
            label = snap.fold.labels.get(key)
            if label is None or label["answer_id"] != of:
                return error_response(
                    409, "retest_stale", "That label changed since the card was dealt: nothing was recorded", **NO_RETEST
                )
            if snap.fold.retest_of(of) is not None:
                return error_response(409, "already_retested", "That label already has its retest: undo it first", **NO_RETEST)
            if key not in review_data.retest_due(snap, now):
                return error_response(409, "not_due", "That label is not due a retest: nothing was recorded", **NO_RETEST)
            written = review_log.retest_row(
                answer_id=review_log.new_answer_id(),
                at=rows.utc_stamp(now),
                surface=surface,
                key=key,
                of=of,
                said=said,
                needs=needs,
            )
            review_log.append(base, surface, written, snap.fold)
            after = review_data.snapshot(base, surface, loaded=loaded)
    except Refusal as refusal:
        return refusal_response(refusal, **NO_RETEST)
    return 200, ok_body(
        "recorded", retest=review_log.retest_ref(written), stats=review_data.stats(after, base, loaded=loaded, now=now)
    )


def rack(base: Path | None, body: Any, *, loaded: Mapping[str, str] | None = None, now: datetime | None = None) -> Response:
    """POST /review/rack {decision_id, open}: the operator's answer to one live verdict, from the ⚠️'s own menu on the
    agent rack (`review_data.py`: THE RACK'S DOOR). In process only: `plugin.py` owns who may call it.

    The row is recorded against THAT decision's own p and cut, never the card's reference: the operator answered the
    mark the rack drew. ✗ NOT OPEN on a ⚠️ is therefore a flip to DONE and ✓ STILL OPEN an agree, whatever Sonnet
    said about the text. Stratum `review_data.RACK_STRATUM`. A text with a live OPEN or DONE answer keeps it
    (409 `already_answered`, with it): the deck's rule, and the first look is the careful one.

    A SKIP is not such an answer: it says the operator passed on the card, and this press is them answering after
    all. The skip is undone and the rack row written in one write (`review_log.append_many`), so the key keeps one
    live answer and the press is the label. Until 2026-11-09 a skipped text answered 409 like a graded one, and
    the bridge told the rack "graded" while no OPEN or DONE row stood for precision, the sweeps or a retrain."""
    try:
        body = _body(body, ("decision_id", "open"))
        decision_id = body["decision_id"]
        if not isinstance(decision_id, str) or not ledger.DECISION_ID.fullmatch(decision_id):
            raise bad_request("bad_body", "decision_id is not a decision id")
        if not isinstance(body["open"], bool):
            raise bad_request("bad_body", "open must be true or false")
    except Refusal as refusal:
        return refusal_response(refusal, **NO_RESULT)
    if not params_ready(base):
        return unavailable()
    assert base is not None
    unknown = error_response(409, "unknown_decision", "No live verdict has that id: nothing was recorded", **NO_RESULT)
    surface = next(
        (name for name in REVIEW_SURFACES if decision_id in review_data.snapshot(base, name, loaded=loaded).items.verdicts), None
    )
    if surface is None:
        return unknown
    try:
        with review_log.writing(base, surface):
            snap = review_data.snapshot(base, surface, loaded=loaded)
            verdict = snap.items.verdicts.get(decision_id)
            if verdict is None:
                return unknown
            found = review_data.card(snap, verdict.key)
            if found is None:
                return error_response(
                    409, "unknown_item", "The ledger kept no text for that verdict: nothing was recorded", **NO_RESULT
                )
            live = snap.fold.labels.get(verdict.key)
            if live is not None:
                return error_response(
                    409,
                    "already_answered",
                    "That text is already answered",
                    answer=review_log.answer_ref(live),
                    card=None,
                    stats=None,
                )
            at = rows.utc_stamp(now)
            # No label stands, so every live row of this key is a skip (or sits under one): each is taken back.
            skips = [
                review_log.undo_row(answer_id=row["answer_id"], at=at, surface=surface)
                for row in snap.fold.live.values()
                if row["key"] == verdict.key
            ]
            laya = snap.items.decision_laya[decision_id]
            written = review_log.answer_row(
                answer_id=review_log.new_answer_id(),
                at=at,
                surface=surface,
                key=verdict.key,
                stratum=review_data.RACK_STRATUM,
                answer="agree" if verdict.flagged == body["open"] else "flip",
                against={"by": laya["model"], "open": verdict.flagged},
                laya={k: laya[k] for k in ("p", "threshold", "source", "checkpoint_path")},
                decision_id=decision_id,
                held_out=found["held_out"],
            )
            review_log.append_many(base, surface, [*skips, written], snap.fold)
    except Refusal as refusal:
        return refusal_response(refusal, **NO_RESULT)
    return 200, ok_body("recorded", answer=review_log.answer_ref(written), card=None, stats=None)


def undo(base: Path | None, body: Any, *, loaded: Mapping[str, str] | None = None, now: datetime | None = None) -> Response:
    """POST /review/undo: append an undo row; the item goes back in the queue. With `wave: true`, every live
    answer the same press wrote is undone with it (one UNDO for one wave card); without it, only the one named,
    so a RECENT regrade of one wave member never takes its siblings back.

    `card` is the card the deck now deals holding what came back (`review_data.holding_card`): a wave's lead, whole,
    whichever row of the press was named. After a reload the deck names `stats.last_answer_id`, the press's LAST row,
    a rider, and that rider's own card would grade one turn alone.

    A retest's id is undone the same way (`review_log.py`: RETEST): its text is due again, and `answer` and `card`
    are null, because a retest is neither a live answer nor a deck card. The deck's next queue read deals it back."""
    try:
        body = _body(body, ("surface", "answer_id"), optional=("wave",))
        surface = _surface(body["surface"], "bad_body")
        answer_id = body["answer_id"]
        if not isinstance(answer_id, str) or not review_log.ANSWER_ID.fullmatch(answer_id):
            raise bad_request("bad_body", "answer_id is not an answer id")
        whole_wave = body.get("wave", False)
        if not isinstance(whole_wave, bool):
            raise bad_request("bad_body", "wave must be true or false")
    except Refusal as refusal:
        return refusal_response(refusal, **NO_RESULT)
    if not params_ready(base):
        return unavailable()
    assert base is not None
    try:
        with review_log.writing(base, surface):
            _code, current = review_log.read_fold(base, surface)
            if answer_id in current.undone:
                return error_response(409, "already_undone", "That answer is already undone", **NO_RESULT)
            row = current.live.get(answer_id)
            if row is None and answer_id in current.retests:
                review_log.append(
                    base, surface, review_log.undo_row(answer_id=answer_id, at=rows.utc_stamp(now), surface=surface), current
                )
                after = review_data.snapshot(base, surface, loaded=loaded)
                return 200, ok_body(
                    "retest undone", answer=None, card=None, stats=review_data.stats(after, base, loaded=loaded, now=now)
                )
            if row is None:
                return error_response(409, "unknown_answer", "No answer has that id", **NO_RESULT)
            undone = current.wave_rows(row) if whole_wave else [row]
            at = rows.utc_stamp(now)
            review_log.append_many(
                base,
                surface,
                [review_log.undo_row(answer_id=gone["answer_id"], at=at, surface=surface) for gone in undone],
                current,
            )
            after = review_data.snapshot(base, surface, loaded=loaded)
    except Refusal as refusal:
        return refusal_response(refusal, **NO_RESULT)
    return 200, ok_body(
        f"undone ×{len(undone)}" if len(undone) > 1 else "undone",
        answer=review_log.answer_ref(row),
        card=review_data.holding_card(after, [gone["key"] for gone in undone]),
        stats=review_data.stats(after, base, loaded=loaded, now=now),
    )
