package world.voidcraft.tldrack.laya

import java.time.ZoneId
import java.time.ZonedDateTime
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * `LayaThreads`, the port of ui/src/laya-review/thread-model.ts: these are ui/tests/unit/laya-thread-model.test.ts's
 * cases, less the ones for what the phone leaves out (EDGE order, the deck's heads, monograms).
 */
class LayaThreadsTest {

    private val a = "11111111-1111-4111-8111-111111111111"
    private val b = "22222222-2222-4222-8222-222222222222"
    private val a_resumed = "33333333-3333-4333-8333-333333333333"
    private val utc = ZoneId.of("UTC")

    private var n = 0

    private fun row(
        conversation: String? = null,
        at: String = at("12:00"),
        subject: String? = "vc-8",
        band: Band = Band.done,
        operator: AnswerRef? = null,
        text: String = "Closing text.",
    ): RecentRow {
        n += 1
        return RecentRow(
            key = n.toString(16).padStart(16, '0'),
            decision_id = "20261107T120000-${n.toString(16).padStart(12, '0')}",
            at = at,
            subject = subject,
            p = 0.2,
            mark = band,
            band = band,
            text = text,
            reference_open = false,
            operator = operator,
            conversation = conversation,
        )
    }

    private fun answer(key: String = "00000000000000aa") =
        AnswerRef(answer_id = "a_0000000000000001", key = key, answer = ReviewAnswer.agree, open = false)

    private fun graded(conversation: String? = null, at: String = at("12:00")) = row(conversation, at, operator = answer())

    private fun turn(
        status: String = "linked",
        thread: String? = a,
        file_id: Long? = 7,
        human_ordinal: Int? = 4,
        woke_by: String? = "human",
        request: HistoryClip? = null,
        ask: HistoryClip? = null,
        closing_tail: String? = "The agent’s own closing.",
    ) = HistoryTurn(status, thread, thread, file_id, 40, human_ordinal, woke_by, request, ask, closing_tail)

    private fun thread(
        thread: String = a,
        title: String? = "Drop the slow suite from regular deploys",
        first_user: HistoryClip? = HistoryClip(0, 1.0, "human", "the slow tests are blocking every deploy"),
        started_at: Double? = ms("10:00") / 1000.0,
        human_turns: Int? = 4,
        resumed: Int = 0,
    ) = HistoryThread(thread, title, first_user, started_at, "/repo", "main", human_turns, resumed)

    private fun join(
        decisions: Map<String, HistoryTurn> = emptyMap(),
        conversations: Map<String, String?> = emptyMap(),
        threads: Map<String, HistoryThread> = emptyMap(),
    ) = HistoryThreads(decisions, conversations, threads)

    private fun at(hm: String) = "2026-11-07T$hm:00Z"

    private fun ms(hm: String) = LayaRules.epoch_ms(at(hm))!!

    private fun ask(seq: Int, text: String = "prompt $seq") = HistoryClip(seq, 1_700_000_000.0 + seq, "human", text)

    private fun ids(band: ThreadBand) = band.rows.map { it.decision_id }

    private fun graded_at(band: ThreadBand, row: RecentRow, at: Int) = GradedAt(ids(band), band.key, row.decision_id, row.at, at)

    private fun grade(rows: List<RecentRow>, r: RecentRow) = LayaRules.with_answer(rows, r.key, answer(r.key))

    // ── thread_key, thread_bands ──────────────────────────────────────────────

    @Test
    fun `a row's thread is the link, then its raw conversation through the join, then nothing`() {
        val r = row(a_resumed)
        assertEquals(a, LayaThreads.thread_key(r, join(decisions = mapOf(r.decision_id to turn(thread = a)))))
        assertEquals(a, LayaThreads.thread_key(r, join(conversations = mapOf(a_resumed to a))))
        assertEquals(a_resumed, LayaThreads.thread_key(r, join(conversations = mapOf(a_resumed to null))))
        assertEquals(a_resumed, LayaThreads.thread_key(r, null))
        assertNull(LayaThreads.thread_key(row(), null))
    }

    @Test
    fun `one conversation per thread, the newest first, its turns oldest first`() {
        val a1 = row(a, at("10:00"))
        val b1 = row(b, at("11:00"), subject = "vc-3")
        val a2 = row(a, at("12:00"))
        val bands = LayaThreads.thread_bands(listOf(a2, b1, a1), null)
        assertEquals(listOf(listOf(a1.decision_id, a2.decision_id), listOf(b1.decision_id)), bands.map(::ids))
        assertEquals(bands.size, bands.map { it.key }.toSet().size)
    }

    @Test
    fun `a conversation takes the rack name its newest turn ran under`() {
        val old = row(a, at("10:00"), subject = "vc-2")
        val fresh = row(a, at("12:00"), subject = "vc-9")
        assertEquals("vc-9", LayaThreads.thread_bands(listOf(fresh, old), null)[0].subject)
    }

    @Test
    fun `a resumed conversation stays one`() {
        val old = row(a, at("10:00"))
        val fresh = row(a_resumed, at("12:00"))
        val j = join(conversations = mapOf(a to a, a_resumed to a), threads = mapOf(a to thread(resumed = 1)))
        val bands = LayaThreads.thread_bands(listOf(fresh, old), j)
        assertEquals(1, bands.size)
        assertNull(bands[0].new_in_rack)
        assertEquals(listOf("RESUMED 1×"), LayaThreads.band_head(bands[0], ms("12:30"), true, null, utc).tags.map { it.text })
    }

    @Test
    fun `the newer conversation of a reused rack name says NEW`() {
        val older = row(a, at("10:00"))
        val newer = row(b, at("11:00"))
        val bands = LayaThreads.thread_bands(listOf(newer, older), null)
        assertEquals("vc-8", bands.first { it.rows[0] == newer }.new_in_rack)
        assertNull(bands.first { it.rows[0] == older }.new_in_rack)
        val mark = LayaThreads.band_head(bands.first { it.new_in_rack != null }, 0, true, null, utc).tags.single()
        assertEquals("NEW", mark.text)
        assertTrue(mark.title.contains("vc-8 held a different one earlier on this page"))
    }

    @Test
    fun `a conversation is dated by its start when the ledger knows it`() {
        // B started first, though only its later turn made the page.
        val ra = row(a, at("10:00"))
        val rb = row(b, at("11:00"))
        val j = join(threads = mapOf(a to thread(), b to thread(thread = b, started_at = ms("08:00") / 1000.0)))
        val bands = LayaThreads.thread_bands(listOf(rb, ra), j)
        assertEquals("vc-8", bands.first { it.rows[0] == ra }.new_in_rack)
        assertNull(bands.first { it.rows[0] == rb }.new_in_rack)
    }

    @Test
    fun `unlinked rows group by rack name and a 30-minute gap, and say NOT LINKED`() {
        assertEquals(30 * 60, LayaThreads.UNLINKED_GAP_S)
        val r1 = row(at = at("15:40"))
        val r2 = row(at = at("15:10"))
        val r3 = row(at = at("14:20"))
        val r4 = row(at = at("15:20"), subject = "vc-3")
        val bands = LayaThreads.thread_bands(listOf(r1, r4, r2, r3), join())
        assertEquals(listOf(2, 1, 1), bands.map { it.rows.size })
        assertTrue(bands.all { it.kind == BandKind.unlinked && it.new_in_rack == null })
        val head = LayaThreads.band_head(bands[0], ms("16:00"), true, null, utc)
        assertEquals(listOf("NOT LINKED"), head.tags.map { it.text })
        assertEquals("vc-8, 3:10 – 3:40 pm", head.title)
        assertEquals(emptyList<HeadTag>(), LayaThreads.band_head(bands[0], 0, false, null, utc).tags)
    }

    @Test
    fun `a conversation says INDEXING while the ledger has not caught up`() {
        val fresh = row(b)
        val loose = row(subject = "vc-11")
        val j = join(decisions = mapOf(fresh.decision_id to turn(status = "pending", thread = null), loose.decision_id to turn(status = "missing", thread = null)))
        val bands = LayaThreads.thread_bands(listOf(fresh, loose), j)
        assertTrue(bands.all { it.indexing })
        assertEquals(listOf("INDEXING"), LayaThreads.band_head(bands[1], 0, true, null, utc).tags.map { it.text })
        assertFalse(LayaThreads.thread_bands(listOf(fresh), null)[0].indexing)
        assertFalse(LayaThreads.thread_bands(listOf(fresh, loose), j, join_ok = false).any { it.indexing })
    }

    // ── heads ─────────────────────────────────────────────────────────────────

    @Test
    fun `a title is the ledger's, then the needle's, then the first prompt, then the rack name`() {
        val now = ms("12:12")
        val band = LayaThreads.thread_bands(listOf(row(a)), join(threads = mapOf(a to thread())))[0]
        assertEquals("Drop the slow suite from regular deploys", LayaThreads.band_title(band, null, now, utc))
        val bare = band.copy(thread = thread(title = null))
        assertEquals("Found title", LayaThreads.band_title(bare, FoundHead("Found title", null), now, utc))
        assertEquals("the slow tests are blocking every deploy", LayaThreads.band_title(bare, null, now, utc))
        assertEquals("vc-8", LayaThreads.band_title(band.copy(thread = null), null, now, utc))
        assertEquals("Conversation", LayaThreads.band_title(band.copy(thread = null, subject = null), null, now, utc))
    }

    @Test
    fun `the rack's display name stands in for the tmux name, in the title and beside it`() {
        val now = ms("12:12")
        val band = LayaThreads.thread_bands(listOf(row(a)), join(threads = mapOf(a to thread())))[0]
        // A titled conversation: the display name is the line under it, never `vc-8`.
        val titled = LayaThreads.band_head(band, now, true, null, utc, name = "laya-review-android")
        assertEquals("Drop the slow suite from regular deploys", titled.title)
        assertEquals("laya-review-android", titled.subject)
        assertEquals("vc-8", LayaThreads.band_head(band, now, true, null, utc).subject)
        // An untitled one is titled by the display name, and does not say it twice.
        val bare = LayaThreads.band_head(band.copy(thread = null), now, true, null, utc, name = "laya-review-android")
        assertEquals("laya-review-android", bare.title)
        assertNull(bare.subject)
        val loose = LayaThreads.thread_bands(listOf(row(at = at("15:10"))), null)[0]
        val run = LayaThreads.band_head(loose, ms("16:00"), false, null, utc, name = "laya-review-android")
        assertEquals("laya-review-android, 3:10 pm", run.title)
        assertNull(run.subject)
    }

    @Test
    fun `a head reads the turns on the page, how many to grade, and when it started`() {
        val now = ms("12:12")
        val bands = LayaThreads.thread_bands(listOf(row(a), graded(a)), join(threads = mapOf(a to thread())))
        val head = LayaThreads.band_head(bands[0], now, true, null, utc)
        assertEquals("2 turns · 1 to grade · started 2h", head.meta)
        assertEquals("the slow tests are blocking every deploy", head.first_user)
        assertFalse(head.first_clipped)
        val cut = thread(first_user = HistoryClip(0, 1.0, "human", "the slow tests", chars = 600))
        val clipped = LayaThreads.thread_bands(listOf(row(a)), join(threads = mapOf(a to cut)))[0]
        assertTrue(LayaThreads.band_head(clipped, now, true, null, utc).first_clipped)
        // What the needle found stands in for a conversation the join did not name.
        val loose = LayaThreads.thread_bands(listOf(row()), null)[0]
        assertEquals("what I asked", LayaThreads.band_head(loose, now, false, FoundHead(null, "what I asked"), utc).first_user)
    }

    @Test
    fun `an unlinked run's span is local clock time, with the day only when it is not today`() {
        fun day(h: Int, m: Int, d: Int = 7) = ZonedDateTime.of(2026, 11, d, h, m, 0, 0, utc).toInstant().toEpochMilli()
        assertEquals("3:10 – 3:40 pm", LayaThreads.clock_span(day(15, 10), day(15, 40), day(18, 0), utc))
        assertEquals("11:50 am – 12:20 pm", LayaThreads.clock_span(day(11, 50), day(12, 20), day(18, 0), utc))
        assertEquals("Nov 7, 9:05 am", LayaThreads.clock_span(day(9, 5), day(9, 5), day(18, 0, 9), utc))
    }

    @Test
    fun `a turn previews the agent's own closing when linked, else what Laya read`() {
        val r = row(text = "fitted … state")
        assertEquals("The agent’s own closing.", LayaThreads.preview_text(r, turn()))
        assertEquals("fitted … state", LayaThreads.preview_text(r, turn(status = "pending", closing_tail = null)))
        assertEquals("fitted … state", LayaThreads.preview_text(r, null))
    }

    @Test
    fun `a pasted prompt reads as its words, and a preview is a message's end`() {
        val pasted = "<pasted_content id=\"b3b7\">\nLet's upgrade the review app.\n</pasted_content id=\"b3b7\">\n\nAnd one more thing."
        assertEquals("Let's upgrade the review app.\n\nAnd one more thing.", LayaThreads.prompt_text(pasted))
        // A tag named mid-line is the operator's own text, not a paste.
        assertEquals("the <pasted_content> tag is odd", LayaThreads.prompt_text("the <pasted_content> tag is odd"))
        assertEquals("a\nb", LayaThreads.tail_text("a\nb\n", 3))
        assertEquals("…\nc\nd", LayaThreads.tail_text("a\nb\nc\nd", 2))
    }

    // ── the join's ask ────────────────────────────────────────────────────────

    @Test
    fun `the join is asked for every verdict and every distinct conversation`() {
        val rows = listOf(row(a), row(a), row())
        val ask = LayaThreads.threads_ask_of(rows)
        assertEquals(rows.map { it.decision_id }, ask.decisions)
        assertEquals(listOf(a), ask.conversations)
    }

    @Test
    fun `an ask is one comma-joined parameter per kind, chunked, and never an id of another shape`() {
        val d1 = "20261107T120000-000000000001"
        val d2 = "20261107T120000-000000000002"
        assertEquals(
            listOf(listOf("d" to "$d1,$d2", "c" to a)),
            LayaThreads.threads_queries(ThreadsAsk(listOf(d1, d2, d1, "not an id&x=1"), listOf(a, "../etc"))),
        )
        assertEquals(
            listOf(listOf("d" to "$d1,$d2"), listOf("c" to a)),
            LayaThreads.threads_queries(ThreadsAsk(listOf(d1, d2), listOf(a)), max = 2),
        )
        assertEquals(emptyList<List<Pair<String, String>>>(), LayaThreads.threads_queries(ThreadsAsk(emptyList(), emptyList())))
        // A full page, the longest kind: still one target inside phone-link's 4096 characters.
        val full = (0 until 100).map { "%08x-0000-4000-8000-%012x".format(it, it) }
        val query = LayaThreads.threads_queries(ThreadsAsk(emptyList(), full)).single().single()
        assertTrue("/ledger/threads?c=${query.second}".length <= 4096)
    }

    @Test
    fun `merged joins keep every id, the later answer winning`() {
        val one = join(decisions = mapOf("x" to turn(status = "pending", thread = null)), conversations = mapOf(a to null))
        val two = join(decisions = mapOf("x" to turn()), conversations = mapOf(a to a), threads = mapOf(a to thread()))
        val merged = LayaThreads.merge_threads(one, two)
        assertTrue(merged.decisions.getValue("x").linked)
        assertEquals(a, merged.conversations[a])
        assertEquals(setOf(a), merged.threads.keys)
    }

    // ── the pager ─────────────────────────────────────────────────────────────

    @Test
    fun `a conversation opens on its first turn to grade, else its newest`() {
        val x = graded(a, at("10:00"))
        val y = row(a, at("11:00"))
        val z = row(a, at("12:00"))
        val band = LayaThreads.thread_bands(listOf(z, y, x), null).single()
        assertSame(y, LayaThreads.opening_row(band))
        assertEquals(2, LayaThreads.todo_count(band))
        val done = LayaThreads.thread_bands(listOf(graded(b, at("09:00")), graded(b, at("09:30"))), null).single()
        assertEquals(at("09:30"), LayaThreads.opening_row(done)?.at)
    }

    @Test
    fun `after a grade the next open turn, then an earlier one, then nothing`() {
        val x = row(a, at("10:00"))
        val y = graded(a, at("11:00"))
        val z = row(a, at("12:00"))
        val band = LayaThreads.thread_bands(listOf(x, y, z), null).single()
        assertSame(z, LayaThreads.next_todo(band, x.decision_id, x.at))
        assertSame(x, LayaThreads.next_todo(band, z.decision_id, z.at))
        val lone = LayaThreads.thread_bands(listOf(row(b)), null).single()
        assertNull(LayaThreads.next_todo(lone, lone.rows[0].decision_id, lone.rows[0].at))
    }

    @Test
    fun `after a grade moves on by place in time, whether or not the graded turn is still there`() {
        val t = listOf("10:00", "11:00", "12:00", "13:00").map { row(a, at(it)) }
        val kept = LayaThreads.thread_bands(listOf(t[0], t[1], t[2].copy(operator = answer(t[2].key)), t[3]), null).single()
        val dropped = LayaThreads.thread_bands(listOf(t[0], t[1], t[3]), null).single()
        assertSame(t[3], LayaThreads.next_todo(kept, t[2].decision_id, t[2].at))
        assertSame(t[3], LayaThreads.next_todo(dropped, t[2].decision_id, t[2].at))
        assertSame(t[0], LayaThreads.next_todo(dropped, "gone", at("13:30")))
    }

    @Test
    fun `the pager moves on only forward, to a conversation with something to grade`() {
        val rows = listOf(graded(a, at("13:00")), row(b, at("12:00")), graded(a_resumed, at("11:00")), row("44444444-4444-4444-8444-444444444444", at("10:00")))
        val bands = LayaThreads.thread_bands(rows, null)
        assertEquals(1, LayaThreads.next_todo_band(bands, 0))
        assertEquals(3, LayaThreads.next_todo_band(bands, 1))
        assertNull(LayaThreads.next_todo_band(bands, 3))
        assertEquals(1, LayaThreads.todo_behind(bands, 3))
        assertEquals(1, LayaThreads.opening_band(bands))
        assertEquals(0, LayaThreads.opening_band(bands.take(1)))
    }

    @Test
    fun `an unlinked run is found again by its turns once older ones move its key`() {
        val page1 = (0 until 5).map { graded(null, at("12:${it}0")) }
        val page2 = (0 until 5).map { row(null, at("11:${it + 1}0")) }
        val before = LayaThreads.thread_bands(page1, null)
        val after = LayaThreads.thread_bands(page1 + page2, null)
        assertNotEquals(before[0].key, after[0].key)
        assertEquals(0, LayaThreads.band_holding(after, ids(before[0]), before[0].key))
        assertEquals(-1, LayaThreads.band_holding(after, emptyList(), before[0].key))
    }

    @Test
    fun `a grade checks its own conversation again once an older page cut through it`() {
        // Page 1: B (graded), then A with one open turn. Page 2: two older open turns of A, and C.
        val rb = graded(b, at("12:00"))
        val a_last = row(a, at("11:00"))
        var rows = listOf(rb, a_last)
        val was = LayaThreads.thread_bands(rows, null)
        val at_a = was.indexOfFirst { a_last in it.rows }
        val g = graded_at(was[at_a], a_last, at_a)
        rows = grade(rows, a_last)
        assertNull(LayaThreads.after_grade(LayaThreads.thread_bands(rows, null), g))
        val a_older = listOf(row(a, at("09:00")), row(a, at("10:00")))
        val c = row("44444444-4444-4444-8444-444444444444", at("08:00"))
        val step = LayaThreads.after_grade(LayaThreads.thread_bands(rows + a_older + c, null), g)
        assertEquals(AfterGrade.Same(at_a, a_older[0]), step)
        assertEquals("Graded", LayaThreads.after_grade_text("Graded", step!!))
    }

    @Test
    fun `a grade under TO GRADE moves to the conversation that slid into its place`() {
        val done = row(a, at("12:00"))
        val next = row(b, at("11:00"))
        val before = LayaThreads.thread_bands(listOf(done, next), null)
        val g = graded_at(before[0], done, 0)
        val shown = LayaRules.visible_rows(grade(listOf(done, next), done), null, unanswered_only = true)
        val step = LayaThreads.after_grade(LayaThreads.thread_bands(shown, null), g)
        assertEquals(AfterGrade.Next(0, back = false), step)
        assertEquals("Graded. That conversation is done: here is the next one", LayaThreads.after_grade_text("Graded", step!!))
        val kept = LayaThreads.after_grade(LayaThreads.thread_bands(grade(listOf(done, next), done), null), g)
        assertEquals(AfterGrade.Next(1, back = true), kept)
        assertTrue(LayaThreads.after_grade_text("Graded", kept!!).endsWith("(◀ goes back)"))
    }

    @Test
    fun `with nothing ahead a grade says what is left, and holds the conversation while it is on the page`() {
        val skipped = row(b, at("12:00"))
        val last = row(a, at("11:00"))
        val before = LayaThreads.thread_bands(listOf(skipped, last), null)
        val g = graded_at(before[1], last, 1)
        val rows = grade(listOf(skipped, last), last)
        val held = LayaThreads.left_after_grade(LayaThreads.thread_bands(rows, null), g)
        assertEquals(AfterGrade.Behind(0, held = true), held)
        assertEquals("Graded. Nothing further ahead: an earlier conversation still has turns to grade", LayaThreads.after_grade_text("Graded", held))
        val gone = LayaThreads.left_after_grade(LayaThreads.thread_bands(LayaRules.visible_rows(rows, null, unanswered_only = true), null), g)
        assertEquals(AfterGrade.Behind(0, held = false), gone)
        assertTrue(LayaThreads.after_grade_text("Graded", gone).endsWith("here is an earlier one with turns to grade"))
        val none = LayaThreads.left_after_grade(LayaThreads.thread_bands(grade(rows, skipped), null), g)
        assertEquals(AfterGrade.None(held = true), none)
        assertEquals("Graded. Nothing else to grade on this page", LayaThreads.after_grade_text("Graded", none))
    }

    @Test
    fun `a band filter that hides only the cursor's turn keeps its conversation`() {
        // Eight conversations, newest first; only 5 and 7 have a ⚠️ turn. The cursor is on conversation 5's ✅ turn.
        val convs = (0 until 8).map { "$it$it$it$it$it$it$it$it-0000-4000-8000-000000000000" }
        val rows = convs.mapIndexed { i, c -> row(c, at("${(20 - i).toString().padStart(2, '0')}:00")) }
        val warn5 = row(convs[5], at("14:30"), band = Band.open)
        val warn7 = row(convs[7], at("12:30"), band = Band.open)
        val all = LayaThreads.thread_bands(rows + warn5 + warn7, null)
        val n5 = all.indexOfFirst { warn5 in it.rows }
        val cursor = rows[5].decision_id
        assertEquals(n5, LayaThreads.pager_at(all, PagerPlace(cursor, emptyList(), null, 0)))
        val warned = LayaThreads.thread_bands(LayaRules.visible_rows(rows + warn5 + warn7, Band.open, unanswered_only = false), null)
        val place = PagerPlace(cursor, ids(all[n5]), all[n5].key, n5)
        assertEquals(all[n5].key, warned[LayaThreads.pager_at(warned, place)].key)
        assertEquals(1, LayaThreads.pager_at(warned, PagerPlace(cursor, emptyList(), null, n5)))
    }

    // ── turn_groups ───────────────────────────────────────────────────────────

    @Test
    fun `a prompt prints once over every turn that answered it, and counts the prompts skipped between`() {
        val r1 = row(a, at("10:00"))
        val r2 = row(a, at("10:30"))
        val r3 = row(a, at("11:00"))
        val j = join(
            decisions = mapOf(
                r1.decision_id to turn(ask = ask(4), human_ordinal = 2),
                r2.decision_id to turn(ask = ask(4), human_ordinal = 2, woke_by = "task"),
                r3.decision_id to turn(ask = ask(30), human_ordinal = 5),
            ),
            threads = mapOf(a to thread(human_turns = 5)),
        )
        val band = LayaThreads.thread_bands(listOf(r3, r2, r1), j).single()
        val groups = LayaThreads.turn_groups(band, j)
        assertEquals(listOf(2, 1), groups.map { it.rows.size })
        assertEquals(listOf(0, 2), groups.map { it.skipped })
        assertEquals("YOU · prompt 2 of 5", LayaThreads.group_head(groups[0]))
        assertEquals("prompt 30", groups[1].anchor?.text)
    }

    @Test
    fun `a turn the rack opened anchors on its own opener, and an unlinked turn has none`() {
        val worker = row(a, at("10:00"))
        val loose = row(at = at("10:05"))
        val j = join(decisions = mapOf(worker.decision_id to turn(ask = null, request = ask(0, "brief from the lead"), human_ordinal = 0, woke_by = "parent")))
        val g = LayaThreads.turn_groups(LayaThreads.thread_bands(listOf(worker), j).single(), j).single()
        assertFalse(g.by_operator)
        assertEquals("brief from the lead", g.anchor?.text)
        assertEquals("LEAD", LayaThreads.group_head(g))
        val lg = LayaThreads.turn_groups(LayaThreads.thread_bands(listOf(loose), j).single(), j).single()
        assertNull(lg.anchor)
        assertEquals("RACK", LayaThreads.group_head(g.copy(woke_by = "peer")))
    }

    @Test
    fun `a skip is never counted across a resumed file, whose prompts count from 1 again`() {
        val r1 = row(a, at("10:00"))
        val r2 = row(a_resumed, at("11:00"))
        val j = join(
            decisions = mapOf(
                r1.decision_id to turn(ask = ask(4), human_ordinal = 2, file_id = 7),
                r2.decision_id to turn(ask = ask(2), human_ordinal = 1, file_id = 9),
            ),
        )
        val groups = LayaThreads.turn_groups(LayaThreads.thread_bands(listOf(r1, r2), j).single(), j)
        assertEquals(listOf(0, 0), groups.map { it.skipped })
    }

    @Test
    fun `the first message is known to print already as a group's prompt`() {
        val r = row(a)
        val first = ask(0, "the slow tests are blocking every deploy")
        val j = join(decisions = mapOf(r.decision_id to turn(ask = first, human_ordinal = 1)))
        val groups = LayaThreads.turn_groups(LayaThreads.thread_bands(listOf(r), j).single(), j)
        assertTrue(LayaThreads.first_in_groups(groups, first))
        assertFalse(LayaThreads.first_in_groups(groups, ask(12, "something else")))
        assertFalse(LayaThreads.first_in_groups(groups, null))
    }

    @Test
    fun `a turn the operator did not open says who woke the agent`() {
        assertNull(LayaThreads.wake_note("human", null))
        assertNull(LayaThreads.wake_note(null, null))
        val task = LayaThreads.wake_note("task", "<task-notification>\n<summary>Workspace   checks failed</summary>\n</task-notification>")
        assertEquals(LayaThreads.WAKE_TAG, task?.tag)
        assertEquals("A background report woke the agent after its last reply: “Workspace checks failed”", task?.line)
        assertEquals("Another agent's message woke the agent after its last reply", LayaThreads.wake_note("peer", "hello")?.line)
        assertEquals("The rack woke the agent after its last reply", LayaThreads.wake_note("relay", null)?.line)
    }
}
