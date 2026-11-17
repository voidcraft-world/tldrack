package world.voidcraft.tldrack.laya

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.clipRect
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import world.voidcraft.tldrack.ui.FuiButton
import world.voidcraft.tldrack.ui.MarkdownRules
import world.voidcraft.tldrack.ui.MarkdownText
import world.voidcraft.tldrack.ui.RackColors
import world.voidcraft.tldrack.ui.RackCorner
import world.voidcraft.tldrack.ui.RackMono
import world.voidcraft.tldrack.ui.RackShape
import world.voidcraft.tldrack.ui.TechLabel
import world.voidcraft.tldrack.ui.rememberMinuteClock

/**
 * LAYA: grade Laya's agent-rack marks from the phone — the RECENT view of `/laya-review.html?view=recent`, native.
 * Rules: `LayaRules`; fetches: `LayaViewModel`; the link to the Mac: plugins/phone-link/README.md.
 *
 * One strip (the link light; the tally, which is also the band filter; TO GRADE; ↻), the list, and one bar of grades
 * that acts on the selected row. The selected row is the one opened to its conversation, so the bar always answers
 * what is read. It is the app's only screen, so BACK leaves the app.
 */
@Composable
fun LayaScreen(vm: LayaViewModel) {
    val ui by vm.ui.collectAsStateWithLifecycle()
    // A return refetches (the rack keeps judging while the phone is in a pocket); a rotation is not a return.
    LifecycleResumeEffect(Unit) {
        vm.load_on_return()
        onPauseOrDispose { }
    }
    val now_ms = rememberMinuteClock().toInstant().toEpochMilli()
    val shown = remember(ui.rows, ui.band) { LayaRules.visible_rows(ui.rows, ui.band, unanswered_only = false) }
    val listing = ui.link == Link.linked && ui.loaded

    Column(Modifier.fillMaxSize().background(RackColors.void).safeDrawingPadding()) {
        TopStrip(ui, listing, vm)
        HorizontalDivider(color = RackColors.line)
        val body = Modifier.weight(1f).fillMaxWidth()
        when {
            ui.link == Link.unlinked -> DownPane("UNLINKED", LayaRules.UNLINKED_FIX, ui.loading, vm::load, body)
            ui.link == Link.bridge_down -> DownPane("BRIDGE DOWN", LayaRules.BRIDGE_DOWN_FIX, ui.loading, vm::load, body)
            ui.link == Link.refused -> DownPane("REFUSED", ui.refusal.orEmpty(), ui.loading, vm::load, body)
            !ui.loaded -> Quiet("ASKING THE MAC…", body)
            ui.rows.isEmpty() -> Quiet(if (ui.to_grade) "NOTHING TO GRADE" else "NO LIVE DECISIONS", body)
            shown.isEmpty() -> Quiet("NOTHING HERE", body)
            else -> VerdictList(ui, shown, now_ms, vm, body)
        }
        if (listing && shown.isNotEmpty()) {
            HorizontalDivider(color = RackColors.line)
            GradeBar(ui, shown.firstOrNull { it.decision_id == ui.selected }, vm)
        }
    }
}

