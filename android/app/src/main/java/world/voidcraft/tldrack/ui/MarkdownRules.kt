package world.voidcraft.tldrack.ui

/**
 * The Markdown an agent's message uses, parsed to blocks. Pure, so the JVM tests cover it; `MarkdownText` draws it.
 *
 * It is the subset agents write: headings, lists, fenced code, tables, quotes, rules, and inline bold, italic, code and
 * links. Anything else stays as the text it was, so a message is never lost to a parse.
 */
data class MdSpan(
    val text: String,
    val bold: Boolean = false,
    val italic: Boolean = false,
    val code: Boolean = false,
    val link: Boolean = false,
)

sealed interface MdBlock {
    data class Paragraph(val spans: List<MdSpan>) : MdBlock
    data class Heading(val level: Int, val spans: List<MdSpan>) : MdBlock
    /** `marker` is what is drawn: a bullet, or the number as written. `depth` counts indent steps. */
    data class Item(val marker: String, val depth: Int, val spans: List<MdSpan>) : MdBlock
    data class Code(val text: String) : MdBlock
    data class Quote(val spans: List<MdSpan>) : MdBlock
    /** Every row has `head.size` cells: a short row is padded, a long one cut. */
    data class Table(val head: List<List<MdSpan>>, val rows: List<List<List<MdSpan>>>) : MdBlock
    data object Rule : MdBlock
}

object MarkdownRules {

    const val BULLET = "•"

    private val HEADING = Regex("""^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$""")
    private val ITEM = Regex("""^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$""")
    private val RULE = Regex("""^ {0,3}([-*_])(\s*\1){2,}\s*$""")
    private val QUOTE = Regex("""^ {0,3}>\s?(.*)$""")
    private val FENCE = Regex("""^\s*(```+|~~~+)(.*)$""")
    private val DIVIDER_CELL = Regex("""^:?-+:?$""")
    private val LINK = Regex("""\[([^\]\n]+)]\(([^)\s]+)\)""")
    private const val ESCAPABLE = "\\`*_[]()#+-.!|>~"

    fun parse(text: String): List<MdBlock> {
        val lines = text.replace("\r\n", "\n").split('\n')
        val out = mutableListOf<MdBlock>()
        val open = mutableListOf<String>()
        // What `open` is building: a paragraph, a quote, or the list item whose marker is kept here.
        var item: Pair<String, Int>? = null
        var quote = false

        fun flush() {
            if (open.isEmpty()) return
            val spans = inline(open.joinToString("\n"))
            val held = item
            out += when {
                held != null -> MdBlock.Item(held.first, held.second, spans)
                quote -> MdBlock.Quote(spans)
                else -> MdBlock.Paragraph(spans)
            }
            open.clear()
            item = null
            quote = false
        }

        var i = 0
        while (i < lines.size) {
            val line = lines[i]
            val fence = FENCE.matchEntire(line)
            if (fence != null) {
                flush()
                val mark = fence.groupValues[1]
                val body = mutableListOf<String>()
                i += 1
                while (i < lines.size && !closes_fence(lines[i], mark)) {
                    body += lines[i]
                    i += 1
                }
                out += MdBlock.Code(body.joinToString("\n"))
                i += 1
                continue
            }
            if (line.isBlank()) {
                flush()
                i += 1
                continue
            }
            if (is_table_start(lines, i)) {
                flush()
                val head = cells(line)
                val rows = mutableListOf<List<String>>()
                i += 2
                while (i < lines.size && lines[i].isNotBlank() && '|' in lines[i]) {
                    rows += cells(lines[i])
                    i += 1
                }
                out += MdBlock.Table(
                    head.map(::inline),
                    rows.map { row -> List(head.size) { column -> inline(row.getOrElse(column) { "" }) } },
                )
                continue
            }
            val heading = HEADING.matchEntire(line)
            val entry = ITEM.matchEntire(line)
            val quoted = QUOTE.matchEntire(line)
            when {
                heading != null -> {
                    flush()
                    out += MdBlock.Heading(heading.groupValues[1].length, inline(heading.groupValues[2]))
                }
                RULE.matches(line) -> {
                    flush()
                    out += MdBlock.Rule
                }
                entry != null -> {
                    flush()
                    val mark = entry.groupValues[2]
                    item = (if (mark.length == 1 && mark[0] in "-*+") BULLET else mark) to entry.groupValues[1].length / 2
                    open += entry.groupValues[3]
                }
                quoted != null -> {
                    if (!quote) flush()
                    quote = true
                    open += quoted.groupValues[1]
                }
                else -> {
                    if (quote) flush()
                    open += if (item != null) line.trim() else line
                }
            }
            i += 1
        }
        flush()
        return out
    }

    /** A message as plain lines, for a preview that cannot draw blocks: marks dropped, a table row on one line. */
    fun plain(text: String): String = parse(text).joinToString("\n") { block ->
        when (block) {
            is MdBlock.Paragraph -> words(block.spans)
            is MdBlock.Heading -> words(block.spans)
            is MdBlock.Item -> "${block.marker} ${words(block.spans)}"
            is MdBlock.Quote -> words(block.spans)
            is MdBlock.Code -> block.text
            is MdBlock.Table -> (listOf(block.head) + block.rows).joinToString("\n") { row -> row.joinToString(" · ", transform = ::words) }
            MdBlock.Rule -> ""
        }
    }.trim()

    fun words(spans: List<MdSpan>): String = spans.joinToString("") { it.text }

