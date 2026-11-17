// Shelf and preview belong to the session, independently of the canvas revision.
function create_artifact_shelf(root) {
    let revision = '';
    let external = false;
    let visible = false;
    // Older rack hosts and direct pages keep this fallback before the document.
    document.getElementById('vc-canvas').before(root);
    const dialog = document.createElement('dialog');
    dialog.className = 'artifact-preview';
    dialog.setAttribute('aria-label', 'Artifact preview');
    const close = document.createElement('button');
    close.type = 'button';
    close.textContent = 'Close preview ✕';
    close.addEventListener('click', () => dialog.close());
    const frame = document.createElement('iframe');
    frame.title = 'Generated artifact';
    frame.setAttribute('sandbox', 'allow-scripts allow-downloads');
    dialog.append(close, frame);
    document.body.append(dialog);
    dialog.addEventListener('close', () => frame.removeAttribute('src'));
    function update(data) {
        visible = data.view !== 'skills' && (!!data.artifacts?.length || !!data.artifact_error);
        root.hidden = external || !visible;
        const next = JSON.stringify([data.key, data.artifacts, data.artifact_error]);
        if (revision === next) return;
        revision = next;
        root.replaceChildren();
        const head = document.createElement('h2');
        head.textContent = `Artifacts · ${data.artifacts?.length || 0}`;
        root.append(head);
        if (data.artifact_error) {
            const error = document.createElement('p');
            error.className = 'artifact-error';
            error.textContent = data.artifact_error;
            root.append(error);
        }
        for (const file of data.artifacts || []) {
            const row = document.createElement('div');
            row.className = 'artifact-row';
            const label = document.createElement('div');
            label.className = 'artifact-label';
            const name = document.createElement('strong');
            name.textContent = file.name;
            name.title = file.path;
            const info = document.createElement('small');
            info.textContent = file.error || `${Math.ceil(file.bytes / 1024)} KB · ${new Date(file.updated * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
            if (file.error) info.className = 'artifact-error';
            label.append(name, info);
            row.append(label);
            if (!file.error) {
                const url = `/plugin/agent-canvas/artifact?key=${encodeURIComponent(data.key)}&id=${encodeURIComponent(file.id)}`;
                const preview = document.createElement('button');
                preview.type = 'button';
                preview.textContent = 'Preview';
                preview.setAttribute('aria-label', `Preview ${file.name}`);
                preview.addEventListener('click', () => { frame.src = url; dialog.showModal(); });
                const open = document.createElement('a');
                open.textContent = 'Open ↗';
                open.href = url;
                open.target = '_blank';
                open.rel = 'noopener noreferrer';
                open.setAttribute('aria-label', `Open ${file.name} in a new tab`);
                row.append(preview, open);
            }
            root.append(row);
        }
    }
    function set_external(value) {
        external = value;
        root.hidden = external || !visible;
        if (external && dialog.open) dialog.close();
    }
    return { update, set_external };
}
