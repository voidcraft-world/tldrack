/**
 * paste-blocks.ts — THE split of a message into its own words and the blocks pasted into it, so the Laya review page
 * folds a pasted DOCUMENT to one line instead of printing it, and prints a paste that IS the operator's words as words.
 * `AgentMessage.vue` is the consumer and imports this statically: it is pure (its one import, `fence-lines.ts`, has
 * none), so it adds to an entry chunk nothing the lazy renderer would.
 *
 * The format is the session ledger's text, which is what the page renders. A paste reads, literally:
 *
 *     <pasted_content id="a1b2">
 *     …the pasted lines…
 *     </pasted_content id="a1b2">
 *
 * - **The closing tag repeats the id**, so a paste ends at the first closer with ITS id. A closer with another id is
 *   text inside the paste, and a paste with no closer runs to the end of the message (rare, but it happens).
 * - **A tag starts its own line** (spaces or tabs before it are fine), **outside a code fence**. Every real paste does,
 *   and the rule keeps an agent's message that names the tag mid-line, in backticks, or quotes the format in a fenced
 *   example, from folding the rest of itself away.
 * - **A paste inside a paste is escaped**, `<\pasted_content id="…">`, which is not a tag here, so it stays text inside
 *   the outer paste. A raw opener inside a paste is text too: only the outer id's closer ends it.
 * - **A one-line paste is the operator's own words.** Dictation arrives as one long line, pasted, and is often the
 *   prompt itself. Folded, such a prompt reads `PASTED · 1 line` and the turns under it answer nothing visible. So a
 *   one-line paste goes back into the text where it sat, tags dropped; a dictation that began after a typed letter
 *   or two on its own line (`T`, then `hen, next…`) joins it with no break.
 * - **A message that is only pastes is its pastes.** With no words of the operator's beside them, there is nothing for
 *   a fold to make room for, so they read as the message.
 * - **The operator fences a paste with `"""`** on a line of its own, before it, after it or both. That line carries
 *   nothing once the paste is folded, so the last non-blank line before a folded paste and the first after one are
 *   dropped when they are exactly `"""`, along with the blank lines that touch the paste.
 * - **A clipped message cuts its last paste short.** The bridge sends prompts head-clipped (`chars` is the whole
 *   length), so an open paste there reads `26+ lines` (`split_pastes` `clipped`): a lower bound, never a wrong size.
 *
 * The body keeps its text verbatim, less the one newline after the opener and the one before the closer.
 */
import { scan_fences } from './fence-lines.ts';

/** One run of a message: the author's own text, or one pasted block (`id` is null for a bare `<pasted_content>`).
 *  `closed`: its closer was in the text; `cut`: it was not, and the text was clipped, so the counts are at least. */
export type PasteSegment = { kind: 'text'; text: string } | { kind: 'paste'; id: string | null; text: string; lines: number; chars: number; cut: boolean };

export type PasteBlock = Extract<PasteSegment, { kind: 'paste' }>;

/** Cheap gate: no opener anywhere means no paste (an escaped `<\pasted_content` or a closer never contains it). */
const OPENER_MARK = '<pasted_content';

const OPEN_TAG = /^[ \t]*<pasted_content(?:[ \t]+id[ \t]*=[ \t]*"([^"]*)")?[ \t]*>/gm;
const CLOSE_TAG = /^[ \t]*<\/pasted_content(?:[ \t]+id[ \t]*=[ \t]*"([^"]*)")?[ \t]*>/gm;

const FENCE = '"""';

/** A typed letter or two on a line of its own, then the paste: `T` before `hen, the second point is…`. */
const LONE_FRAGMENT = /(?:^|\n)[ \t]*\S{1,3}[ \t]*(?:\r?\n[ \t]*)+$/;
/** …and the paste carries on the word it started: it opens in lower case. */
const CARRIES_ON = /^\p{Ll}/u;

