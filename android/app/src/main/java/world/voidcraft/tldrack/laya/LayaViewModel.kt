package world.voidcraft.tldrack.laya

import android.app.Application
import android.os.SystemClock
import androidx.datastore.preferences.core.edit
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.serialization.builtins.ListSerializer
import world.voidcraft.tldrack.APP_JSON
import world.voidcraft.tldrack.PrefKeys
import world.voidcraft.tldrack.tldrack_prefs

/** Whether the Mac answers: the screen draws one state per value, each with its own fix. */
enum class Link { checking, linked, unlinked, bridge_down, refused }

/** The page's join (`/ledger/threads`): out, landed, refused by a link that predates it, or failed. Without one the
 *  conversations fall back to rack name and time (`LayaThreads.thread_bands`). */
enum class Join { loading, ok, old_bridge, unavailable }

data class Status(val ok: Boolean, val text: String)

/** The conversation behind one verdict, as far as it has loaded. */
data class ContextUi(
    val loading: Boolean = false,
    val context: TurnContext? = null,
    /** LOAD EARLIER or REFRESH INDEX running. */
    val busy: Boolean = false,
    val error: String? = null,
)

data class LayaUi(
    val link: Link = Link.checking,
    /** The refusal's line, while `link` is `refused`. */
    val refusal: String? = null,
    val loading: Boolean = false,
    val loaded: Boolean = false,
    val rows: List<RecentRow> = emptyList(),
    val surface: String = LayaRules.DEFAULT_SURFACE,
    val band: Band? = null,
    val to_grade: Boolean = true,
    /** The cursor TURN, by decision id: the one the grade bar answers. Its conversation is the one shown. */
    val selected: String? = null,
    val in_flight: Boolean = false,
    val status: Status? = null,
    /** This list's answers, newest last: what UNDO takes back, one per press. */
    val undo: List<Answered> = emptyList(),
    /** Turns whose BETWEEN fold is open, by decision id: kept here, so scrolling one away or turning the phone keeps it. */
    val between: Set<String> = emptySet(),
    /** An UNDO whose row was not on the page: the next load lands on that text's row. */
    val return_key: String? = null,
    /** `/topic-labels` per rack session (tmux name). */
    val names: Map<String, TopicView> = emptyMap(),
    /** Keyed by decision id, never by text: two verdicts on one text are two turns. */
    val contexts: Map<String, ContextUi> = emptyMap(),
    /** The join the conversations are drawn from; kept while a newer one is out. */
    val threads: HistoryThreads? = null,
    val join: Join = Join.loading,
    /** The cursor of the older page (`summary.next`); null once nothing follows. */
    val next: String? = null,
    val loading_older: Boolean = false,
    // ── derived on every change (`LayaViewModel.settle`), so the screen never works them out ──
    /** The conversations of the rows the band shows, in the pager's order. */
    val bands: List<ThreadBand> = emptyList(),
    /** Which of them is shown. */
    val conv_at: Int = 0,
    /** The shown conversation's turns and key, as they were: how the pager finds it again when its cursor turn leaves the page. */
    val conv_ids: List<String> = emptyList(),
    val conv_key: String? = null,
) {
    val conversation: ThreadBand? get() = bands.getOrNull(conv_at)
    val cursor: RecentRow? get() = conversation?.rows?.firstOrNull { it.decision_id == selected }
    val has_older: Boolean get() = next != null
}

/**
 * The phone's RECENT review, ONE conversation at a time: the fetches and the state. What a conversation is, the order
 * the pager walks them in and where a grade moves on to are `LayaThreads` (a port of the web's `thread-model.ts`, which
 * owns why one at a time); the marks and the answer flow are `LayaRules`; the wire is `LayaClient`.
 *
 * TO GRADE is the FETCH (`todo=1`), not a client filter as on the web, and a reload puts this list's own graded rows
 * back (`LayaRules.keep_graded`): a turn graded here keeps its place and its RIGHT/WRONG for as long as the screen
 * lives, so nothing moves under the thumb that just graded it.
 *
 * What the reader was doing (the band, the cursor turn, its folds, the undo stack) survives process death through
 * `SavedStateHandle`: some phones kill a backgrounded app often, and UNDO must still reach an answer given seconds before.
 */
