package world.voidcraft.tldrack.laya

import java.math.BigDecimal
import java.math.RoundingMode
import java.time.Instant
import java.time.OffsetDateTime
import java.time.format.DateTimeParseException
import kotlin.math.abs
import kotlin.math.max
import kotlinx.serialization.Serializable

// Ports, pure (no Android), of the rack's web review page: ui/src/laya-review/recent-model.ts (marks, grading,
// tally, the list), ui/src/laya-review/turn-context.ts (the conversation behind a verdict), and the answer/undo flow
// of ui/src/laya-review/LayaRecentList.vue. Those files own the reasoning.

/** How a phone-link call came back: its data, or a `Fail` that names the remedy. */
sealed interface Call<out T> {
    data class Ok<out T>(val data: T) : Call<T>
}

sealed interface Fail : Call<Nothing> {
    /** No token pushed, nothing on 7683 (no `adb reverse`: not plugged in, not paired), a 401, or a reply the link did not sign. */
    data object Unlinked : Fail

    /** phone-link answered but the bridge behind it did not (502), or the Mac end dropped the connection. */
    data object BridgeDown : Fail

    /** An answer that says no: its status, code and one sentence, and on answer/undo a 409's own body. */
    data class Refused(
        val status: Int,
        val code: String?,
        val message: String,
        val hint: String? = null,
        val result: ReviewResult? = null,
    ) : Fail
}

/**
 * What the operator says a turn was. The first four are presses; `open` only reads back an OPEN answer recorded
 * without its half (the deck's). `skip` leaves the text out of every count, as on the deck.
 */
enum class Verdict { done, running, you, skip, open }

enum class Grade { right, wrong }

data class BandTally(val n: Int = 0, val reviewed: Int = 0, val right: Int = 0, val open: Int = 0)

data class RecentTally(
    val n: Int,
    val reviewed: Int,
    val skipped: Int,
    val bands: Map<Band, BandTally>,
    /** The OPEN answers split: who the turn was really waiting on. */
    val needs: Map<Needs, Int>,
)

data class BandReadout(
    val band: Band,
    val mark: String,
    val n: Int,
    val text: String,
    /** The sentence version: the readout's accessible name. */
    val title: String,
    /** Amber once a graded ✅ or ⚠️ band is wrong more than `WRONG_WARN` allows; never on 💬. */
    val warn: Boolean,
)

/** A short mono readout and its sentence. */
data class Readout(val text: String, val title: String)

/** `first_user` on a conversation: not known (an older bridge, or a page short of the start), known absent, or the message. */
sealed interface FirstUser {
    data object Unknown : FirstUser

    data object None : FirstUser

    data class Found(val message: HistoryMessage) : FirstUser
}

/** One `/ledger/conversation` page as the wire said it; `first_user` is what the wire SAID, before `context_page` reads it. */
data class LedgerPage(
    val title: String?,
    val cwd: String?,
    val messages: List<HistoryMessage>,
    val first_user: FirstUser,
    val before: Int?,
)

enum class PageMode { before, around }

enum class MissReason { no_text, not_indexed, unavailable }

sealed interface TurnContext {
    data class Found(
        val title: String?,
        val cwd: String?,
        val file_id: Long,
        val closing_seq: Int,
        val messages: List<HistoryMessage>,
        val before: Int?,
        val first_user: FirstUser,
    ) : TurnContext

    data class Missing(val reason: MissReason, val message: String) : TurnContext
}

/** The session ledger, as `turn-context.ts` reads it through the bridge. */
interface LedgerSource {
    suspend fun search(q: String): Call<HistorySearch>

    suspend fun conversation(file_id: Long, mode: PageMode, anchor: Int): Call<LedgerPage>
}

/** The two writes the list makes. */
interface ReviewCalls {
    suspend fun answer(request: AnswerRequest): Call<ReviewResult>

    suspend fun undo(request: UndoRequest): Call<ReviewResult>
}

/**
 * One of this list's answers, as UNDO takes it back: the ROW's key, never the wire answer's (identical; an older
 * bridge's RECENT `operator` omitted it). Serializable, because the undo stack outlives process death (`LayaViewModel`).
 */
