<!-- DO NOT EDIT — regenerate via schemas/scripts/export-agent-canvas.ts (source: schemas/src/agent-canvas.ts) -->
# Agent canvas — block reference

Every block is `{"type": "<name>", …}`. `?` marks an optional key; any key not listed is an error.
`text≤N` is a string of at most N characters; `[x]≤N` is a list of at most N.

| Block | What it is | Keys |
|---|---|---|
| `heading` | mono uppercase head | `text` text≤120 · `level?` 1\|2\|3 (1–3, default 2) |
| `text` | a paragraph; `muted` dims it | `text` text≤4000 · `muted?` bool |
| `list` | bullets, or numbers with `ordered` | `items` [text≤500]≤100 · `ordered?` bool |
| `stat` | one big number with a label — lead with this | `label` text≤40 · `value` text≤24 \| number · `unit?` text≤16 · `tone?` ok\|warn\|bad · `hint?` text≤200 (hover text) |
| `meter` | one ratio against a limit | `label` text≤60 · `value` number≥0 · `max?` number>0 (default 100) · `tone?` ok\|warn\|bad |
| `split` | a share between named parts (not progress) | `parts` [{label: text≤40, value: number≥0}]≤6 |
| `kv` | key → value readout, `[["tests", "41 pass"]]` | `rows` [[text≤60, text≤500 \| number]]≤60 |
| `table` | columns + rows; number cells right-align | `columns` [text≤60]≤12 · `rows` [[text≤500 \| number \| null]≤12]≤200 (numbers right-align) |
| `diff` | BEFORE / AFTER columns | `before` text≤4000 · `after` text≤4000 |
| `tags` | short labels, optionally toned | `items` [text≤40 \| {text: text≤40, tone?: ok\|warn\|bad}]≤20 |
| `callout` | a toned box for the one thing not to miss | `text` text≤1000 · `title?` text≤60 · `tone?` ok\|warn\|bad (default warn) |
| `code` | monospace block | `text` text≤20000 |
| `mermaid` | a rendered Mermaid diagram | `source` text≤8000 |
| `link` | opens in a new tab; http(s) only | `text` text≤120 · `url` url (http(s) only) |
| `choice` | a question answered by buttons (+ a free-text box) | `id?` id (default q1, q2… by order) · `q` text≤120 (as it reads in your transcript) · `options` [text≤40 \| {label: text≤40, reply?: text≤500, hint?: text≤200, does?: text≤240, risk?: outward\|destructive, targets?: [text≤160]≤40, undo?: text≤240}]≤12 · `multi?` bool (pick several) · `other?` bool (free-text box, default true) · `note?` text≤500 |
| `form` | a question answered by fields: text, select, toggle, rank | `id?` id (default q1, q2… by order) · `q` text≤120 (as it reads in your transcript) · `fields` [text\|select\|toggle\|rank]≤12 · `note?` text≤500 |
| `card` | bracket-framed group | `title?` text≤80 · `blocks` [block]≤60 |
| `cols` | side by side; wraps when narrow | `blocks` [block]≤6 (one column each) |
| `details` | collapsed by default — put second tellings here | `summary` text≤120 · `blocks` [block]≤60 |
| `tabs` | one panel visible at a time | `tabs` [{label: text≤40, blocks: [block]≤60}]≤8 |

## Form fields

Every field is `{"type": "<kind>", "name": id, "label": text≤60, …}`.

| Field | Keys |
|---|---|
| `text` | `placeholder?` text≤80 · `multiline?` bool · `default?` text≤500 |
| `select` | `options` [text≤60]≤20 · `default?` text≤60 |
| `toggle` | `default?` bool |
| `rank` | `options` [text≤60]≤12 (user reorders; answer is the order) |

Questions (choice, form) take an `id`, else `q1`, `q2`… in document order; ids are unique.