class LayaViewModel(app: Application, private val saved: SavedStateHandle) : AndroidViewModel(app) {

    private val client = LayaClient(app.filesDir)
    private val prefs = app.tldrack_prefs

    private val state = MutableStateFlow(restore(saved))
    val ui: StateFlow<LayaUi> = state

    /** One conversation lookup at a time: owned by `load_context` — see the note there. */
    private val context_gate = Semaphore(1)

    private var load_job: Job? = null
    private var names_job: Job? = null
    private var join_job: Job? = null

    /** Bumped by every full load: an older page or a join that lands after one belongs to the page it replaced. */
    private var loads = 0

    /** Whether the page on screen was fetched with `todo=1`: its older pages are asked the same way. */
    private var fetched_todo = true

    /** `SystemClock.elapsedRealtime()` when a page last landed; the ViewModel outlives a rotation, so this does too. */
    private var loaded_at_ms = 0L

    /** When REFRESH INDEX was last pressed for the join, by this clock: one ingest wake a minute, page-wide. */
    private var ingest_at_ms = Long.MIN_VALUE / 2

    init {
        viewModelScope.launch {
            state.map { Reading(it.band, it.selected, it.conv_key, it.undo, it.between) }
                .distinctUntilChanged()
                .collect { keep(it) }
        }
    }

    // ── the conversations: derived once per change ───────────────────────────

    private class Banded(val rows: List<RecentRow>, val band: Band?, val threads: HistoryThreads?, val ok: Boolean, val bands: List<ThreadBand>)

    private var banded: Banded? = null

    /** The conversations for a state; the last answer is kept, because most changes (a name, a context) touch none of its inputs. */
    private fun bands_of(s: LayaUi): List<ThreadBand> {
        val ok = s.join == Join.ok
        banded?.let { if (it.rows === s.rows && it.band == s.band && it.threads === s.threads && it.ok == ok) return it.bands }
        val bands = LayaThreads.thread_bands(LayaRules.visible_rows(s.rows, s.band, unanswered_only = false), s.threads, ok)
        banded = Banded(s.rows, s.band, s.threads, ok, bands)
        return bands
    }

    /**
     * Where the pager stands after any change: the cursor turn's conversation, else the one that held it, else the same
     * place (`LayaThreads.pager_at`); and the cursor itself, which is that conversation's opening turn when its own left.
     */
    private fun settle(s: LayaUi): LayaUi {
        val bands = bands_of(s)
        val at = LayaThreads.pager_at(bands, PagerPlace(s.selected, s.conv_ids, s.conv_key, s.conv_at))
        val conv = bands.getOrNull(at)
        val cursor = conv?.rows?.firstOrNull { it.decision_id == s.selected } ?: conv?.let(LayaThreads::opening_row)
        return s.copy(
            bands = bands,
            conv_at = at,
            selected = cursor?.decision_id,
            conv_ids = conv?.rows?.map { it.decision_id }.orEmpty(),
            conv_key = conv?.key,
        )
    }

    /** Every state change goes through here, so `bands`, `conv_at` and the cursor are never stale. */
    private inline fun change(crossinline edit: (LayaUi) -> LayaUi) = state.update { settle(edit(it)) }

    /** Conversation `i`, on its first turn to grade. */
    private fun go_to(s: LayaUi, i: Int): LayaUi {
        val target = bands_of(s).getOrNull(i) ?: return s
        return s.copy(
            conv_at = i,
            selected = LayaThreads.opening_row(target)?.decision_id,
            conv_ids = target.rows.map { it.decision_id },
            conv_key = target.key,
        )
    }

    // ── loading ──────────────────────────────────────────────────────────────

