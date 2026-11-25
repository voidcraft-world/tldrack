// Runs artifact-viewer.js against a fake page and plays `artifact-save` messages into it.
// Prints {replies, saved}: what the viewer answered, and the names it saved.
// usage: node viewer-save-harness.cjs <path to artifact-viewer.js>
'use strict';
const fs = require('node:fs');
const vm = require('node:vm');

const script = fs.readFileSync(process.argv[2], 'utf8');
const base64 = text => Buffer.from(text).toString('base64');
const replies = [];
const saved = [];
const listeners = {};
const activation = { isActive: true };
let now = 1_000_000;
Date.now = () => now;
const sandbox = { postMessage: reply => replies.push(reply) };
const frame = { contentWindow: sandbox, setAttribute() {}, set srcdoc(value) {} };

globalThis.window = { addEventListener: (name, handler) => { listeners[name] = handler; } };
Object.defineProperty(globalThis, 'navigator', { value: { userActivation: activation }, configurable: true });
globalThis.document = {
    body: {
        dataset: {
            metadata: base64(JSON.stringify({ name: 'sheet.html', mime: 'text/html' })),
            payload: base64('<p>sheet</p>'),
            policy: "default-src 'none'",
        },
    },
    getElementById: () => ({ append() {} }),
    createElement: tag => tag === 'iframe' ? frame : { click() { saved.push(this.download); } },
};
vm.runInThisContext(script);

const bytes = (head, length = 64) => {
    const view = new Uint8Array(length);
    view.set(Buffer.from(head, 'latin1'));
    return view.buffer;
};
const PNG = '\x89PNG\r\n\x1a\n';
const send = (id, data, source = sandbox) => listeners.message({ source, data: { type: 'artifact-save', id, ...data } });
const later = seconds => { now += seconds * 1000; };

send('png', { name: '../../outside/My Chart!.html', bytes: bytes(PNG) });
send('webp', { name: 'sheet-web', bytes: bytes('RIFF\x00\x00\x00\x00WEBP') });
send('jpeg', { bytes: bytes('\xff\xd8\xff\xe0') });
send('html', { name: 'page.png', bytes: bytes('<!doctype html><script>') });
send('text', { name: 'a.png', bytes: PNG });
send('huge', { name: 'a.png', bytes: bytes(PNG, 16 * 1024 * 1024 + 1) });
send('stranger', { name: 'a.png', bytes: bytes(PNG) }, { postMessage: reply => replies.push({ ...reply, stranger: true }) });

// A click the viewer saw holds for a minute and 32 saves, then nothing saves until the next one.
activation.isActive = false;
later(61);
send('no-click', { name: 'a.png', bytes: bytes(PNG) });
activation.isActive = true;
listeners.message({ source: { postMessage() {} }, data: { type: 'artifact-save-click' } });
activation.isActive = false;
send('stranger-click', { name: 'a.png', bytes: bytes(PNG) });
activation.isActive = true;
listeners.message({ source: sandbox, data: { type: 'artifact-save-click' } });
activation.isActive = false;
later(30);
send('slow-encode', { name: 'slow.png', bytes: bytes(PNG) });
for (let i = 0; i < 31; i++) send(`batch-${i}`, { name: `batch-${i}`, bytes: bytes(PNG) });
send('past-budget', { name: 'a.png', bytes: bytes(PNG) });
activation.isActive = true;
listeners.message({ source: sandbox, data: { type: 'artifact-save-click' } });
activation.isActive = false;
later(61);
send('past-minute', { name: 'a.png', bytes: bytes(PNG) });

process.stdout.write(JSON.stringify({ replies, saved }));
process.exit(0);
