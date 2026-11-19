package world.voidcraft.tldrack.laya

import java.time.Instant
import java.time.ZoneId
import java.time.ZonedDateTime
import kotlin.math.max
import kotlin.math.min

// A port, pure (no Android), of ui/src/laya-review/thread-model.ts (what a conversation is, the order the pager walks
// them in, where a grade moves on to, a conversation's head) and of the join's ask in ui/src/laya-review/turn-context.ts
// (`threads_urls`, `merge_threads`, `wake_note`). Those files own the reasoning: change them first, then this.
//
// What the phone leaves out, on purpose: EDGE order (it reads newest first only), the deck's card heads, and the
// monogram and hue (the web marks a conversation in a list of them; here one fills the screen).

enum class BandKind { thread, unlinked }

/** One conversation on the page: its turns oldest first, as it happened. */
data class ThreadBand(
    /** The conversation's identity: `t:<thread>`, or an unlinked run's `u:<rack name>@<its oldest turn>`. */
    val key: String,
    val kind: BandKind,
    /** The thread's header from the join, once the ledger knows it. */
    val thread: HistoryThread?,
    val rows: List<RecentRow>,
    /** The rack name on the newest turn. */
    val subject: String?,
    /** The rack name, when an older conversation on this page held it too. */
    val new_in_rack: String?,
    /** The join answered, but the ledger has not caught up with this conversation yet. */
    val indexing: Boolean,
    /** Epoch ms of the oldest and newest turn shown. */
    val span: Pair<Long, Long>,
)

/** A grade, as the pager needs it to move on: the conversation's turns before the answer and its key, the graded turn,
 *  and where the conversation stood. */
data class GradedAt(val ids: List<String>, val key: String?, val row_id: String, val row_at: String, val at: Int)

/** Where a grade moves the cursor (thread-model.ts `AfterGrade`). */
sealed interface AfterGrade {
    data class Same(val band: Int, val row: RecentRow) : AfterGrade

    data class Next(val band: Int, val back: Boolean) : AfterGrade

    data class Behind(val band: Int, val held: Boolean) : AfterGrade

    data class None(val held: Boolean) : AfterGrade
}

/** Where the pager stood: the cursor turn, and the conversation last shown (its turns, its key, its place). */
data class PagerPlace(val cursor: String?, val ids: List<String>, val key: String?, val last: Int)

/** A conversation's turns under the prompt each answered. */
data class TurnGroup(
    val key: String,
    /** The operator's prompt, else the opening message of a turn the rack or a lead opened; null for an unlinked turn. */
    val anchor: HistoryClip?,
    val by_operator: Boolean,
    val woke_by: String?,
    /** Which of the operator's prompts in its session file the anchor is, and the file's count of them. */
    val ordinal: Int?,
    val of: Int?,
    /** Operator prompts between the previous group's and this one's: none of their turns is on the page. */
    val skipped: Int,
    val rows: List<RecentRow>,
)

/** What the needle fallback found for a conversation the join did not name. */
data class FoundHead(val title: String?, val first_user: String?)

data class HeadTag(val text: String, val title: String)

data class ThreadHead(
    val title: String,
    /** The session it ran in (the rack's display name, else the tmux name); null where the title is already that. */
    val subject: String?,
    /** `4 turns · 2 to grade · started 2h`. */
    val meta: String,
    val first_user: String?,
    /** The bridge sent only the first message's head. */
    val first_clipped: Boolean,
    val tags: List<HeadTag>,
)

/** A turn the operator did not open, explained where it is graded. */
data class WakeNote(val tag: String, val line: String)

/** What one page asks the join about. */
data class ThreadsAsk(val decisions: List<String>, val conversations: List<String>)

object LayaThreads {

    /** Turns of one rack name further apart than this are not assumed to be one conversation. */
    const val UNLINKED_GAP_S = 30 * 60

    /** Ids per call, all kinds together (`HISTORY_THREADS_MAX`): one call for a full RECENT page. */
    const val THREADS_MAX = 100

    private fun at_ms(row: RecentRow): Long = LayaRules.epoch_ms(row.at) ?: 0L

    // ── the join's ask ───────────────────────────────────────────────────────