    /** The page: probe the link first unless it is already known good, then the list. A newer load replaces an older one. */
    fun load() {
        load_job?.cancel()
        loads += 1
        load_job = viewModelScope.launch {
            change { it.copy(loading = true) }
            val todo = prefs.data.first()[PrefKeys.laya_to_grade] ?: true
            change { it.copy(to_grade = todo) }
            if (state.value.link != Link.linked) {
                val probe = client.health()
                if (probe is Fail) return@launch fail_link(probe)
            }
            var got = client.recent(todo)
            var with_todo = todo
            var note: Status? = null
            // A bridge from before TO GRADE refuses the name: show the plain list and say why, keeping the setting.
            if (todo && got is Fail.Refused && got.code == "bad_query") {
                got = client.recent(false)
                with_todo = false
                note = Status(ok = false, text = PREDATES_TO_GRADE)
            }
            when (got) {
                is Call.Ok -> take_page(got.data, with_todo, note)
                is Fail -> fail_link(got)
            }
        }
    }

    /**
     * A resume. The rack keeps judging while the phone is in a pocket, so a return refetches; but a rotation, a dark
     * mode switch or a glance at the notification shade also resumes, and a page that just landed is not stale.
     */
    fun load_on_return() {
        if (load_job?.isActive == true) return
        val s = state.value
        if (s.link == Link.linked && s.loaded && SystemClock.elapsedRealtime() - loaded_at_ms < RETURN_FRESH_MS) return
        load()
    }

    fun set_to_grade(on: Boolean) {
        change { it.copy(to_grade = on) }
        viewModelScope.launch {
            prefs.edit { it[PrefKeys.laya_to_grade] = on }
            load()
        }
    }

    /** A band readout narrows the page; the pager stays on the conversation shown while any of its turns is still in
     *  view (`settle`). A second press clears it. */
    fun press_band(band: Band) {
        change { it.copy(band = if (it.band == band) null else band) }
        ensure_context(state.value.selected)
    }

    // ── the pager ────────────────────────────────────────────────────────────

    /**
     * A swipe, or ◀ ▶: conversation `i`, graded or not, opened on its first turn to grade. One past the last is the
     * older page: its turns of the conversation shown join it, so this loads on until a new conversation arrives.
     */
    fun show_conversation(i: Int) {
        val s = state.value
        if (i < 0 || s.loading) return
        if (i < s.bands.size) {
            if (s.bands[i].key == s.conv_key) return
            change { go_to(it.copy(status = null), i) }
            ensure_context(state.value.selected)
            return
        }
        if (!s.has_older || s.loading_older) return
        viewModelScope.launch {
            var pages = 0
            while (state.value.conv_at + 1 >= state.value.bands.size) {
                if (pages >= AUTO_PAGES || !load_older()) return@launch
                pages += 1
            }
            change { go_to(it.copy(status = null), it.conv_at + 1) }
            ensure_context(state.value.selected)
        }
    }

    fun step(delta: Int) = show_conversation(state.value.conv_at + delta)

    /** A tap on a turn: it is the one being read, and the grade bar answers it. */
    fun select(decision_id: String) {
        change { it.copy(selected = decision_id) }
        ensure_context(decision_id)
    }

    /** BETWEEN folds or unfolds one turn's middle messages. */
    fun toggle_between(decision_id: String) {
        change { s -> s.copy(between = if (decision_id in s.between) s.between - decision_id else s.between + decision_id) }
    }

    /**
     * The next page of verdicts after `next`, appended. True when it brought a row this page did not have: a page of
     * rows already here (a text judged again moved up) walks on, a few pages at most. Never while a full load is out.
     */
    private suspend fun load_older(): Boolean {
        val start = state.value
        if (!start.has_older || start.loading_older || start.loading) return false
        val mine = loads
        change { it.copy(loading_older = true) }
        try {
            repeat(AUTO_PAGES) {
                val after = state.value.next ?: return false
                val got = client.recent(fetched_todo, after)
                if (mine != loads) return false
                when (got) {
                    is Fail -> {
                        // A bridge from before the cursor refuses the name: stop offering older pages.
                        val predates = got is Fail.Refused && got.code == "bad_query"
                        change {
                            it.copy(
                                next = if (predates) null else it.next,
                                status = Status(ok = false, text = if (predates) PREDATES_OLDER else LayaRules.fail_text(got)),
                                link = link_after(got, it.link),
                            )
                        }
                        return false
                    }
                    is Call.Ok -> {
                        val seen = state.value.rows.mapTo(HashSet()) { it.decision_id }
                        val fresh = got.data.rows.filter { it.decision_id !in seen }
                        change { it.copy(next = got.data.summary?.next, rows = if (fresh.isEmpty()) it.rows else it.rows + fresh) }
                        if (fresh.isNotEmpty()) {
                            load_join()
                            load_names(state.value.rows)
                            return true
                        }
                    }
                }
            }
            return false
        } finally {
            change { it.copy(loading_older = false) }
        }
    }

