package world.voidcraft.tldrack.laya

import kotlinx.coroutines.runBlocking
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import world.voidcraft.tldrack.APP_JSON

/**
 * The phone's RECENT list. Mirrors ui/tests/unit/laya-recent-model.test.ts and laya-turn-context.test.ts case for
 * case where they exist; the answer/undo flow lives in LayaRecentList.vue on the web, so its cases are written here.
 */
class LayaRulesTest {

    private var n = 0

    private fun row(
        band: Band = Band.done,
        mark: Band = band,
        p: Double = 0.2,
        operator: AnswerRef? = null,
        reference_open: Boolean? = false,
        edge: Edge? = null,
        key: String? = null,
        at: String = "2026-10-30T12:00:00Z",
    ): RecentRow {
        n += 1
        return RecentRow(
            key = key ?: n.toString(16).padStart(16, '0'),
            decision_id = "20261030T120000-${n.toString(16).padStart(12, '0')}",
            at = at,
            subject = "wt-a",
            p = p,
            mark = mark,
            band = band,
            edge = edge,
            text = "shipped",
            reference_open = reference_open,
            operator = operator,
        )
    }

    /** A RECENT row's `operator`, without the `key` (`review_log.answer_ref`) that nothing here reads: an older bridge's shape. */
    private fun said(open: Boolean?, answer: ReviewAnswer = ReviewAnswer.agree, needs: Needs? = null) =
        AnswerRef(answer_id = "a_${"0".repeat(15)}${if (open == true) 1 else 2}", answer = answer, open = open, needs = needs)

    // ── answer_request ───────────────────────────────────────────────────────

    @Test
    fun `a verdict maps onto agree or flip against the reference, and an unanswerable row refuses`() {
        val done_ref = row(reference_open = false)
        val surface = "agent_open_work"
        assertEquals(
            AnswerRequest(surface, done_ref.key, ReviewAnswer.agree, reference_open = false),
            LayaRules.answer_request(done_ref.key, surface, Verdict.done, false),
        )
        assertEquals(
            AnswerRequest(surface, done_ref.key, ReviewAnswer.flip, reference_open = false, needs = Needs.you),
            LayaRules.answer_request(done_ref.key, surface, Verdict.you, false),
        )
        assertEquals(Needs.running, LayaRules.answer_request(done_ref.key, surface, Verdict.running, false)?.needs)
        // A moved reference re-maps the same verdict.
        assertEquals(ReviewAnswer.agree, LayaRules.answer_request(done_ref.key, surface, Verdict.running, true)?.answer)
        // needs rides only on an OPEN answer.
        assertNull(LayaRules.answer_request(done_ref.key, surface, Verdict.done, false)?.needs)
        assertEquals(ReviewAnswer.skip, LayaRules.answer_request(done_ref.key, surface, Verdict.skip, true)?.answer)
        assertNull(LayaRules.answer_request(done_ref.key, surface, Verdict.done, null))
    }

    @Test
    fun `a request the strict routes would refuse is never sendable`() {
        val key = "0123456789abcdef"
        assertTrue(LayaRules.sendable(AnswerRequest("agent_open_work", key, ReviewAnswer.flip, reference_open = false, needs = Needs.you)))
        assertTrue(LayaRules.sendable(AnswerRequest("agent_open_work", "golden:case-1", ReviewAnswer.skip, reference_open = true)))
        assertFalse("needs on a DONE", LayaRules.sendable(AnswerRequest("agent_open_work", key, ReviewAnswer.agree, reference_open = false, needs = Needs.you)))
        assertFalse("needs on a skip", LayaRules.sendable(AnswerRequest("agent_open_work", key, ReviewAnswer.skip, reference_open = true, needs = Needs.you)))
        assertFalse("surface", LayaRules.sendable(AnswerRequest("Agent", key, ReviewAnswer.agree, reference_open = false)))
        assertFalse("key", LayaRules.sendable(AnswerRequest("agent_open_work", "../etc", ReviewAnswer.agree, reference_open = false)))
        assertTrue(LayaRules.sendable(UndoRequest("agent_open_work", "a_0123456789abcdef")))
        assertFalse(LayaRules.sendable(UndoRequest("agent_open_work", "a_xyz")))
    }

    // ── grading ──────────────────────────────────────────────────────────────

    @Test
    fun `the operator verdict reads back, and grades against the band, and a reply row has no right answer`() {
        assertNull(LayaRules.operator_verdict(row().operator))
        assertEquals("an OPEN answer with no half (the deck)", Verdict.open, LayaRules.operator_verdict(said(true)))
        assertEquals(Verdict.you, LayaRules.operator_verdict(said(true, needs = Needs.you)))
        assertEquals(Verdict.running, LayaRules.operator_verdict(said(true, needs = Needs.running)))
        assertEquals("RUNNING is still open work", Grade.right, LayaRules.grade(row(band = Band.open, operator = said(true, needs = Needs.running))))
        assertEquals(Grade.wrong, LayaRules.grade(row(band = Band.done, operator = said(true, needs = Needs.you))))
        assertEquals(Verdict.skip, LayaRules.operator_verdict(said(null, ReviewAnswer.skip)))
        assertEquals(Grade.right, LayaRules.grade(row(band = Band.done, operator = said(false))))
        assertEquals("a ✅ on open work is the costly miss", Grade.wrong, LayaRules.grade(row(band = Band.done, operator = said(true))))
        assertEquals(Grade.right, LayaRules.grade(row(band = Band.open, operator = said(true))))
        assertNull(LayaRules.grade(row(band = Band.reply, operator = said(true))))
        assertNull(LayaRules.grade(row(band = Band.done, operator = said(null, ReviewAnswer.skip))))
    }