/** The resting chrome: the link light, the three band readouts (each a filter), TO GRADE and refresh — one row. */
@Composable
private fun TopStrip(ui: LayaUi, listing: Boolean, vm: LayaViewModel) {
    val tally = remember(ui.rows) { LayaRules.recent_tally(ui.rows) }
    Row(
        Modifier.fillMaxWidth().padding(horizontal = 4.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        LinkLight(ui.link)
        // The readouts scroll before anything clips: TO GRADE and ↻ keep their place at any font scale.
        Row(
            Modifier.weight(1f).horizontalScroll(rememberScrollState()),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            if (listing) {
                val needs = LayaRules.needs_readout(tally)
                for (readout in LayaRules.band_readouts(tally)) {
                    val title = if (readout.band == Band.open && needs != null) "${readout.title}. ${needs.title}" else readout.title
                    StripToggle(
                        label = "${readout.mark} ${LayaRules.strip_text(readout)}",
                        on = ui.band == readout.band,
                        color = if (readout.warn) RackColors.warn else RackColors.accent,
                        description = title,
                        on_toggle = { vm.press_band(readout.band) },
                    )
                }
            } else {
                link_word(ui.link)?.let { TechLabel(it, color = link_color(ui.link), modifier = Modifier.padding(horizontal = 4.dp)) }
            }
        }
        if (listing) {
            StripToggle(
                label = "TO GRADE",
                on = ui.to_grade,
                color = RackColors.mint,
                description = "Only the turns you have not graded",
                on_toggle = { vm.set_to_grade(!ui.to_grade) },
            )
            GlyphButton("↻", "Refresh", enabled = !ui.loading, on_click = vm::load)
        }
    }
}

/**
 * Whether the Mac answers, as one light where the back arrow of a multi-screen app would sit: mint once a signed
 * reply proved the link, amber while it is down. The word rides beside it only until the list arrives, and not at all
 * while a down pane already says it; the accessible name always carries it.
 */
@Composable
private fun LinkLight(link: Link) {
    Box(
        Modifier
            .size(STRIP_CONTROL_HEIGHT)
            .semantics { contentDescription = link_name(link) },
        contentAlignment = Alignment.Center,
    ) {
        Text("●", color = link_color(link), fontSize = 16.sp)
    }
}

private fun link_name(link: Link): String = when (link) {
    Link.checking -> "CHECKING"
    Link.linked -> "LINKED"
    Link.unlinked -> "UNLINKED"
    Link.bridge_down -> "BRIDGE DOWN"
    Link.refused -> "REFUSED"
}

/** The strip's word while no list is shown: only where the body does not already say it (a down pane does). */
private fun link_word(link: Link): String? = when (link) {
    Link.checking, Link.linked -> link_name(link)
    Link.unlinked, Link.bridge_down, Link.refused -> null
}

private fun link_color(link: Link): Color = when (link) {
    Link.checking -> RackColors.dim
    Link.linked -> RackColors.mint
    Link.unlinked, Link.bridge_down, Link.refused -> RackColors.warn
}

@Composable
private fun StripToggle(label: String, on: Boolean, color: Color, description: String, on_toggle: () -> Unit) {
    Box(
        Modifier
            .heightIn(min = STRIP_CONTROL_HEIGHT)
            .border(1.dp, if (on) color else RackColors.line, RackShape)
            .background(if (on) color.copy(alpha = 0.16f) else Color.Transparent, RackShape)
            .toggleable(value = on, role = Role.Checkbox, onValueChange = { on_toggle() })
            .semantics { contentDescription = description }
            .padding(horizontal = 8.dp),
        contentAlignment = Alignment.Center,
    ) {
        TechLabel(label, color = color, size = 12.sp)
    }
}

@Composable
private fun GlyphButton(glyph: String, description: String, enabled: Boolean = true, on_click: () -> Unit) {
    Box(
        Modifier
            .size(STRIP_CONTROL_HEIGHT)
            .clickable(enabled = enabled, role = Role.Button, onClick = on_click)
            .semantics { contentDescription = description },
        contentAlignment = Alignment.Center,
    ) {
        Text(glyph, color = if (enabled) RackColors.mint else RackColors.dim, fontSize = 22.sp)
    }
}

/** No list to show: the one thing that is wrong, and the one way to fix it. */
@Composable
private fun DownPane(head: String, fix: String, loading: Boolean, on_retry: () -> Unit, modifier: Modifier) {
    Column(
        modifier.padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp, Alignment.CenterVertically),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        TechLabel(head, color = RackColors.warn, size = 14.sp)
        Text(fix, color = RackColors.dim, fontSize = 14.sp, textAlign = TextAlign.Center)
        FuiButton(if (loading) "CHECKING…" else "RETRY", on_click = on_retry, enabled = !loading)
    }
}

