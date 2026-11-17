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
    if (metadata.mime === 'text/html') {
        const frame = document.createElement('iframe');
        frame.title = metadata.name;
        frame.setAttribute('sandbox', 'allow-scripts');
        // A later meta policy can only tighten this; output cannot relax it.
        const policy = document.body.dataset.policy.replace(/"/g, '&quot;');
        frame.srcdoc = '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="'
            + policy + '">' + new TextDecoder().decode(bytes);
        root.append(frame);
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