@Serializable
data class Answered(val key: String, val answer_id: String)

/** Where the list lands: the band it narrows to, the row selected, and whether that is the row the reader already had. */
data class Landing(val band: Band?, val selected: String?, val same: Boolean)

/**
 * What one press did, as edits for whatever rows are current when it lands: a reload may have replaced the list
 * meanwhile, and an edit by key lands on the new one or on nothing.
 */
data class GradeOutcome(
    val edits: List<Pair<String, AnswerRef?>> = emptyList(),
    /** The new live answer: what this list's UNDO takes back next. */
    val recorded: AnswerRef? = null,
    val failure: Fail? = null,
)

object LayaRules {

    /** The surface answers go to when the bridge names none (`LayaRecentList.vue`'s fallback). */
    const val DEFAULT_SURFACE = "agent_open_work"

    /** The page the web list asks for (`REVIEW_RECENT_DEFAULT`). */
    const val RECENT_LIMIT = 100

    /** The deep link a push or a note opens: `tldrack://review`. */
    const val LINK_SCHEME = "tldrack"
    const val LINK_HOST = "review"

    /** Whether an intent's action and data are that link. Pure, so the manifest's filter has a tested twin. */
    fun is_review_link(action: String?, scheme: String?, host: String?): Boolean =
        action == "android.intent.action.VIEW" && scheme == LINK_SCHEME && host == LINK_HOST

    const val UNLINKED_FIX = "Plug into the Mac — or pair Wireless debugging once"
    const val BRIDGE_DOWN_FIX = "Start the bridge on the Mac"

    /** The rack's own marks, from their owner (`ui/src/config/terminal-status-flags.ts`): the list draws what the operator saw. */
    val BAND_MARK: Map<Band, String> = mapOf(Band.done to "✅", Band.reply to "💬", Band.open to "⚠️")

    val BAND_WORDS: Map<Band, String> = mapOf(
        Band.done to "Laya: the work looks done",
        Band.reply to "Laya: unsure — the plain reply mark",
        Band.open to "Laya: planned work still open",
    )

    // ── answering ────────────────────────────────────────────────────────────

    fun is_open_verdict(verdict: Verdict?): Boolean =
        verdict == Verdict.running || verdict == Verdict.you || verdict == Verdict.open

    /** The `/review/answer` body for a press, against the reference the row (or a fresh card) carries; null when no card holds the text. */
    fun answer_request(key: String, surface: String, verdict: Verdict, reference_open: Boolean?): AnswerRequest? {
        if (reference_open == null) return null
        val answer = when {
            verdict == Verdict.skip -> ReviewAnswer.skip
            is_open_verdict(verdict) == reference_open -> ReviewAnswer.agree
            else -> ReviewAnswer.flip
        }
        val needs = when (verdict) {
            Verdict.running -> Needs.running
            Verdict.you -> Needs.you
            else -> null
        }
        return AnswerRequest(surface = surface, key = key, answer = answer, reference_open = reference_open, needs = needs)
    }

    /** The operator's verdict on a row, read back from its live answer. */
    fun operator_verdict(operator: AnswerRef?): Verdict? {
        val f = operator ?: return null
        val open = f.open
        if (f.answer == ReviewAnswer.skip || open == null) return Verdict.skip
        if (!open) return Verdict.done
        return when (f.needs) {
            Needs.running -> Verdict.running
            Needs.you -> Verdict.you
            null -> Verdict.open
        }
    }

    /** RIGHT or WRONG against the band; a 💬 row said "unsure", so it has no right answer. RUNNING and NEEDS YOU both grade as open. */
    fun grade(row: RecentRow): Grade? {
        val verdict = operator_verdict(row.operator)
        if (verdict == null || verdict == Verdict.skip || row.band == Band.reply) return null
        return if (is_open_verdict(verdict) == (row.band == Band.open)) Grade.right else Grade.wrong
    }

    // ── the strip ────────────────────────────────────────────────────────────