@Composable
private fun Quiet(label: String, modifier: Modifier) {
    Box(modifier.padding(24.dp), contentAlignment = Alignment.Center) {
        TechLabel(label, color = RackColors.dim)
    }
}

/**
 * The rows, and the opened row's conversation as items of its own after its head — one per part and one per message —
 * so a long turn is laid out as it scrolls in, never all at once. `row_frame` draws those items as one card.
 */
@Composable
private fun VerdictList(ui: LayaUi, shown: List<RecentRow>, now_ms: Long, vm: LayaViewModel, modifier: Modifier) {
    val list_state = rememberLazyListState()
    // Only the selected row opens, so every row above it is one item: its head's index is its place in `shown`.
    val at = shown.indexOfFirst { it.decision_id == ui.selected }
    // Follow the SELECTION when it moves (a grade, a band, a load), never the rows: an answer landing on the row being
    // read must not yank the list. The followed key is saved with the list's own position, so a rotation that
    // restores the reader deep inside a conversation is not taken for a move.
    var followed by rememberSaveable { mutableStateOf<String?>(null) }
    val follow = "${ui.selected}|${ui.band}"
    LaunchedEffect(follow, at >= 0) {
        if (at >= 0 && followed != follow) {
            list_state.animateScrollToItem(at)
            followed = follow
        }
    }
    LazyColumn(modifier, state = list_state, contentPadding = PaddingValues(12.dp)) {
        shown.forEachIndexed { index, row ->
            val selected = row.decision_id == ui.selected
            val opened = selected && ui.expanded
            val name = display_name(row, ui)
            val rule = rule_of(LayaRules.grade(row))
            item(key = row.decision_id) {
                VerdictHead(
                    row = row,
                    selected = selected,
                    opened = opened,
                    name = name,
                    rule = rule,
                    now_ms = now_ms,
                    on_tap = { vm.select(row.decision_id) },
                    modifier = Modifier.padding(top = if (index == 0) 0.dp else ROW_GAP),
                )
            }
            if (opened) turn_items(row, name, rule, ui.contexts[row.decision_id], row.decision_id in ui.between, vm)
        }
    }
}

private fun display_name(row: RecentRow, ui: LayaUi): String {
    val conversation = (ui.contexts[row.decision_id]?.context as? TurnContext.Found)?.title
    val rack = LayaRules.rack_name(row.subject?.let { ui.names[it] }, row.at)
    return LayaRules.review_title(row.subject, rack, conversation)
}

/** The grade is the row's left rule, so RIGHT/WRONG reads at a glance down the list. */
private fun rule_of(grade: Grade?): Color? = when (grade) {
    Grade.right -> RackColors.mint
    Grade.wrong -> RackColors.warn
    null -> null
}

/** The edges of a row's card one list item draws: an opened row is several items that read as one card. */
private enum class Segment(val top: Boolean, val bottom: Boolean) {
    whole(true, true),
    head(true, false),
    middle(false, false),
    foot(false, true),
}

/**
 * One item's share of a row's card: the glass fill, the grade's left rule, and the border, rounded only on the edges
 * this segment closes. An open edge runs the rounded rectangle past the item, where the clip cuts it.
 */
private fun Modifier.row_frame(segment: Segment, selected: Boolean, rule: Color?): Modifier = drawWithContent {
    val stroke = (if (selected) 2.dp else 1.dp).toPx()
    val corner = CornerRadius(RackCorner.toPx())
    val reach = corner.x + stroke
    val top = if (segment.top) 0f else -reach
    val bottom = if (segment.bottom) size.height else size.height + reach
    val ink = if (selected) RackColors.accent else RackColors.line
    clipRect {
        drawRoundRect(RackColors.glass, topLeft = Offset(0f, top), size = Size(size.width, bottom - top), cornerRadius = corner)
        if (rule != null) drawRect(rule, size = Size(4.dp.toPx(), size.height))
    }
    drawContent()
    clipRect {
        val half = stroke / 2
        drawRoundRect(
            ink,
            topLeft = Offset(half, top + half),
            size = Size(size.width - stroke, bottom - top - stroke),
            cornerRadius = corner,
            style = Stroke(stroke),
        )
    }
}

