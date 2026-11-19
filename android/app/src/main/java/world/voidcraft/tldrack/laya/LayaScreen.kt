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
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.text.TextAutoSize
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
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
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.TextUnit
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
 * One strip (the link light; the tally, which is also the band filter; TO GRADE; ↻), the pager's bar, ONE conversation
 * (swipe left or right for the next or the previous one), and one bar of grades that acts on the cursor turn. The
 * cursor turn is the one opened to its closing message, so the bar always answers what is read. It is the app's only
 * screen, so BACK leaves the app.
 */
@Composable
fun LayaScreen(vm: LayaViewModel) {
    val ui by vm.ui.collectAsStateWithLifecycle()
    // A return refetches (the rack keeps judging while the phone is in a pocket); a rotation is not a return.
    LifecycleResumeEffect(Unit) {
        vm.load_on_return()
        vm.watch_link()
        onPauseOrDispose { vm.stop_watching() }
    }
    val now_ms = rememberMinuteClock().toInstant().toEpochMilli()
    val listing = ui.link == Link.linked && ui.loaded
    val paging = listing && ui.bands.isNotEmpty()

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
            ui.bands.isEmpty() -> Quiet("NOTHING HERE", body)
            else -> {
                PagerBar(ui, vm)
                ConversationPager(ui, now_ms, vm, body)
            }
        }
        if (paging) {
            HorizontalDivider(color = RackColors.line)
            GradeBar(ui, ui.cursor, vm)
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

/** ◀ `3 / 21+` ▶: where the pager stands, and the visible twin of the swipe (a gesture is never the only way). */
@Composable
private fun PagerBar(ui: LayaUi, vm: LayaViewModel) {
    val at = ui.conv_at
    val n = ui.bands.size
    Row(Modifier.fillMaxWidth().padding(horizontal = 4.dp), verticalAlignment = Alignment.CenterVertically) {
        GlyphButton("◀", "Previous conversation", enabled = at > 0 && !ui.loading) { vm.step(-1) }
        Box(Modifier.weight(1f), contentAlignment = Alignment.Center) {
            TechLabel(
                "${at + 1} / $n${if (ui.has_older) "+" else ""}",
                color = RackColors.accent,
                size = 15.sp,
                modifier = Modifier.semantics {
                    contentDescription = "Conversation ${at + 1} of $n${if (ui.has_older) ", more older ones to load" else ""}"
                },
            )
        }
        GlyphButton(
            if (ui.loading_older) "…" else "▶",
            if (at + 1 < n) "Next conversation" else "Load older conversations",
            enabled = (at + 1 < n || ui.has_older) && !ui.loading && !ui.loading_older,
        ) { vm.step(1) }
    }
}

/**
 * ONE conversation fills the screen; a swipe left or right is the next or the previous one, and scrolling up and down
 * never leaves it (`LayaThreads` owns what a conversation is, and why one at a time). One page past the last is the
 * older page, which loads on arrival.
 *
 * The ViewModel owns where the pager stands. A settled swipe tells it; a grade that moves on, or ◀ ▶, comes back here
 * as `conv_at` and is followed. Pages are keyed by conversation, so a regrouping (the join landing) keeps the reader's
 * conversation under the thumb.
 */
@Composable
private fun ConversationPager(ui: LayaUi, now_ms: Long, vm: LayaViewModel, modifier: Modifier) {
    val count = ui.bands.size + if (ui.has_older) 1 else 0
    val pager = rememberPagerState(initialPage = ui.conv_at) { count }
    LaunchedEffect(ui.conv_at, ui.conv_key, ui.loading, ui.loading_older) {
        // While an older page loads the reader waits on its page; a load that brought nothing slides back.
        if (!ui.loading_older && pager.currentPage != ui.conv_at) pager.animateScrollToPage(ui.conv_at)
    }
    LaunchedEffect(pager) {
        snapshotFlow { pager.settledPage }.collect { vm.show_conversation(it) }
    }
    HorizontalPager(
        state = pager,
        modifier = modifier,
        // The older page's key is its place, never a name: once it loads, that place holds a real conversation.
        key = { page -> ui.bands.getOrNull(page)?.key ?: "older:$page" },
    ) { page ->
        val band = ui.bands.getOrNull(page)
        if (band == null) {
            Quiet(if (ui.loading_older) "LOADING OLDER…" else "OLDER CONVERSATIONS", Modifier.fillMaxSize())
        } else {
            ConversationPage(ui, band, shown = page == ui.conv_at, now_ms = now_ms, vm = vm)
        }
    }
}

/** What the needle fallback found for a conversation the join did not name: any of its turns' own lookups. */
private fun found_head(ui: LayaUi, band: ThreadBand): FoundHead? =
    band.rows.firstNotNullOfOrNull { ui.contexts[it.decision_id]?.context as? TurnContext.Found }
        ?.let { FoundHead(it.title, (it.first_user as? FirstUser.Found)?.message?.text) }

/** Where a turn sits in the page's list. `top`: it is the conversation's first turn, so the page need not move to show it. */
private data class TurnPlace(val index: Int, val top: Boolean)

/**
 * The list index to bring into view for the cursor turn: its prompt when it is the first turn under one (the prompt
 * and its answer read together), else the turn itself. Counted exactly as `ConversationPage` emits its items: every
 * turn before the cursor is one item, because only the cursor turn opens.
 */
private fun turn_place(groups: List<TurnGroup>, has_first: Boolean, cursor: String?): TurnPlace? {
    var index = if (has_first) 1 else 0
    groups.forEachIndexed { g, group ->
        if (group.skipped > 0) index += 1
        val prompt = if (group.anchor != null) index++ else null
        group.rows.forEachIndexed { i, row ->
            if (row.decision_id == cursor) return TurnPlace(if (i == 0 && prompt != null) prompt else index, top = g == 0 && i == 0)
            index += 1
        }
    }
    return null
}

/**
 * One conversation: its head (the title, large, always in view), then one list: the first message, and its turns
 * oldest first under the prompt each answered. The cursor turn is opened to its closing message and carries the grade
 * bar's answer; every other turn is its last lines.
 */
@Composable
private fun ConversationPage(ui: LayaUi, band: ThreadBand, shown: Boolean, now_ms: Long, vm: LayaViewModel) {
    val join_ok = ui.join == Join.ok
    val found = found_head(ui, band)
    // The rack's display name for the session, while it is still that session's: it stands in for the tmux name.
    val newest = band.rows.last()
    val name = LayaRules.rack_name(newest.subject?.let { ui.names[it] }, newest.at)
    val head = remember(band, now_ms, join_ok, found, name) { LayaThreads.band_head(band, now_ms, join_ok, found, name = name) }
    val groups = remember(band, ui.threads) { LayaThreads.turn_groups(band, ui.threads) }
    // The first message prints above the turns unless a group already prints it as its prompt (a second telling).
    val first = remember(head, groups, band) {
        if (LayaThreads.first_in_groups(groups, band.thread?.first_user)) null
        else head.first_user?.let(LayaThreads::prompt_text)?.ifEmpty { null }?.let { if (head.first_clipped) "$it …" else it }
    }
    // A neighbouring page, seen mid-swipe, opens the turn it will land on, so nothing unfolds under the thumb on arrival.
    val cursor = if (shown) ui.selected else LayaThreads.opening_row(band)?.decision_id
    val list_state = rememberLazyListState()
    val place = remember(groups, first, cursor) { turn_place(groups, first != null, cursor) }
    // Follow the CURSOR when it moves (a grade, a tap), never the rows: an answer landing on the turn being read must
    // not yank the list. A conversation opens at its top (the title, the first message) unless its turn to grade lies
    // further down. Saved with the list's own position, so a rotation deep inside a conversation is not taken for a move.
    var followed by rememberSaveable { mutableStateOf<String?>(null) }
    LaunchedEffect(cursor, place?.index, shown) {
        if (!shown || cursor == null || place == null || followed == cursor) return@LaunchedEffect
        if (followed != null || !place.top) list_state.animateScrollToItem(place.index)
        followed = cursor
    }
    Column(Modifier.fillMaxSize()) {
        ConversationHead(head)
        HorizontalDivider(color = RackColors.line)
        LazyColumn(Modifier.weight(1f).fillMaxWidth(), state = list_state, contentPadding = PaddingValues(start = 12.dp, end = 12.dp, bottom = 12.dp)) {
            if (first != null) {
                item(key = "first") {
                    Prompt("YOUR FIRST MESSAGE", age_of(band.thread?.first_user?.ts, now_ms), first, operator = true, size = 17.sp, save_key = "first")
                }
            }
            groups.forEachIndexed { g, group ->
                if (group.skipped > 0) {
                    item(key = "gap:$g") {
                        Text(
                            "⋯ ${group.skipped} more prompt${if (group.skipped == 1) "" else "s"} in between, none with a verdict on this page",
                            color = RackColors.dim,
                            fontSize = 13.sp,
                            modifier = Modifier.padding(top = ROW_GAP, start = 2.dp),
                        )
                    }
                }
                group.anchor?.let { anchor ->
                    item(key = "prompt:$g") {
                        val text = remember(anchor) { LayaThreads.prompt_text(anchor.text).let { if ((anchor.chars ?: 0) > anchor.text.length) "$it …" else it } }
                        Prompt(LayaThreads.group_head(group), age_of(anchor.ts, now_ms), text, operator = group.by_operator, size = 16.sp, save_key = "prompt:$g")
                    }
                }
                group.rows.forEachIndexed { i, row ->
                    val id = row.decision_id
                    val turn = ui.threads?.decisions?.get(id)?.takeIf { it.linked }
                    // Not on a rack-opened group's first turn: its prompt head already says RACK.
                    val wake = if (turn != null && (group.by_operator || i > 0)) LayaThreads.wake_note(turn.woke_by, turn.request?.text) else null
                    val on = id == cursor
                    val rule = rule_of(LayaRules.grade(row))
                    item(key = id) {
                        TurnHead(
                            row = row,
                            on = on,
                            rule = rule,
                            now_ms = now_ms,
                            wake = wake,
                            preview = if (on) null else remember(row.text, turn) {
                                LayaThreads.tail_text(MarkdownRules.plain(LayaThreads.preview_text(row, turn)), PREVIEW_LINES)
                            },
                            on_tap = { vm.select(id) },
                        )
                    }
                    if (on) turn_items(row, rule, ui.contexts[id], id in ui.between, vm)
                }
            }
            if (LayaThreads.todo_count(band) == 0) {
                item(key = "end") {
                    TechLabel("✓ EVERY TURN HERE IS GRADED", color = RackColors.mint, size = 13.sp, modifier = Modifier.padding(top = 16.dp, start = 2.dp))
                }
            }
        }
    }
}

private fun age_of(ts: Double?, now_ms: Long): String? = ts?.let { LayaRules.age_since((it * 1_000).toLong(), now_ms) }

/**
 * The conversation's own facts, once, above its turns and out of the scroll: the title large (it is what tells one
 * conversation from the next), then the session it ran in, how many turns are here and how many are left to grade.
 */
@Composable
private fun ConversationHead(head: ThreadHead) {
    Column(
        Modifier.fillMaxWidth().padding(start = 14.dp, end = 12.dp, top = 6.dp, bottom = 10.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        Text(head.title, color = RackColors.text, fontSize = 22.sp, lineHeight = 28.sp, fontWeight = FontWeight.Bold, maxLines = 3, overflow = TextOverflow.Ellipsis)
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            val meta = listOfNotNull(head.subject, head.meta.ifEmpty { null }).joinToString(" · ")
            TechLabel(meta, color = RackColors.mint, size = 13.sp, modifier = Modifier.weight(1f))
            for (tag in head.tags) {
                Box(Modifier.border(1.dp, RackColors.warn.copy(alpha = 0.6f), RackShape).semantics { contentDescription = tag.title }.padding(horizontal = 6.dp, vertical = 2.dp)) {
                    TechLabel(tag.text, color = RackColors.warn, size = 12.sp)
                }
            }
        }
    }
}

/**
 * A prompt: the first message, or the one a group of turns answered. Clamped to a few lines with MORE, which is also
 * what keeps a pasted document from pushing its turns off the screen. The operator's own prompts take the mint wash;
 * one the rack or a lead typed takes a plain frame and says whose it was.
 */
@Composable
private fun Prompt(label: String, age: String?, text: String, operator: Boolean, size: TextUnit, save_key: String) {
    var open by rememberSaveable(save_key) { mutableStateOf(false) }
    var clipped by remember(text) { mutableStateOf(false) }
    Column(
        Modifier
            .padding(top = ROW_GAP)
            .fillMaxWidth()
            .then(if (operator) Modifier.background(ASK_TINT, RackShape) else Modifier.border(1.dp, RackColors.line, RackShape))
            .padding(start = 12.dp, end = 12.dp, top = 10.dp, bottom = if (clipped || open) 0.dp else 10.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            SectionLabel(label, if (operator) RackColors.mint else RackColors.warn, Modifier.weight(1f))
            age?.let { TechLabel(it, color = RackColors.dim, size = 12.sp) }
        }
        Text(
            text,
            color = RackColors.text,
            fontSize = size,
            lineHeight = size * 1.45f,
            maxLines = if (open) Int.MAX_VALUE else PROMPT_LINES,
            overflow = TextOverflow.Ellipsis,
            onTextLayout = { if (!open) clipped = it.hasVisualOverflow },
        )
        if (clipped || open) {
            TechLabel(
                if (open) "▴ LESS" else "▾ MORE",
                color = RackColors.mint,
                size = 13.sp,
                modifier = Modifier.fillMaxWidth().clickable(role = Role.Button) { open = !open }.padding(vertical = 12.dp),
            )
        }
    }
}

/** A section's label: bold and in a reading ink, never the faintest text of its section. */
@Composable
private fun SectionLabel(text: String, color: Color, modifier: Modifier = Modifier) {
    Text(text, modifier = modifier, color = color, fontFamily = RackMono, fontSize = 13.sp, fontWeight = FontWeight.Bold, letterSpacing = 0.06.em)
}

/** The grade is the turn's left rule, so RIGHT/WRONG reads at a glance down the conversation. */
private fun rule_of(grade: Grade?): Color? = when (grade) {
    Grade.right -> RackColors.mint
    Grade.wrong -> RackColors.warn
    null -> null
}

/** The edges of a turn's card one list item draws: the opened turn is several items that read as one card. */
private enum class Segment(val top: Boolean, val bottom: Boolean) {
    whole(true, true),
    head(true, false),
    middle(false, false),
    foot(false, true),
}

/**
 * One item's share of a turn's card: the glass fill, the grade's left rule, and the border, rounded only on the edges
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

/**
 * One judged turn's head: what Laya drew and scored, the operator's answer, and (closed) the turn's last lines. A tap
 * makes it the cursor turn. The conversation's name is not here: the page's head says it once.
 */
@Composable
private fun TurnHead(
    row: RecentRow,
    on: Boolean,
    rule: Color?,
    now_ms: Long,
    wake: WakeNote?,
    preview: String?,
    on_tap: () -> Unit,
) {
    Column(
        Modifier
            .padding(top = ROW_GAP)
            .fillMaxWidth()
            .row_frame(if (on) Segment.head else Segment.whole, on, rule)
            .clickable(onClick = on_tap)
            .padding(start = 14.dp, end = 12.dp, top = 10.dp, bottom = if (on) 4.dp else 10.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            val words = LayaRules.BAND_WORDS.getValue(row.band)
            Text(LayaRules.BAND_MARK.getValue(row.band), fontSize = 18.sp, modifier = Modifier.semantics { contentDescription = words })
            TechLabel("p ${LayaRules.score_text(row.p)}", color = RackColors.accent, size = 13.sp)
            LayaRules.edge_note(row)?.let { note ->
                TechLabel(note.text, color = RackColors.warn, size = 12.sp, modifier = Modifier.semantics { contentDescription = note.title })
            }
            LayaRules.drawn_note(row)?.let { TechLabel(it, color = RackColors.dim, size = 12.sp) }
            Spacer(Modifier.weight(1f))
            AnswerLine(row)
            TechLabel(LayaRules.age_text(row.at, now_ms), color = RackColors.dim, size = 12.sp)
        }
        if (wake != null) {
            Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                TechLabel(wake.tag, color = RackColors.warn, size = 12.sp)
                Text(wake.line, color = RackColors.dim, fontSize = 14.sp, lineHeight = 20.sp)
            }
        }
        if (preview != null) {
            // Interpolated text, never markup: an agent's closing message, from disk.
            Text(preview, color = RackColors.text, fontSize = 16.sp, lineHeight = 24.sp, maxLines = PREVIEW_LINES + 2, overflow = TextOverflow.Ellipsis)
        }
    }
}