    /** Counted here, not refetched, so an answer moves the strip without reordering the list under the thumb. */
    fun recent_tally(rows: List<RecentRow>): RecentTally {
        val bands = Band.entries.associateWith { BandTally() }.toMutableMap()
        val needs = Needs.entries.associateWith { 0 }.toMutableMap()
        var reviewed = 0
        var skipped = 0
        for (row in rows) {
            val t = bands.getValue(row.band)
            val verdict = operator_verdict(row.operator)
            if (verdict == null || verdict == Verdict.skip) {
                if (verdict == Verdict.skip) skipped += 1
                bands[row.band] = t.copy(n = t.n + 1)
                continue
            }
            reviewed += 1
            bands[row.band] = BandTally(
                n = t.n + 1,
                reviewed = t.reviewed + 1,
                right = t.right + if (grade(row) == Grade.right) 1 else 0,
                open = t.open + if (is_open_verdict(verdict)) 1 else 0,
            )
            if (verdict == Verdict.running) needs[Needs.running] = needs.getValue(Needs.running) + 1
            if (verdict == Verdict.you) needs[Needs.you] = needs.getValue(Needs.you) + 1
        }
        return RecentTally(n = rows.size, reviewed = reviewed, skipped = skipped, bands = bands, needs = needs)
    }

    /** Past this share of wrong answers a ✅ or ⚠️ readout turns amber: one in five is worth a retrain. */
    const val WRONG_WARN = 0.2

    /** `✅ 18/20` — right over graded, or the count drawn before any is graded; 💬 says how the unsure turns split. */
    fun band_readouts(tally: RecentTally): List<BandReadout> = listOf(Band.done, Band.reply, Band.open).map { band ->
        val t = tally.bands.getValue(band)
        val mark = BAND_MARK.getValue(band)
        if (band == Band.reply) {
            val done = t.reviewed - t.open
            BandReadout(
                band = band, mark = mark, n = t.n, warn = false,
                text = if (t.reviewed > 0) "$done done · ${t.open} open" else "${t.n}",
                title = "${t.n} unsure turns; you called $done done and ${t.open} open — what moves the ✅ cut",
            )
        } else {
            val wrong = t.reviewed - t.right
            BandReadout(
                band = band, mark = mark, n = t.n,
                warn = t.reviewed >= 5 && wrong.toDouble() / t.reviewed > WRONG_WARN,
                text = if (t.reviewed > 0) "${t.right}/${t.reviewed}" else "${t.n}",
                title = if (t.reviewed > 0) "${t.n} drawn; ${t.right} of ${t.reviewed} you graded were right" else "${t.n} drawn; none graded yet",
            )
        }
    }

    /** The phone strip's face: 💬 carries its count, and its split moves to the title — the strip has one row of room. */
    fun strip_text(readout: BandReadout): String = if (readout.band == Band.reply) "${readout.n}" else readout.text

    /** `3/12`: of the OPEN turns split, how many really needed the operator. Null until one is split. */
    fun needs_readout(tally: RecentTally): Readout? {
        val you = tally.needs.getValue(Needs.you)
        val running = tally.needs.getValue(Needs.running)
        val split = you + running
        if (split == 0) return null
        return Readout("$you/$split", "Of $split open turns you split, $you needed you and $running were only waiting on their own work")
    }

    // ── the list ─────────────────────────────────────────────────────────────

    /** The narrowing a band readout applies; a null band shows every row. */
    fun visible_rows(rows: List<RecentRow>, band: Band?, unanswered_only: Boolean): List<RecentRow> =
        rows.filter { (band == null || it.band == band) && (!unanswered_only || operator_verdict(it.operator) == null) }

    /** Every row of the answered text takes the new answer: a text judged twice is one key. */
    fun with_answer(rows: List<RecentRow>, key: String, operator: AnswerRef?): List<RecentRow> =
        rows.map { if (it.key == key) it.copy(operator = operator) else it }

    /** After an answer the selection moves to the next unanswered row below it, else stays. */
    fun next_unanswered(rows: List<RecentRow>, from: Int): Int {
        for (i in from + 1 until rows.size) if (operator_verdict(rows[i].operator) == null) return i
        return from
    }

