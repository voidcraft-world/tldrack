package world.voidcraft.tldrack.laya

import java.io.File
import java.io.IOException
import java.net.ConnectException
import java.net.Proxy
import java.util.Collections
import java.util.WeakHashMap
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.KSerializer
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import okhttp3.ConnectionPool
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import world.voidcraft.tldrack.APP_JSON

/**
 * The Laya review routes, through the Mac's phone-link listener — never the bridge's own port. Every call is
 * phone-link handshake v2 (`PhoneLinkProof`). The contract (the port, the token, the handshake, the allowlist, what
 * each status means): owned by plugins/phone-link/README.md — see the note there.
 *
 * Never throws: every call answers `Call.Ok`, or a `Fail` whose kind is the remedy. The token is read from the file
 * the Mac pushed into this app's private storage on every call (a re-push takes effect at once). It is only an HMAC
 * key: never sent, never logged, never in an error message.
 *
 * `base_url` is phone-link's; a test points it at a fake link on another loopback port.
 */
class LayaClient(private val files_dir: File, private val base_url: HttpUrl = BASE_URL) : ReviewCalls, LedgerSource {

    suspend fun health(): Call<Unit> = send(url("health"), null) { status, text -> read_ack("health", status, text) }

    suspend fun recent(todo: Boolean): Call<RecentPage> {
        val url = url("plugin/laya/review/recent") {
            addQueryParameter("limit", LayaRules.RECENT_LIMIT.toString())
            // The default is never sent: a bridge from before TO GRADE refuses a name it does not know.
            if (todo) addQueryParameter("todo", "1")
        }
        return send(url, null) { status, text -> read("review/recent", status, text, RecentPage.serializer()) }
    }

    override suspend fun answer(request: AnswerRequest): Call<ReviewResult> {
        if (!LayaRules.sendable(request)) return unsent("review/answer")
        val body = APP_JSON.encodeToString(AnswerRequest.serializer(), request)
        return send(url("plugin/laya/review/answer"), body) { status, text -> read_review("review/answer", status, text) }
    }

    override suspend fun undo(request: UndoRequest): Call<ReviewResult> {
        if (!LayaRules.sendable(request)) return unsent("review/undo")
        val body = APP_JSON.encodeToString(UndoRequest.serializer(), request)
        return send(url("plugin/laya/review/undo"), body) { status, text -> read_review("review/undo", status, text) }
    }

    override suspend fun search(q: String): Call<HistorySearch> =
        send(url("ledger/search") { addQueryParameter("q", q) }, null) { status, text ->
            read("ledger/search", status, text, HistorySearch.serializer())
        }

    override suspend fun conversation(file_id: Long, mode: PageMode, anchor: Int): Call<LedgerPage> {
        val url = url("ledger/conversation") {
            addQueryParameter("file_id", file_id.toString())
            addQueryParameter(mode.name, anchor.toString())
        }
        return send(url, null) { status, text -> read_conversation(status, text) }
    }

    /** REFRESH INDEX: wake the ledger's ingest timer (202); the search can find a fresh verdict a few seconds later. */
    suspend fun ingest(): Call<Unit> = send(url("ledger/ingest"), "{}") { status, text -> read_ack("ledger/ingest", status, text) }

    suspend fun topic_labels(session: String): Call<TopicView> =
        send(url("topic-labels") { addQueryParameter("session", session) }, null) { status, text ->
            read("topic-labels", status, text, TopicView.serializer())
        }

    // ── the one request path: nonce, signed request, signed reply ─────────────

    private fun url(path: String, query: HttpUrl.Builder.() -> Unit = {}): HttpUrl =
        base_url.newBuilder().addPathSegments(path).apply(query).build()

