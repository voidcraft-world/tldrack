package world.voidcraft.tldrack.laya

import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import okhttp3.HttpUrl

// phone-link handshake v2, the phone's spelling (pure: no Android): owned by plugins/phone-link/README.md
// § The handshake — see the note there. The Python twin is plugins/phone-link/phone_link/proof.py.

object PhoneLinkProof {
    const val VERSION = 2
    const val SCHEME = "PhoneLink"

    /** The header that carries the link's signature over each reply. */
    const val REPLY_HEADER = "Phone-Link-Mac"

    /** `secrets.token_urlsafe(32)`, as the link mints its nonces. */
    val NONCE_SHAPE = Regex("[A-Za-z0-9_-]{43}")

    /** What the link accepts as `c`. */
    val CHALLENGE_SHAPE = Regex("[A-Za-z0-9_-]{22,128}")

    private const val CHALLENGE_BYTES = 32
    private const val HMAC = "HmacSHA256"
    private const val HEX = "0123456789abcdef"
    private val random = SecureRandom()
    private val base64url = Base64.getUrlEncoder().withoutPadding()

    /** A fresh `c`: 32 random bytes, URL-safe base64 without padding (43 characters). */
    fun challenge(): String = ByteArray(CHALLENGE_BYTES).also(random::nextBytes).let(base64url::encodeToString)

    /** The request target exactly as OkHttp writes it on the request line (`RequestLine.requestPath`): no re-encoding. */
    fun target_of(url: HttpUrl): String {
        val query = url.encodedQuery
        return if (query != null) "${url.encodedPath}?$query" else url.encodedPath
    }

    fun request_message(nonce: String, method: String, target: String, body: ByteArray): ByteArray =
        message("request", nonce, method, target, sha256_hex(body))

    /** A `/nonce` reply, over this call's challenge. Its own domain: why, in the README. */
    fun nonce_message(challenge: String, status: Int, body: ByteArray): ByteArray =
        message("nonce", challenge, status.toString(), sha256_hex(body))

    /** Every reply to a proven request, over that request's nonce. */
    fun reply_message(nonce: String, status: Int, body: ByteArray): ByteArray =
        message("reply", nonce, status.toString(), sha256_hex(body))

    fun request_mac(token: String, nonce: String, method: String, target: String, body: ByteArray): String =
        hmac_hex(token, request_message(nonce, method, target, body))

    fun nonce_mac(token: String, challenge: String, status: Int, body: ByteArray): String =
        hmac_hex(token, nonce_message(challenge, status, body))

    fun reply_mac(token: String, nonce: String, status: Int, body: ByteArray): String =
        hmac_hex(token, reply_message(nonce, status, body))

    fun authorization(nonce: String, mac: String): String = "$SCHEME n=$nonce, mac=$mac"

    /**
     * Constant time over the ASCII bytes, so the one spelling (64 lowercase hex) is the only one that passes: an
     * uppercase or shorter MAC is refused, as the link refuses one. A non-ASCII character encodes to `?`, never hex.
     */
    fun verify(expected: String, presented: String?): Boolean =
        presented != null && MessageDigest.isEqual(expected.toByteArray(Charsets.US_ASCII), presented.toByteArray(Charsets.US_ASCII))

    /** The link's `Phone-Link-Mac` over a `/nonce` reply, checked: false when absent. */
    fun nonce_signed(token: String, challenge: String, status: Int, body: ByteArray, presented: String?): Boolean =
        verify(nonce_mac(token, challenge, status, body), presented)

    /** The link's `Phone-Link-Mac` over a reply to a proven request, checked: false when absent. */
    fun reply_signed(token: String, nonce: String, status: Int, body: ByteArray, presented: String?): Boolean =
        verify(reply_mac(token, nonce, status, body), presented)

    fun sha256_hex(bytes: ByteArray): String = hex(MessageDigest.getInstance("SHA-256").digest(bytes))

    private fun hmac_hex(token: String, message: ByteArray): String {
        val mac = Mac.getInstance(HMAC)
        mac.init(SecretKeySpec(token.toByteArray(Charsets.US_ASCII), HMAC))
        return hex(mac.doFinal(message))
    }

    // UTF-8 is what OkHttp writes on the request line; every part the link sends or mints is ASCII.
    private fun message(kind: String, vararg parts: String): ByteArray =
        (listOf("phone-link/$VERSION $kind") + parts).joinToString("\n").toByteArray(Charsets.UTF_8)

    private fun hex(bytes: ByteArray): String {
        val out = StringBuilder(bytes.size * 2)
        for (b in bytes) {
            val v = b.toInt() and 0xff
            out.append(HEX[v ushr 4]).append(HEX[v and 0x0f])
        }
        return out.toString()
    }
}