    /**
     * A TO GRADE page (`todo=1`: every answered text left out) with this list's own graded rows put back where their
     * time puts them, so a reload — a return from the pocket, a rotation — never takes away a row graded here, its
     * RIGHT/WRONG, or its place in the tally. Both lists are newest first; the merge keeps the page's own order.
     */
    fun keep_graded(page: List<RecentRow>, current: List<RecentRow>, graded_keys: Set<String>): List<RecentRow> {
        val on_page = page.mapTo(HashSet()) { it.decision_id }
        val kept = current.filter { it.key in graded_keys && it.operator != null && it.decision_id !in on_page }
        if (kept.isEmpty()) return page
        val out = ArrayList<RecentRow>(page.size + kept.size)
        var i = 0
        var j = 0
        while (i < page.size || j < kept.size) {
            val take_kept = i == page.size || (j < kept.size && at_ms(kept[j]) > at_ms(page[i]))
            out += if (take_kept) kept[j++] else page[i++]
        }
        return out
    }

    private fun at_ms(row: RecentRow): Long = epoch_ms(row.at) ?: Long.MIN_VALUE

    /** A page (re)loaded: the row being read when it is still shown, else the first ungraded row, as the web lands. */
    fun landing(rows: List<RecentRow>, band: Band?, selected: String?): Landing {
        val shown = visible_rows(rows, band, unanswered_only = false)
        if (selected != null && shown.any { it.decision_id == selected }) return Landing(band, selected, same = true)
        return Landing(band, shown.getOrNull(max(0, next_unanswered(shown, -1)))?.decision_id, same = false)
    }

    /**
     * Back to an answered text's row (UNDO's promise): under the band when it shows it, else with the band cleared,
     * because the reader asked for that row. Null when no row of the text is on the page.
     */
    fun return_to(rows: List<RecentRow>, band: Band?, key: String): Landing? {
        visible_rows(rows, band, unanswered_only = false).firstOrNull { it.key == key }
            ?.let { return Landing(band, it.decision_id, same = false) }
        return rows.firstOrNull { it.key == key }?.let { Landing(null, it.decision_id, same = false) }
    }

    /** `4m`, `3h`, `2d` — how long ago, in one short unit. */
    fun age_text(at: String, now_ms: Long): String {
        val then = epoch_ms(at) ?: return "—"
        val s = max(0L, Math.round((now_ms - then) / 1000.0))
        return when {
            s < 60 -> "${s}s"
            s < 3_600 -> "${s / 60}m"
            s < 86_400 -> "${s / 3_600}h"
            else -> "${s / 86_400}d"
        }
    }

    /** Within this of a cut a row is an edge case and says so: the sweep grid's own step. */
    const val EDGE_NEAR = 0.05

    /** `0.02 FROM ⚠️` when p falls just short of a mark, `⚠️ BY 0.01` when it only just made it; null away from a cut. */
    fun edge_note(row: RecentRow): Readout? {
        val edge = row.edge ?: return null
        if (abs(edge.delta) > EDGE_NEAR) return null
        val mark = if (edge.cut == Cut.open) BAND_MARK.getValue(Band.open) else BAND_MARK.getValue(Band.done)
        val gap = fixed(abs(edge.delta), 2)
        // ⚠️ is at or over its cut; ✅ is strictly under its own.
        val made_it = if (edge.cut == Cut.open) edge.delta >= 0 else edge.delta < 0
        val cut = fixed(row.p - edge.delta, 2)
        val p = fixed(row.p, 3)
        return if (made_it) {
            Readout("$mark BY $gap", "p $p: drew $mark by $gap past its cut ($cut) — one nudge from the plain 💬")
        } else {
            Readout("$gap FROM $mark", "p $p: $gap short of $mark (its cut is $cut) — one nudge from drawing it")
        }
    }

    /** `0.72`: the open-work score as the web prints it. */
    fun score_text(p: Double): String = fixed(p, 2)

    /** The mark the rack drew, when today's band differs: `was 💬` on a pre-band verdict that would be ✅ now. */
    fun drawn_note(row: RecentRow): String? = if (row.mark == row.band) null else "was ${BAND_MARK.getValue(row.mark)}"