    /**
     * Token, nonce, signed request, signed reply, status. Nothing listening on loopback (no `adb reverse` armed) is a
     * refused connect, which is UNLINKED; any other transport failure means the tunnel took the connection and the Mac
     * end dropped it — the bridge, which hosts phone-link, is not running. (A squatter that accepts and hangs up reads
     * the same: nothing unsigned can be told apart from it.) A reply the link did not sign is `unproven`.
     * A 502 is BRIDGE DOWN only when phone-link says `bridge_down`: its `bad_upstream` (the bridge answered, but too
     * much or not JSON) and a bridge's own 502 are refusals.
     */
    private suspend fun <T> send(url: HttpUrl, json_body: String?, decode: (Int, String) -> Call<T>): Call<T> =
        withContext(Dispatchers.IO) {
            val token = read_token() ?: return@withContext Fail.Unlinked
            val nonce = when (val issued = issue(token)) {
                is Call.Ok -> issued.data
                is Fail -> return@withContext issued
            }
            // Hashed and sent as these exact bytes: a String body would let OkHttp append a charset to the type.
            val body = json_body?.toByteArray(Charsets.UTF_8)
            val method = if (body != null) "POST" else "GET"
            val mac = PhoneLinkProof.request_mac(token, nonce, method, PhoneLinkProof.target_of(url), body ?: NO_BYTES)
            val request = link_request(url).header("Authorization", PhoneLinkProof.authorization(nonce, mac))
            if (body != null) request.post(body.toRequestBody(JSON_MEDIA)) else request.get()
            val reply = when (val got = exchange(request.build(), MAX_REPLY_BYTES)) {
                is Call.Ok -> got.data
                is Fail -> return@withContext got
            }
            if (!PhoneLinkProof.reply_signed(token, nonce, reply.status, reply.body, reply.mac)) {
                return@withContext unproven(reply)
            }
            val text = reply.body.toString(Charsets.UTF_8)
            when {
                reply.status == 401 -> Fail.Unlinked
                reply.status == 502 && envelope_of(parse(text))?.error == BRIDGE_DOWN -> Fail.BridgeDown
                else -> decode(reply.status, text)
            }
        }

    /** Step 1: a nonce, believed only once its reply is signed over this call's own challenge. Nothing token-derived goes out before. */
    private fun issue(token: String): Call<String> {
        val challenge = PhoneLinkProof.challenge()
        val url = base_url.newBuilder().addPathSegment("nonce").addQueryParameter("c", challenge).build()
        val reply = when (val got = exchange(link_request(url).get().build(), NONCE_REPLY_BYTES)) {
            is Call.Ok -> got.data
            is Fail -> return got
        }
        if (!PhoneLinkProof.nonce_signed(token, challenge, reply.status, reply.body, reply.mac)) return unproven(reply)
        val issued = try {
            APP_JSON.decodeFromString(NonceReply.serializer(), reply.body.toString(Charsets.UTF_8))
        } catch (error: IllegalArgumentException) {
            null
        }
        val nonce = issued?.takeIf {
            reply.status == 200 && it.ok && it.v == PhoneLinkProof.VERSION && PhoneLinkProof.NONCE_SHAPE.matches(it.nonce)
        }?.nonce
        return if (nonce != null) Call.Ok(nonce) else invalid("nonce", reply.status)
    }

    /** No `Origin` and nothing else a browser sends. `identity`: OkHttp would otherwise ask for gzip and inflate the bytes the MAC covers. */
    private fun link_request(url: HttpUrl): Request.Builder = Request.Builder()
        .url(url)
        .header("Accept", "application/json")
        .header("Accept-Encoding", "identity")
        .header("Connection", "close")

    /**
     * One connection, one request: the status, the exact body bytes, and the reply's signature when it carries exactly
     * one. A signed reply is read up to `max_bytes`, and past that it is not the link's. An unsigned one can only be a
     * pre-proof refusal, so it is read no further than the longest of those: past that its body stays unread, and an
     * empty body matches no code.
     */
    private fun exchange(request: Request, max_bytes: Long): Call<LinkReply> {
        return try {
            LINK_HTTP.newCall(request).execute().use { response ->
                val mac = response.headers(PhoneLinkProof.REPLY_HEADER).singleOrNull()
                val body = bounded_body(response, if (mac != null) max_bytes else UNSIGNED_REPLY_BYTES)
                when {
                    body != null -> Call.Ok(LinkReply(response.code, body, mac))
                    mac != null -> Fail.Unlinked
                    else -> Call.Ok(LinkReply(response.code, NO_BYTES, null))
                }
            }
        } catch (error: ConnectException) {
            Fail.Unlinked
        } catch (error: IOException) {
            Fail.BridgeDown
        }
    }

    /** The body, or null past `max_bytes`. */
    private fun bounded_body(response: Response, max_bytes: Long): ByteArray? {
        val body = response.body ?: return NO_BYTES
        if (body.contentLength() > max_bytes) return null
        val source = body.source()
        return if (source.request(max_bytes + 1)) null else source.readByteArray()
    }

    /**
     * A reply without a valid `Phone-Link-Mac` is not the link's, so it is UNLINKED, except for the refusals phone-link
     * makes before the proof, which it never signs. Those read as a refusal in this app's words: an unsigned body may
     * be anyone's, so it is never parsed and none of its text is shown. Its code survives only when the body is, byte
     * for byte, one phone-link sends at that status.
     */
    private fun unproven(reply: LinkReply): Fail {
        val refusal = PRE_PROOF[reply.status]
        if (reply.mac != null || refusal == null) return Fail.Unlinked
        return Fail.Refused(reply.status, refusal.code_of(reply.body), refusal.message, refusal.hint)
    }