    @Test
    fun `READ is a DONE that owes a read, sent and read back only beside DONE`() {
        val key = "0123456789abcdef"
        val read = LayaRules.answer_request(key, "agent_open_work", Verdict.read, reference_open = true)
        assertEquals(AnswerRequest("agent_open_work", key, ReviewAnswer.flip, reference_open = true, needs = Needs.read), read)
        assertTrue(LayaRules.sendable(read!!))
        assertFalse("READ beside OPEN", LayaRules.sendable(AnswerRequest("agent_open_work", key, ReviewAnswer.agree, reference_open = true, needs = Needs.read)))
        assertEquals(Verdict.read, LayaRules.operator_verdict(said(false, needs = Needs.read)))
        assertEquals("READ is done work", Grade.right, LayaRules.grade(row(band = Band.done, operator = said(false, needs = Needs.read))))
        assertEquals(Grade.wrong, LayaRules.grade(row(band = Band.open, operator = said(false, needs = Needs.read))))
        val t = LayaRules.recent_tally(listOf(row(band = Band.done, operator = said(false, needs = Needs.read))))
        assertEquals(1, t.needs.getValue(Needs.read))
        // The wire value decodes: a name missing from `Needs` would fail the whole RECENT page.
        val ref = APP_JSON.decodeFromString(
            AnswerRef.serializer(),
            """{"answer_id":"a_0123456789abcdef","key":"$key","answer":"flip","open":false,"needs":"read"}""",
        )
        assertEquals(Needs.read, ref.needs)
    }

    @Test
    fun `the tally counts per band, as review_data recent_rows does`() {
        val rows = listOf(
            row(band = Band.done, operator = said(false)),
            row(band = Band.done, operator = said(true)),
            row(band = Band.done),
            row(band = Band.open, operator = said(true)),
            row(band = Band.reply, p = 0.5, operator = said(false)),
            row(band = Band.reply, p = 0.5, operator = said(null, ReviewAnswer.skip)),
        )
        val t = LayaRules.recent_tally(rows)
        assertEquals(6, t.n)
        assertEquals(4, t.reviewed)
        assertEquals(1, t.skipped)
        assertEquals(BandTally(n = 3, reviewed = 2, right = 1, open = 1), t.bands[Band.done])
        assertEquals(BandTally(n = 1, reviewed = 1, right = 1, open = 1), t.bands[Band.open])
        assertEquals(BandTally(n = 2, reviewed = 1, right = 0, open = 0), t.bands[Band.reply])
        val (done, reply, open) = LayaRules.band_readouts(t)
        assertEquals("1/2", done.text)
        assertEquals("✅", done.mark)
        assertEquals("1 done · 0 open", reply.text)
        assertEquals("1/1", open.text)
        assertEquals(mapOf(Needs.you to 0, Needs.running to 0, Needs.read to 0), t.needs)
        assertNull("nothing split yet", LayaRules.needs_readout(t))
    }

    @Test
    fun `the phone strip gives the unsure band its count and moves the split to the title`() {
        val readouts = LayaRules.band_readouts(LayaRules.recent_tally(listOf(row(band = Band.reply, operator = said(false)), row(band = Band.reply))))
        val reply = readouts.single { it.band == Band.reply }
        assertEquals("2", LayaRules.strip_text(reply))
        assertTrue(reply.title.contains("you called 1 done and 0 open"))
        val done = LayaRules.band_readouts(LayaRules.recent_tally(listOf(row(band = Band.done, operator = said(false))))).first()
        assertEquals("1/1", LayaRules.strip_text(done))
    }

    @Test
    fun `the OPEN answers split into who the turn was waiting on`() {
        val t = LayaRules.recent_tally(
            listOf(
                row(band = Band.open, operator = said(true, needs = Needs.you)),
                row(band = Band.open, operator = said(true, needs = Needs.running)),
                row(band = Band.open, operator = said(true, needs = Needs.running)),
                row(band = Band.open, operator = said(true)),
            ),
        )
        assertEquals(mapOf(Needs.you to 1, Needs.running to 2, Needs.read to 0), t.needs)
        assertEquals(4, t.bands.getValue(Band.open).open)
        assertEquals("1/3", LayaRules.needs_readout(t)?.text)
    }

    @Test
    fun `a band turns amber once more than one in five graded answers are wrong, from 5 graded`() {
        fun warns(bad: Int, total: Int): Boolean =
            LayaRules.band_readouts(LayaRules.recent_tally(List(total) { i -> row(band = Band.done, operator = said(i < bad)) }))[0].warn
        assertFalse("too few to judge", warns(1, 4))
        assertFalse(warns(1, 5))
        assertTrue(warns(2, 5))
    }

    // ── the list ─────────────────────────────────────────────────────────────