    // ── grading ──────────────────────────────────────────────────────────────

    /** One verdict on the cursor turn, then on: the conversation's next open turn, else the next conversation with one. */
    fun grade(verdict: Verdict) {
        val cur = state.value
        if (cur.in_flight) return
        val row = cur.cursor ?: return
        if (LayaRules.operator_verdict(row.operator) == verdict) return
        // Who the conversation was, taken before the answer moves anything.
        val graded = GradedAt(cur.conv_ids, cur.conv_key, row.decision_id, row.at, cur.conv_at)
        change { it.copy(in_flight = true, status = null) }
        viewModelScope.launch {
            try {
                val out = LayaRules.grade_row(row, cur.surface, verdict, client)
                val recorded = out.recorded
                val failure = out.failure
                change { s ->
                    var rows = s.rows
                    for ((key, operator) in out.edits) rows = LayaRules.with_answer(rows, key, operator)
                    s.copy(
                        rows = rows,
                        in_flight = false,
                        undo = if (recorded != null) s.undo + Answered(row.key, recorded.answer_id) else s.undo,
                        status = failure?.let { Status(ok = false, text = LayaRules.fail_text(it)) },
                        link = link_after(failure, s.link),
                    )
                }
                // Onward only from the graded turn: one tapped, or a conversation swiped to, while the answer was in
                // flight is what is being read now.
                if (recorded != null && state.value.selected == row.decision_id) {
                    val word = when (LayaRules.grade(row.copy(operator = recorded))) {
                        Grade.right -> "Graded · RIGHT"
                        Grade.wrong -> "Graded · WRONG"
                        null -> "Graded"
                    }
                    move_after_answer(graded, word)
                }
                ensure_context(state.value.selected)
            } finally {
                if (state.value.in_flight) change { it.copy(in_flight = false) }
            }
        }
    }

    /**
     * Where the cursor goes after an answer (`LayaThreads.after_grade` owns the rule and its words): forward, loading
     * older pages while nothing is ahead (their turns of THIS conversation join it, so it is checked first each time);
     * else what is left behind it. A move inside the conversation says nothing: the turn's own rule shows the grade.
     */
    private suspend fun move_after_answer(graded: GradedAt, text: String) {
        var step = LayaThreads.after_grade(state.value.bands, graded)
        var pages = 0
        while (step == null && pages < AUTO_PAGES && load_older()) {
            step = LayaThreads.after_grade(state.value.bands, graded)
            pages += 1
        }
        val failed = if (step == null) state.value.status?.takeIf { !it.ok }?.text else null
        val more = step == null && state.value.has_older
        change { s ->
            val to = step ?: LayaThreads.left_after_grade(bands_of(s), graded)
            val moved = when (to) {
                is AfterGrade.Same -> s.copy(selected = to.row.decision_id)
                is AfterGrade.Next -> go_to(s, to.band)
                is AfterGrade.Behind -> if (to.held) s else go_to(s, to.band)
                is AfterGrade.None -> s
            }
            moved.copy(
                status = when {
                    failed != null -> Status(ok = false, text = "$text. $failed")
                    more -> Status(ok = true, text = "$text. Nothing to grade in the next pages yet: ▶ looks further")
                    to is AfterGrade.Same -> null
                    else -> Status(ok = true, text = LayaThreads.after_grade_text(text, to))
                },
            )
        }
    }

