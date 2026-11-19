package world.voidcraft.tldrack.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

// Reading sizes: a conversation fills the screen, so its text is sized to be read, not scanned.
private val BODY = 16.sp
private val BODY_LINE = 24.sp
private val SMALL = 14.sp
private val SMALL_LINE = 20.sp
private val CODE_WASH = RackColors.mint.copy(alpha = 0.10f)
// A table column is as wide as its longest cell, between these, so a wide table scrolls sideways and a long cell wraps.
private const val COLUMN_MIN_CHARS = 4
private const val COLUMN_MAX_CHARS = 26
private val CHAR_WIDTH = 8.dp
private val CELL_PAD = 6.dp

/** An agent's message with its Markdown drawn: `MarkdownRules` owns what is parsed. */
@Composable
fun MarkdownText(text: String, modifier: Modifier = Modifier) {
    val blocks = remember(text) { MarkdownRules.parse(text) }
    Column(modifier, verticalArrangement = Arrangement.spacedBy(6.dp)) {
        for (block in blocks) {
            when (block) {
                is MdBlock.Paragraph -> Text(styled(block.spans), color = RackColors.text, fontSize = BODY, lineHeight = BODY_LINE)
                is MdBlock.Heading -> Text(
                    styled(block.spans),
                    color = RackColors.accent,
                    fontSize = if (block.level <= 2) 18.sp else 17.sp,
                    lineHeight = 26.sp,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier.padding(top = 2.dp),
                )
                is MdBlock.Item -> Row(Modifier.padding(start = (12 * block.depth).dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text(block.marker, color = RackColors.mint, fontSize = BODY, lineHeight = BODY_LINE)
                    Text(styled(block.spans), color = RackColors.text, fontSize = BODY, lineHeight = BODY_LINE)
                }
                is MdBlock.Quote -> Text(
                    styled(block.spans),
                    color = RackColors.dim,
                    fontSize = BODY,
                    lineHeight = BODY_LINE,
                    modifier = Modifier.quote_rule().padding(start = 10.dp),
                )
                is MdBlock.Code -> Box(Modifier.fillMaxWidth().background(CODE_WASH, RackShape).horizontalScroll(rememberScrollState())) {
                    Text(
                        block.text,
                        color = RackColors.text,
                        fontFamily = RackMono,
                        fontSize = SMALL,
                        lineHeight = SMALL_LINE,
                        softWrap = false,
                        modifier = Modifier.padding(8.dp),
                    )
                }
                is MdBlock.Table -> TableBlock(block)
                MdBlock.Rule -> HorizontalDivider(color = RackColors.line)
            }
        }
    }
}

@Composable
private fun TableBlock(table: MdBlock.Table) {
    val widths = remember(table) { column_widths(table) }
    Column(Modifier.horizontalScroll(rememberScrollState()).border(1.dp, RackColors.line, RackShape)) {
        TableRow(table.head, widths, head = true)
        for (row in table.rows) {
            HorizontalDivider(Modifier.width(widths.fold(0.dp) { sum, width -> sum + width }), color = RackColors.line)
            TableRow(row, widths, head = false)
        }
    }
}

@Composable
private fun TableRow(cells: List<List<MdSpan>>, widths: List<Dp>, head: Boolean) {
    Row(if (head) Modifier.background(CODE_WASH) else Modifier) {
        cells.forEachIndexed { column, spans ->
            Text(
                styled(spans),
                color = if (head) RackColors.accent else RackColors.text,
                fontSize = SMALL,
                lineHeight = SMALL_LINE,
                fontWeight = if (head) FontWeight.Bold else null,
                modifier = Modifier.width(widths[column]).padding(horizontal = CELL_PAD, vertical = 5.dp),
            )
        }
    }
}

private fun column_widths(table: MdBlock.Table): List<Dp> = table.head.indices.map { column ->
    val longest = (listOf(table.head) + table.rows).maxOf { row -> MarkdownRules.words(row[column]).length }
    CHAR_WIDTH * longest.coerceIn(COLUMN_MIN_CHARS, COLUMN_MAX_CHARS) + CELL_PAD * 2
}

/** The rule down a quote's left side. */
private fun Modifier.quote_rule(): Modifier = drawBehind {
    drawLine(RackColors.line, Offset(1.dp.toPx(), 0f), Offset(1.dp.toPx(), size.height), strokeWidth = 2.dp.toPx())
}

private fun styled(spans: List<MdSpan>): AnnotatedString = buildAnnotatedString {
    for (span in spans) {
        val style = SpanStyle(
            fontWeight = if (span.bold) FontWeight.Bold else null,
            fontStyle = if (span.italic) FontStyle.Italic else null,
            fontFamily = if (span.code) RackMono else null,
            background = if (span.code) CODE_WASH else Color.Unspecified,
            color = if (span.link) RackColors.mint else Color.Unspecified,
        )
        withStyle(style) { append(span.text) }
    }
}
