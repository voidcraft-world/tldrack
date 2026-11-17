// The canvas kit's one script — the only one the page's CSP admits (page.py
// owns why). It polls this session's canvas and draws it: a typed doc
// (`<key>.json`) is BUILT here from text nodes, block by block (`BLOCKS`); a
// markup canvas (`<key>.html`) is swapped in as-is. Questions POST /reply, and
// Mermaid blocks are drawn when a canvas has any.
//
// The pane has two faces, and the desk says which (`desk.py` `set_view`): this
// canvas, or the ◇ SKILLS audit (`skills.js`, defined just before this file in
// the same script tag). The chips in `#vc-views` flip it, as does the
// terminal's ◇ SKILLS button.
//
// The block vocabulary is owned by schemas/src/agent-canvas.ts; the server has
// validated the doc against it before it reaches this file, so a builder here
// trusts its block's shape. schemas/tests/agent-canvas.test.ts holds `BLOCKS`
// and the registry to the same list.
(() => {
    'use strict';
    const BASE = '/plugin/agent-canvas';
    const VISIBLE_POLL_MS = 1000;
    const HIDDEN_POLL_MS = 5000;
    const nonce = document.currentScript ? document.currentScript.nonce : '';
    const session = document.body.dataset.session || '';
    const root = document.getElementById('vc-canvas');
    const status = document.getElementById('vc-status');
    const artifacts = create_artifact_shelf(document.getElementById('vc-artifacts'));
    let rev;            // the DRAWN revision: undefined until the first draw, null for "no canvas"
    let latest = null;  // the last /frag answer, drawn or not (the skills face holds the drawing back)
    let view = 'canvas';
    const views_bar = document.getElementById('vc-views');
    const skills_slot = document.getElementById('vc-skills');
    let mermaid_ready = null;
    let asking = null;  // the drawn doc's questions: { widgets: Map, state, fast }
    const LOOPBACK = new Set(['localhost', '127.0.0.1', '[::1]']);
    const framers = new Set((document.body.dataset.framers || '').split(' ').filter(Boolean));
    // Read now, while the body holds only the page's own skeleton. Agent markup lands
    // later, and a named element in it (`<img name="body" data-mermaid=…>`) shadows
    // `document.body`, so a late read could hand this page's nonce to any URL. The
    // prefix check is the second lock: only this plugin's own route is ever loaded.
    const MERMAID_URL = (() => {
        const url = document.body.dataset.mermaid || '';
        return url.startsWith(`${BASE}/mermaid.js?`) && !url.includes('//') ? url : `${BASE}/mermaid.js`;
    })();

    /** The parent's origin when every ancestor is the rack's own (page.py owns
     *  why), the page's own origin when opened directly, else null → inert.
     *  `ancestorOrigins` is Chromium/WebKit; elsewhere the referrer stands in,
     *  and a parent that strips its referrer is refused. */
    function trusted_parent() {
        if (window.top === window) return location.origin;
        let origins = location.ancestorOrigins ? [...location.ancestorOrigins] : [];
        if (!location.ancestorOrigins && document.referrer) {
            try {
                origins = [new URL(document.referrer).origin];
            } catch {
                origins = [];
            }
        }
        const allowed = (origin) => {
            if (framers.has(origin)) return true;
            try {
                return LOOPBACK.has(new URL(origin).hostname);
            } catch {
                return false;
            }
        };
        return origins.length && origins.every(allowed) ? origins[0] : null;
    }
    const parent_origin = trusted_parent();

    // The rack acknowledges the shelf only after validating its metadata. Older hosts keep
    // the in-frame shelf; this transfer adds no polling loop (plugins/agent-canvas/README.md).
    window.addEventListener('message', (event) => {
        if (!parent_origin || event.source !== window.parent || event.origin !== parent_origin) return;
        if (event.data?.type !== 'tldrack-canvas-artifacts' || event.data.session !== session) return;
        artifacts.set_external(event.data.external === true);
    });

    /** Tell the rack what this canvas holds — its liveness signal (no hello →
     *  "canvas offline") and the question chip. Never `'*'`. */
    function hello(data) {
        if (window.parent === window || !parent_origin) return;
        window.parent.postMessage({
            type: 'voidcraft-agent-canvas',
            session,
            has_canvas: data.rev !== null,
            has_content: data.rev !== null || data.view === 'skills' || !!data.errors?.length || !!data.truncated,
            question: data.answered ? null : (data.question || null),
            shelf: {
                key: data.key || null,
                artifacts: data.view === 'skills' ? [] : (data.artifacts || []),
                error: data.view === 'skills' ? null : (data.artifact_error || null),
            },
        }, parent_origin);
    }

    /** The one element factory: text goes in as a text node, never as markup. */
    function el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }
    const toned = (base, tone) => (tone ? `${base} ${tone}` : base);
    const tag = (text, tone) => el('span', toned('tag', tone), text);

    function set_status(parts) {
        status.replaceChildren(...parts);
    }

    function load_mermaid() {
        if (mermaid_ready) return mermaid_ready;
        mermaid_ready = new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.nonce = nonce;
            // The page names the vendored bundle's versioned URL (page.py `MERMAID_URL`).
            script.src = MERMAID_URL;
            script.onload = () => {
                window.mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'strict' });
                resolve(window.mermaid);
            };
            script.onerror = () => reject(new Error('mermaid unavailable'));
            document.head.append(script);
        });
        return mermaid_ready;
    }

    function draw_diagrams() {
        const blocks = root.querySelectorAll('pre.mermaid');
        if (!blocks.length) return;
        load_mermaid()
            .then((mermaid) => mermaid.run({ nodes: blocks }))
            .catch(() => blocks.forEach((block) => block.classList.add('mermaid--raw')));
    }

    async function post_reply(body) {
        const response = await fetch(`${BASE}/reply`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session, rev, ...body }),
        });
        const reply = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(reply.error || `HTTP ${response.status}`);
        return reply;
    }

    function show_state(node, text, tone) {
        node.className = toned('choice__state', tone);
        node.textContent = text;
    }

    // ── a typed doc ──────────────────────────────────────────────────────────

    const fmt = (value) => (typeof value === 'number' ? value.toLocaleString() : value);
    const percent = (value, max) => Math.max(0, Math.min(100, (value / max) * 100));

    /** One builder per registry block: `(block, ctx) → Node`. */
    const BLOCKS = {
        heading(b) {
            return el(`h${b.level || 2}`, null, b.text);
        },
        text(b) {
            return el('p', b.muted ? 'muted' : null, b.text);
        },
        list(b) {
            const list = el(b.ordered ? 'ol' : 'ul');
            b.items.forEach((item) => list.append(el('li', null, item)));
            return list;
        },
        stat(b) {
            const box = el('div', toned('stat', b.tone));
            const value = el('div', 'stat__value', fmt(b.value));
            if (b.unit) value.append(el('span', 'stat__unit', b.unit));
            box.append(value, el('div', 'stat__label', b.label));
            if (b.hint) box.title = b.hint;
            return box;
        },
        meter(b) {
            const max = b.max || 100;
            const box = el('div', toned('meter', b.tone));
            box.style.setProperty('--v', `${percent(b.value, max)}%`);
            box.append(el('span', null, b.label),
                el('span', 'meter__num', max === 100 ? `${fmt(b.value)}%` : `${fmt(b.value)} / ${fmt(max)}`));
            box.setAttribute('role', 'meter');
            box.setAttribute('aria-valuenow', String(b.value));
            box.setAttribute('aria-valuemax', String(max));
            return box;
        },
        split(b) {
            const total = b.parts.reduce((sum, part) => sum + part.value, 0);
            const box = el('div', 'split');
            const bar = el('div', 'split__bar');
            const legend = el('div', 'split__legend');
            b.parts.forEach((part) => {
                const share = total ? Math.round((part.value / total) * 100) : Math.round(100 / b.parts.length);
                const seg = el('div', 'split__seg');
                seg.style.flexGrow = String(total ? part.value : 1);
                seg.title = `${part.label} · ${fmt(part.value)} · ${share}%`;
                bar.append(seg);
                const key = el('span', 'split__key');
                key.append(el('i'), document.createTextNode(`${part.label} ${share}%`));
                legend.append(key);
            });
            box.append(bar, legend);
            return box;
        },
        kv(b) {
            const list = el('dl', 'kv');
            b.rows.forEach(([key, value]) => list.append(el('dt', null, key), el('dd', null, fmt(value))));
            return list;
        },
        table(b) {
            const wrap = el('div', 'table-wrap');
            const table = el('table');
            const numeric = b.columns.map((_, col) =>
                b.rows.length > 0 && b.rows.every((row) => row[col] === null || row[col] === undefined
                    || typeof row[col] === 'number'));
            const head = el('tr');
            b.columns.forEach((name, col) => head.append(el('th', numeric[col] ? 'num' : null, name)));
            const body = el('tbody');
            b.rows.forEach((row) => {
                const line = el('tr');
                b.columns.forEach((_, col) => {
                    const cell = row[col];
                    if (cell === null || cell === undefined) line.append(el('td', 'muted', '—'));
                    else line.append(el('td', typeof cell === 'number' ? 'num' : null, fmt(cell)));
                });
                body.append(line);
            });
            const thead = el('thead');
            thead.append(head);
            table.append(thead, body);
            wrap.append(table);
            return wrap;
        },
        diff(b) {
            const box = el('div', 'diff');
            box.append(el('del', null, b.before), el('ins', null, b.after));
            return box;
        },
        tags(b) {
            const box = el('div', 'tags');
            b.items.forEach((item) => box.append(typeof item === 'string' ? tag(item) : tag(item.text, item.tone)));
            return box;
        },
        callout(b) {
            const box = el('div', toned('callout', b.tone || 'warn'));
            if (b.title) box.append(el('div', 'callout__title', b.title));
            box.append(el('p', null, b.text));
            return box;
        },
        code(b) {
            const pre = el('pre');
            pre.append(el('code', null, b.text));
            return pre;
        },
        mermaid(b) {
            return el('pre', 'mermaid', b.source);
        },
        link(b) {
            if (!/^https?:\/\//i.test(b.url)) return el('span', null, b.text);
            const link = el('a', 'link', b.text);
            link.href = b.url;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            return link;
        },
        choice(b, ctx) {
            return choice_widget(b, ctx);
        },
        form(b, ctx) {
            return form_widget(b, ctx);
        },
        card(b, ctx) {
            const box = el('div', 'card');
            if (b.title) box.append(el('h2', null, b.title));
            box.append(...build_all(b.blocks, ctx));
            return box;
        },
        cols(b, ctx) {
            const box = el('div', 'cols');
            build_all(b.blocks, ctx).forEach((node) => {
                const col = el('div', 'col');
                col.append(node);
                box.append(col);
            });
            return box;
        },
        details(b, ctx) {
            const box = el('details', 'disclosure');
            box.append(el('summary', null, b.summary), ...build_all(b.blocks, ctx));
            return box;
        },
        tabs(b, ctx) {
            const box = el('div', 'tabs');
            const bar = el('div', 'tabs__bar');
            bar.setAttribute('role', 'tablist');
            const panels = b.tabs.map((tab) => {
                const panel = el('div', 'tabs__panel');
                panel.append(...build_all(tab.blocks, ctx));
                return panel;
            });
            const select = (index) => {
                [...bar.children].forEach((button, at) => button.setAttribute('aria-selected', String(at === index)));
                panels.forEach((panel, at) => {
                    panel.hidden = at !== index;
                });
            };
            b.tabs.forEach((tab, index) => {
                const button = el('button', 'tabs__tab', tab.label);
                button.type = 'button';
                button.setAttribute('role', 'tab');
                button.addEventListener('click', () => select(index));
                bar.append(button);
            });
            box.append(bar, ...panels);
            select(0);
            return box;
        },
    };

    function build(block, ctx) {
        const builder = BLOCKS[block.type];
        return builder ? builder(block, ctx) : el('p', 'muted', `[${block.type}]`);
    }

    function build_all(blocks, ctx) {
        return blocks.map((block) => build(block, ctx));
    }

    // ── questions ────────────────────────────────────────────────────────────
    // A widget is `{ value() → answer | undefined, show(answer) }`. With ONE
    // single-pick choice on the canvas a click sends at once (the old canvas's
    // behaviour); otherwise picks are held and the bar under the doc sends
    // every answer as one line (desk.py owns why one line).

    function question_box(b, ctx) {
        const box = el('div', 'choice');
        box.dataset.q = b.q;
        box.append(el('div', 'choice__q', b.q));
        if (b.note) box.append(el('p', 'muted', b.note));
        return { box, id: ctx.next_id() };
    }

    /** How long an armed option waits for its second press. */
    const ARM_MS = 8000;

    /** An acting option's face: what it runs, what it deletes, how to undo it
     *  (schemas/src/agent-canvas.ts owns why these are never hover-only). */
    function option_card(option, button) {
        const card = el('div', toned('choice__opt', option.risk));
        card.append(button);
        if (option.risk) button.prepend(el('span', 'choice__risk', option.risk === 'destructive' ? 'DELETES' : 'LEAVES MAC'));
        const body = el('div', 'choice__body');
        if (option.does) body.append(el('div', 'choice__does', option.does));
        if (option.targets) {
            const list = el('ul', 'choice__targets');
            option.targets.forEach((target) => list.append(el('li', null, target)));
            body.append(list);
        }
        if (option.undo) body.append(el('div', 'choice__undo', `UNDO · ${option.undo}`));
        if (body.childNodes.length) card.append(body);
        return card;
    }

    function choice_widget(b, ctx) {
        const { box, id } = question_box(b, ctx);
        const options = b.options.map((option) => (typeof option === 'string'
            ? { label: option, reply: option }
            : { ...option, reply: option.reply || option.label }));
        const acting = options.some((option) => option.does || option.risk || option.targets);
        const risky = options.some((option) => option.risk);
        // What a press does, before anyone presses: the operator should never
        // have to guess whether a click acts or only selects.
        const how = ctx.fast
            ? (risky ? 'ONE PRESS SENDS · ⚠ ONES TAKE TWO' : 'ONE PRESS SENDS')
            : (risky ? 'PICK, THEN SEND ANSWERS · ⚠ ONES TAKE TWO' : 'PICK, THEN SEND ANSWERS');
        box.append(el('div', 'choice__how', how));
        const picked = new Set();
        let free = '';
        let input = null;
        let armed = null;   // { button, option, timer } while a risky option waits for its second press
        const disarm = () => {
            if (!armed) return;
            clearTimeout(armed.timer);
            armed.button.classList.remove('is-armed');
            armed.button.lastChild.textContent = armed.option.label;
            box.classList.remove('has-armed');
            armed = null;
        };
        const row = el('div', acting ? 'choice__opts' : 'choice__buttons');
        const buttons = options.map((option) => {
            const button = el('button', null);
            button.append(el('span', 'choice__label', option.label));
            button.type = 'button';
            if (option.hint) button.title = option.hint;
            button.addEventListener('click', () => {
                if (option.risk && !(armed && armed.button === button) && !picked.has(option.reply)) {
                    disarm();
                    button.classList.add('is-armed');
                    button.lastChild.textContent = `PRESS AGAIN · ${option.label}`;
                    box.classList.add('has-armed');
                    armed = { button, option, timer: setTimeout(disarm, ARM_MS) };
                    return;
                }
                disarm();
                if (ctx.fast) {
                    ctx.send({ [id]: option.reply });
                    return;
                }
                if (!b.multi) picked.clear();
                if (picked.has(option.reply)) picked.delete(option.reply);
                else picked.add(option.reply);
                if (!b.multi && input) input.value = free = '';
                paint();
                ctx.changed();
            });
            row.append(acting ? option_card(option, button) : button);
            return button;
        });
        box.append(row);
        if (b.other !== false) {
            const other = el('div', 'choice__other');
            input = el('input');
            input.placeholder = 'other…';
            input.maxLength = 1500;
            input.addEventListener('input', () => {
                free = input.value.trim();
                if (free && !b.multi) picked.clear();
                paint();
                ctx.changed();
            });
            input.addEventListener('keydown', (event) => {
                if (event.key === 'Enter' && ctx.fast && free) ctx.send({ [id]: free });
            });
            other.append(input);
            if (ctx.fast) {
                const go = el('button', null, 'SEND');
                go.type = 'button';
                go.addEventListener('click', () => free && ctx.send({ [id]: free }));
                other.append(go);
            }
            box.append(other);
        }
        if (ctx.fast) {
            ctx.state = el('div', 'choice__state');
            box.append(ctx.state);
        }
        function paint() {
            buttons.forEach((button, at) => {
                const on = picked.has(options[at].reply);
                button.classList.toggle('is-chosen', on);
                button.closest('.choice__opt')?.classList.toggle('is-chosen', on);
            });
        }
        const widget = {
            value() {
                if (b.multi) {
                    const all = [...picked, ...(free ? [free] : [])];
                    return all.length ? all : undefined;
                }
                return free || [...picked][0];
            },
            show(answer) {
                const chosen = new Set(Array.isArray(answer) ? answer : [answer]);
                disarm();
                box.classList.add('is-answered');
                buttons.forEach((button, at) => {
                    button.disabled = true;
                    button.classList.toggle('is-chosen', chosen.has(options[at].reply));
                    const card = button.closest('.choice__opt');
                    if (card) card.classList.toggle('is-chosen', chosen.has(options[at].reply));
                });
                box.querySelector('.choice__how')?.remove();
                if (input) input.closest('.choice__other').remove();
                input = null;
                const extra = [...chosen].filter((value) => !options.some((option) => option.reply === value));
                if (extra.length) box.append(el('div', 'choice__free', `→ ${extra.join(', ')}`));
            },
        };
        ctx.widgets.set(id, widget);
        return box;
    }

    function form_widget(b, ctx) {
        const { box, id } = question_box(b, ctx);
        const readers = [];
        const writers = [];
        b.fields.forEach((field) => {
            // A rank row is not a <label>: a label forwards a stray click to
            // its first button, which here would move an item.
            const row = el(field.type === 'rank' ? 'div' : 'label', 'field');
            row.append(el('span', 'field__label', field.label));
            if (field.type === 'text') {
                const input = el(field.multiline ? 'textarea' : 'input');
                if (field.placeholder) input.placeholder = field.placeholder;
                input.value = field.default || '';
                input.maxLength = 2000;
                input.addEventListener('input', () => ctx.changed());
                row.append(input);
                readers.push(() => (input.value.trim() ? [field.name, input.value.trim()] : null));
                writers.push((answer) => {
                    if (field.name in answer) input.value = answer[field.name];
                    input.disabled = true;
                });
            } else if (field.type === 'select') {
                const select = el('select');
                select.append(el('option', null, '—'));
                select.firstChild.value = '';
                field.options.forEach((option) => {
                    const item = el('option', null, option);
                    item.value = option;
                    select.append(item);
                });
                select.value = field.default && field.options.includes(field.default) ? field.default : '';
                select.addEventListener('change', () => ctx.changed());
                row.append(select);
                readers.push(() => (select.value ? [field.name, select.value] : null));
                writers.push((answer) => {
                    if (field.name in answer) select.value = answer[field.name];
                    select.disabled = true;
                });
            } else if (field.type === 'toggle') {
                row.classList.add('field--toggle');
                const input = el('input');
                input.type = 'checkbox';
                input.checked = Boolean(field.default);
                input.addEventListener('change', () => ctx.changed());
                row.prepend(input);
                readers.push(() => [field.name, input.checked]);
                writers.push((answer) => {
                    if (field.name in answer) input.checked = Boolean(answer[field.name]);
                    input.disabled = true;
                });
            } else if (field.type === 'rank') {
                let order = [...field.options];
                const list = el('ol', 'rank');
                const draw = (locked) => {
                    list.replaceChildren(...order.map((option, at) => {
                        const item = el('li', null);
                        item.append(el('span', 'rank__label', option));
                        if (!locked) {
                            [['↑', -1], ['↓', 1]].forEach(([glyph, step]) => {
                                const move = el('button', 'rank__move', glyph);
                                move.type = 'button';
                                move.title = step < 0 ? 'move up' : 'move down';
                                move.disabled = at + step < 0 || at + step >= order.length;
                                move.addEventListener('click', (event) => {
                                    event.preventDefault();
                                    [order[at], order[at + step]] = [order[at + step], order[at]];
                                    draw(false);
                                    ctx.changed();
                                });
                                item.append(move);
                            });
                        }
                        return item;
                    }));
                };
                draw(false);
                row.append(list);
                readers.push(() => [field.name, [...order]]);
                writers.push((answer) => {
                    if (Array.isArray(answer[field.name])) order = [...answer[field.name]];
                    draw(true);
                });
            }
            box.append(row);
        });
        ctx.widgets.set(id, {
            value() {
                const entries = readers.map((read) => read()).filter(Boolean);
                return entries.length ? Object.fromEntries(entries) : undefined;
            },
            show(answer) {
                box.classList.add('is-answered');
                writers.forEach((write) => write(answer && typeof answer === 'object' ? answer : {}));
            },
        });
        return box;
    }

    /** The bar that sends every held answer as one line. */
    function submit_bar(ctx) {
        const bar = el('div', 'submit');
        const button = el('button', null, 'SEND ANSWERS');
        button.type = 'button';
        ctx.state = el('div', 'choice__state');
        const count = () => {
            const answered = [...ctx.widgets.values()].filter((widget) => widget.value() !== undefined).length;
            show_state(ctx.state, `${answered}/${ctx.widgets.size} ANSWERED`);
        };
        ctx.changed = count;
        button.addEventListener('click', () => {
            const answers = {};
            ctx.widgets.forEach((widget, id) => {
                const value = widget.value();
                if (value !== undefined) answers[id] = value;
            });
            if (!Object.keys(answers).length) {
                show_state(ctx.state, 'NOTHING TO SEND YET', 'warn');
                return;
            }
            ctx.send(answers);
        });
        bar.append(button, ctx.state);
        ctx.bar = bar;
        count();
        return bar;
    }

    function draw_doc(data) {
        const ids = (data.questions || []).map((question) => question.id);
        const kinds = [];
        const scan = (blocks) => blocks.forEach((block) => {
            if (block.type === 'choice' || block.type === 'form') kinds.push(block);
            if (block.type === 'tabs') block.tabs.forEach((tab) => scan(tab.blocks));
            else if (Array.isArray(block.blocks)) scan(block.blocks);
        });
        scan(data.doc.blocks);
        const ctx = {
            widgets: new Map(),
            state: null,
            bar: null,
            fast: kinds.length === 1 && kinds[0].type === 'choice' && !kinds[0].multi,
            changed: () => {},
            next_id: () => ids.shift() || `q${Date.now()}`,
            async send(answers) {
                show_state(ctx.state, 'SENDING…');
                try {
                    const reply = await post_reply({ answers });
                    apply_doc_answers(reply.delivered ? { answers } : null,
                        reply.queued ? { answers, reason: reply.reason } : null);
                } catch (error) {
                    show_state(ctx.state, `NOT SENT · ${error.message} — answer in the terminal`, 'warn');
                }
            },
        };
        const nodes = [];
        if (data.doc.title) nodes.push(el('h1', null, data.doc.title));
        nodes.push(...build_all(data.doc.blocks, ctx));
        if (ctx.widgets.size && !ctx.fast) nodes.push(submit_bar(ctx));
        root.replaceChildren(...nodes);
        asking = ctx.widgets.size ? ctx : null;
    }

    function apply_doc_answers(answered, queued) {
        if (!asking) return;
        if (answered) {
            asking.widgets.forEach((widget, id) => {
                if (!widget.shown) widget.show(answered.answers[id]);
                widget.shown = true;
            });
            if (asking.bar) asking.bar.querySelector('button').remove();
            // The question stays, locked, until the agent redraws: say so, or a
            // canvas that has not changed yet reads as "did my press do anything?".
            const sent = Object.keys(answered.answers).length;
            const of = sent === asking.widgets.size ? '' : ` ${sent}/${asking.widgets.size}`;
            show_state(asking.state, `SENT${of} · THE AGENT HAS IT · THIS CANVAS UPDATES WHEN IT ACTS`, 'ok');
            asking.changed = () => {};
        } else if (queued) {
            show_state(asking.state, `QUEUED · ${queued.reason}`, 'warn');
        }
    }

    function issues_strip(errors) {
        const box = el('details', 'vc-errors');
        box.append(el('summary', null, `${errors.length} ISSUE${errors.length === 1 ? '' : 'S'} · NOT DRAWN`));
        const list = el('ul');
        errors.forEach((issue) => list.append(el('li', null, `${issue.path} — ${issue.message}`)));
        box.append(list);
        box.title = 'The agent was told too (agent-canvas-check hook).';
        return box;
    }

    // ── a markup canvas ──────────────────────────────────────────────────────

    function question_of(choice) {
        if (choice.dataset.q) return choice.dataset.q;
        const clone = choice.cloneNode(true);
        clone.querySelectorAll('button, .choice__other, .choice__state').forEach((node) => node.remove());
        return clone.textContent.trim().replace(/\s+/g, ' ').slice(0, 120);
    }

    async function send_markup(choice, answer) {
        const state = choice.querySelector('.choice__state');
        show_state(state, 'SENDING…');
        try {
            const reply = await post_reply({ answer, question: question_of(choice) });
            apply_markup_answer(choice, reply.delivered ? { answers: { q1: answer } } : null,
                reply.queued ? { reason: reply.reason } : null);
        } catch (error) {
            show_state(state, `NOT SENT · ${error.message} — answer in the terminal`, 'warn');
        }
    }

    function apply_markup_answer(choice, answered, queued) {
        const state = choice.querySelector('.choice__state');
        const buttons = choice.querySelectorAll('button[data-reply]');
        if (answered) {
            const answer = answered.answers.q1;
            choice.classList.add('is-answered');
            buttons.forEach((button) => {
                button.disabled = true;
                button.classList.toggle('is-chosen', button.dataset.reply === answer);
            });
            const other = choice.querySelector('.choice__other');
            if (other) other.remove();
            show_state(state, `SENT → ${answer}`);
        } else if (queued) {
            show_state(state, `QUEUED · ${queued.reason}`, 'warn');
        }
    }

    function wire_choice(choice) {
        const buttons = [...choice.querySelectorAll('button[data-reply]')];
        const row = el('div', 'choice__buttons');
        buttons.forEach((button) => {
            button.type = 'button';
            button.addEventListener('click', () => send_markup(choice, button.dataset.reply));
            row.append(button);
        });
        choice.append(row);
        // Every question gets an escape hatch, written once here rather than by
        // every agent: a free-text answer for when no button fits.
        const other = el('div', 'choice__other');
        const input = el('input');
        input.placeholder = 'other…';
        input.maxLength = 1500;
        const go = el('button', null, 'SEND');
        go.type = 'button';
        const submit = () => {
            if (input.value.trim()) send_markup(choice, input.value.trim());
        };
        go.addEventListener('click', submit);
        input.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') submit();
        });
        other.append(input, go);
        choice.append(other, el('div', 'choice__state'));
    }

    // ── the two faces ────────────────────────────────────────────────────────

    // `vc_skills_view` is skills.js, which page.py puts first in this same script tag.
    const skills = typeof vc_skills_view === 'function' && skills_slot ? vc_skills_view({ el, tag }) : null;

    async function ask_view(target, button) {
        button.disabled = true;
        try {
            const response = await fetch(`${BASE}/view`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session, view: target }),
            });
            const reply = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(reply.error || `HTTP ${response.status}`);
            show_view(reply.view);
        } catch (error) {
            button.title = `Could not switch: ${error.message}`;
        } finally {
            button.disabled = false;
        }
    }

    function view_chip(text, target, title) {
        const button = el('button', 'vc-view', text);
        button.type = 'button';
        button.title = title;
        button.addEventListener('click', () => ask_view(target, button));
        return button;
    }

    /** The chip row. On the skills face, `●` says the canvas moved (or asks) while you read. */
    function draw_views() {
        if (!views_bar || !skills) return;
        if (view === 'canvas') {
            views_bar.replaceChildren(view_chip('◇ SKILLS', 'skills',
                'Every skill an agent here can call: project, global, synced, plugin, built-in'));
            return;
        }
        const asking = Boolean(latest && latest.question && !latest.answered);
        const moved = Boolean(latest && latest.rev !== null && latest.rev !== rev);
        const back = view_chip(asking || moved ? '◂ CANVAS ●' : '◂ CANVAS', 'canvas',
            asking ? 'Back to the canvas: the agent is asking' : moved ? 'Back to the canvas: it changed' : 'Back to the canvas');
        if (asking || moved) back.classList.add('is-news');
        views_bar.replaceChildren(back);
    }

    function show_view(next) {
        const target = skills && next === 'skills' ? 'skills' : 'canvas';
        if (target !== view) {
            view = target;
            const on_skills = view === 'skills';
            root.hidden = on_skills;
            status.hidden = on_skills;
            skills_slot.hidden = !on_skills;
            if (on_skills) {
                if (!skills.node.isConnected) skills_slot.append(skills.node);
                skills.show();
            }
        }
        // A canvas is drawn only while it shows: Mermaid cannot lay out a hidden diagram.
        if (view === 'canvas' && latest) {
            if (latest.rev !== rev) {
                rev = latest.rev;
                render(latest);
            }
            sync(latest);
        }
        draw_views();
    }

    // ── the loop ─────────────────────────────────────────────────────────────

    function render(data) {
        asking = null;
        if (data.rev === null) {
            root.replaceChildren(el('div', 'vc-empty', data.gone ? 'SESSION GONE' : 'NO CANVAS YET'));
            return;
        }
        if (data.kind === 'doc') {
            if (data.doc) draw_doc(data);
            else root.replaceChildren(el('div', 'vc-empty', 'CANVAS NOT DRAWN · see the issues above'));
        } else {
            // Parsed inert first, in a template. A <meta http-equiv="refresh"> acts the moment it is
            // inserted, and no CSP directive covers a navigation: agent markup could send the frame,
            // and whatever it put in the URL, to another site with no click.
            const holder = document.createElement('template');
            holder.innerHTML = data.html;
            holder.content.querySelectorAll('meta, base, link, script, object, embed').forEach((node) => node.remove());
            // A srcdoc frame is this origin's document; a frame of the artifact viewer uses `src`.
            holder.content.querySelectorAll('[srcdoc]').forEach((node) => node.removeAttribute('srcdoc'));
            root.replaceChildren(holder.content);
            root.querySelectorAll('a[href]').forEach((link) => {
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
            });
            root.querySelectorAll('.choice').forEach(wire_choice);
        }
        draw_diagrams();
    }

    let shown_status = '';
    function sync(data) {
        if (data.kind === 'doc') {
            apply_doc_answers(data.answered, data.queued);
        } else {
            const choice = root.querySelector('.choice');
            if (choice) apply_markup_answer(choice, data.answered, data.queued);
        }
        // Rebuilt only when it changes, so an open issues disclosure stays open.
        const key = JSON.stringify(data.errors || []) + String(data.truncated);
        if (key === shown_status) return;
        shown_status = key;
        const parts = data.truncated ? [tag('TRUNCATED', 'warn')] : [];
        if (data.errors && data.errors.length) parts.push(issues_strip(data.errors));
        set_status(parts);
    }

    function fail(text) {
        shown_status = '';
        set_status([tag(text, 'warn')]);
    }

    async function poll() {
        try {
            const response = await fetch(`${BASE}/frag?session=${encodeURIComponent(session)}`);
            if (response.ok) {
                const data = await response.json();
                latest = data;
                artifacts.update(data);
                show_view(data.view);
                hello(data);
            } else {
                fail(`CANVAS ${response.status}`);
            }
        } catch {
            fail('BRIDGE UNREACHABLE');
        }
        setTimeout(poll, document.hidden ? HIDDEN_POLL_MS : VISIBLE_POLL_MS);
    }

    if (parent_origin) {
        poll();
    } else {
        root.replaceChildren(el('div', 'vc-empty', 'CANVAS REFUSED · framed by an unknown page'));
    }
})();
