/**
 * The agent canvas's two generated twins, from `src/agent-canvas.ts`:
 *
 *   - `plugins/agent-canvas/agent_canvas/assets/canvas-schema.json` — the
 *     JSON Schema the bridge plugin validates every canvas doc with. The plugin is
 *     stdlib-only Python and cannot import Zod, so it reads this instead.
 *   - `.claude/skills/agent-canvas/reference.md` — the block reference agents read.
 *     Generated so a block added to the registry is a block agents are told about.
 *
 *   node --experimental-strip-types schemas/scripts/export-agent-canvas.ts
 *   node --experimental-strip-types schemas/scripts/export-agent-canvas.ts --check
 *
 * Why a generated twin, and what `--check` promises: owned by
 * `schemas/scripts/generated-json.ts` — see the note there. The plugin's
 * `tests/test_schema_twin.py` runs `--check`.
 */
import { z } from 'zod';
import { emit_generated_json, emit_generated_text } from './generated-json.ts';
import {
    AGENT_CANVAS_BLOCK_NOTES,
    AGENT_CANVAS_BLOCK_SCHEMAS,
    AGENT_CANVAS_CONTAINERS,
    AGENT_CANVAS_DOC_SHAPE,
    AGENT_CANVAS_QUESTION_TYPES,
} from '../src/agent-canvas.ts';

const SOURCE = 'schemas/src/agent-canvas.ts';
const SCRIPT = 'schemas/scripts/export-agent-canvas.ts';

type Json = Record<string, unknown>;

/** One property's type, compactly: `text≤40`, `number≥0`, `[text≤500]≤100`, `ok|warn|bad`. */
function describe(schema: Json): string {
    if ('$ref' in schema) return 'block';
    if ('const' in schema) return JSON.stringify(schema.const);
    if (Array.isArray(schema.enum)) return schema.enum.join('|');
    const union = (schema.anyOf ?? schema.oneOf) as Json[] | undefined;
    if (union) {
        if (union.every((member) => 'const' in member)) return union.map((member) => JSON.stringify(member.const)).join('|');
        if (union.every((member) => (member.properties as Json | undefined)?.type)) {
            return union.map((member) => ((member.properties as Json).type as Json).const).join('|');
        }
        return union.map(describe).join(' | ');
    }
    switch (schema.type) {
        case 'string': {
            if (schema.pattern === '^https?:\\/\\/') return 'url';
            if (typeof schema.pattern === 'string') return 'id';
            return schema.maxLength ? `text≤${schema.maxLength}` : 'text';
        }
        case 'number':
            if ('minimum' in schema) return `number≥${schema.minimum}`;
            if ('exclusiveMinimum' in schema) return `number>${schema.exclusiveMinimum}`;
            return 'number';
        case 'boolean':
            return 'bool';
        case 'null':
            return 'null';
        case 'array': {
            const bound = schema.maxItems ? `≤${schema.maxItems}` : '';
            if (Array.isArray(schema.prefixItems)) return `[${(schema.prefixItems as Json[]).map(describe).join(', ')}]`;
            return `[${describe(schema.items as Json)}]${bound}`;
        }
        case 'object': {
            const required = new Set((schema.required as string[] | undefined) ?? []);
            const props = Object.entries((schema.properties as Json) ?? {}).map(
                ([key, value]) => `${key}${required.has(key) ? '' : '?'}: ${describe(value as Json)}`,
            );
            return `{${props.join(', ')}}`;
        }
        default:
            return '?';
    }
}

/** An object schema's keys as reference cells: `` `key?` type (note) ``, `skip` left out. */
function key_cells(json: Json, skip: string[]): string {
    const required = new Set((json.required as string[] | undefined) ?? []);
    return Object.entries(json.properties as Json)
        .filter(([key]) => !skip.includes(key))
        .map(([key, value]) => {
            const note = (value as Json).description ? ` (${(value as Json).description})` : '';
            return `\`${key}${required.has(key) ? '' : '?'}\` ${describe(value as Json)}${note}`;
        })
        .join(' · ')
        .replaceAll('|', '\\|');
}

function block_line(name: string, schema: z.ZodType): string {
    const json = z.toJSONSchema(schema, { io: 'input' }) as Json;
    const note = AGENT_CANVAS_BLOCK_NOTES[name as keyof typeof AGENT_CANVAS_BLOCK_NOTES];
    return `| \`${name}\` | ${note} | ${key_cells(json, ['type'])} |`;
}

const doc_schema = z.toJSONSchema(AGENT_CANVAS_DOC_SHAPE, { io: 'input' });
const names = Object.keys(AGENT_CANVAS_BLOCK_SCHEMAS);

emit_generated_json({
    label: 'agent canvas schema',
    out: 'plugins/agent-canvas/agent_canvas/assets/canvas-schema.json',
    source: SOURCE,
    script: SCRIPT,
    payload: {
        question_types: [...AGENT_CANVAS_QUESTION_TYPES],
        containers: AGENT_CANVAS_CONTAINERS,
        schema: doc_schema,
    },
    summary: () => `${names.length} blocks`,
});

const field_union = (AGENT_CANVAS_BLOCK_SCHEMAS.form.shape.fields.element as z.ZodDiscriminatedUnion).options;
const field_lines = field_union.map((field) => {
    const json = z.toJSONSchema(field, { io: 'input' }) as Json;
    const kind = ((json.properties as Json).type as Json).const as string;
    return `| \`${kind}\` | ${key_cells(json, ['type', 'name', 'label']) || '—'} |`;
});

emit_generated_text({
    label: 'agent canvas reference',
    out: '.claude/skills/agent-canvas/reference.md',
    source: SOURCE,
    script: SCRIPT,
    text: [
        `<!-- DO NOT EDIT — regenerate via ${SCRIPT} (source: ${SOURCE}) -->`,
        '# Agent canvas — block reference',
        '',
        'Every block is `{"type": "<name>", …}`. `?` marks an optional key; any key not listed is an error.',
        '`text≤N` is a string of at most N characters; `[x]≤N` is a list of at most N.',
        '',
        '| Block | What it is | Keys |',
        '|---|---|---|',
        ...names.map((name) => block_line(name, AGENT_CANVAS_BLOCK_SCHEMAS[name as keyof typeof AGENT_CANVAS_BLOCK_SCHEMAS])),
        '',
        '## Form fields',
        '',
        'Every field is `{"type": "<kind>", "name": id, "label": text≤60, …}`.',
        '',
        '| Field | Keys |',
        '|---|---|',
        ...field_lines,
        '',
        `Questions (${AGENT_CANVAS_QUESTION_TYPES.join(', ')}) take an \`id\`, else \`q1\`, \`q2\`… in document order; ids are unique.`,
        '',
    ].join('\n'),
    summary: () => `${names.length} blocks, ${field_lines.length} field kinds`,
});
