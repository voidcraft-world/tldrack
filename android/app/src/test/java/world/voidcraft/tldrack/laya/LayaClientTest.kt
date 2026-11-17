package world.voidcraft.tldrack.laya

import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.CopyOnWriteArrayList
import kotlin.concurrent.thread
import kotlinx.coroutines.runBlocking
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Request
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * `LayaClient` against a fake phone-link on loopback: the handshake's order (nothing token-derived before a signed
 * challenge), the exact target and body bytes it signs, and how each unsigned or signed reply reads. The numbers
 * themselves are `PhoneLinkProofTest`'s; the contract is plugins/phone-link/README.md's.
 */
class LayaClientTest {

    @get:Rule
    val tmp = TemporaryFolder()

    private val token = "TEST_VECTOR_TOKEN_do_not_use_0123456789abcd"
    private val links = mutableListOf<FakeLink>()

    @After
    fun close_links() = links.forEach(FakeLink::close)

    // ── a fake link: HTTP/1.0, one request per connection, every request recorded ──

    private class Seen(val method: String, val target: String, val headers: Map<String, List<String>>, val body: ByteArray, val raw: ByteArray)

    private class Answer(
        val status: Int,
        val body: ByteArray,
        val mac: String? = null,
        val drop: Boolean = false,
        val headers: Map<String, String> = emptyMap(),
    )

    private class FakeLink(private val answer: (Seen) -> Answer) : AutoCloseable {
        private val server = ServerSocket(0, 16, InetAddress.getByName("127.0.0.1"))
        val seen = CopyOnWriteArrayList<Seen>()
        val url: HttpUrl = "http://127.0.0.1:${server.localPort}/".toHttpUrl()

        init {
            thread(isDaemon = true) {
                while (true) {
                    val socket = try {
                        server.accept()
                    } catch (error: IOException) {
                        break
                    }
                    socket.use(::serve)
                }
            }
        }

        private fun serve(socket: Socket) {
            socket.soTimeout = 5_000
            val input = socket.getInputStream()
            val head = read_head(input)
            val lines = String(head, Charsets.ISO_8859_1).split("\r\n").filter { it.isNotEmpty() }
            if (lines.isEmpty()) return // connected, then closed with nothing sent
            val (method, target) = lines[0].split(" ")
            val headers = lines.drop(1).groupBy({ it.substringBefore(':').trim().lowercase() }, { it.substringAfter(':').trim() })
            val body = read_exactly(input, headers["content-length"]?.single()?.toInt() ?: 0)
            val seen_now = Seen(method, target, headers, body, head + body)
            seen.add(seen_now)
            val reply = answer(seen_now)
            if (reply.drop) return
            val out = StringBuilder("HTTP/1.0 ${reply.status} Fake\r\nContent-Type: application/json\r\n")
                .append("Content-Length: ${reply.body.size}\r\n")
            reply.mac?.let { out.append("${PhoneLinkProof.REPLY_HEADER}: $it\r\n") }
            reply.headers.forEach { (name, value) -> out.append("$name: $value\r\n") }
            out.append("Connection: close\r\n\r\n")
            socket.getOutputStream().apply {
                write(out.toString().toByteArray(Charsets.ISO_8859_1))
                write(reply.body)
                flush()
            }
        }

        private fun read_head(input: InputStream): ByteArray {
            val head = ByteArrayOutputStream()
            var tail = 0
            while (tail != 0x0d0a0d0a) {
                val b = input.read()
                if (b < 0) break
                head.write(b)
                tail = (tail shl 8) or b
            }
            return head.toByteArray()
        }

        private fun read_exactly(input: InputStream, n: Int): ByteArray {
            val out = ByteArray(n)
            var at = 0
            while (at < n) {
                val got = input.read(out, at, n - at)
                if (got < 0) break
                at += got
            }
            return out.copyOf(at)
        }

        override fun close() = server.close()
    }

    private fun json(text: String) = text.toByteArray(Charsets.UTF_8)