    private fun closes_fence(line: String, mark: String): Boolean {
        val trimmed = line.trim()
        return trimmed.length >= mark.length && trimmed.all { it == mark[0] }
    }

    /** A header row with a pipe, then a divider row (`|---|:-:|`) with as many cells. */
    private fun is_table_start(lines: List<String>, at: Int): Boolean {
        if ('|' !in lines[at] || at + 1 >= lines.size) return false
        val divider = lines[at + 1]
        if ('|' !in divider && cells(lines[at]).size != 1) return false
        val marks = cells(divider)
        return marks.isNotEmpty() && marks.all { DIVIDER_CELL.matches(it) } && marks.size == cells(lines[at]).size
    }

    /** One table row's cells, trimmed. A pipe inside a code span or behind a backslash is text. */
    fun cells(line: String): List<String> {
        val out = mutableListOf<String>()
        val cell = StringBuilder()
        var in_code = false
        var i = 0
        while (i < line.length) {
            val c = line[i]
            when {
                c == '\\' && i + 1 < line.length && line[i + 1] == '|' -> {
                    cell.append('|')
                    i += 1
                }
                c == '`' -> {
                    in_code = !in_code
                    cell.append(c)
                }
                c == '|' && !in_code -> {
                    out += cell.toString()
                    cell.clear()
                }
                else -> cell.append(c)
            }
            i += 1
        }
        out += cell.toString()
        val trimmed = out.map { it.trim() }
        val from = if (trimmed.first().isEmpty()) 1 else 0
        val to = if (trimmed.size > from && trimmed.last().isEmpty()) trimmed.size - 1 else trimmed.size
        return trimmed.subList(from, to)
    }

    fun inline(text: String): List<MdSpan> {
        val out = mutableListOf<MdSpan>()
        inline_into(text, bold = false, italic = false, out)
        return merge(out)
    }

    private fun inline_into(text: String, bold: Boolean, italic: Boolean, out: MutableList<MdSpan>) {
        val plain = StringBuilder()
        fun flush() {
            if (plain.isNotEmpty()) out += MdSpan(plain.toString(), bold, italic)
            plain.clear()
        }
        var i = 0
        while (i < text.length) {
            val c = text[i]
            if (c == '\\' && i + 1 < text.length && text[i + 1] in ESCAPABLE) {
                plain.append(text[i + 1])
                i += 2
                continue
            }
            if (c == '`') {
                val end = code_end(text, i)
                if (end != null) {
                    val ticks = ticks_at(text, i)
                    flush()
                    out += MdSpan(text.substring(i + ticks, end - ticks).trim(' '), bold, italic, code = true)
                    i = end
                    continue
                }
            }
            if (c == '[') {
                val link = LINK.matchAt(text, i)
                if (link != null) {
                    flush()
                    out += MdSpan(link.groupValues[1], bold, italic, link = true)
                    i = link.range.last + 1
                    continue
                }
            }
            if (c == '*' || c == '_') {
                val strong = i + 1 < text.length && text[i + 1] == c
                val width = if (strong) 2 else 1
                val close = if (opens(text, i, width)) closer(text, i + width, c, width) else null
                if (close != null) {
                    flush()
                    inline_into(text.substring(i + width, close), bold || strong, italic || !strong, out)
                    i = close + width
                    continue
                }
                plain.append(text, i, i + width)
                i += width
                continue
            }
            plain.append(c)
            i += 1
        }
        flush()
    }

    private fun ticks_at(text: String, at: Int): Int {
        var n = 0
        while (at + n < text.length && text[at + n] == '`') n += 1
        return n
    }

    /** The index just past the code span that opens at `at`, or null when its run of backticks never closes. */
    private fun code_end(text: String, at: Int): Int? {
        val ticks = ticks_at(text, at)
        var i = at + ticks
        while (i < text.length) {
            if (text[i] == '`') {
                val run = ticks_at(text, i)
                if (run == ticks) return i + run
                i += run
            } else {
                i += 1
            }
        }
        return null
    }

    /** An opener touches the word it starts. `_` also needs a word edge before it, so `snake_case` stays text. */
    private fun opens(text: String, at: Int, width: Int): Boolean {
        val next = text.getOrNull(at + width) ?: return false
        if (next.isWhitespace() || next == text[at]) return false
        return text[at] != '_' || text.getOrNull(at - 1)?.isLetterOrDigit() != true
    }

    /** The closing run for an opener, on the same line and outside code spans; null when there is none. */
    private fun closer(text: String, from: Int, mark: Char, width: Int): Int? {
        var i = from
        while (i < text.length) {
            val c = text[i]
            when {
                c == '\n' -> return null
                c == '\\' -> i += 2
                c == '`' -> i = code_end(text, i) ?: (i + 1)
                c == mark -> {
                    var run = 0
                    while (i + run < text.length && text[i + run] == mark) run += 1
                    val touches = i > from && !text[i - 1].isWhitespace()
                    val word_edge = mark != '_' || text.getOrNull(i + run)?.isLetterOrDigit() != true
                    if (run == width && touches && word_edge) return i
                    i += run
                }
                else -> i += 1
            }
        }
        return null
    }

    private fun merge(spans: List<MdSpan>): List<MdSpan> {
        val out = mutableListOf<MdSpan>()
        for (span in spans) {
            val last = out.lastOrNull()
            if (last != null && !last.code && !last.link && last.copy(text = span.text) == span) {
                out[out.size - 1] = last.copy(text = last.text + span.text)
            } else if (span.text.isNotEmpty()) {
                out += span
            }
        }
        return out
    }
}