/** The operator's answer, and RIGHT/WRONG once there is one to grade against, at the end of the meta line. */
@Composable
private fun AnswerLine(row: RecentRow) {
    val verdict = LayaRules.operator_verdict(row.operator) ?: return
    TechLabel(verdict_label(verdict), color = RackColors.accent, size = 13.sp)
    when (LayaRules.grade(row)) {
        Grade.right -> TechLabel("RIGHT", color = RackColors.mint, size = 13.sp)
        Grade.wrong -> TechLabel("WRONG", color = RackColors.warn, size = 13.sp)
        null -> Unit
    }
}

/**
 * The cursor turn below its head, up to the judged close: what came between its prompt and its end (folded), and the
 * closing message Laya scored — each its own list item, so a long turn is laid out as it scrolls in, never all at
 * once. The closing text is drawn whatever the index says, so the turn can always be graded.
 */
private fun LazyListScope.turn_items(row: RecentRow, rule: Color?, ui: ContextUi?, between_open: Boolean, vm: LayaViewModel) {
    val id = row.decision_id
    val context = ui?.context
    val busy = ui?.busy == true
    val error = ui?.error
    fun part(key: String, last: Boolean = false, content: @Composable () -> Unit) {
        item(key = "$id:$key") { TurnPart(rule, last, content) }
    }
    when {
        ui == null || ui.loading -> part("finding") { TechLabel("FINDING THE CONVERSATION…", color = RackColors.dim, size = 12.sp) }
        context is TurnContext.Found -> between_items(id, rule, context, busy, between_open, vm, ::part)
        context is TurnContext.Missing -> part("missing") {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(context.message, color = RackColors.dim, fontSize = 14.sp)
                FuiButton(if (busy) "INDEXING…" else "REFRESH INDEX", on_click = { vm.refresh_index(id) }, enabled = !busy)
            }
        }
    }
    // The agent's own message when the index holds it; else what Laya read of it (its fitted state).
    val closing = (context as? TurnContext.Found)?.let { found -> found.messages.lastOrNull { it.seq == found.closing_seq }?.text } ?: row.text
    part("closing", last = error == null) { Message("CLOSING MESSAGE", closing, CLOSING_TINT) }
    if (error != null) part("error", last = true) { Text(error, color = RackColors.warn, fontSize = 14.sp) }
}

