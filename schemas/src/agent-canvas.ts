/**
 * The agent canvas's typed vocabulary — what an agent may put in its
 * `<canvas_dir>/<key>.json`, and the ONE owner of that contract.
 *
 * ── WHY A REGISTRY AND NOT MORE HTML CLASSES ────────────────────────────────
 * The canvas began as agent-written HTML against a CSS kit. Nothing checked it:
 * a misspelt class rendered as plain text, a question the kit could not find
 * asked nothing, and the only interaction an agent could offer was one
 * yes/no/defer row. A registry of typed blocks (the generative-UI move, after
 * thesysdev/openui: register components → tell the model exactly those → the
 * runtime validates and renders) buys three things HTML could not:
 *   - an agent learns its mistakes (`agent-canvas-check.sh` runs on every write),
 *   - a canvas can ask SEVERAL questions and real forms, answered as one line,
 *   - the renderer builds DOM from text nodes only, so a block cannot carry
 *     markup at all — strictly safer than the HTML path's CSP-only containment.
 *
 * Consumers, none of which may restate a rule kept here:
 *   - `schemas/scripts/export-agent-canvas.ts` → the JSON Schema the bridge
 *     plugin validates with (stdlib-only Python, so it reads a generated twin)
 *     and the skill's block reference.
 *   - `plugins/agent-canvas/agent_canvas/doc.py` — validation, pruning,
 *     question extraction, answer checking.
 *   - `plugins/agent-canvas/agent_canvas/assets/kit.js` — the renderer.
 *
 * Every object is STRICT: an unknown key is an error, which is what turns
 * `"colums"` from a silently empty table into a message the agent can act on.
 */
import { z } from 'zod';

const MAX_BLOCKS = 60;
const SLUG_RE = /^[a-z0-9_-]{1,32}$/;