    private val DECISION_ID = Regex("\\d{8}T\\d{6}-[0-9a-f]{12}")
    private val CONVERSATION_ID = Regex("[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

    /** Every verdict, and every raw conversation (for a fresh verdict the links have not reached, its thread). */
    fun threads_ask_of(rows: List<RecentRow>): ThreadsAsk =
        ThreadsAsk(rows.map { it.decision_id }, rows.mapNotNull { it.conversation }.distinct())

    /**
     * The queries for an ask: at most `max` ids a call, all kinds together, each kind ONE comma-joined parameter
     * (`d`, then `c`). An id of another shape is never sent: the shapes are what make the raw commas safe.
     */
    fun threads_queries(ask: ThreadsAsk, max: Int = THREADS_MAX): List<List<Pair<String, String>>> {
        val ids = ask.decisions.distinct().filter(DECISION_ID::matches).map { "d" to it } +
            ask.conversations.distinct().filter(CONVERSATION_ID::matches).map { "c" to it }
        return ids.chunked(max).map { chunk ->
            listOf("d", "c").mapNotNull { name ->
                chunk.filter { it.first == name }.takeIf { it.isNotEmpty() }?.let { list -> name to list.joinToString(",") { it.second } }
            }
        }
    }

    /** Later answers win, id by id. */
    fun merge_threads(a: HistoryThreads, b: HistoryThreads): HistoryThreads =
        HistoryThreads(a.decisions + b.decisions, a.conversations + b.conversations, a.threads + b.threads)

    /** Not linked YET: the ledger has not ingested the verdict or its turn (turn-context.ts `awaits_ingest`). */
    private fun awaits_ingest(turn: HistoryTurn): Boolean = turn.status == "pending" || turn.status == "missing"

    // ── conversations ────────────────────────────────────────────────────────

    /** The thread a row belongs to, or null when nothing ties it to one. */
    fun thread_key(row: RecentRow, join: HistoryThreads?): String? {
        join?.decisions?.get(row.decision_id)?.thread?.let { return it }
        val raw = row.conversation ?: return null
        return join?.conversations?.get(raw) ?: raw
    }

    private data class Identity(val key: String, val kind: BandKind)

    /** Every row's identity: its thread, or its unlinked run (rack name + gap, walked newest first). */
    private fun row_identities(rows: List<RecentRow>, join: HistoryThreads?): Map<String, Identity> {
        val out = LinkedHashMap<String, Identity>()
        val loose = LinkedHashMap<String, MutableList<RecentRow>>()
        for (row in rows) {
            val key = thread_key(row, join)
            if (key != null) out[row.decision_id] = Identity("t:$key", BandKind.thread)
            else loose.getOrPut(row.subject.orEmpty()) { mutableListOf() } += row
        }
        for ((subject, list) in loose) {
            val sorted = list.sortedByDescending(::at_ms)
            var run = mutableListOf<RecentRow>()
            fun close() {
                // Keyed by the run's OLDEST turn, so a fresh verdict joining it keeps its key.
                val identity = Identity("u:$subject@${at_ms(run.last())}", BandKind.unlinked)
                for (r in run) out[r.decision_id] = identity
                run = mutableListOf()
            }
            for (row in sorted) {
                if (run.isNotEmpty() && at_ms(run.last()) - at_ms(row) > UNLINKED_GAP_S * 1_000L) close()
                run += row
            }
            if (run.isNotEmpty()) close()
        }
        return out
    }

    /** Per conversation, the rack name it was the NEWER conversation in. Unlinked runs neither mark nor get marked. */
    private fun new_in_rack(rows: List<RecentRow>, ids: Map<String, Identity>, join: HistoryThreads?): Map<String, String> {
        val start = LinkedHashMap<String, Long>()
        val by_rack = LinkedHashMap<String, LinkedHashSet<String>>()
        for (row in rows) {
            val id = ids[row.decision_id] ?: continue
            if (id.kind != BandKind.thread) continue
            start[id.key] = min(start[id.key] ?: Long.MAX_VALUE, at_ms(row))
            row.subject?.takeIf { it.isNotEmpty() }?.let { by_rack.getOrPut(it) { LinkedHashSet() } += id.key }
        }
        for (key in start.keys.toList()) {
            join?.threads?.get(key.drop(2))?.started_at?.let { start[key] = (it * 1_000).toLong() }
        }
        val marks = LinkedHashMap<String, String>()
        for ((rack, set) in by_rack) {
            if (set.size < 2) continue
            for (key in set.sortedBy { start[it] ?: 0L }.drop(1)) marks.putIfAbsent(key, rack)
        }
        return marks
    }

    /**
     * The page's conversations, newest first by their newest turn. `join` null: no join (yet, or a bridge without
     * one); `join_ok` false while one is in flight, when the conversations keep the last join but claim nothing about
     * indexing.
     */
    fun thread_bands(rows: List<RecentRow>, join: HistoryThreads?, join_ok: Boolean = join != null): List<ThreadBand> {
        val ids = row_identities(rows, join)
        val marks = new_in_rack(rows, ids, join)
        val groups = LinkedHashMap<String, MutableList<RecentRow>>()
        for (row in rows) groups.getOrPut(ids.getValue(row.decision_id).key) { mutableListOf() } += row
        return groups.map { (key, list) ->
            val kind = ids.getValue(list[0].decision_id).kind
            val thread = if (kind == BandKind.thread) join?.threads?.get(key.drop(2)) else null
            val times = list.map(::at_ms)
            ThreadBand(
                key = key,
                kind = kind,
                thread = thread,
                // Oldest first: a conversation reads in the order it happened. The sort is stable, so a tie keeps the server's order.
                rows = list.sortedBy(::at_ms),
                subject = list.reduce { newest, r -> if (at_ms(r) >= at_ms(newest)) r else newest }.subject,
                new_in_rack = marks[key],
                indexing = join != null && join_ok && (
                    if (kind == BandKind.thread) thread == null
                    else list.any { r -> join.decisions[r.decision_id]?.let(::awaits_ingest) == true }
                ),
                span = times.min() to times.max(),
            )
        }.sortedByDescending { it.span.second }
    }

    // ── the pager: where a conversation opens, and where grading moves on to ──

    private fun ungraded(row: RecentRow): Boolean = LayaRules.operator_verdict(row.operator) == null

    /** Turns still to grade in a conversation. */
    fun todo_count(band: ThreadBand): Int = band.rows.count(::ungraded)

    /** The turn a conversation opens on: its first ungraded turn, else its newest. */
    fun opening_row(band: ThreadBand): RecentRow? = band.rows.firstOrNull(::ungraded) ?: band.rows.lastOrNull()

    /** After a grade: the next ungraded turn after the graded one, else an earlier one still ungraded; null when every
     *  turn is graded. By place in time, so the answer is the same whether the graded turn is still in the conversation. */
    fun next_todo(band: ThreadBand, from_id: String, from_at: String): RecentRow? {
        val rows = band.rows
        val from_ms = LayaRules.epoch_ms(from_at) ?: 0L
        val own = rows.indexOfFirst { it.decision_id == from_id }
        val later = rows.indexOfFirst { at_ms(it) > from_ms }
        val at = if (own >= 0) own else if (later < 0) rows.size else later
        val open = { r: RecentRow -> ungraded(r) && r.decision_id != from_id }
        return rows.drop(if (own >= 0) at + 1 else at).firstOrNull(open) ?: rows.take(at).firstOrNull(open)
    }

    /** The next conversation after `from` with a turn to grade; forward only, so one skipped by a swipe is never jumped back to. */
    fun next_todo_band(bands: List<ThreadBand>, from: Int): Int? {
        for (i in max(0, from + 1) until bands.size) if (todo_count(bands[i]) > 0) return i
        return null
    }

    /** The nearest conversation before `at` with a turn to grade: what is left once nothing ahead is. */
    fun todo_behind(bands: List<ThreadBand>, at: Int): Int? {
        for (i in min(at, bands.size) - 1 downTo 0) if (todo_count(bands[i]) > 0) return i
        return null
    }

    /** The conversation `ids` (its turns, as they were) and `key` stood for, where it is now; -1 when it left the page.
     *  By any of its turns first: a key can move (an unlinked run is keyed by its oldest turn). */
    fun band_holding(bands: List<ThreadBand>, ids: List<String>, key: String?): Int {
        val wanted = ids.toSet()
        val by_turn = if (wanted.isEmpty()) -1 else bands.indexOfFirst { b -> b.rows.any { it.decision_id in wanted } }
        return if (by_turn >= 0 || key == null) by_turn else bands.indexOfFirst { it.key == key }
    }

    /** Where the pager stands: the cursor turn's conversation; when that turn left the page (a filter, TO GRADE), the
     *  conversation that held it, if it is still here; else the same place in the list. */
    fun pager_at(bands: List<ThreadBand>, place: PagerPlace): Int {
        val own = if (place.cursor == null) -1 else band_holding(bands, listOf(place.cursor), null)
        if (own >= 0) return own
        val held = band_holding(bands, place.ids, place.key)
        return if (held >= 0) held else min(place.last, max(0, bands.size - 1))
    }

    /** The conversation a fresh page opens on: the first with a turn to grade, else the first. */
    fun opening_band(bands: List<ThreadBand>): Int = max(0, bands.indexOfFirst { todo_count(it) > 0 })

    /** Forward from a grade, on the page as it is now: null when nothing lies ahead on it, so the caller loads an older
     *  page and asks again, then settles for `left_after_grade`. */
    fun after_grade(bands: List<ThreadBand>, graded: GradedAt): AfterGrade? {
        val here = band_holding(bands, graded.ids, graded.key)
        val row = if (here >= 0) next_todo(bands[here], graded.row_id, graded.row_at) else null
        if (row != null) return AfterGrade.Same(here, row)
        // Gone from the page: the conversation that slid into its place is the first one after it.
        val to = next_todo_band(bands, if (here >= 0) here else graded.at - 1) ?: return null
        return AfterGrade.Next(to, back = here >= 0 && to == here + 1)
    }

    /** Once nothing lies ahead: the nearest earlier conversation with a turn to grade, or none. `held`: the graded
     *  conversation is still on the page, so the pager stays on it. */
    fun left_after_grade(bands: List<ThreadBand>, graded: GradedAt): AfterGrade {
        val here = band_holding(bands, graded.ids, graded.key)
        val behind = todo_behind(bands, if (here >= 0) here else graded.at)
        return if (behind == null) AfterGrade.None(held = here >= 0) else AfterGrade.Behind(behind, held = here >= 0)
    }

    /** The status line after a grade (`graded` is `Graded ✓ DONE · RIGHT`), in the words of where it went. */
    fun after_grade_text(graded: String, step: AfterGrade): String = when (step) {
        is AfterGrade.Same -> graded
        is AfterGrade.Next -> "$graded. That conversation is done: here is the next one${if (step.back) " (◀ goes back)" else ""}"
        is AfterGrade.Behind ->
            if (!step.held) "$graded. That conversation is done, and nothing lies ahead: here is an earlier one with turns to grade"
            else "$graded. ${end_text(true)}"
        is AfterGrade.None -> "$graded. ${end_text(false)}"
    }

    /** What is left once a conversation is graded and nothing lies ahead. */
    fun end_text(behind: Boolean): String =
        if (behind) "Nothing further ahead: an earlier conversation still has turns to grade" else "Nothing else to grade on this page"

    // ── a conversation's turns, under the prompt each answered ───────────────────

    /** A conversation's turns grouped by the prompt each answered: the turns a task notification or the rack woke
     *  after one prompt sit under that prompt once. */
    fun turn_groups(band: ThreadBand, join: HistoryThreads?): List<TurnGroup> {
        val out = mutableListOf<TurnGroup>()
        var last: Pair<Long, Int>? = null
        for (row in band.rows) {
            val turn = join?.decisions?.get(row.decision_id)?.takeIf { it.linked }
            val ask = turn?.ask
            val anchor = ask ?: turn?.request
            val file = turn?.file_id
            val key = if (anchor != null && file != null) "$file:${anchor.seq}" else "row:${row.decision_id}"
            val prev = out.lastOrNull()
            if (prev != null && prev.key == key) {
                out[out.size - 1] = prev.copy(rows = prev.rows + row)
                continue
            }
            val ordinal = if (ask != null) turn.human_ordinal?.takeIf { it > 0 } else null
            val before = last
            val skipped = if (before != null && ordinal != null && file == before.first) max(0, ordinal - before.second - 1) else 0
            out += TurnGroup(
                key = key,
                anchor = anchor,
                by_operator = ask != null,
                woke_by = if (ask != null) null else turn?.woke_by,
                ordinal = ordinal,
                of = if (ordinal != null) band.thread?.human_turns else null,
                skipped = skipped,
                rows = listOf(row),
            )
            if (ordinal != null && file != null) last = file to ordinal
        }
        return out
    }

    /** Whether the conversation's first message already draws as a group's prompt, so its head need not print it again. */
    fun first_in_groups(groups: List<TurnGroup>, first: HistoryClip?): Boolean {
        if (first == null) return false
        val lead = { text: String -> text.trim().take(80) }
        return groups.any { g -> g.by_operator && g.anchor?.seq == first.seq && g.anchor.let { lead(it.text) == lead(first.text) } }
    }

    /** `YOU · prompt 2 of 5`: a group's head. */
    fun group_head(group: TurnGroup): String {
        if (!group.by_operator) return if (group.woke_by == "parent") "LEAD" else "RACK"
        val ordinal = group.ordinal ?: return "YOU"
        val of = group.of
        return if (of != null && of >= ordinal) "YOU · prompt $ordinal of $of" else "YOU · prompt $ordinal"
    }

    /** What woke a turn the operator did not open (`turns.origin`), as words; null for a prompt of theirs. */
    private fun woke_text(woke_by: String?): String? = when (woke_by) {
        null, "", "human", "command" -> null
        "task" -> "a background report"
        "peer" -> "another agent's message"
        else -> "the rack"
    }

    private val WAKE_SUMMARY = Regex("<task-notification>[\\s\\S]*?<summary>\\s*([\\s\\S]*?)\\s*</summary>")
    private const val WAKE_SUMMARY_CHARS = 90
    const val WAKE_TAG = "⟲ NOT YOUR PROMPT"

    /** A turn the operator did not open: who woke the agent, and a task notification's own one-line summary. */
    fun wake_note(woke_by: String?, request_text: String?): WakeNote? {
        val who = woke_text(woke_by) ?: return null
        val found = if (woke_by == "task" && request_text != null) {
            WAKE_SUMMARY.find(request_text)?.groupValues?.get(1)?.replace(Regex("\\s+"), " ")?.trim().orEmpty()
        } else {
            ""
        }
        val summary = if (found.length > WAKE_SUMMARY_CHARS) "${found.take(WAKE_SUMMARY_CHARS - 1)}…" else found
        val line = "${who.replaceFirstChar(Char::uppercase)} woke the agent after its last reply"
        return WakeNote(WAKE_TAG, if (summary.isEmpty()) line else "$line: “$summary”")
    }

    // ── heads ────────────────────────────────────────────────────────────────────

    private val MONTHS = listOf("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

    private data class Clock(val day: String, val year: Int, val time: String, val half: String)

    private fun clock(ms: Long, zone: ZoneId): Clock {
        val d = ZonedDateTime.ofInstant(Instant.ofEpochMilli(ms), zone)
        val h = d.hour
        return Clock(
            day = "${MONTHS[d.monthValue - 1]} ${d.dayOfMonth}",
            year = d.year,
            time = "${if (h % 12 == 0) 12 else h % 12}:${d.minute.toString().padStart(2, '0')}",
            half = if (h < 12) "am" else "pm",
        )
    }

    /** `3:10 – 3:40 pm` in local time, with the day in front when it is not today. */
    fun clock_span(from_ms: Long, to_ms: Long, now_ms: Long, zone: ZoneId = ZoneId.systemDefault()): String {
        val a = clock(from_ms, zone)
        val b = clock(to_ms, zone)
        if (a.day != b.day || a.year != b.year) return "${a.day}, ${a.time} ${a.half} – ${b.day}, ${b.time} ${b.half}"
        val today = clock(now_ms, zone)
        val day = if (a.day == today.day && a.year == today.year) "" else "${a.day}, "
        if (a.time == b.time && a.half == b.half) return "$day${a.time} ${a.half}"
        return if (a.half == b.half) "$day${a.time} – ${b.time} ${b.half}" else "$day${a.time} ${a.half} – ${b.time} ${b.half}"
    }

    private fun first_line(text: String?): String? = text?.trim()?.split(Regex("\r?\n"))?.first()?.take(120)?.ifEmpty { null }

    /**
     * The ledger's title, else what the needle found, else the first prompt's first line, else the session's name (with
     * its time span, for an unlinked run), else `Conversation`.
     *
     * `name` is the rack's display name for the session (`LayaRules.rack_name`), and where there is one it stands in
     * for the tmux name everywhere: `vc-2` is a slot tmux hands to the next agent, the display name is what the operator
     * called the work. The web prints the tmux name here; the phone has one line for it, so it prints the better one.
     */
    fun band_title(band: ThreadBand, found: FoundHead?, now_ms: Long, zone: ZoneId = ZoneId.systemDefault(), name: String? = null): String {
        val named = band.thread?.title?.trim()?.ifEmpty { null }
            ?: found?.title?.trim()?.ifEmpty { null }
            ?: first_line(band.thread?.first_user?.text)
            ?: first_line(found?.first_user)
        if (named != null) return named
        val session = session_name(band, name)
        if (band.kind == BandKind.unlinked) return "${session ?: "A session"}, ${clock_span(band.span.first, band.span.second, now_ms, zone)}"
        return session ?: "Conversation"
    }

    /** The session a conversation ran in, as the head names it: the rack's display name, else the tmux name. */
    private fun session_name(band: ThreadBand, name: String?): String? =
        name?.trim()?.ifEmpty { null } ?: band.subject?.trim()?.ifEmpty { null }

    private fun head_tags(band: ThreadBand, join_ok: Boolean): List<HeadTag> {
        val tags = mutableListOf<HeadTag>()
        band.new_in_rack?.let { tags += HeadTag("NEW", "A new conversation: $it held a different one earlier on this page") }
        val resumed = band.thread?.resumed ?: 0
        if (resumed > 0) tags += HeadTag("RESUMED $resumed×", "Resumed in a new session file; it still reads as one thread")
        if (band.indexing) {
            tags += HeadTag("INDEXING", "The session ledger has not caught up with this turn yet; the head fills in within a minute")
        } else if (band.kind == BandKind.unlinked && join_ok) {
            tags += HeadTag("NOT LINKED", "Not matched to a conversation, so grouped by rack name, at most 30 minutes between turns")
        }
        return tags
    }

    /** A conversation's full head: `4 turns · 2 to grade · started 2h`. `name`: see `band_title`. */
    fun band_head(
        band: ThreadBand,
        now_ms: Long,
        join_ok: Boolean,
        found: FoundHead?,
        zone: ZoneId = ZoneId.systemDefault(),
        name: String? = null,
    ): ThreadHead {
        val n = band.rows.size
        val todo = todo_count(band)
        val started = band.thread?.started_at?.let { LayaRules.age_since((it * 1_000).toLong(), now_ms) }
        val meta = listOfNotNull(
            "$n turn${if (n == 1) "" else "s"}",
            if (todo > 0) "$todo to grade" else null,
            started?.let { "started $it" },
        ).joinToString(" · ")
        val clip = band.thread?.first_user
        val title = band_title(band, found, now_ms, zone, name)
        return ThreadHead(
            title = title,
            // Not where the title already says it: an unnamed conversation is titled by its session.
            subject = session_name(band, name)?.takeIf { !title.startsWith(it) },
            meta = meta,
            first_user = clip?.text ?: found?.first_user,
            first_clipped = clip != null && (clip.chars ?: 0) > clip.text.length,
            tags = head_tags(band, join_ok),
        )
    }

    private val PASTE_TAG = Regex("""^[ \t]*<\/?pasted_content(?: id="[^"]*")?>[ \t]*$""")

    /**
     * A prompt as its words. Dictation arrives pasted, so the operator's own prompt is often wrapped in
     * `<pasted_content id>` lines (ui/src/laya-review/paste-blocks.ts owns the format): the tag lines go, the words
     * stay where they sat. The web folds a pasted DOCUMENT to one line; here a prompt is clamped to a few lines with
     * MORE, which does the same job without a second parser.
     */
    fun prompt_text(text: String): String =
        text.split('\n').filterNot { PASTE_TAG.matches(it.trimEnd('\r')) }.joinToString("\n").trim()

    /** The last `lines` lines of a message: Laya judges a turn by how it ends (the question, the "say go"). */
    fun tail_text(text: String, lines: Int): String {
        val all = text.trimEnd().split('\n')
        return if (all.size <= lines) text.trimEnd() else "…\n" + all.takeLast(lines).joinToString("\n")
    }

    /** A turn's preview: the agent's own closing message when linked (its end, cut at a line), else what Laya read. */
    fun preview_text(row: RecentRow, turn: HistoryTurn?): String =
        turn?.takeIf { it.linked }?.closing_tail?.ifEmpty { null } ?: row.text
}