    @Test
    fun `the list narrows by band and to the ungraded`() {
        val rows = listOf(row(band = Band.done, operator = said(false)), row(band = Band.open), row(band = Band.done))
        assertEquals(2, LayaRules.visible_rows(rows, Band.done, false).size)
        assertEquals(2, LayaRules.visible_rows(rows, null, true).size)
        assertEquals(1, LayaRules.visible_rows(rows, Band.done, true).size)
    }

    @Test
    fun `an answer lands on every row of its text`() {
        val a = row()
        val twin = row().copy(key = a.key)
        val other = row()
        val after = LayaRules.with_answer(listOf(a, twin, other), a.key, said(true))
        assertEquals(listOf(Verdict.open, Verdict.open, null), after.map { LayaRules.operator_verdict(it.operator) })
    }

    @Test
    fun `the row says what the rack drew when today's band differs`() {
        assertEquals("was 💬", LayaRules.drawn_note(row(mark = Band.reply, band = Band.done)))
        assertNull(LayaRules.drawn_note(row(mark = Band.done, band = Band.done)))
    }

    @Test
    fun `ages read in one short unit`() {
        val at = "2026-10-30T12:00:00Z"
        val t0 = LayaRules.epoch_ms(at)!!
        assertEquals("30s", LayaRules.age_text(at, t0 + 30_000))
        assertEquals("5m", LayaRules.age_text(at, t0 + 5 * 60_000))
        assertEquals("3h", LayaRules.age_text(at, t0 + 3 * 3_600_000))
        assertEquals("2d", LayaRules.age_text(at, t0 + 2 * 86_400_000))
        assertEquals("—", LayaRules.age_text("nope", t0))
        assertEquals("an offset and a fraction parse too", t0, LayaRules.epoch_ms("2026-10-30T12:00:00.000+00:00"))
    }

    @Test
    fun `a near miss says how far it fell short, and a mark only just made says by how much`() {
        assertEquals("0.02 FROM ⚠️", LayaRules.edge_note(row(p = 0.68, edge = Edge(Cut.open, -0.02)))?.text)
        assertEquals("⚠️ BY 0.01", LayaRules.edge_note(row(p = 0.71, edge = Edge(Cut.open, 0.01)))?.text)
        assertEquals("p at the cut is a ⚠️", "⚠️ BY 0.00", LayaRules.edge_note(row(p = 0.7, edge = Edge(Cut.open, 0.0)))?.text)
        assertEquals("0.01 FROM ✅", LayaRules.edge_note(row(p = 0.41, edge = Edge(Cut.done, 0.01)))?.text)
        assertEquals("p at the cut is not a ✅", "0.00 FROM ✅", LayaRules.edge_note(row(p = 0.4, edge = Edge(Cut.done, 0.0)))?.text)
        assertEquals("✅ BY 0.02", LayaRules.edge_note(row(p = 0.38, edge = Edge(Cut.done, -0.02)))?.text)
        assertTrue(LayaRules.edge_note(row(p = 0.68, edge = Edge(Cut.open, -0.02)))!!.title.contains("cut is 0.70"))
    }

    @Test
    fun `the edge note stays quiet away from a cut, and on a bridge that sends no edge`() {
        assertNull(LayaRules.edge_note(row(p = 0.6, edge = Edge(Cut.open, -0.1))))
        assertNotNull("the boundary is in", LayaRules.edge_note(row(p = 0.65, edge = Edge(Cut.open, -LayaRules.EDGE_NEAR))))
        assertNull(LayaRules.edge_note(row(p = 0.68)))
    }

    @Test
    fun `scores print as the web's toFixed prints them`() {
        // Node, 2026-11-07: (0.725).toFixed(2) is "0.72" (the double sits under the tie), (0.125).toFixed(2) is "0.13".
        assertEquals("0.72", LayaRules.score_text(0.7249))
        assertEquals("0.72", LayaRules.score_text(0.725))
        assertEquals("0.13", LayaRules.score_text(0.125))
        assertEquals("1.00", LayaRules.score_text(1.0))
    }

    @Test
    fun `the rack name names a verdict only while the tmux name still wears the same naming`() {
        val named_at = LayaRules.epoch_ms("2026-11-07T10:00:00Z")!! / 1_000.0
        val view = TopicView(named = TopicNamed(name = "phone link grading", at = named_at), shown = "phone link grading")
        assertEquals("phone link grading", LayaRules.rack_name(view, "2026-11-07T10:05:00Z"))
        assertNull("an older verdict belongs to whoever wore vc-N before", LayaRules.rack_name(view, "2026-11-07T09:00:00Z"))
        assertNull("never auto-named: shown may be a placeholder", LayaRules.rack_name(TopicView(named = null, shown = "vc-3"), "2026-11-07T10:05:00Z"))
        assertNull(LayaRules.rack_name(null, "2026-11-07T10:05:00Z"))
    }

    @Test
    fun `a previous wearer's naming never heads the new session's placeholder onto its own verdicts`() {
        // Session A wore vc-3 and was topic-named at 10:00; B now wears vc-3 and its first prompt wrote a placeholder,
        // which logs no naming, so `named` is still A's.
        val named_at = LayaRules.epoch_ms("2026-11-07T10:00:00Z")!! / 1_000.0
        val view = TopicView(named = TopicNamed(name = "parser cleanup pass", at = named_at), shown = "fix the phone link")
        assertNull("A's verdict after its naming is not B's", LayaRules.rack_name(view, "2026-11-07T10:30:00Z"))
        assertNull("a --title label or a better name logs no naming either", LayaRules.rack_name(view.copy(shown = "LEAD: deploy"), "2026-11-07T11:00:00Z"))
        assertNull("a bridge that sends no name cannot pass the gate", LayaRules.rack_name(TopicView(named = TopicNamed(at = named_at), shown = "x"), "2026-11-07T11:00:00Z"))
    }