/** Where the first closer for `id` at or after `from` starts and ends, or null when the paste is never closed. */
function find_close(source: string, id: string | null, from: number): { start: number; end: number } | null {
    const close = new RegExp(CLOSE_TAG.source, CLOSE_TAG.flags);
    close.lastIndex = from;
    for (let match = close.exec(source); match; match = close.exec(source)) {
        if ((match[1] ?? null) === id) return { start: match.index, end: match.index + match[0].length };
    }
    return null;
}

function paste_segment(id: string | null, raw: string, cut: boolean): PasteBlock {
    const text = raw.replace(/^\r?\n/, '').replace(/\r?\n$/, '');
    return { kind: 'paste', id, text, lines: text === '' ? 0 : text.split('\n').length, chars: text.length, cut };
}

/** `text` less the first (or last) non-blank line, when that line is exactly a `"""` fence. */
function drop_fence(text: string, side: 'first' | 'last'): string {
    const lines = text.split('\n');
    for (let k = 0; k < lines.length; k++) {
        const i = side === 'first' ? k : lines.length - 1 - k;
        const line = lines[i].trim();
        if (line === '') continue;
        if (line === FENCE) lines.splice(i, 1);
        break;
    }
    return lines.join('\n');
}

/** A text run between pastes: its fences dropped, and the blank lines on each side that touches a paste. */
function tidy_text(text: string, after_paste: boolean, before_paste: boolean): string {
    let out = text;
    if (after_paste) out = drop_fence(out, 'first').replace(/^(?:[ \t]*\r?\n)+/, '');
    if (before_paste) out = drop_fence(out, 'last').trimEnd();
    return out;
}

/** The message as raw runs: the text between tags verbatim, and each paste. An opener inside a code fence of the text
 *  run it would end is an example, so the scan passes it by. */
function raw_runs(source: string, clipped: boolean): PasteSegment[] {
    const runs: PasteSegment[] = [];
    const open = new RegExp(OPEN_TAG.source, OPEN_TAG.flags);
    let at = 0;
    for (let match = open.exec(source); match; match = open.exec(source)) {
        // `at` is a paste boundary, never inside a fence, so the text from it says whether this opener is in one.
        if (scan_fences(source.slice(at, match.index)).open_at_end) continue;
        const id = match[1] ?? null;
        const body_from = match.index + match[0].length;
        const close = find_close(source, id, body_from);
        runs.push({ kind: 'text', text: source.slice(at, match.index) });
        runs.push(paste_segment(id, source.slice(body_from, close ? close.start : source.length), !close && clipped));
        at = close ? close.end : source.length;
        open.lastIndex = at;
        if (!close) break;
    }
    if (runs.length) runs.push({ kind: 'text', text: source.slice(at) });
    return runs;
}

/** One-line pastes back into the text where they sat, and neighbouring text runs merged. */
function unfold_lines(runs: readonly PasteSegment[]): PasteSegment[] {
    const out: PasteSegment[] = [];
    // The tags' own lines go with them: the text after a closer starts on the line after the body.
    let after_body = false;
    for (const run of runs) {
        const prev = out.at(-1);
        if (run.kind === 'paste' && run.lines >= 2) {
            out.push(run);
            after_body = false;
            continue;
        }
        let text = run.text;
        if (run.kind === 'paste') {
            if (prev?.kind === 'text' && LONE_FRAGMENT.test(prev.text) && CARRIES_ON.test(run.text)) prev.text = prev.text.trimEnd();
        } else if (after_body && text !== '' && !text.startsWith('\n')) {
            text = `\n${text}`;
        }
        after_body = run.kind === 'paste';
        if (prev?.kind === 'text') prev.text += text;
        else out.push({ kind: 'text', text });
    }
    return out;
}

/**
 * A message as its runs, in order: the author's text and each folded paste. With nothing to fold, one text run (the
 * message unchanged when it held no paste at all). Text runs that are only whitespace once their fences go are
 * dropped. `clipped`: the text is the head of a longer message, so a paste it leaves open was cut there.
 */
