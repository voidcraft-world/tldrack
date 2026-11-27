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
import kotlinx.serialization.json.JsonPrimitive
import okhttp3.ConnectionPool
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
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
 * Two ways to the same listener, and the same handshake on both. The NEAR way is the phone's own loopback, carried
 * by `adb reverse` (the cable, or Wireless debugging). The FAR way exists only when the operator switched the Mac's
 * tailnet door on: its address arrives in a signed `/health` reply and is kept in `REMOTE_FILE`. A call goes near
 * first; when nothing there proves itself the link, it goes far. What the door is, and why its address needs no
 * trust of its own: owned by plugins/phone-link/README.md § The tailnet door — see the note there.
 *
 * `base_url` is phone-link's; a test points it at a fake link on another loopback port. `remote` is the far way's
 * address, or null: a test hands it another fake link.
 */
class LayaClient(
    private val files_dir: File,
    private val base_url: HttpUrl = BASE_URL,
    private val remote: (() -> HttpUrl?)? = null,
) : ReviewCalls, LedgerSource {

    /** The "linked?" probe. Its signed reply is also where the far way's address comes from, and where it is taken away. */
    suspend fun health(): Call<Unit> = send(url("health"), null) { status, text ->
        val got = read_ack("health", status, text)
        if (got is Call.Ok) keep_remote(text)
        got
    }

    /** One page of RECENT; `after` is the page before's `summary.next`, for the older page that follows it. */
    suspend fun recent(todo: Boolean, after: String? = null): Call<RecentPage> {
        val url = url("plugin/laya/review/recent") {
            addQueryParameter("limit", LayaRules.RECENT_LIMIT.toString())
            // A default is never sent: a bridge from before TO GRADE, or before the cursor, refuses a name it does not know.
            if (todo) addQueryParameter("todo", "1")
            if (after != null) addQueryParameter("after", after)
        }
        return send(url, null) { status, text -> read("review/recent", status, text, RecentPage.serializer()) }
    }

    /**
     * The page's join: which conversation each verdict belongs to. One call per `LayaThreads.threads_queries` chunk,
     * merged; any failed chunk fails the whole, because half a join would group half the page by thread and half by
     * rack name. The ids' shapes are URL-safe, so they go on the wire as they are (`addEncodedQueryParameter`): a
     * full page then fits phone-link's one target.
     */
    suspend fun threads(ask: ThreadsAsk): Call<HistoryThreads> {
        var all = HistoryThreads()
        for (query in LayaThreads.threads_queries(ask)) {
            val url = url("ledger/threads") { for ((name, ids) in query) addEncodedQueryParameter(name, ids) }
            val got = send(url, null) { status, text -> read("ledger/threads", status, text, HistoryThreads.serializer()) }
            when (got) {
                is Call.Ok -> all = LayaThreads.merge_threads(all, got.data)
                is Fail -> return got
            }
        }
        return Call.Ok(all)
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
     *
     * Near first. When the near way's nonce step proved nothing (nobody there, or somebody who is not the link), the
     * far way is tried, if there is one. Whatever sits on the phone's loopback therefore cannot keep the app from the
     * tailnet door, and it has seen only a random challenge. A far way that does not answer either leaves the near
     * way's verdict standing: it names the fix the operator can reach from the phone.
     */
    private suspend fun <T> send(url: HttpUrl, json_body: String?, decode: (Int, String) -> Call<T>): Call<T> =
        withContext(Dispatchers.IO) {
            val token = read_token() ?: return@withContext Fail.Unlinked
            val near = attempt(Way(base_url, LINK_HTTP, far = false), url, token, json_body, decode)
            if (near.proven) return@withContext near.result
            val far = far_url() ?: return@withContext near.result
            val away = attempt(Way(far, REMOTE_HTTP, far = true), rebase(url, far), token, json_body, decode)
            if (away.result is Fail.Unlinked) near.result else away.result
        }

    /** One way to the link: where it is, the client that goes there, and whether it leaves the phone. */
    private class Way(val base: HttpUrl, val http: OkHttpClient, val far: Boolean)

    /** A call's outcome on one way. `proven`: its nonce step was answered by the link itself, with a signed nonce. */
    private class Tried<out T>(val result: Call<T>, val proven: Boolean)

    private fun <T> attempt(way: Way, url: HttpUrl, token: String, json_body: String?, decode: (Int, String) -> Call<T>): Tried<T> {
        val nonce = when (val issued = issue(way, token)) {
            is Call.Ok -> issued.data
            // No signed nonce: nothing here proved itself the link. An unsigned refusal reads as the link's (`unproven`),
            // but anything on the port can send those bytes, so it does not count either.
            is Fail -> return Tried(issued, proven = false)
        }
        // Hashed and sent as these exact bytes: a String body would let OkHttp append a charset to the type.
        val body = json_body?.toByteArray(Charsets.UTF_8)
        val method = if (body != null) "POST" else "GET"
        val mac = PhoneLinkProof.request_mac(token, nonce, method, PhoneLinkProof.target_of(url), body ?: NO_BYTES)
        val request = link_request(url).header("Authorization", PhoneLinkProof.authorization(nonce, mac))
        if (body != null) request.post(body.toRequestBody(JSON_MEDIA)) else request.get()
        val reply = when (val got = exchange(way, request.build(), MAX_REPLY_BYTES)) {
            is Call.Ok -> got.data
            is Fail -> return Tried(got, proven = true)
        }
        if (!PhoneLinkProof.reply_signed(token, nonce, reply.status, reply.body, reply.mac)) return Tried(unproven(reply), proven = true)
        val text = reply.body.toString(Charsets.UTF_8)
        return Tried(
            when {
                reply.status == 401 -> Fail.Unlinked
                reply.status == 502 && envelope_of(parse(text))?.error == BRIDGE_DOWN -> Fail.BridgeDown
                else -> decode(reply.status, text)
            },
            proven = true,
        )
    }

    /** The same path and query on the far way: the handshake signs the target, never the host, so it is one request. */
    private fun rebase(url: HttpUrl, base: HttpUrl): HttpUrl =
        url.newBuilder().scheme(base.scheme).host(base.host).port(base.port).build()

    private fun far_url(): HttpUrl? = if (remote != null) remote.invoke() else read_remote()

    /**
     * The far way's address as the Mac last said it, or null. Only ever an `https` address of one machine's
     * `*.ts.net` name with a port and nothing else: the file is this app's own, but a value of another shape is
     * never dialled. A hostile address could at worst be shown a random challenge, which it cannot sign.
     */
    private fun read_remote(): HttpUrl? {
        val raw = try {
            File(files_dir, REMOTE_FILE).readText().trim()
        } catch (error: IOException) {
            return null
        }
        return remote_url(raw)
    }

    /** A signed health reply names the door (`remote`) or, by leaving it out, says there is none: keep exactly that. */
    private fun keep_remote(text: String) {
        val said = (parse(text) as? JsonObject)?.get("remote")?.let { (it as? JsonPrimitive)?.takeIf { p -> p.isString }?.content }
        val file = File(files_dir, REMOTE_FILE)
        try {
            val url = said?.let(::remote_url)
            if (url == null) file.delete() else if (!file.exists() || file.readText() != url.toString()) file.writeText(url.toString())
        } catch (error: IOException) {
            // The address is a convenience: the near way does not need it, and the next health reply writes it again.
        }
    }

    /** Step 1: a nonce, believed only once its reply is signed over this call's own challenge. Nothing token-derived goes out before. */
    private fun issue(way: Way, token: String): Call<String> {
        val challenge = PhoneLinkProof.challenge()
        val url = way.base.newBuilder().addPathSegment("nonce").addQueryParameter("c", challenge).build()
        val reply = when (val got = exchange(way, link_request(url).get().build(), NONCE_REPLY_BYTES)) {
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
    private fun exchange(way: Way, request: Request, max_bytes: Long): Call<LinkReply> {
        return try {
            way.http.newCall(request).execute().use { response ->
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
            // Near, the tunnel took the connection and the Mac end dropped it. Far, any failure (no such name with
            // Tailscale off, a certificate, a timeout) says only that the door was not reached.
            if (way.far) Fail.Unlinked else Fail.BridgeDown
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

        /** Under `Context.filesDir`; this app writes it, from a signed health reply. Absent: there is no far way. */
        const val REMOTE_FILE = "phone-link-remote"

        private val REMOTE_HOST = Regex("[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\\.ts\\.net")

        /** `https://<machine>.<tailnet>.ts.net:<port>/` and nothing more, or null (`phone_link/config.py` `TailnetDoor.url`). */
        internal fun remote_url(text: String): HttpUrl? {
            val url = text.toHttpUrlOrNull() ?: return null
            val plain = url.username.isEmpty() && url.password.isEmpty() && url.encodedPath == "/" && url.query == null && url.fragment == null
            return url.takeIf { it.isHttps && plain && REMOTE_HOST.matches(it.host) && text == "https://${it.host}:${it.port}/" }
        }

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

        /**
         * The far way's client: the link's own settings, with the time a tailnet needs (a relayed first packet, a TLS
         * handshake) and HTTP/1.1 only, because the handshake is one request per connection.
         */
        internal val REMOTE_HTTP: OkHttpClient by lazy {
            LINK_HTTP.newBuilder()
                .protocols(listOf(Protocol.HTTP_1_1))
                .connectTimeout(8, TimeUnit.SECONDS)
                .callTimeout(20, TimeUnit.SECONDS)
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
            403 to PreProof("phone-link refused this request's headers", UPDATE_APP, "host_refused", "proxy_refused", "browser_refused", "tailnet_refused"),
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