    @Test
    fun `a failure reads as one line, its remedy after a dash`() {
        assertEquals("No card — undo it first", LayaRules.fail_text(Fail.Refused(409, "already_answered", "No card", "undo it first")))
        assertEquals("Index busy", LayaRules.fail_text(Fail.Refused(503, null, "Index busy", "")))
        assertTrue(LayaRules.fail_text(Fail.Unlinked).contains(LayaRules.UNLINKED_FIX))
    }

    // ── one press: the answer/undo flow ──────────────────────────────────────

    private class Review(vararg replies: Call<ReviewResult>) : ReviewCalls {
        val queue = ArrayDeque(replies.toList())
        val sent = mutableListOf<Any>()

        override suspend fun answer(request: AnswerRequest): Call<ReviewResult> {
            sent += request
            return queue.removeFirst()
        }

        override suspend fun undo(request: UndoRequest): Call<ReviewResult> {
            sent += request
            return queue.removeFirst()
        }
    }

    private val surface = LayaRules.DEFAULT_SURFACE
    private fun ok(answer: AnswerRef?) = Call.Ok(ReviewResult(message = "recorded", answer = answer))
    private fun refused(code: String, result: ReviewResult? = null) = Fail.Refused(409, code, code, result = result)
    private fun recorded(key: String, open: Boolean, needs: Needs? = null) =
        AnswerRef(answer_id = "a_00000000000000aa", key = key, answer = ReviewAnswer.agree, open = open, needs = needs)

    @Test
    fun `a moved reference maps the same verdict again without changing what was said`() = runBlocking {
        val r = row(reference_open = false)
        val fresh = ReviewResult(card = ReviewCard(ReviewReference(open = true)))
        val ref = recorded(r.key, open = false)
        val review = Review(refused("reference_changed", fresh), ok(ref))
        val out = LayaRules.grade_row(r, surface, Verdict.done, review)
        assertEquals(
            listOf(
                AnswerRequest(surface, r.key, ReviewAnswer.agree, reference_open = false),
                AnswerRequest(surface, r.key, ReviewAnswer.flip, reference_open = true),
            ),
            review.sent,
        )
        assertEquals(ref, out.recorded)
        assertEquals(listOf(r.key to ref), out.edits)
        assertNull(out.failure)
    }

    @Test
    fun `a second reference change gives up rather than loop`() = runBlocking {
        val r = row(reference_open = false)
        val fresh = ReviewResult(card = ReviewCard(ReviewReference(open = true)))
        val review = Review(refused("reference_changed", fresh), refused("reference_changed", fresh))
        val out = LayaRules.grade_row(r, surface, Verdict.done, review)
        assertEquals(2, review.sent.size)
        assertNull(out.recorded)
        assertTrue(out.edits.isEmpty())
    }

    @Test
    fun `a changed answer undoes the live one first`() = runBlocking {
        val live = said(false)
        val r = row(operator = live, reference_open = false)
        val ref = recorded(r.key, open = true, needs = Needs.you)
        val review = Review(Call.Ok(ReviewResult(message = "undone", answer = live)), ok(ref))
        val out = LayaRules.grade_row(r, surface, Verdict.you, review)
        assertEquals(UndoRequest(surface, live.answer_id), review.sent[0])
        assertEquals(AnswerRequest(surface, r.key, ReviewAnswer.flip, reference_open = false, needs = Needs.you), review.sent[1])
        assertEquals(listOf(r.key to null, r.key to ref), out.edits)
        assertEquals(ref, out.recorded)
    }

    @Test
    fun `an undo the log already holds is not a reason to stop`() = runBlocking {
        val r = row(operator = said(false), reference_open = false)
        val ref = recorded(r.key, open = true, needs = Needs.running)
        val out = LayaRules.grade_row(r, surface, Verdict.running, Review(refused("already_undone"), ok(ref)))
        assertEquals(ref, out.recorded)
    }

    @Test
    fun `a refused undo stops before any answer is sent`() = runBlocking {
        val r = row(operator = said(false), reference_open = false)
        val busy = Fail.Refused(503, "params_unavailable", "The laya params folder is not there yet")
        val review = Review(busy)
        val out = LayaRules.grade_row(r, surface, Verdict.you, review)
        assertEquals(1, review.sent.size)
        assertSame(busy, out.failure)
        assertTrue(out.edits.isEmpty())
    }

    @Test
    fun `a lost link mid-press is the press's failure`() = runBlocking {
        val out = LayaRules.grade_row(row(operator = said(false)), surface, Verdict.skip, Review(Fail.Unlinked))
        assertEquals(Fail.Unlinked, out.failure)
    }