    /**
     * phone-link as the README draws it, keyed by `key`: `/nonce` signed over `c`; a request proven by a single-use
     * nonce and a MAC over the target and body AS RECEIVED; every proven answer signed over its nonce.
     */
    private fun the_link(key: String = token, route: (Seen) -> Pair<Int, String>): (Seen) -> Answer {
        val issued = mutableSetOf<String>()
        return answer@{ seen ->
            if (seen.method == "GET" && seen.target.startsWith("/nonce?c=")) {
                val c = seen.target.removePrefix("/nonce?c=")
                val nonce = PhoneLinkProof.challenge()
                issued += nonce
                val body = json("""{"ok":true,"v":2,"nonce":"$nonce","ttl_s":30}""")
                return@answer Answer(200, body, PhoneLinkProof.nonce_mac(key, c, 200, body))
            }
            val held = seen.headers["authorization"]?.singleOrNull()?.let { AUTH.matchEntire(it) }
            val nonce = held?.groupValues?.get(1)
            val expected = nonce?.let { PhoneLinkProof.request_mac(key, it, seen.method, seen.target, seen.body) }
            // Spent when presented, valid MAC or not, as the link's nonce book spends it.
            if (nonce == null || !issued.remove(nonce) || expected == null || !PhoneLinkProof.verify(expected, held?.groupValues?.get(2))) {
                return@answer Answer(401, json("""{"ok":false,"error":"unlinked"}"""))
            }
            val (status, text) = route(seen)
            val body = json(text)
            Answer(status, body, PhoneLinkProof.reply_mac(key, nonce, status, body))
        }
    }

    private fun link(answer: (Seen) -> Answer): FakeLink = FakeLink(answer).also(links::add)

    private fun client(link: FakeLink, with_token: String? = token): LayaClient {
        val dir = tmp.newFolder()
        // The Mac writes the token with a trailing newline (token_store.py); the app trims it.
        if (with_token != null) File(dir, LayaClient.TOKEN_FILE).writeText("$with_token\n")
        return LayaClient(dir, link.url)
    }

    private fun assert_never_shown_the_token(link: FakeLink) {
        for (seen in link.seen) assertTrue(!String(seen.raw, Charsets.ISO_8859_1).contains(token))
    }

    private val answer_request = AnswerRequest(
        surface = "agent_open_work",
        key = "0123456789abcdef",
        answer = ReviewAnswer.agree,
        reference_open = false,
    )

    // ── a linked call ────────────────────────────────────────────────────────

    @Test
    fun `a call is an unsigned nonce, then one signed request, on two connections`() = runBlocking {
        val fake = link(the_link { 200 to """{"ok":true,"v":2}""" })
        assertEquals(Call.Ok(Unit), client(fake).health())
        assertEquals(2, fake.seen.size)
        val (nonce, request) = fake.seen
        assertEquals("GET", nonce.method)
        assertTrue(nonce.target, Regex("/nonce\\?c=[A-Za-z0-9_-]{43}").matches(nonce.target))
        assertNull(nonce.headers["authorization"])
        assertEquals("/health", request.target)
        assertTrue(Regex("PhoneLink n=[A-Za-z0-9_-]{43}, mac=[0-9a-f]{64}").matches(request.headers.getValue("authorization").single()))
        for (seen in fake.seen) {
            assertNull(seen.headers["origin"])
            assertTrue(seen.headers.keys.none { it.startsWith("sec-fetch-") })
            assertEquals(listOf("close"), seen.headers["connection"])
            assertEquals(listOf("identity"), seen.headers["accept-encoding"])
        }
        assert_never_shown_the_token(fake)
    }

    @Test
    fun `a POST is signed over the exact bytes it sends, with no charset added`() = runBlocking {
        val fake = link(the_link { 200 to """{"message":"Recorded"}""" })
        val got = client(fake).answer(answer_request)
        assertEquals(Call.Ok(ReviewResult(message = "Recorded")), got)
        val sent = fake.seen.last()
        assertEquals("POST", sent.method)
        assertEquals("/plugin/laya/review/answer", sent.target)
        assertEquals(listOf("application/json"), sent.headers["content-type"])
        assertEquals(
            """{"surface":"agent_open_work","key":"0123456789abcdef","answer":"agree","reference_open":false}""",
            String(sent.body, Charsets.UTF_8),
        )
        assert_never_shown_the_token(fake)
    }

    @Test
    fun `a query is signed as the request line carries it`() = runBlocking {
        val fake = link(the_link { 200 to """{"hits":[]}""" })
        assertEquals(Call.Ok(HistorySearch(hits = emptyList())), client(fake).search("a+b & café ✓"))
        assertEquals("/ledger/search?q=a%2Bb%20%26%20caf%C3%A9%20%E2%9C%93", fake.seen.last().target)
    }

