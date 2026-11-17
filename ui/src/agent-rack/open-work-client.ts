/**
 * The ⚠️'s three answers, the ONE caller every rack payload owner shares:
 * - `POST /open-work/dismiss` (✗ NOT OPEN) — Laya was wrong; clears the ⚠️ until the
 *   next turn end and records the operator's grade of that verdict.
 * - `POST /open-work/confirm` (✓ STILL OPEN) — Laya was right; the ⚠️ stays and the
 *   grade is recorded.
 * - `POST /open-work/snooze` (⏸ NOT NOW) — seen, not now; parks the loop so its repeats
 *   draw nothing until the operator next prompts the session. It grades nothing:
 *   `tldrack/attention.py` § 3.
 * What a grade records: owned by bridge/tldrack/open_work.py — see
 * "The operator's grade".
 *
 * And the ✅'s one call, `POST /open-work/read`: opening the pane of an answer to read reads it
 * (`read_answer`). It grades nothing. Owned by bridge/tldrack/open_work.py — see
 * "An answer to read".
 */
import { local_fetch } from '../engine/terminal/local-network-gate.ts';

// A grade is a label write behind the review log's lock (2 s at worst) after a ledger fold, so the answer may take
// seconds. The row itself repaints before the write and on the next poll either way.
const ANSWER_TIMEOUT_MS = 6000;

/** True when the bridge answered 200. Never throws: a failure leaves the badge,
 *  which the next poll repaints truthfully either way. */
async function dismiss_open_work(url: (path: string) => string, session: string): Promise<boolean> {
    return post_open_work(url, '/open-work/dismiss', session);
}

/** ⏸ NOT NOW. True when the bridge answered 200; an older bridge 404s and the ⚠️ stays. */
async function snooze_open_work(url: (path: string) => string, session: string): Promise<boolean> {
    return post_open_work(url, '/open-work/snooze', session);
}

/** ✓ STILL OPEN. True when the bridge answered 200; an older bridge 404s and nothing is graded. */
async function confirm_open_work(url: (path: string) => string, session: string): Promise<boolean> {
    return post_open_work(url, '/open-work/confirm', session);
}

/** The rack actions that answer a ⚠️, each with its call, in the order the menu draws them. */
const ANSWERS = {
    snooze_open_work,
    dismiss_open_work,
    confirm_open_work,
} as const;
export type OpenWorkAnswer = keyof typeof ANSWERS;

/** Which ⚠️ answer a rack action is, or null for every other action. */
export function open_work_answer(action: string): ((url: (path: string) => string, session: string) => Promise<boolean>) | null {
    return Object.hasOwn(ANSWERS, action) ? ANSWERS[action as OpenWorkAnswer] : null;
}

/** The rows `has_unread_answer` reads: a snapshot row's session and its answer-to-read mark. */
interface AnswerRow {
    session: string;
    answer_to_read?: boolean | null;
}

/** Does `session`'s row carry an answer to read? The one test both doors use: a row selected, and a
 *  row already on screen when its answer lands (the 💬's `is_watching` rule). */
export function has_unread_answer(rows: readonly AnswerRow[], session: string | null): boolean {
    return session !== null && rows.some((row) => row.session === session && row.answer_to_read === true);
}

/** The operator opened this pane: its answer is read. True when the bridge answered 200; an older
 *  bridge 404s and the mark (which it never sends) is not there to clear. */
export async function read_answer(url: (path: string) => string, session: string): Promise<boolean> {
    return post_open_work(url, '/open-work/read', session);
}

async function post_open_work(url: (path: string) => string, route: string, session: string): Promise<boolean> {
    try {
        const res = await local_fetch(url(route), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session }),
            signal: AbortSignal.timeout(ANSWER_TIMEOUT_MS),
        });
        return res.ok;
    } catch {
        return false;
    }
}