    @Test
    fun `an answer given elsewhere lands on the row, and says so`() = runBlocking {
        val r = row(reference_open = false)
        val standing = recorded(r.key, open = false)
        val out = LayaRules.grade_row(r, surface, Verdict.you, Review(refused("already_answered", ReviewResult(answer = standing))))
        assertEquals(listOf(r.key to standing), out.edits)
        assertEquals("already_answered", (out.failure as Fail.Refused).code)
        assertNull(out.recorded)
    }

    @Test
    fun `the same verdict twice, or a row no card holds, sends nothing`() = runBlocking {
        val review = Review()
        assertEquals(GradeOutcome(), LayaRules.grade_row(row(operator = said(false)), surface, Verdict.done, review))
        assertEquals(GradeOutcome(), LayaRules.grade_row(row(reference_open = null), surface, Verdict.done, review))
        assertTrue(review.sent.isEmpty())
    }

    @Test
    fun `an accepted answer without one is a refusal with the server's words`() = runBlocking {
        val out = LayaRules.grade_row(row(), surface, Verdict.done, Review(Call.Ok(ReviewResult(message = "odd"))))
        assertEquals("odd", (out.failure as Fail.Refused).message)
    }

    @Test
    fun `undo takes back the answer, and an answer already gone counts as taken back`() = runBlocking {
        val id = "a_00000000000000aa"
        assertNull(LayaRules.take_back(surface, id, Review(ok(null))))
        assertNull(LayaRules.take_back(surface, id, Review(refused("already_undone"))))
        assertNull(LayaRules.take_back(surface, id, Review(refused("unknown_answer"))))
        assertEquals(Fail.BridgeDown, LayaRules.take_back(surface, id, Review(Fail.BridgeDown)))
    }

    // ── the conversation behind a verdict (laya-turn-context.test.ts) ────────

    private val hit_ts = LayaRules.epoch_ms("2026-11-04T09:00:00Z")!! / 1_000.0
    private fun hit(seq: Int = 5, ts: Double? = hit_ts, role: String = "agent", file_id: Long = 1) =
        HistoryHit(file_id = file_id, seq = seq, ts = ts, role = role)

    @Test
    fun `the needle is the first line that is the message's own words, capped`() {
        assertEquals(
            "It isn’t a typo, it was the wrong key. Nothing caps a retry count.",
            LayaRules.closing_needle("ok\n\nIt isn’t a typo, it was the wrong key. Nothing caps a retry count."),
        )
        assertEquals(LayaRules.NEEDLE_CHARS, LayaRules.closing_needle("  ${"x".repeat(300)}")?.length)
        assertNull("too short to find one message", LayaRules.closing_needle("Waiting.\nDone."))
    }

    @Test
    fun `pick_hit takes the nearest agent message inside the window`() {
        val at = "2026-11-04T09:00:02Z"
        val at_s = LayaRules.epoch_ms(at)!! / 1_000.0
        val near = hit(seq = 9, ts = at_s - 2)
        assertSame(near, LayaRules.pick_hit(listOf(hit(ts = at_s - 600), near), at))
    }

    @Test
    fun `pick_hit refuses a user echo, a message from another day, and a bad stamp`() {
        val at = "2026-11-04T09:00:02Z"
        val at_s = LayaRules.epoch_ms(at)!! / 1_000.0
        assertNull(LayaRules.pick_hit(listOf(hit(role = "user", ts = at_s)), at))
        assertNull(LayaRules.pick_hit(listOf(hit(ts = at_s - 86_400)), at))
        assertNull(LayaRules.pick_hit(listOf(hit(ts = null)), at))
        assertNull(LayaRules.pick_hit(listOf(hit()), "nope"))
    }

    @Test
    fun `pick_hit without a recorded time requires one unique match and a complete result page`() {
        val only = hit()
        assertSame(only, LayaRules.pick_hit(listOf(hit(role = "human"), only), null))
        assertNull(LayaRules.pick_hit(listOf(only, hit(file_id = 2)), null))
        assertNull(LayaRules.pick_hit(listOf(only), null, more = true))
        assertNull(LayaRules.pick_hit(emptyList(), null))
    }

    @Test
    fun `the ask is the last prompt at or before the closing message`() {
        fun m(seq: Int, role: String) = HistoryMessage(seq = seq, role = role, text = "")
        assertEquals(3, LayaRules.ask_seq(listOf(m(1, "user"), m(2, "agent"), m(3, "user"), m(4, "agent"), m(6, "user")), 4))
        assertNull(LayaRules.ask_seq(listOf(m(2, "agent")), 2))
        assertEquals(1, LayaRules.ask_seq(listOf(m(1, "human"), m(2, "agent")), 2))
        assertTrue(LayaRules.is_human_role("human"))
        assertTrue(LayaRules.is_human_role("user"))
        assertFalse(LayaRules.is_human_role("machine"))
    }

    private fun message(seq: Int, role: String = "agent", text: String = "Message $seq") = HistoryMessage(seq = seq, role = role, text = text)

    private fun context(
        messages: List<HistoryMessage> = listOf(message(59), message(60)),
        closing_seq: Int = 60,
        before: Int? = 59,
        file_id: Long = 1,
        first_user: FirstUser = FirstUser.Unknown,
    ) = TurnContext.Found(title = "PR #12", cwd = null, file_id = file_id, closing_seq = closing_seq, messages = messages, before = before, first_user = first_user)