    @Test
    fun `each call takes a fresh nonce, and none is presented twice`() = runBlocking {
        val fake = link(the_link { 200 to """{"ok":true,"v":2}""" })
        val laya = client(fake)
        repeat(3) { assertEquals(Call.Ok(Unit), laya.health()) }
        val nonces = fake.seen.mapNotNull { it.headers["authorization"]?.single()?.let { h -> AUTH.matchEntire(h)?.groupValues?.get(1) } }
        assertEquals(3, nonces.toSet().size)
    }

    // ── something else on the port ───────────────────────────────────────────

    @Test
    fun `a squatter that cannot sign the challenge sees only the challenge`() = runBlocking {
        for (mac in listOf(null, "0".repeat(64))) {
            val squatter = link { Answer(200, json("""{"ok":true,"v":2,"nonce":"${"A".repeat(43)}","ttl_s":30}"""), mac) }
            assertEquals(Fail.Unlinked, client(squatter).answer(answer_request))
            assertEquals(1, squatter.seen.size)
            assertEquals("GET", squatter.seen.single().method)
            assertNull(squatter.seen.single().headers["authorization"])
            assert_never_shown_the_token(squatter)
        }
    }

    @Test
    fun `a link keyed by another token is unlinked after the nonce`() = runBlocking {
        val rotated = link(the_link(key = "ROTATED_TOKEN_do_not_use_0123456789abcdefgh") { 200 to "{}" })
        assertEquals(Fail.Unlinked, client(rotated).health())
        assertEquals(1, rotated.seen.size)
    }

    @Test
    fun `a proven request answered without a valid signature is unlinked`() = runBlocking {
        for (mac in listOf(null, "0".repeat(64))) {
            val signs_only_nonces = the_link { 200 to "{}" }
            val fake = link { seen ->
                val real = signs_only_nonces(seen)
                if (seen.target.startsWith("/nonce")) real else Answer(200, json("""{"ok":true}"""), mac)
            }
            assertEquals(Fail.Unlinked, client(fake).health())
        }
    }

    @Test
    fun `an unsigned 401 is unlinked`() = runBlocking {
        val no_token_on_mac = link { Answer(401, json("""{"ok":false,"error":"unlinked"}""")) }
        assertEquals(Fail.Unlinked, client(no_token_on_mac).health())
    }

    @Test
    fun `unsigned pre-proof refusals read in the app's words`() = runBlocking {
        val busy = link { Answer(503, json("""{"ok":false,"error":"busy"}""")) }
        assertEquals(Fail.Refused(503, "busy", "The Mac's link is busy", "retry in a moment"), client(busy).health())
        val host = link { Answer(403, json("""{"ok":false,"error":"host_refused"}""")) }
        assertEquals(
            Fail.Refused(403, "host_refused", "phone-link refused this request's headers", "update the app (./gradlew installDebug)"),
            client(host).health(),
        )
    }

    @Test
    fun `an unsigned body never chooses the code or the words`() = runBlocking {
        // `already_undone` would tell UNDO the answer is gone: only phone-link's own pre-proof codes pass unsigned.
        val spoof = link { Answer(400, json("""{"ok":false,"error":"already_undone","message":"Open evil.example"}""")) }
        val got = client(spoof).health()
        assertEquals(Fail.Refused(400, null, "phone-link could not read this request", "update the app (./gradlew installDebug)"), got)
        // A status phone-link never sends unsigned is simply not the link.
        val teapot = link { Answer(418, json("""{"ok":false,"error":"busy"}""")) }
        assertEquals(Fail.Unlinked, client(teapot).health())
        val unsigned_ok = link { Answer(200, json("""{"ok":true,"v":2}""")) }
        assertEquals(Fail.Unlinked, client(unsigned_ok).health())
    }

    @Test
    fun `an unsigned code survives only as phone-link spells it, byte for byte`() = runBlocking {
        // http.server's own 414 is phone-link's `send_error`: `bad_request` at that status.
        val long_line = link { Answer(414, json("""{"ok":false,"error":"bad_request"}""")) }
        assertEquals(Fail.Refused(414, "bad_request", "This request is too large for phone-link", null), client(long_line).health())
        val spaced = link { Answer(503, json("""{"ok": false, "error": "busy"}""")) }
        assertEquals(Fail.Refused(503, null, "The Mac's link is busy", "retry in a moment"), client(spaced).health())
    }

    @Test
    fun `an unsigned body is never parsed, however it nests`() = runBlocking {
        // A parser recursing once per `[` would overflow the stack on this and take the process with it.
        val squatter = link { Answer(400, json("[".repeat(50_000))) }
        assertEquals(Fail.Refused(400, null, "phone-link could not read this request", "update the app (./gradlew installDebug)"), client(squatter).health())
        assertEquals(1, squatter.seen.size)
    }

