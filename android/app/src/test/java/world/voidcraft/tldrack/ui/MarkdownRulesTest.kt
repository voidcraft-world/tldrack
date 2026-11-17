package world.voidcraft.tldrack.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** What `MarkdownText` is handed to draw. Every message here is invented. */
class MarkdownRulesTest {

    private fun plain(text: String) = MdSpan(text)

    @Test
    fun plain_text_is_one_paragraph_and_keeps_its_line_breaks() {
        assertEquals(listOf(MdBlock.Paragraph(listOf(plain("The cache is warm.\nNothing else changed.")))), MarkdownRules.parse("The cache is warm.\nNothing else changed."))
    }

    @Test
    fun a_blank_line_splits_paragraphs() {
        val blocks = MarkdownRules.parse("One.\n\nTwo.")
        assertEquals(listOf(MdBlock.Paragraph(listOf(plain("One."))), MdBlock.Paragraph(listOf(plain("Two.")))), blocks)
    }

    @Test
    fun a_table_parses_to_a_head_and_rows_of_the_same_width() {
        val text = "Result:\n\n| Step | State | Note |\n|---|:-:|--:|\n| build | **ok** | 12 s |\n| lint | failed |\n\nDone."
        val blocks = MarkdownRules.parse(text)
        assertEquals(3, blocks.size)
        val table = blocks[1] as MdBlock.Table
        assertEquals(listOf("Step", "State", "Note"), table.head.map(MarkdownRules::words))
        assertEquals(listOf(listOf("build", "ok", "12 s"), listOf("lint", "failed", "")), table.rows.map { row -> row.map(MarkdownRules::words) })
        assertTrue(table.rows[0][1].single().bold)
    }

    @Test
    fun a_table_without_outer_pipes_parses_too() {
        val table = MarkdownRules.parse("Name | Size\n--- | ---\nindex | 4 kB").single() as MdBlock.Table
        assertEquals(listOf("Name", "Size"), table.head.map(MarkdownRules::words))
        assertEquals(listOf(listOf("index", "4 kB")), table.rows.map { row -> row.map(MarkdownRules::words) })
    }

    @Test
    fun a_pipe_in_code_or_behind_a_backslash_is_not_a_column() {
        assertEquals(listOf("`a | b`", "x | y"), MarkdownRules.cells("| `a | b` | x \\| y |"))
    }

    @Test
    fun a_line_with_a_pipe_and_no_divider_row_stays_a_paragraph() {
        val blocks = MarkdownRules.parse("Run a | b to pipe it.\nThen stop.")
        assertTrue(blocks.single() is MdBlock.Paragraph)
    }

    @Test
    fun headings_lists_quotes_and_rules_are_their_own_blocks() {
        val blocks = MarkdownRules.parse("## What changed\n- first\n  - nested\n2. second\n> a quote\n---\n")
        assertEquals(
            listOf(
                MdBlock.Heading(2, listOf(plain("What changed"))),
                MdBlock.Item(MarkdownRules.BULLET, 0, listOf(plain("first"))),
                MdBlock.Item(MarkdownRules.BULLET, 1, listOf(plain("nested"))),
                MdBlock.Item("2.", 0, listOf(plain("second"))),
                MdBlock.Quote(listOf(plain("a quote"))),
                MdBlock.Rule,
            ),
            blocks,
        )
    }

    @Test
    fun a_wrapped_list_item_keeps_its_second_line() {
        val item = MarkdownRules.parse("- first line\n  and its second").single() as MdBlock.Item
        assertEquals("first line\nand its second", MarkdownRules.words(item.spans))
    }

    @Test
    fun a_fence_keeps_its_text_as_written_even_when_it_never_closes() {
        assertEquals(listOf(MdBlock.Code("| not | a table |\n**not bold**")), MarkdownRules.parse("```sh\n| not | a table |\n**not bold**\n```"))
        assertEquals(listOf(MdBlock.Code("still code")), MarkdownRules.parse("```\nstill code"))
    }

    @Test
    fun inline_marks_become_spans() {
        assertEquals(
            listOf(
                plain("Fixed "),
                MdSpan("the parser", bold = true),
                plain(", "),
                MdSpan("mostly", italic = true),
                plain(", in "),
                MdSpan("parse_rows()", code = true),
                plain(": see "),
                MdSpan("the notes", link = true),
                plain("."),
            ),
            MarkdownRules.inline("Fixed **the parser**, *mostly*, in `parse_rows()`: see [the notes](https://example.com/notes)."),
        )
    }

    @Test
    fun bold_can_hold_italic_and_code() {
        assertEquals(
            listOf(MdSpan("all ", bold = true), MdSpan("of", bold = true, italic = true), MdSpan(" ", bold = true), MdSpan("it", bold = true, code = true)),
            MarkdownRules.inline("**all *of* `it`**"),
        )
    }

    @Test
    fun marks_that_are_not_markdown_stay_text() {
        for (text in listOf("snake_case_name and other_name", "matches *.kt and *.kts files", "2 * 3 * 4", "a lone ** here", "an open `tick", "5 * x_1 = y_2")) {
            assertEquals(text, listOf(plain(text)), MarkdownRules.inline(text))
        }
    }

    @Test
    fun marks_inside_code_stay_as_written() {
        assertEquals(listOf(MdSpan("**kwargs and _private", code = true)), MarkdownRules.inline("`**kwargs and _private`"))
    }

    @Test
    fun a_backslash_keeps_a_mark_as_text() {
        assertEquals(listOf(plain("*not italic*")), MarkdownRules.inline("\\*not italic\\*"))
    }

    @Test
    fun the_preview_drops_marks_and_puts_a_table_row_on_one_line() {
        assertEquals("Done\nBuild is **not** shown raw: ok\nStep · State\nbuild · ok", MarkdownRules.plain("# Done\nBuild is \\*\\*not\\*\\* shown raw: **ok**\n\n| Step | State |\n|---|---|\n| build | `ok` |"))
    }
}