    @Test
    fun `paging back keeps the judged close and drops overlapping or later messages`() {
        val current = context()
        val merged = LayaRules.prepend_context(
            current,
            context(messages = listOf(message(1, "human"), message(59), message(61)), closing_seq = 59, before = null),
        )
        assertEquals(listOf(1, 59, 60), merged.messages.map { it.seq })
        assertEquals(60, merged.closing_seq)
        assertNull(merged.before)
        assertSame(current, LayaRules.prepend_context(current, context(file_id = 2)))
        assertSame("no progress preserves the existing page", current, LayaRules.prepend_context(current, context()))
    }

    @Test
    fun `the title is the first request only for an untitled session, and only when the page reaches its start`() {
        val messages = listOf(HistoryMessage(0, "human", "get the branch ready, please\nhttps://github.com/o/r/pull/12"))
        assertEquals("get the branch ready, please", LayaRules.context_title(null, messages, null, FirstUser.Unknown))
        assertNull("a later turn must not rename the session", LayaRules.context_title(null, messages, 15, FirstUser.Unknown))
        assertEquals("get the branch ready, please", LayaRules.context_title(null, emptyList(), 500, FirstUser.Found(messages[0])))
        assertEquals("Saved title", LayaRules.context_title("Saved title", messages, null, FirstUser.Unknown))
        assertNull(LayaRules.context_title(null, emptyList(), null, FirstUser.Unknown))
    }

    /** Serves pages in order and records every conversation read; more reads than pages fails the test. */
    private class Ledger(private val search: HistorySearch, vararg pages: Call<LedgerPage>) : LedgerSource {
        private val queue = ArrayDeque(pages.toList())
        val reads = mutableListOf<Pair<PageMode, Int>>()

        override suspend fun search(q: String): Call<HistorySearch> = Call.Ok(search)

        override suspend fun conversation(file_id: Long, mode: PageMode, anchor: Int): Call<LedgerPage> {
            reads += mode to anchor
            assertTrue("no unbounded history reads", queue.isNotEmpty())
            return queue.removeFirst()
        }
    }

    private val search = HistorySearch(hits = listOf(hit(seq = 60)), next_offset = null)
    private val closing = "The requested pull request is now mergeable."
    private val judged_at = "2026-11-04T09:00:02Z"
    private fun page(messages: List<HistoryMessage>, before: Int?, first_user: FirstUser = FirstUser.Unknown, title: String? = "t") =
        Call.Ok(LedgerPage(title = title, cwd = null, messages = messages, first_user = first_user, before = before))

    @Test
    fun `the original request shows independently of a long session's recent page`() = runBlocking {
        val first = message(0, "human", "The original user request")
        val ledger = Ledger(search, page(listOf(message(58, "human"), message(60)), 58, FirstUser.Found(first)))
        val found = LayaRules.load_turn_context(closing, judged_at, ledger) as TurnContext.Found
        assertEquals("not the recent prompt at 58", FirstUser.Found(first), found.first_user)
        assertEquals(listOf(58, 60), found.messages.map { it.seq })
        assertEquals(60, found.closing_seq)
        assertEquals(58, found.before)
        assertEquals("does not scan backwards through the session", listOf(PageMode.before to 61), ledger.reads)
    }

    @Test
    fun `an older bridge has the start read directly, and later turns are left out`() = runBlocking {
        val first = message(0, "user", "Original request\nFull details")
        val ledger = Ledger(
            search,
            page(listOf(message(59, "human"), message(60)), 59, title = null),
            page(listOf(first, message(90)), null, title = null),
        )
        val found = LayaRules.load_turn_context(closing, judged_at, ledger) as TurnContext.Found
        assertEquals(FirstUser.Found(first), found.first_user)
        assertEquals("Original request", found.title)
        assertEquals(listOf(59, 60), found.messages.map { it.seq })
        assertEquals(listOf(PageMode.before to 61, PageMode.around to 0), ledger.reads)
    }

    @Test
    fun `a request from after the judged close is never the first one`() = runBlocking {
        val ledger = Ledger(search, page(listOf(message(60)), 60, FirstUser.Found(message(90, "human"))))
        val found = LayaRules.load_turn_context(closing, judged_at, ledger) as TurnContext.Found
        assertEquals(FirstUser.None, found.first_user)
    }

    @Test
    fun `the closing turn stays readable when the start page is unavailable`() = runBlocking {
        val ledger = Ledger(search, page(listOf(message(59, "human"), message(60)), 59), Fail.Refused(503, null, "Index busy"))
        val found = LayaRules.load_turn_context(closing, judged_at, ledger) as TurnContext.Found
        assertEquals("never substitutes the latest user message", FirstUser.Unknown, found.first_user)
        assertEquals(listOf(59, 60), found.messages.map { it.seq })
    }

    @Test
    fun `a verdict the index has not caught up with says so, and a short text is never looked up`() = runBlocking {
        val missing = LayaRules.load_turn_context(closing, judged_at, Ledger(HistorySearch(hits = emptyList()))) as TurnContext.Missing
        assertEquals(MissReason.not_indexed, missing.reason)
        assertEquals(LayaRules.NOT_INDEXED, missing.message)
        val short = LayaRules.load_turn_context("Done.", judged_at, Ledger(search)) as TurnContext.Missing
        assertEquals(MissReason.no_text, short.reason)
    }

