// Runs only in the trusted artifact wrapper; output HTML stays in the sandbox below.
(() => {
    'use strict';
    const decode = value => Uint8Array.from(atob(value), char => char.charCodeAt(0));
    const metadata = JSON.parse(new TextDecoder().decode(decode(document.body.dataset.metadata)));
    const bytes = decode(document.body.dataset.payload);
    const root = document.getElementById('preview');
    document.title = metadata.name;
    document.getElementById('name').textContent = metadata.name;
    // The download link is never a typed blob. A blob URL has THIS page's origin, so an HTML or SVG one
    // opened as a page (Open link in new tab) would run the output's script outside the sandbox below.
    const url = URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' }));
    const download = document.getElementById('download');
    download.href = url;
    download.download = metadata.name;

    // The image-save channel: owned by artifact_page.py — see the note there.
    const SAVE_LIMIT = 16 * 1024 * 1024;
    const CLICK_WINDOW_MS = 60000;
    const CLICK_SAVES = 32;
    const CLICK_BYTES = 64 * 1024 * 1024;
    let click_until = 0;
    let click_saves = 0;
    let click_bytes = 0;
    // Armed once a window and never refilled inside one, so a page cannot stretch a click into more.
    function arm_click() {
        if (!navigator.userActivation?.isActive || Date.now() < click_until) return;
        click_until = Date.now() + CLICK_WINDOW_MS;
        click_saves = CLICK_SAVES;
        click_bytes = CLICK_BYTES;
    }
    function refuse_click(size) {
        arm_click();
        if (Date.now() >= click_until) return 'saving needs a click: press the button again';
        if (click_saves < 1 || size > click_bytes) return 'this click has saved its limit: press the button again in a minute';
        return '';
    }
    const starts_with = (view, offset, text) => [...text].every((char, i) => view[offset + i] === char.charCodeAt(0));
    function image_extension(view) {
        if (view.length < 12) return '';
        if (starts_with(view, 0, '\x89PNG\r\n\x1a\n')) return 'png';
        if (starts_with(view, 0, '\xff\xd8\xff')) return 'jpg';
        if (starts_with(view, 0, 'RIFF') && starts_with(view, 8, 'WEBP')) return 'webp';
        return '';
    }
    function save_name(name, extension) {
        const base = String(name ?? '').split(/[\\/]/).pop().replace(/\.[A-Za-z0-9]{1,5}$/, '');
        const clean = base.replace(/[^A-Za-z0-9._ -]/g, '_').replace(/^[. ]+/, '').slice(0, 120);
        return `${clean || 'image'}.${extension}`;
    }
    function refuse_save(data) {
        if (!(data.bytes instanceof ArrayBuffer) || !data.bytes.byteLength) return 'no image bytes were sent';
        if (data.bytes.byteLength > SAVE_LIMIT) return 'the image is larger than 16 MiB';
        if (!image_extension(new Uint8Array(data.bytes))) return 'only PNG, JPEG and WebP images can be saved';
        return refuse_click(data.bytes.byteLength);
    }
    function listen_for_saves(frame) {
        window.addEventListener('message', event => {
            const data = event.data;
            if (event.source !== frame.contentWindow) return;
            if (data?.type === 'artifact-save-click') arm_click();
            if (data?.type !== 'artifact-save') return;
            const error = refuse_save(data);
            const reply = { type: 'artifact-save-result', id: String(data.id ?? '').slice(0, 64), ok: !error };
            if (error) {
                reply.error = error;
            } else {
                click_saves -= 1;
                click_bytes -= data.bytes.byteLength;
                reply.name = save_name(data.name, image_extension(new Uint8Array(data.bytes)));
                const link = document.createElement('a');
                link.href = URL.createObjectURL(new Blob([data.bytes], { type: 'application/octet-stream' }));
                link.download = reply.name;
                link.click();
                setTimeout(() => URL.revokeObjectURL(link.href), 4000);
            }
            // The sandbox below has an opaque origin, so '*' is the only target that reaches it.
            event.source.postMessage(reply, '*');
        });
    }

    if (metadata.mime === 'text/html') {
        const frame = document.createElement('iframe');
        frame.title = metadata.name;
        frame.setAttribute('sandbox', 'allow-scripts');
        // A later meta policy can only tighten this; output cannot relax it.
        const policy = document.body.dataset.policy.replace(/"/g, '&quot;');
        frame.srcdoc = '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="'
            + policy + '">' + new TextDecoder().decode(bytes);
        root.append(frame);
        listen_for_saves(frame);
    } else if (metadata.mime.startsWith('image/')) {
        const image = document.createElement('img');
        const image_url = URL.createObjectURL(new Blob([bytes], { type: metadata.mime }));
        image.src = image_url;
        window.addEventListener('pagehide', () => URL.revokeObjectURL(image_url), { once: true });
        image.alt = metadata.name;
        root.append(image);
    } else {
        const content = document.createElement(metadata.mime === 'application/pdf' ? 'p' : 'pre');
        content.textContent = metadata.mime === 'application/pdf'
            ? 'Download this PDF to open it in your PDF reader.' : new TextDecoder().decode(bytes);
        root.append(content);
    }
    window.addEventListener('pagehide', () => URL.revokeObjectURL(url), { once: true });
})();
