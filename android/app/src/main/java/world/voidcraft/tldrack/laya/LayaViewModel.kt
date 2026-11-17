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
import world.voidcraft.tldrack.PrefKeys
import world.voidcraft.tldrack.APP_JSON
import world.voidcraft.tldrack.tldrack_prefs

/** Whether the Mac answers: the screen draws one state per value, each with its own fix. */
enum class Link { checking, linked, unlinked, bridge_down, refused }

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
    /** The selected verdict, by decision id: an index would point at another row once a narrowing moves the list. */
    val selected: String? = null,
    val expanded: Boolean = true,
    val in_flight: Boolean = false,
    val status: Status? = null,
    /** This list's answers, newest last: what UNDO takes back, one per press. */
    val undo: List<Answered> = emptyList(),
    /** Rows whose BETWEEN fold is open, by decision id: kept here, so scrolling a row away or turning the phone keeps it. */
    val between: Set<String> = emptySet(),
    /** An UNDO whose row was not on the page: the next load lands on that text's row. */
    val return_key: String? = null,
    /** `/topic-labels` per rack session (tmux name). */
    val names: Map<String, TopicView> = emptyMap(),
    /** Keyed by decision id, never by text: two verdicts on one text are two turns. */
    val contexts: Map<String, ContextUi> = emptyMap(),
)

/**
 * The phone's RECENT list: the fetches and the state. Every rule is `LayaRules`; the wire is `LayaClient`.
 *
 * TO GRADE is the FETCH (`todo=1`), not a client filter as on the web, and a reload puts this list's own graded rows
 * back (`LayaRules.keep_graded`): a row graded here keeps its place and its RIGHT/WRONG for as long as the screen
 * lives, so nothing moves under the thumb that just graded it.
 *
 * What the reader was doing (the band, the row, its folds, the undo stack) survives process death through
 * `SavedStateHandle`: some phones kill a backgrounded app often, and UNDO must still reach an answer given seconds before.
 */
class LayaViewModel(app: Application, private val saved: SavedStateHandle) : AndroidViewModel(app) {

    private val client = LayaClient(app.filesDir)
    private val prefs = app.tldrack_prefs

    private val state = MutableStateFlow(restore(saved))
    val ui: StateFlow<LayaUi> = state

    private var load_job: Job? = null
    private var names_job: Job? = null

    /** `SystemClock.elapsedRealtime()` when a page last landed; the ViewModel outlives a rotation, so this does too. */
    private var loaded_at_ms = 0L

    init {
        viewModelScope.launch {
            state.map { Reading(it.band, it.selected, it.expanded, it.undo, it.between) }
                .distinctUntilChanged()
                .collect { keep(it) }
        }
    }