    /**
     * This list's last answer, taken back; the cursor returns to its turn. One press, as on the web. A row the page
     * no longer holds (a TO GRADE page after process death) is unanswered again, so a reload brings it back to land on.
     */
    fun undo() {
        val cur = state.value
        val top = cur.undo.lastOrNull() ?: return
        if (cur.in_flight) return
        change { it.copy(in_flight = true, status = null) }
        viewModelScope.launch {
            try {
                val failure = LayaRules.take_back(cur.surface, top.answer_id, client)
                var reload = false
                change { s ->
                    if (failure != null) {
                        reload = false
                        s.copy(in_flight = false, status = Status(ok = false, text = LayaRules.fail_text(failure)), link = link_after(failure, s.link))
                    } else {
                        val rows = LayaRules.with_answer(s.rows, top.key, null)
                        val back = LayaRules.return_to(rows, s.band, top.key)
                        reload = back == null
                        s.copy(
                            in_flight = false,
                            rows = rows,
                            undo = s.undo.dropLast(1),
                            // A null band is the widening `return_to` chose, not "keep the old one".
                            band = if (back != null) back.band else s.band,
                            selected = back?.selected ?: s.selected,
                            return_key = if (back == null) top.key else null,
                            status = Status(ok = true, text = TAKEN_BACK),
                        )
                    }
                }
                if (reload) load() else ensure_context(state.value.selected)
            } finally {
                if (state.value.in_flight) change { it.copy(in_flight = false) }
            }
        }
    }

    // ── a turn's conversation ────────────────────────────────────────────────

    /** LOAD EARLIER: one page further back; the judged close stays the anchor. */
    fun earlier(decision_id: String) {
        val ctx = state.value.contexts[decision_id] ?: return
        val found = ctx.context as? TurnContext.Found ?: return
        val before = found.before ?: return
        if (ctx.busy) return
        put_context(decision_id) { it.copy(busy = true, error = null) }
        viewModelScope.launch {
            val page = LayaRules.load_earlier(client, found.file_id, found.messages.firstOrNull()?.seq ?: before)
            put_context(decision_id) { c ->
                val current = c.context as? TurnContext.Found ?: found
                when (page) {
                    is TurnContext.Found -> c.copy(context = LayaRules.prepend_context(current, page), busy = false)
                    is TurnContext.Missing -> c.copy(error = page.message, busy = false)
                }
            }
        }
    }

    /**
     * REFRESH INDEX: wake the ledger's ingest, give it a moment, then look the turn up again. A refusal says its own
     * remedy (a ledger that is off answers 503 with what to set; retrying never helps it), and a lost link takes the
     * screen to its down state, as a failed write does.
     */
    fun refresh_index(decision_id: String) {
        val ctx = state.value.contexts[decision_id] ?: return
        val row = state.value.rows.firstOrNull { it.decision_id == decision_id } ?: return
        if (ctx.busy) return
        put_context(decision_id) { it.copy(busy = true, error = null) }
        viewModelScope.launch {
            when (val got = client.ingest()) {
                is Call.Ok -> {
                    delay(INGEST_SETTLE_MS)
                    load_context(row)
                }
                is Fail -> {
                    put_context(decision_id) { it.copy(busy = false, error = LayaRules.fail_text(got)) }
                    change { it.copy(link = link_after(got, it.link)) }
                }
            }
        }
    }

    // ── internals ────────────────────────────────────────────────────────────

    private fun take_page(page: RecentPage, todo: Boolean, note: Status?) {
        loaded_at_ms = SystemClock.elapsedRealtime()
        fetched_todo = todo
        change { cur ->
            val rows = if (todo) LayaRules.keep_graded(page.rows, cur.rows, cur.undo.mapTo(HashSet()) { it.key }) else page.rows
            val base = cur.copy(
                link = Link.linked,
                refusal = null,
                loading = false,
                loaded = true,
                rows = rows,
                surface = page.surface ?: LayaRules.DEFAULT_SURFACE,
                next = page.summary?.next,
                between = cur.between.filterTo(HashSet()) { id -> rows.any { it.decision_id == id } },
                return_key = null,
                // A reload clears a stale line, except the "taken back" that sent it looking for the row.
                status = note ?: cur.status.takeIf { cur.return_key != null },
            )
            val back = cur.return_key?.let { LayaRules.return_to(rows, cur.band, it) }
            if (back != null) return@change base.copy(band = back.band, selected = back.selected)
            // The cursor's turn when it is still here, else its conversation, else the first one with a turn to grade.
            val bands = bands_of(base)
            if (cur.selected != null && bands.any { b -> b.rows.any { it.decision_id == cur.selected } }) return@change base
            val held = LayaThreads.band_holding(bands, cur.conv_ids, cur.conv_key)
            go_to(base, if (held >= 0) held else LayaThreads.opening_band(bands))
        }
        load_join()
        load_names(state.value.rows)
    }