private fun LazyListScope.between_items(
    id: String,
    rule: Color?,
    context: TurnContext.Found,
    busy: Boolean,
    between_open: Boolean,
    vm: LayaViewModel,
    part: (String, Boolean, @Composable () -> Unit) -> Unit,
) {
    val middle = context.messages.filter { it.seq < context.closing_seq }
    if (middle.isEmpty() && context.before == null) return
    part("between", false) {
        SectionLabel(
            "${if (between_open) "▾" else "▸"} BEFORE IT · ${middle.size}${if (context.before != null) "+" else ""}",
            RackColors.mint,
            Modifier.fillMaxWidth().clickable(role = Role.Button) { vm.toggle_between(id) }.padding(vertical = 12.dp),
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

/** One item of the cursor turn below its head: the card's sides (and its foot on the last), the turn's inset. */
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
            .padding(10.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        SectionLabel(who, RackColors.mint)
        MarkdownText(text)
    }
}

/** The grades for the cursor turn, then UNDO: five peers at one height, so the bar reads as one control. */
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
                fontSize = 14.sp,
                maxLines = 3,
                overflow = TextOverflow.Ellipsis,
            )
            row != null && row.reference_open == null -> TechLabel("NO CURRENT CARD HOLDS THIS TEXT", color = RackColors.dim, size = 12.sp)
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
        // An emoji ignores the ink, so a face that cannot be pressed fades instead.
        Text(glyph, color = ink, fontSize = 20.sp, modifier = Modifier.alpha(if (enabled || on) 1f else 0.4f))
        // Six faces share the row: the word shrinks to its button before it would lose a letter (`NEEDS YOU`).
        BasicText(
            word,
            style = TextStyle(color = ink, fontFamily = RackMono, letterSpacing = 0.02.em, textAlign = TextAlign.Center),
            maxLines = 1,
            softWrap = false,
            autoSize = TextAutoSize.StepBased(minFontSize = 7.sp, maxFontSize = 11.sp, stepSize = 0.5.sp),
        )
    }
}