    @Test
    fun `a page the ledger lost reads as not indexed, any other failure as unavailable`() = runBlocking {
        val gone = LayaRules.load_turn_context(closing, judged_at, Ledger(search, Fail.Refused(404, null, "no such file"))) as TurnContext.Missing
        assertEquals(MissReason.not_indexed, gone.reason)
        val down = LayaRules.load_turn_context(closing, judged_at, Ledger(search, Fail.BridgeDown)) as TurnContext.Missing
        assertEquals(MissReason.unavailable, down.reason)
    }

    // ── the list across a reload ─────────────────────────────────────────────

    private fun at_row(at: String, operator: AnswerRef? = null, key: String? = null) = row(at = at, operator = operator, key = key)

    @Test
    fun `a TO GRADE reload puts this list's graded rows back where their time puts them`() {
        val graded = at_row("2026-11-07T10:30:00Z", operator = said(false))
        val other_answer = at_row("2026-11-07T10:10:00Z", operator = said(true))
        val page = listOf(at_row("2026-11-07T11:00:00Z"), at_row("2026-11-07T10:20:00Z"), at_row("2026-11-07T10:00:00Z"))
        val merged = LayaRules.keep_graded(page, listOf(graded, other_answer) + page, setOf(graded.key))
        assertEquals(listOf(page[0], graded, page[1], page[2]), merged)
        assertSame("nothing graded here: the page as it came", page, LayaRules.keep_graded(page, page, emptySet()))
        val oldest = at_row("2026-11-06T09:00:00Z", operator = said(false))
        assertEquals("older than the whole page: at its foot", oldest, LayaRules.keep_graded(page, listOf(oldest), setOf(oldest.key)).last())
    }

    @Test
    fun `a row the page already holds, or one taken back since, is never put back twice`() {
        val page_row = at_row("2026-11-07T10:30:00Z")
        val answered_copy = page_row.copy(operator = said(false))
        assertEquals(listOf(page_row), LayaRules.keep_graded(listOf(page_row), listOf(answered_copy), setOf(page_row.key)))
        val taken_back = at_row("2026-11-07T10:40:00Z")
        assertEquals(listOf(page_row), LayaRules.keep_graded(listOf(page_row), listOf(taken_back), setOf(taken_back.key)))
    }

    @Test
    fun `UNDO lands back on its text's row, widening a band that hides it, and says when the page has none`() {
        val rows = listOf(row(band = Band.done), row(band = Band.open))
        assertEquals(Landing(Band.open, rows[1].decision_id), LayaRules.return_to(rows, Band.open, rows[1].key))
        assertEquals(Landing(null, rows[1].decision_id), LayaRules.return_to(rows, Band.done, rows[1].key))
        assertNull(LayaRules.return_to(rows, null, "ffffffffffffffff"))
    }

    @Test
    fun `the undo stack round-trips through the saved state`() {
        val stack = listOf(Answered("0123456789abcdef", "a_00000000000000aa"), Answered("golden:case-1", "a_00000000000000ab"))
        val list = ListSerializer(Answered.serializer())
        assertEquals(stack, APP_JSON.decodeFromString(list, APP_JSON.encodeToString(list, stack)))
    }

    // ── the wire, decoded as the Python writers send it ──────────────────────

    /**
     * `review_data.recent_rows`: an answered row's `operator` is `review_log.answer_ref`, `key` included, and a skip's
     * `open` is null. The skip's operator has no `key`: an older bridge's shape, which must still decode.
     */
    private val recent_body = """
        {"v":1,"ok":true,"message":"","surface":"agent_open_work","rows":[
          {"key":"0a0b0c0d0e0f1013","decision_id":"20261107T224000-000000000003","at":"2026-11-07T22:40:00Z",
           "subject":"vc-7","p":0.8601,"threshold":0.7,"done_threshold":0.4,"mark":"open","band":"open",
           "edge":{"cut":"open","delta":0.1601},"model":"tldrack-agent-open-work",
           "checkpoint":"tldrack-agent-open-work-20260102-0304","text":"Added to review backlog","text_chars":23,
           "stratum":"flagged","reference_open":true,
           "operator":{"answer_id":"a_0000000000000003","key":"0a0b0c0d0e0f1013","answer":"flip","open":false,"needs":null}},
          {"key":"0a0b0c0d0e0f1012","decision_id":"20261107T210000-000000000003","at":"2026-11-07T21:00:00Z",
           "subject":"vc-7","p":0.55,"threshold":0.7,"done_threshold":null,"mark":"reply","band":"reply",
           "edge":{"cut":"open","delta":-0.15},"model":"tldrack-agent-open-work","checkpoint":null,
           "text":"Refactored the test.","text_chars":20,"stratum":"flagged","reference_open":false,
           "operator":{"answer_id":"a_0000000000000002","answer":"skip","open":null,"needs":null}},
          {"key":"0a0b0c0d0e0f1011","decision_id":"20261107T200000-000000000001","at":"2026-11-07T20:00:00Z",
           "subject":null,"p":0.2,"threshold":0.7,"done_threshold":0.4,"mark":"done","band":"done",
           "edge":{"cut":"done","delta":-0.2},"model":"tldrack-agent-open-work","checkpoint":null,
           "text":"Shipped.","text_chars":8,"stratum":null,"reference_open":null,"operator":null}
        ],"summary":{"n":3,"skipped":1,"done_at":0.4,"order":"newest","todo":false,"pool":3},"generated_at":"2026-11-07T22:50:00Z"}
    """.trimIndent()

