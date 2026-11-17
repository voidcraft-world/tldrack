package world.voidcraft.tldrack.laya

import java.util.Base64
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.internal.http.RequestLine
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * phone-link handshake v2, the phone's spelling. Every number of the README's test vector
 * (plugins/phone-link/README.md § The handshake), which plugins/phone-link/tests/test_proof.py checks too.
 */
class PhoneLinkProofTest {

    private val token = "TEST_VECTOR_TOKEN_do_not_use_0123456789abcd"
    private val challenge = "TEST_VECTOR_CHALLENGE_0123456789abcdefghij"
    private val nonce = "TEST_VECTOR_NONCE_0123456789abcdefghijklmno"
    private val nonce_body = """{"ok":true,"v":2,"nonce":"TEST_VECTOR_NONCE_0123456789abcdefghijklmno","ttl_s":30}""".toByteArray()
    private val answer_body = """{"id":"r1","verdict":"open"}""".toByteArray()
    private val ok_body = """{"ok":true}""".toByteArray()

    private val link = "http://127.0.0.1:7683/".toHttpUrl()

    // ── the vector ───────────────────────────────────────────────────────────

    @Test
    fun `the vector's inputs are the shapes the link mints and accepts`() {
        assertEquals(43, token.length)
        assertTrue(PhoneLinkProof.NONCE_SHAPE.matches(nonce))
        assertTrue(PhoneLinkProof.CHALLENGE_SHAPE.matches(challenge))
    }

    @Test
    fun `the nonce reply is signed over the challenge`() {
        assertEquals(
            "d08023bee422685b7cb6d7002e662d159cca9fdec5d9d7e8936a849a20800675",
            PhoneLinkProof.nonce_mac(token, challenge, 200, nonce_body),
        )
    }

    @Test
    fun `a nonce reply's signature is never a proven reply over the same value`() {
        // `c` is the caller's choice, nonce-shaped if it likes: the README's signing-oracle note.
        val body = """{"ok":true}""".toByteArray()
        val issued = PhoneLinkProof.nonce_mac(token, nonce, 200, body)
        assertFalse(PhoneLinkProof.reply_signed(token, nonce, 200, body, issued))
        assertTrue(PhoneLinkProof.nonce_signed(token, nonce, 200, body, issued))
    }

    @Test
    fun `the POST answer request`() {
        val target = PhoneLinkProof.target_of(link.newBuilder().addPathSegments("plugin/laya/review/answer").build())
        assertEquals("/plugin/laya/review/answer", target)
        assertEquals("d008ed8c71bdb40455d43d00250edfc100aa391c6760ca4f7fa223bc0fdefefe", PhoneLinkProof.sha256_hex(answer_body))
        assertEquals(
            "phone-link/2 request\nTEST_VECTOR_NONCE_0123456789abcdefghijklmno\nPOST\n/plugin/laya/review/answer\n" +
                "d008ed8c71bdb40455d43d00250edfc100aa391c6760ca4f7fa223bc0fdefefe",
            String(PhoneLinkProof.request_message(nonce, "POST", target, answer_body), Charsets.UTF_8),
        )
        val mac = PhoneLinkProof.request_mac(token, nonce, "POST", target, answer_body)
        assertEquals("893bac9380754babbf40a9bfcfcc0baea347a15bb194ba7c9ed13e779a543d0d", mac)
        assertEquals(
            "PhoneLink n=TEST_VECTOR_NONCE_0123456789abcdefghijklmno, mac=893bac9380754babbf40a9bfcfcc0baea347a15bb194ba7c9ed13e779a543d0d",
            PhoneLinkProof.authorization(nonce, mac),
        )
    }

    @Test
    fun `the GET search request, its target built from an HttpUrl`() {
        val target = PhoneLinkProof.target_of("http://127.0.0.1:7683/ledger/search?q=open+work".toHttpUrl())
        assertEquals("/ledger/search?q=open+work", target)
        assertEquals(
            "12157b675a0412d9e46bade13cdfd47ba30ecdd51ba034127c94fe3e6d7fb9bc",
            PhoneLinkProof.request_mac(token, nonce, "GET", target, ByteArray(0)),
        )
    }