const short = (max: number) => z.string().min(1).max(max);
const tone = z.enum(['ok', 'warn', 'bad']);
const cell = z.union([z.string().max(500), z.number(), z.null()]);
const slug = z.string().regex(SLUG_RE).describe('a-z 0-9 _ -, ≤32');
const safe_url = z
    .string()
    .max(2000)
    .regex(/^https?:\/\//)
    .describe('http(s) only');

// ── Readouts ─────────────────────────────────────────────────────────────────

const heading = z.strictObject({
    type: z.literal('heading'),
    text: short(120),
    level: z.union([z.literal(1), z.literal(2), z.literal(3)]).optional().describe('1–3, default 2'),
});

const text = z.strictObject({
    type: z.literal('text'),
    text: short(4000),
    muted: z.boolean().optional(),
});

const list = z.strictObject({
    type: z.literal('list'),
    items: z.array(short(500)).min(1).max(100),
    ordered: z.boolean().optional(),
});

const stat = z.strictObject({
    type: z.literal('stat'),
    label: short(40),
    value: z.union([short(24), z.number()]),
    unit: short(16).optional(),
    tone: tone.optional(),
    hint: short(200).optional().describe('hover text'),
});

const meter = z.strictObject({
    type: z.literal('meter'),
    label: short(60),
    value: z.number().min(0),
    max: z.number().positive().optional().describe('default 100'),
    tone: tone.optional(),
});

const split = z.strictObject({
    type: z.literal('split'),
    parts: z
        .array(z.strictObject({ label: short(40), value: z.number().min(0) }))
        .min(2)
        .max(6),
});

const kv = z.strictObject({
    type: z.literal('kv'),
    rows: z.array(z.tuple([short(60), z.union([z.string().max(500), z.number()])])).min(1).max(60),
});

const table = z.strictObject({
    type: z.literal('table'),
    columns: z.array(z.string().max(60)).min(1).max(12),
    rows: z.array(z.array(cell).max(12)).max(200).describe('numbers right-align'),
});

const diff = z.strictObject({
    type: z.literal('diff'),
    before: z.string().max(4000),
    after: z.string().max(4000),
});

const tags = z.strictObject({
    type: z.literal('tags'),
    items: z
        .array(z.union([short(40), z.strictObject({ text: short(40), tone: tone.optional() })]))
        .min(1)
        .max(20),
});

const callout = z.strictObject({
    type: z.literal('callout'),
    text: short(1000),
    title: short(60).optional(),
    tone: tone.optional().describe('default warn'),
});

const code = z.strictObject({
    type: z.literal('code'),
    text: short(20000),
});

const mermaid = z.strictObject({
    type: z.literal('mermaid'),
    source: short(8000),
});

const link = z.strictObject({
    type: z.literal('link'),
    text: short(120),
    url: safe_url,
});

// ── Questions ────────────────────────────────────────────────────────────────

// ── An option that ACTS ──────────────────────────────────────────────────────
// A button that deploys, pushes or deletes is not a menu item, and a label
// alone ("DELETE ABSORBED 3") made the operator guess what a press would do and
// whether it could be taken back (2026-10-29). So an acting option says it on
// its face, never only on hover:
//   - `does`: what the press runs, in plain words, printed under the button;
//   - `risk`: `outward` (leaves this Mac: a push, a deploy, a post, a merge on
//     GitHub) or `destructive` (deletes something). Either ARMS the button, so
//     it takes two presses, the first showing what the second will do;
//   - `targets` + `undo`, required on a destructive option: every thing it
//     deletes, by name, and how to get each back (or "none: permanent").
// The required-when-risky rule is `option_risk_issues`, on the doc's refine
// (JSON Schema cannot say it); `doc.py` `collect_questions` ports it.
const option_risk = z.enum(['outward', 'destructive']);
const option = z.union([
    short(40),
    z.strictObject({
        label: short(40),
        reply: short(500).optional().describe('typed back; default label'),
        hint: short(200).optional().describe('hover text'),
        does: short(240).optional().describe('shown under the button: what pressing it runs'),
        risk: option_risk.optional().describe('outward = leaves this Mac, destructive = deletes; either takes two presses'),
        targets: z.array(short(160)).min(1).max(40).optional().describe('everything it deletes, by name'),
        undo: short(240).optional().describe('how to reverse it, or "none: permanent"'),
    }),
]);

type AgentCanvasOption = z.infer<typeof option>;

/** Why a risky option is under-described, or none. Shared with `doc.py` (ported). */
export function option_risk_issues(item: AgentCanvasOption): string[] {
    if (typeof item === 'string' || !item.risk) return [];
    const issues: string[] = [];
    if (!item.does) issues.push(`"${item.label}" is ${item.risk}: say what it runs in "does"`);
    if (item.risk === 'destructive') {
        if (!item.targets) issues.push(`"${item.label}" deletes: list every target in "targets"`);
        if (!item.undo) issues.push(`"${item.label}" deletes: say how to undo it in "undo"`);
    }
    return issues;
}

const choice = z.strictObject({
    type: z.literal('choice'),
    id: slug.optional().describe('default q1, q2… by order'),
    q: short(120).describe('as it reads in your transcript'),
    options: z.array(option).min(1).max(12),
    multi: z.boolean().optional().describe('pick several'),
    other: z.boolean().optional().describe('free-text box, default true'),
    note: short(500).optional(),
});

const field_base = { name: slug, label: short(60) };
const field = z.discriminatedUnion('type', [
    z.strictObject({
        type: z.literal('text'),
        ...field_base,
        placeholder: short(80).optional(),
        multiline: z.boolean().optional(),
        default: z.string().max(500).optional(),
    }),
    z.strictObject({
        type: z.literal('select'),
        ...field_base,
        options: z.array(short(60)).min(1).max(20),
        default: short(60).optional(),
    }),
    z.strictObject({
        type: z.literal('toggle'),
        ...field_base,
        default: z.boolean().optional(),
    }),
    z.strictObject({
        type: z.literal('rank'),
        ...field_base,
        options: z.array(short(60)).min(2).max(12).describe('user reorders; answer is the order'),
    }),
]);

const form = z.strictObject({
    type: z.literal('form'),
    id: slug.optional().describe('default q1, q2… by order'),
    q: short(120).describe('as it reads in your transcript'),
    fields: z.array(field).min(1).max(12),
    note: short(500).optional(),
});

// ── Containers (recursive) ───────────────────────────────────────────────────

const card = z.strictObject({
    type: z.literal('card'),
    title: short(80).optional(),
    get blocks() {
        return z.array(agent_canvas_block).min(1).max(MAX_BLOCKS);
    },
});

const cols = z.strictObject({
    type: z.literal('cols'),
    get blocks() {
        return z.array(agent_canvas_block).min(1).max(6).describe('one column each');
    },
});

const details = z.strictObject({
    type: z.literal('details'),
    summary: short(120),
    get blocks() {
        return z.array(agent_canvas_block).min(1).max(MAX_BLOCKS);
    },
});

const tabs = z.strictObject({
    type: z.literal('tabs'),
    tabs: z
        .array(
            z.strictObject({
                label: short(40),
                get blocks() {
                    return z.array(agent_canvas_block).min(1).max(MAX_BLOCKS);
                },
            }),
        )
        .min(2)
        .max(8),
});

/** Every block, keyed on `type`. Adding one: a member here, a case in
 *  kit.js `BLOCKS`, and a re-run of the export (the skill reference follows). */
export const agent_canvas_block: z.ZodType<AgentCanvasBlock> = z.lazy(() =>
    z.discriminatedUnion('type', [
        heading, text, list, stat, meter, split, kv, table, diff, tags, callout, code, mermaid, link,
        choice, form,
        card, cols, details, tabs,
    ]),
);

export type AgentCanvasBlock =
    | z.infer<typeof heading> | z.infer<typeof text> | z.infer<typeof list> | z.infer<typeof stat>
    | z.infer<typeof meter> | z.infer<typeof split> | z.infer<typeof kv> | z.infer<typeof table>
    | z.infer<typeof diff> | z.infer<typeof tags> | z.infer<typeof callout> | z.infer<typeof code>
    | z.infer<typeof mermaid> | z.infer<typeof link> | z.infer<typeof choice> | z.infer<typeof form>
    | { type: 'card'; title?: string; blocks: AgentCanvasBlock[] }
    | { type: 'cols'; blocks: AgentCanvasBlock[] }
    | { type: 'details'; summary: string; blocks: AgentCanvasBlock[] }
    | { type: 'tabs'; tabs: { label: string; blocks: AgentCanvasBlock[] }[] };

/** The blocks an agent can ASK with; their answers come back typed into its prompt. */
export const AGENT_CANVAS_QUESTION_TYPES = ['choice', 'form'] as const;

/** Containers, and the key under which each holds child blocks. `tabs` nests one
 *  level deeper (`tabs[i].blocks`), so a walker reads it from here, not by guess. */
export const AGENT_CANVAS_CONTAINERS = {
    card: 'blocks',
    cols: 'blocks',
    details: 'blocks',
    tabs: 'tabs',
} as const;

const doc_shape = z.strictObject({
    title: short(80).optional(),
    blocks: z.array(agent_canvas_block).min(1).max(80),
});

export type AgentCanvasDoc = z.infer<typeof doc_shape>;

/** The shape, plus the rules JSON Schema cannot say: question ids are unique, and a
 *  risky option describes itself (`option_risk_issues`). The export reads `doc_shape`
 *  (a refinement has no JSON Schema form); the bridge ports both rules in `doc.py`
 *  `collect_questions`. */
export const agent_canvas_doc = doc_shape.superRefine((doc, ctx) => {
    const seen = new Set<string>();
    for (const id of agent_canvas_question_ids(doc)) {
        if (seen.has(id)) ctx.addIssue({ code: 'custom', message: `duplicate question id "${id}"` });
        seen.add(id);
    }
    walk_questions(doc.blocks, (block) => {
        if (block.type !== 'choice') return;
        for (const item of block.options) {
            for (const message of option_risk_issues(item)) ctx.addIssue({ code: 'custom', message });
        }
    });
});

export const AGENT_CANVAS_DOC_SHAPE = doc_shape;

/** One list per member, so the export can describe each block without
 *  reaching into Zod internals. Order is the reference's order. */
export const AGENT_CANVAS_BLOCK_SCHEMAS = {
    heading, text, list, stat, meter, split, kv, table, diff, tags, callout, code, mermaid, link,
    choice, form,
    card, cols, details, tabs,
} as const;

/** One line per block for the generated skill reference. */
export const AGENT_CANVAS_BLOCK_NOTES: Record<keyof typeof AGENT_CANVAS_BLOCK_SCHEMAS, string> = {
    heading: 'mono uppercase head',
    text: 'a paragraph; `muted` dims it',
    list: 'bullets, or numbers with `ordered`',
    stat: 'one big number with a label — lead with this',
    meter: 'one ratio against a limit',
    split: 'a share between named parts (not progress)',
    kv: 'key → value readout, `[["tests", "41 pass"]]`',
    table: 'columns + rows; number cells right-align',
    diff: 'BEFORE / AFTER columns',
    tags: 'short labels, optionally toned',
    callout: 'a toned box for the one thing not to miss',
    code: 'monospace block',
    mermaid: 'a rendered Mermaid diagram',
    link: 'opens in a new tab; http(s) only',
    choice: 'a question answered by buttons (+ a free-text box)',
    form: 'a question answered by fields: text, select, toggle, rank',
    card: 'bracket-framed group',
    cols: 'side by side; wraps when narrow',
    details: 'collapsed by default — put second tellings here',
    tabs: 'one panel visible at a time',
};

/**
 * Question ids, in document order: an explicit `id`, else `q<n>` by position
 * among questions. Duplicate explicit ids are the caller's error to report.
 * Ported to `doc.py` `collect_questions` — keep the walk order identical.
 */
export function agent_canvas_question_ids(doc: AgentCanvasDoc): string[] {
    const ids: string[] = [];
    walk_questions(doc.blocks, (block) => ids.push(block.id ?? `q${ids.length + 1}`));
    return ids;
}

type QuestionBlock = Extract<AgentCanvasBlock, { type: 'choice' | 'form' }>;

/** Every question block, in document order. */
function walk_questions(blocks: AgentCanvasBlock[], visit: (block: QuestionBlock) => void): void {
    for (const block of blocks) {
        if (block.type === 'choice' || block.type === 'form') visit(block);
        else if (block.type === 'tabs') block.tabs.forEach((tab) => walk_questions(tab.blocks, visit));
        else if ('blocks' in block) walk_questions(block.blocks, visit);
    }
}