    @Test
    fun `a RECENT page with answered rows decodes, with or without the operator's key`() {
        val page = APP_JSON.decodeFromString(RecentPage.serializer(), recent_body)
        assertEquals("agent_open_work", page.surface)
        assertEquals(3, page.rows.size)
        val (flipped, skipped, unanswered) = page.rows
        assertEquals(flipped.key, flipped.operator?.key)
        assertNull("an older bridge's operator", skipped.operator?.key)
        assertEquals(Verdict.done, LayaRules.operator_verdict(flipped.operator))
        assertEquals(Grade.wrong, LayaRules.grade(flipped))
        assertEquals("needs: null on a DONE answer", null, flipped.operator?.needs)
        assertEquals(Verdict.skip, LayaRules.operator_verdict(skipped.operator))
        assertNull(unanswered.operator)
        assertNull("no current card holds it", unanswered.reference_open)
        assertEquals(Edge(Cut.done, -0.2), unanswered.edge)
    }

    private fun conversation(first_user: String?): JsonObject {
        val tail = if (first_user == null) "" else ""","first_user":$first_user"""
        val body = """
            {"session":{"file_id":41,"session_id":"0a0b0c0d","source":"codex","profile":"second","kind":"main",
             "title":null,"cwd":"/Users/x/project"},
             "messages":[{"seq":11,"ts":1794085700.1,"role":"human","text":"make it mergeable"},
                         {"seq":12,"ts":1794085705.5,"role":"agent","text":"Refactored the test."}],
             "before":11,"after":13,"indexed_at":1794085800.0$tail}
        """.trimIndent()
        return APP_JSON.parseToJsonElement(body).jsonObject
    }

    @Test
    fun `first_user is three-state on the wire, read from the key's presence`() {
        val older = ledger_page(conversation(null))
        assertEquals("an older bridge sends no first_user at all", FirstUser.Unknown, older.first_user)
        assertEquals(listOf(11, 12), older.messages.map { it.seq })
        assertEquals(11, older.before)
        assertNull(older.title)
        assertEquals("/Users/x/project", older.cwd)
        assertEquals("the session's start holds no request", FirstUser.None, ledger_page(conversation("null")).first_user)
        val found = ledger_page(conversation("""{"seq":0,"ts":1794082000.5,"role":"human","text":"get the branch ready, please"}"""))
        assertEquals(FirstUser.Found(HistoryMessage(0, "human", "get the branch ready, please")), found.first_user)
    }

    @Test
    fun `a 409 reference_changed body hands back a full card, and already_answered the standing answer`() {
        val changed = """
            {"v":1,"ok":false,"code":"reference_changed","message":"The reference changed since the card was shown",
             "error":"The reference changed since the card was shown","answer":null,
             "card":{"item_id":"agent_open_work:0a0b0c0d0e0f1013","key":"0a0b0c0d0e0f1013","surface":"agent_open_work",
               "stratum":"flagged","text":"Added to review backlog","text_chars":23,"truncated":false,"held_out":false,
               "reference":{"by":"claude-sonnet","open":true,"why":"lists work still to do"},
               "sonnet":{"by":"claude-sonnet","open":true,"why":"lists work still to do"},
               "laya":{"p":0.8601,"threshold":0.7,"model":"tldrack-agent-open-work","checkpoint":null,
                       "question_version":3,"source":"turn_end"},
               "golden":null,"decision":{"decision_id":"20261107T224000-000000000003","at":"2026-11-07T22:40:00Z"},
               "answers":["agree","flip","skip"]},
             "stats":null}
        """.trimIndent()
        val result = APP_JSON.decodeFromString(ReviewResult.serializer(), changed)
        assertEquals(true, result.card?.reference?.open)
        assertNull(result.answer)
        assertEquals("reference_changed", APP_JSON.decodeFromString(Envelope.serializer(), changed).code)
        val standing = """
            {"v":1,"ok":false,"code":"already_answered","message":"That item is already answered: undo it first",
             "error":"That item is already answered: undo it first",
             "answer":{"answer_id":"a_0000000000000003","key":"0a0b0c0d0e0f1013","answer":"agree","open":true,"needs":"you"},
             "card":null,"stats":null}
        """.trimIndent()
        val answer = APP_JSON.decodeFromString(ReviewResult.serializer(), standing).answer
        assertEquals(AnswerRef("a_0000000000000003", "0a0b0c0d0e0f1013", ReviewAnswer.agree, open = true, needs = Needs.you), answer)
        assertEquals(Verdict.you, LayaRules.operator_verdict(answer))
    }

    @Test
    fun `only a VIEW of tldrack review is the review link`() {
        val view = "android.intent.action.VIEW"
        assertTrue(LayaRules.is_review_link(view, "tldrack", "review"))
        assertFalse("a launch is not the link", LayaRules.is_review_link("android.intent.action.MAIN", "tldrack", "review"))
        assertFalse(LayaRules.is_review_link(view, "tldrack", "laya"))
        assertFalse("another app's scheme is not this app's", LayaRules.is_review_link(view, "other-app", "review"))
        assertFalse(LayaRules.is_review_link(view, null, null))
    }
}