    /**
     * ONE join for the page: every verdict and every raw conversation on it. While it is out the conversations keep
     * the last join (a fresh page shares most rows with the one before); a failed one falls back to rack name and time.
     * A conversation the ledger has not caught up with wakes its ingest (once a minute at most) and asks twice more.
     * The cursor turn's own conversation is read only once this has settled, so a linked turn never runs the search.
     */
    private fun load_join() {
        join_job?.cancel()
        val mine = loads
        change { it.copy(join = Join.loading) }
        join_job = viewModelScope.launch {
            var waits = REASK_MS
            while (true) {
                val ask = LayaThreads.threads_ask_of(state.value.rows)
                val got = client.threads(ask)
                if (mine != loads) return@launch
                when (got) {
                    is Call.Ok -> change { it.copy(threads = got.data, join = Join.ok) }
                    is Fail -> {
                        val predates = got is Fail.Refused && got.status == 404
                        change { s ->
                            s.copy(
                                threads = null,
                                join = if (predates) Join.old_bridge else Join.unavailable,
                                status = s.status ?: if (predates) Status(ok = false, text = PREDATES_JOIN) else null,
                            )
                        }
                    }
                }
                ensure_context(state.value.selected)
                val wait = waits.firstOrNull() ?: break
                if (got is Fail || state.value.bands.none { it.indexing }) break
                if (waits.size == REASK_MS.size) {
                    val now = SystemClock.elapsedRealtime()
                    if (now - ingest_at_ms < INGEST_EVERY_MS || client.ingest() is Fail) break
                    ingest_at_ms = now
                }
                waits = waits.drop(1)
                delay(wait)
            }
        }
    }

    /** A failed LOAD replaces the list with its down state, a refusal included: there is nothing to grade. */
    private fun fail_link(fail: Fail) {
        change {
            it.copy(
                loading = false,
                link = when (fail) {
                    Fail.Unlinked -> Link.unlinked
                    Fail.BridgeDown -> Link.bridge_down
                    is Fail.Refused -> Link.refused
                },
                refusal = (fail as? Fail.Refused)?.let { refused -> LayaRules.fail_text(refused) },
            )
        }
    }

    /** A failed WRITE: a lost link takes the screen to its down state, a refusal is a line beside the bar and the list stays. */
    private fun link_after(fail: Fail?, link: Link): Link = when (fail) {
        Fail.Unlinked -> Link.unlinked
        Fail.BridgeDown -> Link.bridge_down
        is Fail.Refused, null -> link
    }

    private fun ensure_context(decision_id: String?) {
        val id = decision_id ?: return
        // While the join is out, wait for it: `load_join` calls back here once it settles.
        if (state.value.join == Join.loading || state.value.contexts[id] != null) return
        state.value.rows.firstOrNull { it.decision_id == id }?.let(::load_context)
    }

    /**
     * By the join's link when the turn has one (its file and closing message, no search), else by the closing text.
     *
     * One at a time, and only for the turn still being read. The ledger serves two reads at once and answers the third
     * "busy" (bridge `ledger/reader.py` `ledger_read`), and a few quick swipes used to start a lookup per conversation
     * passed: the one the reader stopped on then came back busy. A turn swiped past is dropped unread, so it is looked
     * up when it is the cursor again.
     */
    private fun load_context(row: RecentRow) {
        val id = row.decision_id
        put_context(id) { ContextUi(loading = true) }
        viewModelScope.launch {
            val found = context_gate.withPermit {
                if (state.value.selected != id) return@withPermit null
                val turn = state.value.threads?.decisions?.get(id)?.takeIf { it.linked }
                val file_id = turn?.file_id
                val closing_seq = turn?.closing_seq
                if (file_id != null && closing_seq != null) {
                    LayaRules.load_context_at(client, file_id, closing_seq)
                } else {
                    LayaRules.load_turn_context(row.text, row.at, client)
                }
            }
            if (found != null) return@launch put_context(id) { ContextUi(context = found) }
            change { it.copy(contexts = it.contexts - id) }
            // The reader may have come back to it in the meantime.
            ensure_context(state.value.selected)
        }
    }