export function split_pastes(text: string, opts: { clipped?: boolean } = {}): PasteSegment[] {
    const source = text ?? '';
    if (!source.includes(OPENER_MARK)) return [{ kind: 'text', text: source }];
    const raw = raw_runs(source, opts.clipped === true);
    if (raw.length === 0) return [{ kind: 'text', text: source }];
    const runs = unfold_lines(raw);
    if (!runs.some((run) => run.kind === 'paste')) return [{ kind: 'text', text: (runs[0] as { text: string }).text.trimEnd() }];

    const out: PasteSegment[] = [];
    runs.forEach((run, i) => {
        if (run.kind === 'paste') {
            out.push(run);
            return;
        }
        const tidy = tidy_text(run.text, runs[i - 1]?.kind === 'paste', runs[i + 1]?.kind === 'paste');
        if (tidy.trim() !== '') out.push({ kind: 'text', text: tidy });
    });
    // Only pastes: they are the message.
    if (out.every((run) => run.kind === 'paste')) return [{ kind: 'text', text: out.map((run) => run.text).join('\n\n') }];
    return out;
}

/**
 * The end of a split message for a tail preview, about `max_chars` long. Walking back from the last run, a paste costs
 * one folded line (`line_chars`) and a text run its length; the text run the budget runs out in is cut by `cut_text`
 * (the renderer's `tail_at_block`), and every run above it is dropped. The result is a suffix of `segments`, except
 * that its first run may be that cut text. `cut` says something above was dropped.
 */
export function tail_segments(
    segments: readonly PasteSegment[],
    max_chars: number,
    line_chars: number,
    cut_text: (text: string, max_chars: number) => string,
): { segments: PasteSegment[]; cut: boolean } {
    let left = Math.max(0, max_chars);
    const kept: PasteSegment[] = [];
    for (let i = segments.length - 1; i >= 0; i--) {
        if (left <= 0) return { segments: kept.reverse(), cut: true };
        const run = segments[i];
        if (run.kind === 'paste') {
            kept.push(run);
            left -= Math.max(1, line_chars);
            continue;
        }
        const whole = run.text.trimEnd();
        if (whole.length <= left) {
            kept.push(run);
            left -= whole.length;
            continue;
        }
        const tail = cut_text(whole, left);
        if (tail.trim() !== '') kept.push({ kind: 'text', text: tail });
        return { segments: kept.reverse(), cut: true };
    }
    return { segments: kept.reverse(), cut: false };
}

const COUNT = new Intl.NumberFormat('en-US');
const COMPACT = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });

/** Past this many characters a line, a paste's line count undersells it (a few long paragraphs), so the label says
 *  its size too. */
export const LONG_LINE_CHARS = 120;

function count(n: number, one: string, many: string, at_least = false): string {
    return `${COUNT.format(n)}${at_least ? '+' : ''} ${n === 1 && !at_least ? one : many}`;
}

/** The folded line: `PASTED · 42 lines`; `PASTED · 3 lines · 4.2K chars` when its lines are long paragraphs;
 *  `26+ lines` for a paste the clip cut. */
export function paste_label(seg: Pick<PasteBlock, 'lines' | 'chars' | 'cut'>): string {
    const long = seg.chars > Math.max(1, seg.lines) * LONG_LINE_CHARS;
    return `PASTED · ${count(seg.lines, 'line', 'lines', seg.cut)}${long ? ` · ${COMPACT.format(seg.chars)}${seg.cut ? '+' : ''} chars` : ''}`;
}

/** Its size in words, exact: `Pasted text, 42 lines, 3,104 characters`. The tooltip of the bare folded line, where a
 *  press opens the message around it, not the paste. */
export function paste_size_title(seg: Pick<PasteBlock, 'lines' | 'chars' | 'cut'>): string {
    if (seg.cut) return `Pasted text, at least ${count(seg.lines, 'line', 'lines')} and ${count(seg.chars, 'character', 'characters')}: the message was clipped here`;
    return `Pasted text, ${count(seg.lines, 'line', 'lines')}, ${count(seg.chars, 'character', 'characters')}`;
}

/** The fold's own tooltip, where a press does open it. */
export function paste_title(seg: Pick<PasteBlock, 'lines' | 'chars' | 'cut'>): string {
    return `${paste_size_title(seg)}: press to show it`;
}
