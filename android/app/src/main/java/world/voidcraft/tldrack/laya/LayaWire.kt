package world.voidcraft.tldrack.laya

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import world.voidcraft.tldrack.APP_JSON

// Wire shapes: owned by schemas/src/laya-review.ts (the RECENT list, answer, undo), schemas/src/agent-history.ts
// (ledger search + conversation) and schemas/src/topic-labels.ts — see the notes there.
// Hand-written, and only the fields this app reads: APP_JSON's `ignoreUnknownKeys` drops the rest, the same
// lenience the schemas' default strip gives a newer bridge. The enum NAMES are the wire values.

/** The three marks a turn end can draw on the rack: ✅ done · 💬 reply (unsure) · ⚠️ open. */
@Serializable
enum class Band { done, reply, open }

/** An answer's second half. Beside OPEN: the turn waited on the operator (`you`), or only on its own work (`running`).
 *  Beside DONE: `read`, the agent is done but its answer is the operator's to read (schemas/src/laya-review.ts
 *  `REVIEW_NEEDS` owns why). A name missing here fails the whole page's decode, so this list follows the wire's. */
@Serializable
enum class Needs { you, running, read }

@Serializable
enum class ReviewAnswer { agree, flip, skip }

@Serializable
enum class Cut { open, done }

/**
 * A live operator answer. `open` is null on a skip; `needs` is absent from a bridge older than the three-way grade.
 * `key` is the text's key on every route (`review_log.answer_ref`). It is nullable only for a bridge older than that
 * fix, and nothing here reads it, because the row's own `key` is the same value.
 */
@Serializable
data class AnswerRef(
    val answer_id: String,
    val key: String? = null,
    val answer: ReviewAnswer,
    val open: Boolean? = null,
    val needs: Needs? = null,
)

/** The cut p sits nearest, and p minus it. */
@Serializable
data class Edge(val cut: Cut, val delta: Double)

/** One live verdict. `mark` is what the rack drew; `band` is where p falls at today's ✅ cut, and what RIGHT/WRONG grade against. */
@Serializable
data class RecentRow(
    val key: String,
    val decision_id: String,
    val at: String,
    /** The rack session (its tmux name) the verdict was recorded under. */
    val subject: String? = null,
    val p: Double,
    val mark: Band,
    val band: Band,
    val edge: Edge? = null,
    val text: String = "",
    /** Null: no current card holds the text, so it cannot be answered. */
    val reference_open: Boolean? = null,
    val operator: AnswerRef? = null,
)

/** GET /plugin/laya/review/recent. */
@Serializable
data class RecentPage(
    val surface: String? = null,
    val rows: List<RecentRow>,
)

/** Every refusal's readable half: the laya envelope, the bridge's `{error}` and phone-link's `{ok:false, error}` alike. */
@Serializable
data class Envelope(
    val ok: Boolean? = null,
    val message: String? = null,
    val code: String? = null,
    val error: String? = null,
    val hint: String? = null,
)

@Serializable
data class ReviewReference(val open: Boolean)

/** Only the card's reference: `reference_changed` hands back a fresh card, and what AGREE now agrees with is all the list needs. */
@Serializable
data class ReviewCard(val reference: ReviewReference)

/** POST /review/answer and /review/undo, and their 409 bodies: `already_answered` carries `answer`, `reference_changed` a `card`. */
@Serializable
data class ReviewResult(
    val message: String = "",
    val answer: AnswerRef? = null,
    val card: ReviewCard? = null,
)

/** Strict on the server: exactly these fields, and `needs` only on an answer that records OPEN. */
@Serializable
data class AnswerRequest(
    val surface: String,
    val key: String,
    val answer: ReviewAnswer,
    val reference_open: Boolean,
    val needs: Needs? = null,
)

@Serializable
data class UndoRequest(val surface: String, val answer_id: String)

@Serializable
data class HistoryHit(
    val file_id: Long,
    val seq: Int,
    val ts: Double? = null,
    val role: String,
)

@Serializable
data class HistorySearch(
    val hits: List<HistoryHit>,
    val next_offset: Int? = null,
)

@Serializable
data class HistoryMessage(
    val seq: Int,
    val role: String,
    val text: String,
)

@Serializable
data class HistorySession(
    val title: String? = null,
    val cwd: String? = null,
)

/** `first_user` is three-state on the wire (absent on an older bridge, null, or the message): `ledger_page` reads the key's presence. */
@Serializable
data class HistoryConversation(
    val session: HistorySession,
    val messages: List<HistoryMessage>,
    val first_user: HistoryMessage? = null,
    val before: Int? = null,
)

/**
 * One `/ledger/conversation` body as the wire said it. Only the key's presence tells an older bridge (no `first_user`
 * at all: Unknown) from a session whose start holds no request (`null`: None). Throws kotlinx's
 * `SerializationException` (an `IllegalArgumentException`) on a body of another shape.
 */
fun ledger_page(raw: JsonObject): LedgerPage {
    val page = APP_JSON.decodeFromJsonElement(HistoryConversation.serializer(), raw)
    val said = page.first_user
    val first_user = when {
        "first_user" !in raw -> FirstUser.Unknown
        said == null -> FirstUser.None
        else -> FirstUser.Found(said)
    }
    return LedgerPage(page.session.title, page.session.cwd, page.messages, first_user, page.before)
}

@Serializable
data class TopicNamed(
    /** The topic label that call settled on: what `shown` reads while nothing has replaced it. */
    val name: String? = null,
    /** Epoch seconds of the topic call that named the session now wearing this tmux name. */
    val at: Double,
)

/** GET /topic-labels: `shown` is what the rack draws for the session RIGHT NOW. */
@Serializable
data class TopicView(
    val named: TopicNamed? = null,
    val shown: String? = null,
)
