/** README image capture and its network isolation: owned here. See the readme-images skill for authoring. */
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync, mkdirSync, readdirSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { resolve, relative } from 'node:path';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';

const root = resolve(fileURLToPath(new URL('../../', import.meta.url)));
const require = createRequire(new URL('../../ui/package.json', import.meta.url));
const { chromium } = require('playwright');
const { createServer } = await import(pathToFileURL(require.resolve('vite')).href);
const args = process.argv.slice(2);
if (args.includes('--help')) {
    console.log('node scripts/readme-images/capture.mjs [--scene path.json] [--out directory] [--check]');
    process.exit(0);
}
function option(name, fallback) {
    const index = args.indexOf(name);
    if (index < 0) return fallback;
    if (!args[index + 1] || args[index + 1].startsWith('--')) throw new Error(`Missing value for ${name}`);
    return args[index + 1];
}
for (let i = 0; i < args.length; i += 2) {
    if (args[i] === '--check') { i -= 1; continue; }
    if (!['--scene', '--out'].includes(args[i])) throw new Error(`Unknown option: ${args[i]}`);
}
const scene_path = resolve(root, option('--scene', 'scripts/readme-images/scene.json'));
const out = resolve(root, option('--out', 'output/playwright/readme'));
const scene = JSON.parse(readFileSync(scene_path, 'utf8'));
if (!Array.isArray(scene.sessions) || !scene.sessions.some((row) => row.session === scene.focus)) throw new Error('Scene needs sessions and a matching focus');
if (!Number.isInteger(scene.viewport?.width) || !Number.isInteger(scene.viewport?.height)) throw new Error('Scene needs an integer viewport');
if (scene.viewport.width < 1200 || scene.viewport.width > 3840 || scene.viewport.height < 800 || scene.viewport.height > 2160) throw new Error('Viewport outside supported desktop sizes');
const names = new Set(scene.sessions.map((row) => row.session));
if (names.size !== scene.sessions.length) throw new Error('Duplicate session name');
for (const row of scene.sessions) {
    if (!/^[a-z0-9_-]{1,64}$/.test(row.session) || typeof row.auto_name !== 'string') throw new Error('Invalid session name or label');
    if (row.session.startsWith('svc-') && !scene.services?.some((service) => service.session === row.session)) throw new Error(`Service row without readout: ${row.session}`);
    if (row.parent && !names.has(row.parent)) throw new Error(`Unknown parent for ${row.session}`);
    if (row.worker && !scene.sessions.some((lead) => lead.session === row.parent && lead.lead?.run === row.worker.run)) throw new Error(`Worker without matching LEAD: ${row.session}`);
    if (row.merged && (!row.branch || row.state !== 'free')) throw new Error(`Merged example must be a finished worktree: ${row.session}`);
}
if (!Array.isArray(scene.annotations) || !scene.annotations.length || !scene.annotations.every((a) => ['selector', 'title', 'text'].every((key) => typeof a[key] === 'string' && a[key]))) throw new Error('Annotations need selector, title and text');
if (!Array.isArray(scene.terminal) || !scene.canvas) throw new Error('Scene needs terminal and canvas content');
function sha256(data) { return createHash('sha256').update(data).digest('hex'); }
function source_hash() {
    const hash = createHash('sha256');
    function add(path) {
        for (const entry of readdirSync(resolve(root, path), { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
            const name = `${path}/${entry.name}`;
            if (entry.isDirectory()) add(name);
            else if (entry.isFile()) { hash.update(name); hash.update(readFileSync(resolve(root, name))); }
        }
    }
    for (const path of ['ui/src', 'schemas/src', 'plugins/agent-canvas/agent_canvas/assets', 'scripts/readme-images']) add(path);
    for (const path of ['ui/index.html', 'ui/vite.config.ts', 'ui/package-lock.json', 'plugins/agent-canvas/agent_canvas/page.py']) {
        hash.update(path); hash.update(readFileSync(resolve(root, path)));
    }
    hash.update(readFileSync(scene_path));
    return hash.digest('hex');
}
const sources_sha256 = source_hash();
if (args.includes('--check')) {
    const saved = JSON.parse(readFileSync(resolve(out, 'manifest.json'), 'utf8'));
    if (saved.sources_sha256 !== sources_sha256) throw new Error('README captures are stale: regenerate after source or scene changes');
    for (const name of ['rack-demo.webp', 'rack-annotated.webp']) {
        if (sha256(readFileSync(resolve(out, name))) !== saved.images?.[name]) throw new Error(`Capture changed or missing: ${name}`);
    }
    console.log('README captures match their source and image hashes.');
    process.exit(0);
}
mkdirSync(out, { recursive: true });
const now = 1795687200;
function identity(index) { return `00000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`; }
const sessions = scene.sessions.map((row, index) => ({
    age_seconds: 12, message: null, title: null, attached: false, live: true,
    created_at: now - 3600 + index, repo: '/code/demo', cwd: row.branch ? `/worktrees/demo/${row.session}` : '/code/demo',
    context_left: 78 - index * 5, starred: false,
    ...row,
    lineage: { id: identity(index), ...(row.parent ? { parent_id: identity(scene.sessions.findIndex((p) => p.session === row.parent)), parent_name: row.parent } : {}) },
}));
const worktrees = sessions.filter((row) => row.branch).map((row, index) => ({
    session: row.session, branch: row.branch, path: row.cwd, dirty: !row.merged, ahead: row.merged ? 0 : 2, behind: 0,
    merged: !!row.merged, landed: !!row.merged, landing: row.merged ? { how: 'pr', pr: 101 + index } : null,
    pr_status: row.merged ? 'merged' : 'open', pr_url: `https://github.com/demo/demo/pull/${101 + index}`,
    creating: false, error: null, can_clean_up: !!row.merged,
}));
const runs = sessions.filter((row) => row.lead).map((row) => ({
    id: row.lead.run, title: row.auto_name, state: 'open', lead_session: row.session, lead_live: true,
    profile: row.profile, plan_path: null, created_at: now - 3600, decisions: [],
    steps: sessions.filter((worker) => worker.worker?.run === row.lead.run).map((worker) => ({
        step_id: worker.worker.step, title: worker.auto_name, kind: 'change', lane: worker.profile,
        state: worker.merged ? 'closed' : 'working', parked: false, worker_session: worker.session,
        worker_live: true, profile: worker.profile, branch: worker.branch ?? null,
        pr_url: worktrees.find((tree) => tree.session === worker.session)?.pr_url ?? null,
        ci: worker.merged ? 'passing' : 'pending',
    })),
}));
const responses = {
    '/snapshot': { sessions, profiles: [{ id: 'claude', kind: 'claude', label: 'CLAUDE', default: true, hooks_ok: true }, { id: 'codex', kind: 'codex', label: 'CODEX', hooks_ok: true }], plugins: ['worktree', 'agent-canvas', 'laya', 'devices'], launch_pick: { id: 'claude', label: 'CLAUDE', reason: 'Available' } },
    '/plugin/worktree/state': { root: '/worktrees/demo', base: 'main', worktrees, worktree_paths: worktrees.map((row) => row.path), worktree_trees: worktrees.map((row) => ({ path: row.path, branch: row.branch, landing: row.landing, pr: { number: Number(row.pr_url.split('/').at(-1)), state: row.pr_status, url: row.pr_url } })), pr_known: true },
    '/lead/state': { runs },
    '/launch-directories': { directories: [{ label: 'DEMO', path: '/code/demo' }], default_path: '/code/demo', bridge_path: '/code/demo' },
    '/profile-availability': { paused: [], error: null },
    '/activity': { entries: [] },
    '/plugin/dev-services/state': { services: scene.services ?? [] },
    '/plugin/devices/state': { sample_s: 2, devices: scene.devices ?? [] },
};
function escape_html(value) { return String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'); }
function terminal_page() {
    return `<!doctype html><meta charset="utf-8"><style>body{margin:0;padding:28px;background:#17191f;color:#e2e4ed;font:18px/1.65 ui-monospace,monospace}p{white-space:pre-wrap;margin:0 0 15px}.muted{color:#969aaa}.prompt{background:#252936;padding:16px;border-left:3px solid #a3cfc4}.heading{font-weight:bold;margin-top:22px}.success{color:#a7d7ad}.active{color:#96dbe7}.removed{color:#ef9b9b}</style>${scene.terminal.map((line) => `<p class="${escape_html(line.tone)}">${escape_html(line.text)}</p>`).join('')}`;
}
const canvas_page = execFileSync('python3', ['-c', 'import sys; sys.path.insert(0,sys.argv[1]); from agent_canvas.page import build_page; print(build_page(sys.argv[2], "demo"))', resolve(root, 'plugins/agent-canvas'), scene.focus], { encoding: 'utf8' });
const canvas_frag = {
    key: '1-1795687200', rev: 1, kind: 'html',
    html: `<h2>${escape_html(scene.canvas.title)}</h2><div class="card"><b>${escape_html(scene.canvas.progress)}</b><p>${escape_html(scene.canvas.summary)}</p></div><table><tr><th>Worker</th><th>State</th></tr>${scene.canvas.rows.map((row) => `<tr>${row.map((cell) => `<td>${escape_html(cell)}</td>`).join('')}</tr>`).join('')}</table>`,
    artifacts: scene.canvas.files.map((name, index) => ({ id: String(index).repeat(24), revision: String(index).repeat(64), name, path: `output/${name}`, bytes: 1024 })),
    view: 'canvas', errors: [], questions: [],
};
let server;
let browser;
const blocked = new Set();
const failures = [];
try {
    server = await createServer({ root: resolve(root, 'ui'), configFile: resolve(root, 'ui/vite.config.ts'), server: { host: '127.0.0.1', port: 0, strictPort: false }, logLevel: 'warn' });
    await server.listen();
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await chromium.launch({ args: ['--enable-unsafe-swiftshader'] });
    const context = await browser.newContext({ viewport: scene.viewport, deviceScaleFactor: 1, colorScheme: 'dark', reducedMotion: 'reduce', serviceWorkers: 'block' });
    // Intercept EVERY request before navigation. Only static Vite assets reach the disposable server;
    // no live bridge, ttyd, external host, WebSocket, or mutation is allowed through.
    await context.routeWebSocket('**/*', (socket) => socket.close());
    await context.route('**/*', async (route) => {
        const request = route.request();
        const url = new URL(request.url());
        if (request.method() !== 'GET') { blocked.add(`${request.method()} ${url.pathname}`); return route.fulfill({ status: 405, json: { error: 'Demo capture is read-only' } }); }
        if (url.searchParams.has('arg')) return route.fulfill({ contentType: 'text/html', body: terminal_page() });
        if (url.origin !== origin) { blocked.add(url.origin); return route.abort(); }
        if (url.pathname === '/__capture') return route.fulfill({ contentType: 'text/html', body: `<!doctype html><meta charset="utf-8"><style>body{margin:0;background:#17191f;color:#f4f5fb;font-family:Arial,sans-serif}iframe{display:block;border:0;width:${scene.viewport.width}px;height:${scene.viewport.height}px}#heading{box-sizing:border-box;height:110px;padding:26px 38px}h1{font-size:32px;margin:0 0 8px}#heading p{color:#acb6c7;margin:0;font-size:18px}</style><div id="heading"><h1>TLDRack, at a glance</h1><p>Real interface · illustrative demo sessions and transcript</p></div><iframe name="rack" src="/"></iframe><div id="annotations"></div>` });
        if (url.pathname in responses) return route.fulfill({ json: responses[url.pathname] });
        if (url.pathname === '/events') return route.fulfill({ status: 204 });
        if (url.pathname === '/plugin/agent-canvas/page') return route.fulfill({ contentType: 'text/html', body: canvas_page });
        if (url.pathname === '/plugin/agent-canvas/frag') return route.fulfill({ json: canvas_frag });
        if (url.pathname === '/plugin/agent-canvas/health') return route.fulfill({ json: { ok: true } });
        const static_asset = url.pathname === '/' || /^\/(?:src|node_modules|@vite|@id|@fs)\//.test(url.pathname);
        if (static_asset) return route.continue();
        blocked.add(url.pathname);
        return route.fulfill({ status: 404, json: { error: 'Not part of the demo scene' } });
    });
    await context.addInitScript(() => {
        if (location.pathname !== '/' || location.search) return;
        localStorage.setItem('tldrack:agent-rack-split-px', '600');
        localStorage.setItem('tldrack:agent-canvas-layout', 'column');
        localStorage.setItem('tldrack:agent-canvas-size', JSON.stringify({ column_px: 560, under_px: null }));
        localStorage.setItem('tldrack:rack-prefs-v1', JSON.stringify({ office: true, office_quality: 'light', motion: false }));
        localStorage.setItem('tldrack:rack-complete-open', '1');
        localStorage.setItem('tldrack:rack-devices-open', '1');
        localStorage.setItem('tldrack:canvas-section-height:office', '460');
        localStorage.setItem('tldrack:canvas-section-height:details', '340');
        localStorage.setItem('tldrack:canvas-section-height:document', '320');
    });
    const wrapper = await context.newPage();
    wrapper.on('pageerror', (error) => failures.push(error.message));
    await wrapper.setViewportSize({ width: scene.viewport.width, height: scene.viewport.height + 430 });
    await wrapper.goto(origin + '/__capture');
    const page = wrapper.frames().find((frame) => frame.name() === 'rack');
    if (!page) throw new Error('Capture frame did not load');
    await page.getByText(scene.sessions.find((row) => row.session === scene.focus).auto_name, { exact: true }).first().waitFor();
    await page.getByText(scene.sessions.find((row) => row.session === scene.focus).auto_name, { exact: true }).first().click();
    await page.locator('.rack__project').click();
    await page.locator('.rack__chip').filter({ hasText: 'CLAUDE' }).last().click();
    await page.locator('.row--family').hover();
    await page.locator('.artifact-shelf__file').first().waitFor();
    await page.frameLocator('.canvas__frame').getByText(scene.canvas.progress, { exact: true }).waitFor();
    await page.locator('.canvas-section[aria-label="3D office"] canvas').waitFor();
    await wrapper.waitForTimeout(1500);
    await page.evaluate(() => document.fonts.ready);
    await wrapper.locator('iframe[name="rack"]').screenshot({ path: resolve(out, 'rack-demo.png'), animations: 'disabled' });
    writeFileSync(resolve(out, 'page.txt'), await page.locator('body').innerText());
    const targets = [];
    for (const annotation of scene.annotations) {
        const locator = page.locator(annotation.selector).first();
        const box = await locator.boundingBox();
        if (!box) throw new Error(`Annotation target missing: ${annotation.selector}`);
        targets.push({ ...annotation, box: { ...box, y: box.y - 110 } });
    }
    writeFileSync(resolve(out, 'targets.json'), JSON.stringify(targets, null, 2) + '\n');
    await wrapper.evaluate(({ targets, height }) => {
        const layer = document.getElementById('annotations');
        const style = document.createElement('style');
        style.textContent = `.notes{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;padding:24px 38px}.note{border-top:2px solid #ee4b99;padding:15px 16px;background:#222631;min-height:105px;box-sizing:border-box}.note h2{font-size:21px;margin:0 0 9px}.note p{font-size:18px;line-height:1.4;color:#c4ccdb;margin:0}.pin{position:absolute;border:2px solid #fff;background:#c72370;color:white;border-radius:50%;width:30px;height:30px;text-align:center;font:bold 18px/30px Arial;box-shadow:0 0 0 3px #17191f}.outline{position:absolute;border:2px solid #ee4b99;border-radius:4px;pointer-events:none;box-sizing:border-box}`;
        document.head.append(style);
        const notes = document.createElement('div');
        notes.className = 'notes';
        layer.append(notes);
        targets.forEach((target, index) => {
            const box = target.box;
            const outline = document.createElement('div');
            outline.className = 'outline';
            Object.assign(outline.style, { left: `${box.x}px`, top: `${box.y + 110}px`, width: `${box.width}px`, height: `${box.height}px` });
            layer.append(outline);
            const pin = document.createElement('span');
            pin.className = 'pin';
            pin.textContent = String(index + 1);
            Object.assign(pin.style, { left: `${Math.max(3, box.x + box.width - 34)}px`, top: `${Math.min(height + 75, box.y + 110)}px` });
            layer.append(pin);
            const card = document.createElement('section');
            card.className = 'note';
            const title = document.createElement('h2');
            title.textContent = `${index + 1}  ${target.title}`;
            const text = document.createElement('p');
            text.textContent = target.text;
            card.append(title, text);
            notes.append(card);
        });
    }, { targets, height: scene.viewport.height });
    await wrapper.screenshot({ path: resolve(out, 'rack-annotated.png'), fullPage: true, animations: 'disabled' });
    if (failures.length) throw new Error(`Browser errors: ${failures.join('; ')}`);
    const images = {};
    for (const stem of ['rack-demo', 'rack-annotated']) {
        const filename = `${stem}.webp`;
        execFileSync('cwebp', ['-quiet', '-lossless', resolve(out, `${stem}.png`), '-o', resolve(out, filename)]);
        images[filename] = sha256(readFileSync(resolve(out, filename)));
    }
    const scene_relative = relative(root, scene_path);
    writeFileSync(resolve(out, 'manifest.json'), JSON.stringify({
        scene: scene_relative.startsWith('..') ? '(external scene)' : scene_relative,
        source_commit: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim(),
        sources_sha256, scene_sha256: sha256(readFileSync(scene_path)), viewport: scene.viewport,
        blocked: [...blocked].sort(), images,
    }, null, 2) + '\n');
    console.log(`Captured ${resolve(out, 'rack-demo.png')}`);
} finally {
    await browser?.close();
    await server?.close();
}
