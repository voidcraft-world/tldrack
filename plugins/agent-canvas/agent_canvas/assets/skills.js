// The ◇ SKILLS face of the canvas pane: every skill an agent on this machine can call, as a
// chevron tree to audit — one click per level, and a skill's files load only when its row
// opens (~100 SKILL.md bodies are a megabyte nobody asked for).
//
// The data is the claude-skills plugin's, read same-origin (`GET /state`, `GET /skill`,
// `POST /open`; plugins/claude-skills/README.md). Which face shows is the desk's
// (`desk.py` `set_view`). This file shares kit.js's one script tag (page.py) and builds with
// kit's `el` / `tag`, so every string goes in as a text node.
const vc_skills_view = (kit) => {
    'use strict';
    const { el, tag } = kit;
    const BASE = '/plugin/claude-skills';
    /** Scope bands, in reading order. Your own skills open; what you were handed starts shut. */
    const BANDS = [
        { tier: 'project', label: 'PROJECT', open: true },
        { tier: 'global', label: 'GLOBAL', open: true },
        { tier: 'synced', label: 'SYNCED', open: false, hint: 'claude.ai account skills (anthropic-skills:*)' },
        { tier: 'plugin', label: 'PLUGINS', open: false, by_plugin: true },
        { tier: 'builtin', label: 'BUILT-IN', open: false, hint: 'compiled into the CLI; no file to read' },
    ];
    /** The plugin re-reads built-ins on a thread; while it still is, ask once more. */
    const BUILTIN_RETRY_MS = 3000;
    const OFFLINE_HINT = 'Add plugins/claude-skills to ~/.config/tldrack/plugins.json, then ↻ Restart bridge';

    const node = el('section', 'sk');
    const head = el('div', 'sk__head');
    const counts = el('div', 'sk__counts');
    const tree = el('div', 'sk__tree');
    const input = el('input', 'sk__filter');
    input.type = 'search';
    input.placeholder = 'filter skills…';
    input.setAttribute('aria-label', 'Filter skills by name or description');
    const sort_name = chip('NAME', 'Sort by name');
    const sort_stale = chip('STALEST', 'Least recently edited first: the audit order');
    const reload = chip('↻', 'Re-scan the skill folders');
    head.append(counts, input, sort_name, sort_stale, reload);
    node.append(head, tree);

    let state = null;
    let loading = null;
    let sort = 'name';
    let filter = '';
    // A rebuild (filter, sort, ↻) must not close what you opened or re-fetch what you read.
    const open_rows = new Set();
    const band_open = new Map();
    const files_cache = new Map();

    function chip(text, title) {
        const button = el('button', 'sk__chip', text);
        button.type = 'button';
        button.title = title;
        return button;
    }

    input.addEventListener('input', () => {
        filter = input.value.trim().toLowerCase();
        draw();
    });
    sort_name.addEventListener('click', () => set_sort('name'));
    sort_stale.addEventListener('click', () => set_sort('stalest'));
    reload.addEventListener('click', () => load());

    function set_sort(next) {
        sort = next;
        draw();
    }

    // ── readouts ────────────────────────────────────────────────────────────
    function age(mtime) {
        if (typeof mtime !== 'number') return '';
        const seconds = Math.max(0, Date.now() / 1000 - mtime);
        const steps = [[31536000, 'y'], [2592000, 'mo'], [86400, 'd'], [3600, 'h'], [60, 'm']];
        for (const [unit, suffix] of steps) {
            if (seconds >= unit) return `${Math.floor(seconds / unit)}${suffix}`;
        }
        return 'now';
    }
    function size(bytes) {
        if (typeof bytes !== 'number') return '';
        return bytes < 1024 ? `${bytes}b` : `${(bytes / 1024).toFixed(bytes < 10240 ? 1 : 0)}k`;
    }
    const first_line = (text) => (text || '').split('\n')[0];
    const stamp = (mtime) => (typeof mtime === 'number' ? new Date(mtime * 1000).toLocaleString() : 'unknown');

    // ── loading ─────────────────────────────────────────────────────────────
    async function get_json(url, options) {
        const response = await fetch(url, options);
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
        return body;
    }

    function load(retry = true) {
        reload.disabled = true;
        loading = get_json(`${BASE}/state`)
            .then((body) => {
                state = body;
                files_cache.clear();
                draw();
                if (retry && body.builtin_status === 'scanning') setTimeout(() => load(false), BUILTIN_RETRY_MS);
            })
            .catch(() => {
                state = null;
                counts.replaceChildren();
                const offline = el('div', 'vc-empty', 'SKILLS OFFLINE · claude-skills plugin not answering');
                offline.title = OFFLINE_HINT;
                tree.replaceChildren(offline);
            })
            .finally(() => {
                loading = null;
                reload.disabled = false;
            });
        return loading;
    }

    // ── the tree ────────────────────────────────────────────────────────────
    function matches(skill) {
        if (!filter) return true;
        return [skill.name, skill.id, skill.plugin, skill.description]
            .some((value) => typeof value === 'string' && value.toLowerCase().includes(filter));
    }

    function ordered(rows) {
        const by_name = (a, b) => a.name.localeCompare(b.name);
        if (sort === 'name') return [...rows].sort(by_name);
        const when = (row) => (typeof row.mtime === 'number' ? row.mtime : Infinity);
        return [...rows].sort((a, b) => when(a) - when(b) || by_name(a, b));
    }

    /** Every account any skill names. A row on all of them carries no account tag. */
    function all_accounts() {
        const seen = new Set();
        Object.values(state.tiers || {}).forEach((rows) =>
            (rows || []).forEach((row) => (row.profiles || []).forEach((name) => seen.add(name))));
        return seen;
    }

    function draw() {
        if (!state) return;
        sort_name.setAttribute('aria-pressed', String(sort === 'name'));
        sort_stale.setAttribute('aria-pressed', String(sort === 'stalest'));
        const accounts = all_accounts();
        const tally = [];
        const bands = [];
        BANDS.forEach((band) => {
            const rows = (state.tiers && state.tiers[band.tier]) || [];
            if (!rows.length) return;
            const shown = rows.filter(matches);
            tally.push(tag(`${band.label} ${filter ? `${shown.length}/` : ''}${rows.length}`));
            if (!shown.length) return;
            const box = el('details', 'disclosure sk__band');
            const summary = el('summary', null, `${band.label} · ${shown.length}`);
            if (band.hint) summary.title = band.hint;
            box.append(summary);
            box.open = filter ? true : band_open.has(band.tier) ? band_open.get(band.tier) : band.open;
            box.addEventListener('toggle', () => {
                if (!filter) band_open.set(band.tier, box.open);
            });
            if (band.by_plugin) {
                const groups = new Map();
                shown.forEach((row) => {
                    const name = row.plugin || '?';
                    if (!groups.has(name)) groups.set(name, []);
                    groups.get(name).push(row);
                });
                [...groups.keys()].sort().forEach((name) => {
                    const group = el('details', 'disclosure sk__group');
                    const key = `plugin:${name}`;
                    group.append(el('summary', null, `${name} · ${groups.get(name).length}`));
                    group.open = filter ? true : band_open.get(key) === true;
                    group.addEventListener('toggle', () => {
                        if (!filter) band_open.set(key, group.open);
                    });
                    ordered(groups.get(name)).forEach((row) => group.append(skill_row(row, accounts)));
                    box.append(group);
                });
            } else {
                ordered(shown).forEach((row) => box.append(skill_row(row, accounts)));
            }
            bands.push(box);
        });
        counts.replaceChildren(...tally);
        tree.replaceChildren(...(bands.length ? bands : [el('div', 'vc-empty', filter ? 'NO SKILL MATCHES' : 'NO SKILLS FOUND')]));
    }

    function skill_row(skill, accounts) {
        const key = `${skill.tier}:${skill.id}`;
        const row = el('details', 'sk__skill');
        const summary = el('summary');
        summary.append(
            el('span', 'sk__name', skill.name),
            el('span', 'sk__desc', first_line(skill.description)),
            el('span', 'sk__age', age(skill.mtime)),
            el('span', 'sk__size', size(skill.bytes)),
        );
        summary.title = skill.description || skill.name;
        if (skill.superseded_by) row.classList.add('is-superseded');
        row.append(summary);
        row.addEventListener('toggle', () => {
            if (row.open) {
                open_rows.add(key);
                fill(row, skill, accounts);
            } else {
                open_rows.delete(key);
            }
        });
        if (open_rows.has(key)) {
            row.open = true;
            fill(row, skill, accounts);
        }
        return row;
    }

    function fill(row, skill, accounts) {
        if (row.dataset.filled) return;
        row.dataset.filled = '1';
        const body = el('div', 'sk__body');
        if (skill.description) body.append(el('p', 'sk__about', skill.description));
        const marks = [];
        if (skill.superseded_by) marks.push(tag(`→ /${skill.superseded_by}`, 'warn'));
        (skill.supersedes || []).forEach((target) => marks.push(tag(`replaces ${target}`)));
        const profiles = skill.profiles || [];
        if (profiles.length && profiles.length < accounts.size) marks.push(tag(`only ${profiles.join(' · ')}`));
        if (marks.length) {
            const line = el('div', 'tags');
            line.append(...marks);
            body.append(line);
        }
        if (skill.path) {
            const where = el('div', 'sk__where');
            const path = el('code', null, skill.path);
            path.title = `last edited ${stamp(skill.mtime)}`;
            where.append(path, reveal_button(skill.path));
            body.append(where);
            const files = el('div', 'sk__files');
            files.append(el('div', 'muted', 'loading files…'));
            body.append(files);
            load_files(skill.path, files);
        }
        row.append(body);
    }

    function reveal_button(path) {
        const button = chip('⌖ REVEAL', 'Show this folder in Finder');
        button.addEventListener('click', () => {
            button.disabled = true;
            get_json(`${BASE}/open`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path }),
            })
                .then(() => { button.title = 'Shown in Finder'; })
                .catch((error) => {
                    button.textContent = '⌖ FAILED';
                    button.title = error.message;
                })
                .finally(() => { button.disabled = false; });
        });
        return button;
    }

    // ── files ───────────────────────────────────────────────────────────────
    function skill_url(path, file) {
        const query = new URLSearchParams({ path });
        if (file) query.set('file', file);
        return `${BASE}/skill?${query}`;
    }

    function fetch_file(path, file) {
        const key = `${path}\u0000${file || ''}`;
        if (!files_cache.has(key)) {
            const pending = get_json(skill_url(path, file));
            pending.catch(() => files_cache.delete(key));  // a failure is not remembered
            files_cache.set(key, pending);
        }
        return files_cache.get(key);
    }

    function load_files(path, slot) {
        fetch_file(path, null)
            .then((first) => {
                const rows = first.files.map((file) => file_row(path, file, file.rel === first.file ? first : null));
                if (first.files_truncated) rows.push(el('div', 'muted', `listing stops at ${first.files.length} files`));
                slot.replaceChildren(...rows);
            })
            .catch((error) => slot.replaceChildren(el('div', 'muted', `files unavailable · ${error.message}`)));
    }

    function file_row(path, file, loaded) {
        const row = el('details', 'sk__file');
        const summary = el('summary');
        summary.append(el('span', 'sk__name', file.rel), el('span', 'sk__age', age(file.mtime)),
            el('span', 'sk__size', size(file.bytes)));
        row.append(summary);
        const show = (body) => {
            if (row.dataset.filled) return;
            row.dataset.filled = '1';
            const pre = el('pre', 'sk__text');
            pre.append(el('code', null, body.text));
            row.append(pre);
            if (body.truncated) row.append(tag('TRUNCATED · open it in your editor', 'warn'));
        };
        if (loaded) {
            row.open = true;
            show(loaded);
        }
        row.addEventListener('toggle', () => {
            if (!row.open || row.dataset.filled) return;
            fetch_file(path, file.rel)
                .then(show)
                .catch((error) => {
                    row.dataset.filled = '1';
                    row.append(el('div', 'muted', `not shown · ${error.message}`));
                });
        });
        return row;
    }

    return {
        node,
        /** Called each time the face comes up: the first one loads, later ones keep your place. */
        show() {
            if (!state && !loading) load();
        },
    };
};