    private fun put_context(id: String, edit: (ContextUi) -> ContextUi) {
        change { s -> s.copy(contexts = s.contexts + (id to edit(s.contexts[id] ?: ContextUi()))) }
    }

    /** The rack's display name per session, a few at a time. A name is a nicety: the tmux name stands in until it lands. */
    private fun load_names(rows: List<RecentRow>) {
        names_job?.cancel()
        val known = state.value.names
        val subjects = rows.mapNotNull { it.subject }.distinct().filter { it !in known }
        names_job = viewModelScope.launch {
            val gate = Semaphore(NAME_CALLS)
            for (subject in subjects) {
                launch {
                    gate.withPermit {
                        val got = client.topic_labels(subject)
                        if (got is Call.Ok) change { it.copy(names = it.names + (subject to got.data)) }
                    }
                }
            }
        }
    }

    // ── what outlives the process ────────────────────────────────────────────

    /** The part of `LayaUi` that is the reader's, not the Mac's: the rows themselves are refetched. */
    private data class Reading(
        val band: Band?,
        val selected: String?,
        val conv_key: String?,
        val undo: List<Answered>,
        val between: Set<String>,
    )

    private fun keep(reading: Reading) {
        saved[SAVED_BAND] = reading.band?.name
        saved[SAVED_SELECTED] = reading.selected
        saved[SAVED_CONVERSATION] = reading.conv_key
        saved[SAVED_UNDO] = APP_JSON.encodeToString(UNDO_LIST, reading.undo)
        saved[SAVED_BETWEEN] = ArrayList(reading.between)
    }

    private companion object {
        const val PREDATES_TO_GRADE = "This bridge predates TO GRADE: restart it (↻ › Restart bridge)"
        const val PREDATES_OLDER = "This bridge predates older pages: restart it (↻ › Restart bridge)"
        const val PREDATES_JOIN = "This bridge predates conversation links, so turns group by rack name: restart it (↻ › Restart bridge)"
        const val TAKEN_BACK = "Answer taken back"
        /** The web's wait between REFRESH INDEX and the second lookup (`LayaConversation.vue`). */
        const val INGEST_SETTLE_MS = 4_000L
        const val NAME_CALLS = 4
        /** A resume this soon after a page landed is a rotation or a glance, not a return from the pocket. */
        const val RETURN_FRESH_MS = 15_000L
        /** Older pages one press walks through before it stops and says so (`LayaRecentList.vue` `AUTO_PAGES`). */
        const val AUTO_PAGES = 3
        /** An ingest run takes ~1.7 s: the first re-ask lands after it, the second catches one that queued (`turn-context.ts` `REASK_MS`). */
        val REASK_MS = listOf(3_000L, 6_000L)
        const val INGEST_EVERY_MS = 60_000L

        const val SAVED_BAND = "laya.band"
        const val SAVED_SELECTED = "laya.selected"
        const val SAVED_CONVERSATION = "laya.conversation"
        const val SAVED_UNDO = "laya.undo"
        const val SAVED_BETWEEN = "laya.between"
        val UNDO_LIST = ListSerializer(Answered.serializer())

        /** A saved stack that no longer decodes (an older build's) is dropped, never a crash on launch. */
        fun restore(saved: SavedStateHandle): LayaUi {
            val undo = saved.get<String>(SAVED_UNDO)?.let { text ->
                try {
                    APP_JSON.decodeFromString(UNDO_LIST, text)
                } catch (error: IllegalArgumentException) {
                    null
                }
            }
            return LayaUi(
                band = saved.get<String>(SAVED_BAND)?.let { name -> Band.entries.firstOrNull { it.name == name } },
                selected = saved.get<String>(SAVED_SELECTED),
                conv_key = saved.get<String>(SAVED_CONVERSATION),
                undo = undo.orEmpty(),
                between = saved.get<ArrayList<String>>(SAVED_BETWEEN)?.toSet().orEmpty(),
            )
        }
    }
}