    private fun read_token(): String? {
        val raw = try {
            File(files_dir, TOKEN_FILE).readText()
        } catch (error: IOException) {
            return null
        }
        return raw.trim().takeIf { TOKEN_SHAPE.matches(it) }
    }

    /** A 2xx body is parsed once; an envelope saying `ok: false` is a refusal whatever its status. */
    private fun <T> read(route: String, status: Int, text: String, serializer: KSerializer<T>): Call<T> {
        if (status !in 200..299) return refused(route, status, text, null)
        val json = parse(text) ?: return invalid(route, status)
        if (envelope_of(json)?.ok == false) return refused(route, status, text, null)
        return try {
            Call.Ok(APP_JSON.decodeFromJsonElement(serializer, json))
        } catch (error: IllegalArgumentException) {
            invalid(route, status)
        }
    }

    private fun read_ack(route: String, status: Int, text: String): Call<Unit> {
        if (status !in 200..299) return refused(route, status, text, null)
        val json = parse(text) ?: return invalid(route, status)
        return if (envelope_of(json)?.ok == false) refused(route, status, text, null) else Call.Ok(Unit)
    }

    /** A 409 from answer/undo carries a body the list acts on (`already_answered`, `reference_changed`): it rides the refusal. */
    private fun read_review(route: String, status: Int, text: String): Call<ReviewResult> {
        if (status != 409) return read(route, status, text, ReviewResult.serializer())
        val json = parse(text) ?: return invalid(route, status)
        val result = try {
            APP_JSON.decodeFromJsonElement(ReviewResult.serializer(), json)
        } catch (error: IllegalArgumentException) {
            return invalid(route, status)
        }
        return refused(route, status, text, result)
    }

    /** `first_user` is three-state on the wire: `ledger_page` owns how it is read. */
    private fun read_conversation(status: Int, text: String): Call<LedgerPage> {
        val route = "ledger/conversation"
        if (status !in 200..299) return refused(route, status, text, null)
        val raw = parse(text) as? JsonObject ?: return invalid(route, status)
        return try {
            Call.Ok(ledger_page(raw))
        } catch (error: IllegalArgumentException) {
            invalid(route, status)
        }
    }

    private fun parse(text: String): JsonElement? =
        try {
            APP_JSON.parseToJsonElement(text)
        } catch (error: IllegalArgumentException) {
            null
        }

    private fun envelope_of(json: JsonElement?): Envelope? =
        try {
            json?.let { APP_JSON.decodeFromJsonElement(Envelope.serializer(), it) }
        } catch (error: IllegalArgumentException) {
            null
        }

    /**
     * The refusal as the server said it: the laya envelope's `message`, else the bridge's or phone-link's `error`.
     * phone-link's own codes are words, not sentences, so the ones an app user can act on get a sentence and a remedy.
     */
    private fun refused(route: String, status: Int, text: String, result: ReviewResult?): Fail.Refused {
        val envelope = envelope_of(parse(text))
        if (status == 404 && envelope?.error == NOT_ALLOWED) {
            return Fail.Refused(404, NOT_ALLOWED, "phone-link does not forward $route", "restart the bridge on the Mac")
        }
        if (status == 502 && envelope?.error == BAD_UPSTREAM) {
            return Fail.Refused(502, BAD_UPSTREAM, "The bridge answered $route, but too large or not JSON for phone-link to pass on")
        }
        val message = envelope?.message?.ifBlank { null } ?: envelope?.error?.ifBlank { null } ?: "$route failed ($status)"
        return Fail.Refused(status, envelope?.code ?: envelope?.error?.takeIf { CODE_SHAPE.matches(it) }, message, envelope?.hint, result)
    }

    private fun invalid(route: String, status: Int): Fail.Refused = Fail.Refused(status, "invalid", "$route: unreadable answer")

    private fun unsent(route: String): Fail.Refused = Fail.Refused(0, "invalid", "$route: invalid request")

    /** One exchange with whatever answered on the link's port, before anything says it was the link. */
    private class LinkReply(val status: Int, val body: ByteArray, val mac: String?)

    /** `GET /nonce`'s body. */
    @Serializable
    private data class NonceReply(val ok: Boolean = false, val v: Int = 0, val nonce: String = "")

    /** A status phone-link answers unsigned: the app's words for it, and the codes it sends there. */
    private class PreProof(val message: String, val hint: String?, vararg codes: String) {
        /** Each code's body as phone-link spells it (`link_server.error_body`, compact `json.dumps`). */
        val bodies: Map<String, ByteArray> = codes.associateWith { """{"ok":false,"error":"$it"}""".toByteArray(Charsets.US_ASCII) }

        fun code_of(body: ByteArray): String? = bodies.entries.firstOrNull { it.value.contentEquals(body) }?.key
    }