/** The row's head: the only part that takes the tap (select, or fold an opened row), so reading never folds it. */
@Composable
private fun VerdictHead(
    row: RecentRow,
    selected: Boolean,
    opened: Boolean,
    name: String,
    rule: Color?,
    now_ms: Long,
    on_tap: () -> Unit,
    modifier: Modifier,
) {
    Column(
        modifier
            .fillMaxWidth()
            .row_frame(if (opened) Segment.head else Segment.whole, selected, rule)
            .clickable(onClick = on_tap)
            .padding(start = 14.dp, end = 12.dp, top = 10.dp, bottom = if (opened) 4.dp else 10.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            val words = LayaRules.BAND_WORDS.getValue(row.band)
            Text(LayaRules.BAND_MARK.getValue(row.band), fontSize = 18.sp, modifier = Modifier.semantics { contentDescription = words })
            Text(
                name,
                color = RackColors.text,
                fontSize = 15.sp,
                fontWeight = FontWeight.Medium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
            TechLabel(LayaRules.age_text(row.at, now_ms), color = RackColors.dim, size = 11.sp)
        }
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            TechLabel("p ${LayaRules.score_text(row.p)}", color = RackColors.accent, size = 12.sp)
            LayaRules.edge_note(row)?.let { note ->
                TechLabel(note.text, color = RackColors.warn, size = 11.sp, modifier = Modifier.semantics { contentDescription = note.title })
            }
            LayaRules.drawn_note(row)?.let { TechLabel(it, color = RackColors.dim, size = 11.sp) }
            Spacer(Modifier.weight(1f))
            AnswerLine(row)
        }
        if (!opened) {
            // Interpolated text, never markup: an agent's closing message, from disk.
            Text(remember(row.text) { MarkdownRules.plain(row.text) }, color = RackColors.text, fontSize = 14.sp, lineHeight = 20.sp, maxLines = 4, overflow = TextOverflow.Ellipsis)
        }
    }
}

/** The operator's answer, and RIGHT/WRONG once there is one to grade against, at the end of the meta line. */
@Composable
private fun AnswerLine(row: RecentRow) {
    val verdict = LayaRules.operator_verdict(row.operator) ?: return
    TechLabel(verdict_label(verdict), color = RackColors.accent, size = 12.sp)
    when (LayaRules.grade(row)) {
        Grade.right -> TechLabel("RIGHT", color = RackColors.mint, size = 12.sp)
        Grade.wrong -> TechLabel("WRONG", color = RackColors.warn, size = 12.sp)
        null -> Unit
    }
}

/**
 * The turn behind the opened row, up to the judged close: the first request, what came between (folded), and the
 * closing message Laya scored — each its own list item. The closing text is drawn whatever the index says, so the row
 * can always be graded.
 */
private fun LazyListScope.turn_items(row: RecentRow, name: String, rule: Color?, ui: ContextUi?, between_open: Boolean, vm: LayaViewModel) {
    val id = row.decision_id
    val context = ui?.context
    val busy = ui?.busy == true
    val error = ui?.error
    fun part(key: String, last: Boolean = false, content: @Composable () -> Unit) {
        item(key = "$id:$key") { TurnPart(rule, last, content) }
    }
    when {
        ui == null || ui.loading -> part("finding") { TechLabel("FINDING THE CONVERSATION…", color = RackColors.dim, size = 11.sp) }
        context is TurnContext.Found -> found_items(row, name, rule, context, busy, between_open, vm, ::part)
        context is TurnContext.Missing -> part("missing") {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(context.message, color = RackColors.dim, fontSize = 13.sp)
                FuiButton(if (busy) "INDEXING…" else "REFRESH INDEX", on_click = { vm.refresh_index(id) }, enabled = !busy)
            }
        }
    }
    part("closing", last = error == null) { Message("CLOSING", row.text, CLOSING_TINT) }
    if (error != null) part("error", last = true) { Text(error, color = RackColors.warn, fontSize = 13.sp) }
}