    @Test
    fun `the reply to that nonce`() {
        assertEquals(
            "e3c4bb9073d51afafa3dec53caf4a76f92e2fffde59f2008ccd6fd0a99b67ea6",
            PhoneLinkProof.reply_mac(token, nonce, 200, ok_body),
        )
        assertTrue(PhoneLinkProof.reply_signed(token, nonce, 200, ok_body, "e3c4bb9073d51afafa3dec53caf4a76f92e2fffde59f2008ccd6fd0a99b67ea6"))
    }

    @Test
    fun `a GET signs the digest of no bytes`() {
        assertEquals("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", PhoneLinkProof.sha256_hex(ByteArray(0)))
    }

    // ── what each signature binds ────────────────────────────────────────────

    @Test
    fun `a request and a reply never sign the same string`() {
        val request = String(PhoneLinkProof.request_message(nonce, "GET", "/health", ByteArray(0)), Charsets.UTF_8)
        val reply = String(PhoneLinkProof.reply_message(nonce, 200, ByteArray(0)), Charsets.UTF_8)
        val issued = String(PhoneLinkProof.nonce_message(nonce, 200, ByteArray(0)), Charsets.UTF_8)
        assertEquals("phone-link/2 request", request.substringBefore('\n'))
        assertEquals("phone-link/2 reply", reply.substringBefore('\n'))
        assertEquals("phone-link/2 nonce", issued.substringBefore('\n'))
        assertFalse(request.endsWith("\n"))
        assertFalse(reply.endsWith("\n"))
    }

    @Test
    fun `a reply signed for another binding, status, body or key does not verify`() {
        val mac = "e3c4bb9073d51afafa3dec53caf4a76f92e2fffde59f2008ccd6fd0a99b67ea6"
        assertFalse(PhoneLinkProof.reply_signed(token, challenge, 200, ok_body, mac))
        assertFalse(PhoneLinkProof.reply_signed(token, nonce, 201, ok_body, mac))
        assertFalse(PhoneLinkProof.reply_signed(token, nonce, 200, """{"ok":true} """.toByteArray(), mac))
        assertFalse(PhoneLinkProof.reply_signed(token.replace('d', 'e'), nonce, 200, ok_body, mac))
        assertFalse(PhoneLinkProof.reply_signed(token, nonce, 200, ok_body, null))
    }

    @Test
    fun `every signed request field changes the mac`() {
        val body = "{}".toByteArray()
        val seen = setOf(
            PhoneLinkProof.request_mac(token, nonce, "POST", "/ledger/ingest", body),
            PhoneLinkProof.request_mac(token, nonce.replace('T', 'U'), "POST", "/ledger/ingest", body),
            PhoneLinkProof.request_mac(token, nonce, "GET", "/ledger/ingest", body),
            PhoneLinkProof.request_mac(token, nonce, "POST", "/ledger/ingest?x=1", body),
            PhoneLinkProof.request_mac(token, nonce, "POST", "/ledger/ingest", "{} ".toByteArray()),
        )
        assertEquals(5, seen.size)
    }

    // ── the challenge ────────────────────────────────────────────────────────

    @Test
    fun `a challenge is 32 random bytes in url-safe base64 without padding`() {
        val made = List(200) { PhoneLinkProof.challenge() }
        for (c in made) {
            assertEquals(43, c.length)
            assertTrue(c, Regex("[A-Za-z0-9_-]{43}").matches(c))
            assertFalse(c.contains('='))
            assertTrue(PhoneLinkProof.CHALLENGE_SHAPE.matches(c))
            assertEquals(32, Base64.getUrlDecoder().decode(c).size)
        }
        assertEquals(made.size, made.toSet().size)
    }

    // ── constant-time verify ─────────────────────────────────────────────────

    private val mac = "e3c4bb9073d51afafa3dec53caf4a76f92e2fffde59f2008ccd6fd0a99b67ea6"

    @Test
    fun `verify takes the one spelling`() {
        assertTrue(PhoneLinkProof.verify(mac, mac))
        assertTrue(PhoneLinkProof.verify(mac, String(mac.toCharArray())))
    }