    /** Display names never replace the stable rack identity used by filters and answer requests. */
    fun review_title(subject: String?, rack_title: String?, conversation_title: String?): String =
        rack_title?.trim()?.ifEmpty { null }
            ?: conversation_title?.trim()?.ifEmpty { null }
            ?: subject?.trim()?.ifEmpty { null }
            ?: "Conversation"

    /**
     * The rack's display name for a row's session, only while it is still that session's. `/topic-labels` answers for
     * the tmux name NOW, and tmux hands a `vc-N` name to the next agent, so `shown` names a verdict only while it still
     * IS the newest naming's label and that naming is no newer than the verdict. A placeholder, a `--title` label or a
     * ✎ name logs no naming, so under one of those the naming may be a previous wearer's: the conversation title,
     * then the tmux name, carry the row instead.
     */
    fun rack_name(view: TopicView?, at: String): String? {
        val named = view?.named ?: return null
        val at_ms = epoch_ms(at) ?: return null
        return view.shown?.takeIf { it == named.name && at_ms >= named.at * 1_000 }
    }

    /** A failure as one line beside the bar: the refusal, then its remedy when it named one. */
    fun fail_text(message: String, hint: String?): String = if (hint.isNullOrEmpty()) message else "$message — $hint"

    fun fail_text(fail: Fail): String = when (fail) {
        Fail.Unlinked -> "Unlinked — $UNLINKED_FIX"
        Fail.BridgeDown -> "Bridge down — $BRIDGE_DOWN_FIX"
        is Fail.Refused -> fail_text(fail.message, fail.hint)
    }

    // ── what the routes take (strict on the server, so strict here: a request it would refuse is never sent) ──

    private val REVIEW_KEY = Regex("[0-9a-f]{16}|golden:[a-z0-9][a-z0-9-]{0,63}")
    private val SURFACE = Regex("[a-z][a-z0-9_]{0,63}")
    private val ANSWER_ID = Regex("a_[0-9a-f]{16}")

    fun sendable(request: AnswerRequest): Boolean {
        val records_open = request.answer != ReviewAnswer.skip &&
            (if (request.answer == ReviewAnswer.agree) request.reference_open else !request.reference_open)
        return SURFACE.matches(request.surface) && REVIEW_KEY.matches(request.key) && (request.needs == null || records_open)
    }

    fun sendable(request: UndoRequest): Boolean = SURFACE.matches(request.surface) && ANSWER_ID.matches(request.answer_id)

    // ── one press, as LayaRecentList.vue `answer` and `undo` run it ─────────────

    /** An undo the log already holds, or of an answer it never had: either way the answer is not live. */
    private val GONE = setOf("already_undone", "unknown_answer")

    private fun is_gone(fail: Fail): Boolean = fail is Fail.Refused && fail.code in GONE

    /**
     * One verdict on one row. A changed mind undoes the live answer first (the log keeps both), and a reference
     * that moved meanwhile (`reference_changed`) maps the SAME verdict again: only its agree/flip encoding moves.
     */
    suspend fun grade_row(row: RecentRow, surface: String, verdict: Verdict, calls: ReviewCalls): GradeOutcome {
        if (operator_verdict(row.operator) == verdict) return GradeOutcome()
        val edits = mutableListOf<Pair<String, AnswerRef?>>()
        val live = row.operator
        if (live != null) {
            val undone = calls.undo(UndoRequest(surface = surface, answer_id = live.answer_id))
            if (undone is Fail && !is_gone(undone)) return GradeOutcome(edits, failure = undone)
            edits += row.key to null
        }
        var reference = row.reference_open
        for (attempt in 0 until 2) {
            val request = answer_request(row.key, surface, verdict, reference) ?: return GradeOutcome(edits)
            when (val got = calls.answer(request)) {
                is Call.Ok -> {
                    val recorded = got.data.answer
                        ?: return GradeOutcome(edits, failure = Fail.Refused(200, null, got.data.message))
                    edits += row.key to recorded
                    return GradeOutcome(edits, recorded = recorded)
                }
                is Fail -> {
                    val result = (got as? Fail.Refused)?.result
                    val code = (got as? Fail.Refused)?.code
                    val fresh = result?.card
                    if (code == "reference_changed" && fresh != null) {
                        reference = fresh.reference.open
                        continue
                    }
                    val standing = result?.answer
                    if (code == "already_answered" && standing != null) edits += row.key to standing
                    return GradeOutcome(edits, failure = got)
                }
            }
        }
        return GradeOutcome(edits)
    }