private fun LazyListScope.found_items(
    row: RecentRow,
    name: String,
    rule: Color?,
    context: TurnContext.Found,
    busy: Boolean,
    between_open: Boolean,
    vm: LayaViewModel,
    part: (String, Boolean, @Composable () -> Unit) -> Unit,
) {
    val id = row.decision_id
    // The session's own title, only when the heading shows another name for it.
    context.title?.takeIf { it.trim() != name }?.let { title ->
        part("title", false) { Text(title, color = RackColors.accent, fontSize = 14.sp, fontWeight = FontWeight.Medium) }
    }
    when (val first = context.first_user) {
        is FirstUser.Found -> part("first", false) { Message("YOU · FIRST", first.message.text, ASK_TINT) }
        FirstUser.None -> part("first", false) { TechLabel("NO FIRST MESSAGE IN THE INDEX", color = RackColors.dim, size = 11.sp) }
        FirstUser.Unknown -> part("first", false) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                TechLabel("FIRST MESSAGE DID NOT LOAD", color = RackColors.warn, size = 11.sp, modifier = Modifier.weight(1f))
                FuiButton("RETRY", on_click = { vm.retry_context(id) })
            }
        }
    }
    val first_seq = (context.first_user as? FirstUser.Found)?.message?.seq
    val middle = context.messages.filter { it.seq < context.closing_seq && it.seq != first_seq }
    if (middle.isEmpty() && context.before == null) return
    part("between", false) {
        TechLabel(
            "${if (between_open) "▾" else "▸"} BETWEEN · ${middle.size}",
            color = RackColors.mint,
            size = 12.sp,
            modifier = Modifier.fillMaxWidth().clickable { vm.toggle_between(id) }.padding(vertical = 12.dp),
        )
    }
    if (!between_open) return
    if (context.before != null) {
        part("earlier", false) { FuiButton(if (busy) "LOADING…" else "LOAD EARLIER", on_click = { vm.earlier(id) }, enabled = !busy) }
    }
    val asked = LayaRules.ask_seq(context.messages, context.closing_seq)
    items(middle, key = { "$id:m${it.seq}" }) { message ->
        val who = when {
            message.seq == asked -> "YOU · ASKED"
            LayaRules.is_human_role(message.role) -> "YOU"
            else -> message.role.uppercase()
        }
        TurnPart(rule, last = false) { Message(who, message.text, if (message.seq == asked) ASK_TINT else null) }
    }
}

/** One item of an opened row below its head: the card's sides (and its foot on the last), the row's inset. */
@Composable
private fun TurnPart(rule: Color?, last: Boolean, content: @Composable () -> Unit) {
    Box(
        Modifier
            .fillMaxWidth()
            .row_frame(if (last) Segment.foot else Segment.middle, selected = true, rule)
            .padding(start = 14.dp, end = 12.dp, bottom = if (last) 10.dp else 8.dp),
    ) {
        content()
    }
}

@Composable
private fun Message(who: String, text: String, tint: Color?) {
    Column(
        Modifier
            .fillMaxWidth()
            .then(if (tint != null) Modifier.background(tint, RackShape) else Modifier)
            .padding(8.dp),
        verticalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        TechLabel(who, color = RackColors.dim, size = 10.sp)
        MarkdownText(text)
    }
}