    @Test
    fun `verify refuses a missing, empty, short or long mac`() {
        assertFalse(PhoneLinkProof.verify(mac, null))
        assertFalse(PhoneLinkProof.verify(mac, ""))
        assertFalse(PhoneLinkProof.verify(mac, mac.dropLast(1)))
        assertFalse(PhoneLinkProof.verify(mac, mac + "0"))
        assertFalse(PhoneLinkProof.verify(mac, " $mac"))
        assertFalse(PhoneLinkProof.verify(mac, "$mac\n"))
    }

    @Test
    fun `verify refuses uppercase hex, which decodes to the same bytes`() {
        assertFalse(PhoneLinkProof.verify(mac, mac.uppercase()))
        val one_upper = mac.indexOfFirst { it in 'a'..'f' }
        assertFalse(PhoneLinkProof.verify(mac, mac.substring(0, one_upper) + mac[one_upper].uppercaseChar() + mac.substring(one_upper + 1)))
    }

    @Test
    fun `verify refuses every one-bit flip of every character`() {
        for (i in mac.indices) {
            for (bit in 0 until 16) {
                val flipped = (mac[i].code xor (1 shl bit)).toChar()
                val presented = mac.substring(0, i) + flipped + mac.substring(i + 1)
                assertFalse("char $i bit $bit", PhoneLinkProof.verify(mac, presented))
            }
        }
    }

    @Test
    fun `verify refuses every one-bit flip of the mac's bytes`() {
        val bytes = mac.chunked(2).map { it.toInt(16).toByte() }.toByteArray()
        for (i in bytes.indices) {
            for (bit in 0 until 8) {
                val flipped = bytes.copyOf().also { it[i] = (it[i].toInt() xor (1 shl bit)).toByte() }
                val presented = flipped.joinToString("") { "%02x".format(it.toInt() and 0xff) }
                assertFalse("byte $i bit $bit", PhoneLinkProof.verify(mac, presented))
            }
        }
    }

    // ── the target, exactly as the request line carries it ───────────────────

    private fun assert_target(expected: String, url: HttpUrl) {
        assertEquals(expected, PhoneLinkProof.target_of(url))
        // What OkHttp itself writes on the request line: the signed target must be that string, not a re-spelling.
        assertEquals(RequestLine.requestPath(url), PhoneLinkProof.target_of(url))
    }

    @Test
    fun `a target without a query is the encoded path`() {
        assert_target("/health", link.newBuilder().addPathSegments("health").build())
        assert_target("/plugin/laya/review/recent", link.newBuilder().addPathSegments("plugin/laya/review/recent").build())
        assert_target("/", link)
    }

    @Test
    fun `a target with a query keeps it as encoded`() {
        assert_target(
            "/plugin/laya/review/recent?limit=80&todo=1",
            link.newBuilder().addPathSegments("plugin/laya/review/recent").addQueryParameter("limit", "80")
                .addQueryParameter("todo", "1").build(),
        )
        // An empty query is still a query: OkHttp sends the `?`.
        assert_target("/health?", "http://127.0.0.1:7683/health?".toHttpUrl())
    }

    @Test
    fun `percent-encoding is kept exactly, never re-spelled`() {
        // A space built by the app is %20; one parsed as + stays +.
        assert_target("/ledger/search?q=open%20work", link.newBuilder().addPathSegments("ledger/search").addQueryParameter("q", "open work").build())
        assert_target("/ledger/search?q=open+work", "http://127.0.0.1:7683/ledger/search?q=open+work".toHttpUrl())
        // A literal + and & in a value, and UTF-8 text, as the builder encodes them.
        assert_target(
            "/ledger/search?q=a%2Bb%26c%3Dd%20caf%C3%A9%20%E2%9C%93",
            link.newBuilder().addPathSegments("ledger/search").addQueryParameter("q", "a+b&c=d café ✓").build(),
        )
        // Escapes already in a parsed URL keep their case and their bytes, in the path and the query.
        assert_target("/a%2Fb/c%2f?x=%e2%9c%93&y=%2B1", "http://127.0.0.1:7683/a%2Fb/c%2f?x=%e2%9c%93&y=%2B1".toHttpUrl())
        assert_target("/topic-labels?session=vc-12", "http://127.0.0.1:7683/topic-labels?session=vc-12".toHttpUrl())
    }
}