    /** UNDO: null when the answer is no longer live, which is also what an undo the log already holds means. */
    suspend fun take_back(surface: String, answer_id: String, calls: ReviewCalls): Fail? =
        when (val got = calls.undo(UndoRequest(surface = surface, answer_id = answer_id))) {
            is Call.Ok -> null
            is Fail -> if (is_gone(got)) null else got
        }

    // ── the conversation behind a verdict (turn-context.ts) ────────────────────

    /** A needle shorter than this matches too much ("Waiting."); past the cap it gains nothing and risks a fold seam. */
    const val NEEDLE_MIN = 24
    const val NEEDLE_CHARS = 100

    /** A verdict is asked ~1–2 s after its message is written; a quarter hour covers a slow judge and a clock skew. */
    const val MATCH_WINDOW_S = 15 * 60

    const val NOT_INDEXED = "Not in the session index yet: it catches up every 15 minutes, or press REFRESH"
    private const val NO_UNIQUE =
        "No unique conversation match: this card has no live decision time. It may be a written test case, missing from the index, or repeated in several turns."

    /** The first line long enough to be the message's own words, cut to `NEEDLE_CHARS`; null when no line will do. */
    fun closing_needle(text: String): String? {
        for (raw in text.split('\n')) {
            val line = raw.trim()
            if (line.length >= NEEDLE_MIN) return line.take(NEEDLE_CHARS)
        }
        return null
    }

    /** The agent message the verdict judged: the nearest agent hit within `MATCH_WINDOW_S` of the decision. */
    fun pick_hit(hits: List<HistoryHit>, at_iso: String?, more: Boolean = false): HistoryHit? {
        if (at_iso == null) {
            val agents = hits.filter { it.role == "agent" }
            return if (!more && agents.size == 1) agents[0] else null
        }
        val at = (epoch_ms(at_iso) ?: return null) / 1_000.0
        var best: HistoryHit? = null
        var best_gap = Double.MAX_VALUE
        for (hit in hits) {
            val ts = hit.ts ?: continue
            val gap = abs(ts - at)
            if (hit.role != "agent" || gap > MATCH_WINDOW_S) continue
            if (best == null || gap < best_gap) {
                best = hit
                best_gap = gap
            }
        }
        return best
    }

    /** The operator's prompt that started the judged turn: the last user message at or before the closing one. */
    fun ask_seq(messages: List<HistoryMessage>, closing_seq: Int): Int? {
        var found: Int? = null
        for (m in messages) if (is_human_role(m.role) && m.seq <= closing_seq) found = m.seq
        return found
    }

    fun is_human_role(role: String): Boolean = role == "human" || role == "user"

    /** The ledger's title, else the first request's first line — and that only when the page can see the session's start. */
    fun context_title(title: String?, messages: List<HistoryMessage>, before: Int?, first_user: FirstUser): String? {
        if (!title.isNullOrBlank()) return title
        val prompt = (first_user as? FirstUser.Found)?.message?.text
            ?: if (before == null) messages.firstOrNull { is_human_role(it.role) }?.text else null
        return prompt?.trim()?.split(Regex("\r?\n"))?.first()?.take(120)?.ifEmpty { null }
    }

    private fun context_title(context: TurnContext.Found): String? =
        context_title(context.title, context.messages, context.before, context.first_user)

    /** Keep the judged turn's anchor when paging back; later messages never leak into its review. */
    fun prepend_context(current: TurnContext.Found, earlier: TurnContext.Found): TurnContext.Found {
        if (earlier.file_id != current.file_id) return current
        val first = current.messages.firstOrNull()?.seq ?: (current.closing_seq + 1)
        val messages = earlier.messages.filter { it.seq < first }
        if (messages.isEmpty()) return current
        val merged = current.copy(
            messages = messages + current.messages,
            before = earlier.before,
            first_user = if (current.first_user == FirstUser.Unknown) earlier.first_user else current.first_user,
        )
        return merged.copy(title = context_title(merged))
    }