/** A grade button's face: the glyph and word, and the sentence that is its accessible name (the web's title). */
private data class Face(val verdict: Verdict, val glyph: String, val word: String, val title: String)

// The desktop review page's own marks (ui/src/laya-review/recent-model.ts `RECENT_VERDICTS`), so a grade reads the same
// on both: the operator asked for them over the plain glyphs this bar drew first (2026-11-19). An emoji keeps its own
// colours, so a disabled face dims by alpha (`GradeButton`), not by ink. SKIP has no mark on the desktop; `»` is this
// bar's, because every face here has a glyph row.
private val FACES = listOf(
    Face(Verdict.done, "✓", "DONE", "The turn finished what it set out to do, and you can close it without reading it"),
    Face(Verdict.read, "📖", "READ", "The agent is done, but its answer is for you to read: a question it answered, a finding, an explanation"),
    Face(Verdict.running, "⏳", "RUNNING", "Work still open, but the agent was only waiting on its own agents or jobs: you were not needed"),
    Face(Verdict.you, "⚠️", "NEEDS YOU", "Work still open and waiting on you: a question, a decision, a go-ahead, or blocked"),
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

/** A closed turn shows this many of its last lines; a prompt this many of its first, then MORE. */
private const val PREVIEW_LINES = 6
private const val PROMPT_LINES = 6

private val ASK_TINT = RackColors.mint.copy(alpha = 0.08f)
private val CLOSING_TINT = RackColors.warn.copy(alpha = 0.10f)