/** The grades for the selected row, then UNDO: five peers at one height, so the bar reads as one control. */
@Composable
private fun GradeBar(ui: LayaUi, row: RecentRow?, vm: LayaViewModel) {
    val current = row?.let { LayaRules.operator_verdict(it.operator) }
    val answerable = row != null && row.reference_open != null && !ui.in_flight
    val status = ui.status
    Column(Modifier.fillMaxWidth().padding(8.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        when {
            status != null -> Text(
                status.text,
                color = if (status.ok) RackColors.mint else RackColors.warn,
                fontSize = 13.sp,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
            row != null && row.reference_open == null -> TechLabel("NO CURRENT CARD HOLDS THIS TEXT", color = RackColors.dim, size = 11.sp)
        }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            for (face in FACES) {
                GradeButton(face.glyph, face.word, face.title, on = current == face.verdict, enabled = answerable, modifier = Modifier.weight(1f)) {
                    vm.grade(face.verdict)
                }
            }
            GradeButton(
                "↶",
                "UNDO",
                "Take back this list's last answer",
                on = false,
                enabled = ui.undo.isNotEmpty() && !ui.in_flight,
                modifier = Modifier.weight(1f),
                on_click = vm::undo,
            )
        }
    }
}

@Composable
private fun GradeButton(
    glyph: String,
    word: String,
    title: String,
    on: Boolean,
    enabled: Boolean,
    modifier: Modifier,
    on_click: () -> Unit,
) {
    val ink = if (enabled || on) RackColors.mint else RackColors.dim
    Column(
        modifier
            .heightIn(min = GRADE_BUTTON_HEIGHT)
            .border(1.dp, if (on) RackColors.mint else if (enabled) RackColors.mint.copy(alpha = 0.5f) else RackColors.line, RackShape)
            .background(if (on) RackColors.mint.copy(alpha = 0.18f) else Color.Transparent, RackShape)
            .clickable(enabled = enabled, role = Role.Button, onClick = on_click)
            .semantics {
                contentDescription = "$word: $title"
                if (on) stateDescription = "your answer"
            }
            .padding(vertical = 6.dp, horizontal = 2.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(glyph, color = ink, fontSize = 18.sp)
        Text(
            word,
            color = ink,
            fontFamily = RackMono,
            fontSize = 10.sp,
            letterSpacing = 0.04.em,
            maxLines = 1,
            softWrap = false,
            overflow = TextOverflow.Clip,
        )
    }
}

/** A grade button's face: the glyph and word, and the sentence that is its accessible name (the web's title). */
private data class Face(val verdict: Verdict, val glyph: String, val word: String, val title: String)

// Text-presentation glyphs only, so a face takes its ink and dims with the bar: the web's ⏳ is an emoji wherever it
// is drawn, and its ⚠ would read as the strip's ⚠️ band here (one mark, one meaning).
private val FACES = listOf(
    Face(Verdict.done, "✓", "DONE", "The turn finished what it set out to do"),
    Face(Verdict.running, "◔", "RUNNING", "Work still open, but the agent was only waiting on its own agents or jobs: you were not needed"),
    Face(Verdict.you, "△", "NEEDS YOU", "Work still open and waiting on you: a question, a decision, a go-ahead, or blocked"),
    Face(Verdict.skip, "»", "SKIP", "Ambiguous: no label, and it leaves training too"),
)

private fun verdict_label(verdict: Verdict): String =
    FACES.firstOrNull { it.verdict == verdict }?.let { "${it.glyph} ${it.word}" } ?: "OPEN"

/**
 * Android's touch minimum (48dp, what Material3 pads its own buttons to): these are hand-built boxes, so they take it
 * as their size. The grades, pressed most, get more.
 */
private val STRIP_CONTROL_HEIGHT = 48.dp
private val GRADE_BUTTON_HEIGHT = 56.dp
private val ROW_GAP = 8.dp

private val ASK_TINT = RackColors.mint.copy(alpha = 0.08f)
private val CLOSING_TINT = RackColors.warn.copy(alpha = 0.10f)