    /** One page, cut at the judged close; what the wire said of `first_user` becomes what can be known of it. */
    fun context_page(page: LedgerPage, file_id: Long, closing_seq: Int): TurnContext.Found {
        val messages = page.messages.filter { it.seq <= closing_seq }
        val first_user = when (val said = page.first_user) {
            FirstUser.Unknown ->
                if (page.before == null) {
                    messages.firstOrNull { is_human_role(it.role) }?.let { FirstUser.Found(it) } ?: FirstUser.None
                } else {
                    FirstUser.Unknown
                }
            FirstUser.None -> FirstUser.None
            is FirstUser.Found -> if (said.message.seq <= closing_seq) said else FirstUser.None
        }
        return TurnContext.Found(
            title = context_title(page.title, messages, page.before, first_user),
            cwd = page.cwd,
            file_id = file_id,
            closing_seq = closing_seq,
            messages = messages,
            before = page.before,
            first_user = first_user,
        )
    }

    /** The conversation behind one verdict: its title, and the messages up to and including the closing one. */
    suspend fun load_turn_context(text: String, at_iso: String?, ledger: LedgerSource): TurnContext {
        val needle = closing_needle(text)
            ?: return TurnContext.Missing(MissReason.no_text, "The closing text is too short to look up")
        val search = when (val found = ledger.search(needle)) {
            is Fail -> return TurnContext.Missing(MissReason.unavailable, fail_text(found))
            is Call.Ok -> found.data
        }
        val hit = pick_hit(search.hits, at_iso, search.next_offset != null)
            ?: return TurnContext.Missing(MissReason.not_indexed, if (at_iso == null) NO_UNIQUE else NOT_INDEXED)
        val page = load_earlier(ledger, hit.file_id, hit.seq + 1)
        if (page !is TurnContext.Found) return page
        var context = page.copy(closing_seq = hit.seq)
        // An older bridge omits first_user: read the start directly, never mistake the end page's latest prompt for it.
        if (context.first_user == FirstUser.Unknown) {
            val start = load_context_page(ledger, hit.file_id, PageMode.around, 0, hit.seq)
            if (start is TurnContext.Found) context = context.copy(first_user = start.first_user, title = context.title ?: start.title)
        }
        return context
    }

    /** One page before `before_seq`; paging back never moves the review's closing anchor. */
    suspend fun load_earlier(ledger: LedgerSource, file_id: Long, before_seq: Int): TurnContext =
        load_context_page(ledger, file_id, PageMode.before, before_seq, before_seq - 1)

    private suspend fun load_context_page(
        ledger: LedgerSource,
        file_id: Long,
        mode: PageMode,
        anchor: Int,
        closing_seq: Int,
    ): TurnContext = when (val got = ledger.conversation(file_id, mode, anchor)) {
        is Call.Ok -> context_page(got.data, file_id, closing_seq)
        is Fail -> TurnContext.Missing(
            if (got is Fail.Refused && got.status == 404) MissReason.not_indexed else MissReason.unavailable,
            fail_text(got),
        )
    }

    // ── shared readers ─────────────────────────────────────────────────────────

    /** `Date.parse` for the bridge's ISO stamps (`Z` or an offset, any fraction); null when it is not one. */
    fun epoch_ms(iso: String): Long? =
        try {
            OffsetDateTime.parse(iso).toInstant().toEpochMilli()
        } catch (error: DateTimeParseException) {
            try {
                Instant.parse(iso).toEpochMilli()
            } catch (again: DateTimeParseException) {
                null
            }
        }

    /** JavaScript's `toFixed`: rounded from the double's exact value, so a near-tie rounds the way the web's does. */
    private fun fixed(value: Double, digits: Int): String =
        BigDecimal(value).setScale(digits, RoundingMode.HALF_UP).toPlainString()
}