    /**
     * The link's one network attempt per call. OkHttp sends a request again on its own after some replies (a 503 with
     * `Retry-After: 0`, whatever `retryOnConnectionFailure` says), and the second copy would present the nonce again,
     * to whatever holds the port by then. `Retry-After` is not covered by the MAC, so it is dropped before OkHttp reads
     * it; any other second attempt fails here, before a byte of it is sent.
     */
    internal object OneAttempt : Interceptor {
        private const val RETRY_AFTER = "Retry-After"
        private val sent: MutableSet<okhttp3.Call> = Collections.synchronizedSet(Collections.newSetFromMap(WeakHashMap()))

        override fun intercept(chain: Interceptor.Chain): Response {
            if (!sent.add(chain.call())) throw IOException("phone-link: a second attempt of one call")
            val response = chain.proceed(chain.request())
            return if (response.header(RETRY_AFTER) == null) response else response.newBuilder().removeHeader(RETRY_AFTER).build()
        }
    }

    companion object {
        /** `PHONE_LINK_PORT` on the Mac; the only port `adb reverse` tunnels. */
        const val PHONE_LINK_PORT = 7683

        /** Under `Context.filesDir`; the Mac writes it with `run-as`, so only a debug build can be linked. */
        const val TOKEN_FILE = "phone-link-token"

        private val BASE_URL: HttpUrl = "http://127.0.0.1:$PHONE_LINK_PORT/".toHttpUrl()
        private val JSON_MEDIA = "application/json".toMediaType()
        private val NO_BYTES = ByteArray(0)

        /** phone-link's cap on a forwarded reply (`phone_link/upstream.py`); its own bodies are a few bytes. */
        private const val MAX_REPLY_BYTES = 8L * 1024 * 1024

        /** `GET /nonce`'s answer is about 100 bytes. */
        private const val NONCE_REPLY_BYTES = 1024L

        /**
         * The link's own client, never derived from the app's, so no interceptor, proxy or cookie jar added there can
         * reach this port. No proxy: it would see the request, and write an absolute URL on the request line the MAC
         * does not cover. Nothing idles in the pool (one request per connection) and nothing is retried, because a
         * replayed request presents a spent nonce (`OneAttempt`). No redirects: a 3xx from loopback is never a place to
         * follow. Loopback through `adb reverse` answers in milliseconds; the timeouts only bound a hung Mac.
         */
        internal val LINK_HTTP: OkHttpClient by lazy {
            OkHttpClient.Builder()
                .proxy(Proxy.NO_PROXY)
                .addNetworkInterceptor(OneAttempt)
                .connectionPool(ConnectionPool(0, 1, TimeUnit.SECONDS))
                .retryOnConnectionFailure(false)
                .followRedirects(false)
                .followSslRedirects(false)
                .connectTimeout(3, TimeUnit.SECONDS)
                .readTimeout(12, TimeUnit.SECONDS)
                .writeTimeout(5, TimeUnit.SECONDS)
                .callTimeout(15, TimeUnit.SECONDS)
                .build()
        }

        /** Exactly what `phone_link/token_store.py` mints: `secrets.token_urlsafe(32)`. */
        private val TOKEN_SHAPE = Regex("[A-Za-z0-9_-]{43}")
        private val CODE_SHAPE = Regex("[a-z][a-z0-9_]{0,39}")
        private const val NOT_ALLOWED = "not_allowed"
        private const val BRIDGE_DOWN = "bridge_down"
        private const val BAD_UPSTREAM = "bad_upstream"

        // phone-link's unsigned refusals, by status (README § The listener's checks). 414 and 431 are http.server's
        // own, which phone-link's `send_error` answers as `bad_request`.
        private const val UPDATE_APP = "update the app (./gradlew installDebug)"
        private const val UNREADABLE = "phone-link could not read this request"
        private const val TOO_LARGE = "This request is too large for phone-link"
        private val PRE_PROOF: Map<Int, PreProof> = mapOf(
            400 to PreProof(UNREADABLE, UPDATE_APP, "bad_challenge", "bad_body", "bad_request"),
            403 to PreProof("phone-link refused this request's headers", UPDATE_APP, "host_refused", "proxy_refused", "browser_refused"),
            411 to PreProof(UNREADABLE, UPDATE_APP, "length_required"),
            413 to PreProof(TOO_LARGE, null, "too_large"),
            414 to PreProof(TOO_LARGE, null, "bad_request"),
            431 to PreProof(TOO_LARGE, null, "bad_request"),
            503 to PreProof("The Mac's link is busy", "retry in a moment", "busy"),
        )

        /** The longest body phone-link sends unsigned: the most of an unsigned reply that is ever read. */
        private val UNSIGNED_REPLY_BYTES: Long = PRE_PROOF.values.maxOf { refusal -> refusal.bodies.values.maxOf { it.size } }.toLong()
    }
}