    /** The page: probe the link first unless it is already known good, then the list. A newer load replaces an older one. */
    fun load() {
        load_job?.cancel()
        load_job = viewModelScope.launch {
            state.update { it.copy(loading = true) }
            val todo = prefs.data.first()[PrefKeys.laya_to_grade] ?: true
            state.update { it.copy(to_grade = todo) }
            if (state.value.link != Link.linked) {
                val probe = client.health()
                if (probe is Fail) return@launch fail_link(probe)
            }
            var got = client.recent(todo)
            var fetched_todo = todo
            var note: Status? = null
            // A bridge from before TO GRADE refuses the name: show the plain list and say why, keeping the setting.
            if (todo && got is Fail.Refused && got.code == "bad_query") {
                got = client.recent(false)
                fetched_todo = false
                note = Status(ok = false, text = PREDATES_TO_GRADE)
            }
            when (got) {
                is Call.Ok -> take_page(got.data, fetched_todo, note)
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
        state.update { it.copy(to_grade = on) }
        viewModelScope.launch {
            prefs.edit { it[PrefKeys.laya_to_grade] = on }
            load()
        }
    }

    /** A band readout narrows the list and lands on its first row; a second press clears it. */
    fun press_band(band: Band) {
        state.update { cur ->
            val next = if (cur.band == band) null else band
            val shown = LayaRules.visible_rows(cur.rows, next, unanswered_only = false)
            cur.copy(band = next, selected = shown.firstOrNull()?.decision_id, expanded = true)
        }
        ensure_context(state.value.selected)
    }

    /** A tap selects a row; a tap on the selected row folds or unfolds its conversation. */
    fun select(decision_id: String) {
        state.update { cur ->
            if (cur.selected == decision_id) cur.copy(expanded = !cur.expanded) else cur.copy(selected = decision_id, expanded = true)
        }
        ensure_context(decision_id)
    }

    /** BETWEEN folds or unfolds one row's middle messages. */
    fun toggle_between(decision_id: String) {
        state.update { s -> s.copy(between = if (decision_id in s.between) s.between - decision_id else s.between + decision_id) }
    }

    /** One verdict on the selected row, then on to the next ungraded row below it. */
    fun grade(verdict: Verdict) {
        val cur = state.value
        if (cur.in_flight) return
        val row = selected_row(cur) ?: return
        if (LayaRules.operator_verdict(row.operator) == verdict) return
        state.update { it.copy(in_flight = true, status = null) }
        viewModelScope.launch {
            try {
                val out = LayaRules.grade_row(row, cur.surface, verdict, client)
                state.update { s -> graded(s, row, out) }
                ensure_context(state.value.selected)
            } finally {
                if (state.value.in_flight) state.update { it.copy(in_flight = false) }
            }
        }
    }

    /** A press's outcome on the CURRENT rows: the edits by key, the selection onward, the undo stack, a refusal's line. */
    private fun graded(s: LayaUi, row: RecentRow, out: GradeOutcome): LayaUi {
        var rows = s.rows
        for ((key, operator) in out.edits) rows = LayaRules.with_answer(rows, key, operator)
        val recorded = out.recorded
        var selected = s.selected
        // Onward only from the graded row: a row tapped while the answer was in flight is what is being read now.
        if (recorded != null && s.selected == row.decision_id) {
            val shown = LayaRules.visible_rows(rows, s.band, unanswered_only = false)
            val at = shown.indexOfFirst { it.decision_id == row.decision_id }
            if (at >= 0) selected = shown[LayaRules.next_unanswered(shown, at)].decision_id
        }
        val failure = out.failure
        return s.copy(
            rows = rows,
            selected = selected,
            expanded = if (selected != s.selected) true else s.expanded,
            in_flight = false,
            undo = if (recorded != null) s.undo + Answered(row.key, recorded.answer_id) else s.undo,
            status = failure?.let { Status(ok = false, text = LayaRules.fail_text(it)) },
            link = link_after(failure, s.link),
        )
    }

    /**
     * This list's last answer, taken back; the selection returns to its row. One press, as on the web. A row the page
     * no longer holds (a TO GRADE page after process death) is unanswered again, so a reload brings it back to land on.
     */
    fun undo() {
        val cur = state.value
        val top = cur.undo.lastOrNull() ?: return
        if (cur.in_flight) return
        state.update { it.copy(in_flight = true, status = null) }
        viewModelScope.launch {
            try {
                val failure = LayaRules.take_back(cur.surface, top.answer_id, client)
                var reload = false
                state.update { s ->
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
                            expanded = if (back != null && back.selected != s.selected) true else s.expanded,
                            return_key = if (back == null) top.key else null,
                            status = Status(ok = true, text = TAKEN_BACK),
                        )
                    }
                }
                if (reload) load() else ensure_context(state.value.selected)
            } finally {
                if (state.value.in_flight) state.update { it.copy(in_flight = false) }
            }
        }
    }

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
                    state.update { it.copy(link = link_after(got, it.link)) }
                }
            }
        }
    }

    /** The first message failed to load on its own: look the whole turn up again. */
    fun retry_context(decision_id: String) {
        state.value.rows.firstOrNull { it.decision_id == decision_id }?.let(::load_context)
    }

    // ── internals ────────────────────────────────────────────────────────────

    private fun take_page(page: RecentPage, todo: Boolean, note: Status?) {
        loaded_at_ms = SystemClock.elapsedRealtime()
        state.update { cur ->
            val rows = if (todo) LayaRules.keep_graded(page.rows, cur.rows, cur.undo.mapTo(HashSet()) { it.key }) else page.rows
            val land = cur.return_key?.let { LayaRules.return_to(rows, cur.band, it) }
                ?: LayaRules.landing(rows, cur.band, cur.selected)
            cur.copy(
                link = Link.linked,
                refusal = null,
                loading = false,
                loaded = true,
                rows = rows,
                surface = page.surface ?: LayaRules.DEFAULT_SURFACE,
                band = land.band,
                selected = land.selected,
                expanded = if (land.same) cur.expanded else true,
                between = cur.between.filterTo(HashSet()) { id -> rows.any { it.decision_id == id } },
                return_key = null,
                // A reload clears a stale line, except the "taken back" that sent it looking for the row.
                status = note ?: cur.status.takeIf { cur.return_key != null },
            )
        }
        ensure_context(state.value.selected)
        load_names(state.value.rows)
    }

    /** A failed LOAD replaces the list with its down state, a refusal included: there is nothing to grade. */
    private fun fail_link(fail: Fail) {
        state.update {
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

    private fun selected_row(ui: LayaUi): RecentRow? =
        LayaRules.visible_rows(ui.rows, ui.band, unanswered_only = false).firstOrNull { it.decision_id == ui.selected }

    /** A failed WRITE: a lost link takes the screen to its down state, a refusal is a line beside the bar and the list stays. */
    private fun link_after(fail: Fail?, link: Link): Link = when (fail) {
        Fail.Unlinked -> Link.unlinked
        Fail.BridgeDown -> Link.bridge_down
        is Fail.Refused, null -> link
    }

    private fun ensure_context(decision_id: String?) {
        val id = decision_id ?: return
        if (state.value.contexts[id] != null) return
        state.value.rows.firstOrNull { it.decision_id == id }?.let(::load_context)
    }

    private fun load_context(row: RecentRow) {
        val id = row.decision_id
        put_context(id) { ContextUi(loading = true) }
        viewModelScope.launch {
            val found = LayaRules.load_turn_context(row.text, row.at, client)
            put_context(id) { ContextUi(context = found) }
        }
    }

    private fun put_context(id: String, change: (ContextUi) -> ContextUi) {
        state.update { s -> s.copy(contexts = s.contexts + (id to change(s.contexts[id] ?: ContextUi()))) }
    }

    /** The rack's display name per session, a few at a time. A name is a nicety: the tmux name stands in until it lands. */
    private fun load_names(rows: List<RecentRow>) {
        names_job?.cancel()
        val subjects = rows.mapNotNull { it.subject }.distinct()
        names_job = viewModelScope.launch {
            val gate = Semaphore(NAME_CALLS)
            for (subject in subjects) {
                launch {
                    gate.withPermit {
                        val got = client.topic_labels(subject)
                        if (got is Call.Ok) state.update { it.copy(names = it.names + (subject to got.data)) }
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
        val expanded: Boolean,
        val undo: List<Answered>,
        val between: Set<String>,
    )

    private fun keep(reading: Reading) {
        saved[SAVED_BAND] = reading.band?.name
        saved[SAVED_SELECTED] = reading.selected
        saved[SAVED_EXPANDED] = reading.expanded
        saved[SAVED_UNDO] = APP_JSON.encodeToString(UNDO_LIST, reading.undo)
        saved[SAVED_BETWEEN] = ArrayList(reading.between)
    }

    private companion object {
        const val PREDATES_TO_GRADE = "This bridge predates TO GRADE: restart it (↻ › Restart bridge)"
        const val TAKEN_BACK = "Answer taken back"
        /** The web's wait between REFRESH INDEX and the second lookup (`LayaConversation.vue`). */
        const val INGEST_SETTLE_MS = 4_000L
        const val NAME_CALLS = 4
        /** A resume this soon after a page landed is a rotation or a glance, not a return from the pocket. */
        const val RETURN_FRESH_MS = 15_000L

        const val SAVED_BAND = "laya.band"
        const val SAVED_SELECTED = "laya.selected"
        const val SAVED_EXPANDED = "laya.expanded"
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
                expanded = saved.get<Boolean>(SAVED_EXPANDED) ?: true,
                undo = undo.orEmpty(),
                between = saved.get<ArrayList<String>>(SAVED_BETWEEN)?.toSet().orEmpty(),
            )
        }
    }
}