    @Test
    fun `a nonce reply longer than any nonce is not the link's`() = runBlocking {
        val long_nonce = link { seen ->
            val c = seen.target.removePrefix("/nonce?c=")
            val body = json("""{"ok":true,"v":2,"nonce":"${"A".repeat(43)}","pad":"${"x".repeat(2_000)}"}""")
            Answer(200, body, PhoneLinkProof.nonce_mac(token, c, 200, body))
        }
        assertEquals(Fail.Unlinked, client(long_nonce).health())
        assertEquals(1, long_nonce.seen.size)
    }

    // ── the link's own answers ───────────────────────────────────────────────

    @Test
    fun `a signed bridge_down is BRIDGE DOWN, a signed bad_upstream a refusal`() = runBlocking {
        val down = link(the_link { 502 to """{"ok":false,"error":"bridge_down"}""" })
        assertEquals(Fail.BridgeDown, client(down).health())
        val bad = link(the_link { 502 to """{"ok":false,"error":"bad_upstream"}""" })
        val got = client(bad).recent(todo = true)
        assertTrue(got.toString(), got is Fail.Refused && got.code == "bad_upstream")
    }

    @Test
    fun `a signed 409 carries its body to the list`() = runBlocking {
        val fake = link(the_link { 409 to """{"message":"Already answered","code":"already_answered"}""" })
        val got = client(fake).answer(answer_request)
        assertTrue(got.toString(), got is Fail.Refused && got.status == 409 && got.result == ReviewResult(message = "Already answered"))
    }

    // ── no link at all ───────────────────────────────────────────────────────

    @Test
    fun `no token file makes no connection`() = runBlocking {
        val fake = link(the_link { 200 to "{}" })
        assertEquals(Fail.Unlinked, client(fake, with_token = null).health())
        assertEquals(Fail.Unlinked, client(fake, with_token = "short").health())
        assertTrue(fake.seen.isEmpty())
    }

    @Test
    fun `nothing listening is unlinked`() = runBlocking {
        val port = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1")).use { it.localPort }
        val dir = tmp.newFolder().also { File(it, LayaClient.TOKEN_FILE).writeText(token) }
        assertEquals(Fail.Unlinked, LayaClient(dir, "http://127.0.0.1:$port/".toHttpUrl()).health())
    }

    @Test
    fun `a dropped request is never replayed`() = runBlocking {
        val link_answer = the_link { 200 to "{}" }
        val fake = link { seen -> if (seen.target.startsWith("/nonce")) link_answer(seen) else Answer(0, ByteArray(0), drop = true) }
        assertEquals(Fail.BridgeDown, client(fake).answer(answer_request))
        assertEquals(listOf("GET", "POST"), fake.seen.map { it.method })
    }

    @Test
    fun `a 503 asking for an immediate retry is answered, never re-sent`() = runBlocking {
        val again = mapOf("Retry-After" to "0")
        val busy = json("""{"ok":false,"error":"busy"}""")
        val busy_refusal = Fail.Refused(503, "busy", "The Mac's link is busy", "retry in a moment")
        val at_nonce = link { Answer(503, busy, headers = again) }
        assertEquals(busy_refusal, client(at_nonce).answer(answer_request))
        assertEquals(1, at_nonce.seen.size)
        val link_answer = the_link { 200 to "{}" }
        val at_request = link { seen -> if (seen.target.startsWith("/nonce")) link_answer(seen) else Answer(503, busy, headers = again) }
        assertEquals(busy_refusal, client(at_request).answer(answer_request))
        assertEquals(listOf("GET", "POST"), at_request.seen.map { it.method })
    }

    @Test
    fun `the link's client puts each call on the wire once, whatever asks for a second`() {
        val fake = link { Answer(401, json("""{"ok":false,"error":"unlinked"}""")) }
        // An authenticator re-sends on every 401: the one follow-up a test can ask OkHttp for.
        val resends = LayaClient.LINK_HTTP.newBuilder().authenticator { _, response -> response.request }.build()
        val call = resends.newCall(Request.Builder().url(fake.url.resolve("health")!!).build())
        assertThrows(IOException::class.java) { call.execute().close() }
        assertEquals(1, fake.seen.size)
    }

    private companion object {
        val AUTH = Regex("PhoneLink n=([A-Za-z0-9_-]{43}), mac=([0-9a-f]{64})")
    }
}
