"""Patched ttyd index page: inject the rack's pane script.

ttyd serves a single self-contained index.html (~730 KB, everything inlined) and
has no on-disk copy — the only way to get it is to fetch it from a running ttyd.
On first run per (ttyd version, script revision) this module launches a scratch
ttyd on a random loopback port, fetches `/`, injects INJECTED_SCRIPT before the
final </body>, and caches the result under ~/.cache/tldrack/. main.py
then serves it via `ttyd -I <path>`.

Every failure degrades gracefully to ttyd's stock page: the terminal still works,
you only lose the live font size, the Shift+Enter newline, the hidden
scrollbar (see the style sheet in INJECTED_SCRIPT), copy-on-select, image attachments, ◧ DRAW,
◇ SKILLS, ⟳ SYNC, ⇡ DEPS, ✓ CHECKS, ⤒ TOP and the work-persona MODE selector.

Copy-on-select: a tmux copy reaches this page only as an OSC 52 escape, which
ttyd's xterm.js drops. The script handles it. Why, and the read-refusal rule:
owned by ui/src/engine/terminal/terminal-clipboard.ts — see the note there.

Contract with the page that frames ttyd (keep the string literals in sync):
  'voidcraft-terminal-config'    ↔ ui/src/events/messages.ts MESSAGE_TERMINAL_CONFIG
  'voidcraft-terminal-font-step' ↔ ui/src/events/messages.ts MESSAGE_TERMINAL_FONT_STEP
  'voidcraft-terminal-key'       ↔ ui/src/events/messages.ts MESSAGE_TERMINAL_KEY
The config message carries `font_size` (device px), applied live to xterm, and
Cmd/Ctrl + = / − / 0 are consumed here as font-size steps so they never trigger
browser page zoom while the terminal has focus.

Soft keys ('voidcraft-terminal-key') back the REMOTE rack's on-screen key bar:
an Android keyboard has no Esc, Tab, Ctrl or arrows, and Claude Code needs Esc
and Ctrl-C constantly. **The message carries a TOKEN and this file owns the byte
sequence — never the other way round.** A caller-supplied byte string would be a
writable tty for whoever can post to this page, which is precisely the drive-by
RCE `-O` exists to stop. SOFT_KEYS is closed, and deliberately contains no Enter
and nothing printable — Up-then-Enter is "re-run the last command", so a table
with both would be command execution.

Who can post: only the frame that holds this page, and only from loopback or an
origin the operator allowed (`trusted_parents`, filled in per bridge by
`configured_script`). The page's response refuses any other parent already
(`frame-ancestors`); this check also covers a hostile frame BESIDE the terminal on
a page that frames it legitimately. Who may frame a bridge page: owned by
tldrack/framing.py — see the note there.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import click

CACHE_DIR = Path.home() / ".cache" / "tldrack"
FETCH_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.1

# Plain ES5-ish, runs as the last <script> in <body>: the message listener is
# registered at parse time, so it is guaranteed live before the iframe's `load`
# event fires in the parent (which is when the parent pushes config).
INJECTED_SCRIPT = r"""
<script>
/* tldrack patch — see bridge/tldrack/index_patch.py */
(function () {
    'use strict';
    var pending_font_size = null;
    var image_bridge_port = 7682;
    var trusted_parents = [];

    /* ttyd loads its addons but exposes none of them on window, so they are
       found by SHAPE on xterm's (unmangled) addon manager. Null when the
       private API drifts — every caller degrades. */
    function find_addon(term, has_shape) {
        try {
            var addons = term._addonManager._addons;
            for (var i = 0; i < addons.length; i++) {
                var inst = addons[i] && addons[i].instance;
                if (inst && has_shape(inst)) return inst;
            }
        } catch (err) { /* private API drifted */ }
        return null;
    }

    /* ttyd 1.7.x refits via a ResizeObserver on the terminal container — a
       fontSize change doesn't resize the container, so we must call the fit
       addon ourselves; without it, fall back to a resize event. */
    function find_fit_addon(term) {
        return find_addon(term, function (inst) {
            return typeof inst.fit === 'function' && typeof inst.proposeDimensions === 'function';
        });
    }

    /* Live font resize: set xterm's fontSize, then refit so cols/rows match
       the new glyph size (xterm's onResize tells ttyd, ttyd tells tmux).
       window.term may not exist yet when config arrives — the poll below
       applies the pending value once it does. */
    function apply_font_size() {
        var term = window.term;
        if (!term || !term.options || pending_font_size === null) return;
        if (term.options.fontSize === pending_font_size) return;
        term.options.fontSize = pending_font_size;
        var fit = find_fit_addon(term);
        if (fit) fit.fit();
        else window.dispatchEvent(new Event('resize'));
    }

    /* Soft-key table for the remote rack's on-screen key bar. CLOSED, and the
       sequences live HERE rather than in the message: accepting caller-supplied
       bytes would hand a writable tty to whoever can post to this page.

       No Enter, and nothing printable — see the module docstring. Up/Down are
       included because scrolling an agent's history is worth the residual
       annoyance risk; Enter alongside them would not be. */
    /* NOTE: INJECTED_SCRIPT is a RAW python string, so these escapes reach the
       browser verbatim and are interpreted by JS — single backslashes, not
       doubled. (Doubling them would send the literal text "\x1b".) */
    var SOFT_KEYS = {
        escape: '\x1b',
        tab: '\t',
        ctrl_c: '\x03',
        up: '\x1b[A',
        down: '\x1b[B'
    };

    function send_soft_key(token) {
        var term = window.term;
        if (!term || typeof term.input !== 'function') return;
        /* hasOwnProperty, not a bare lookup: a token of 'constructor' would
           otherwise pull a FUNCTION off Object.prototype and be sent as text. */
        if (!Object.prototype.hasOwnProperty.call(SOFT_KEYS, token)) return;
        term.input(SOFT_KEYS[token]);
        if (typeof term.focus === 'function') term.focus();
    }

    /* Only the frame that holds this page, on loopback or an origin the operator
       allowed, may drive it (module docstring). The loopback test is the core
       guard's is_loopback_origin, ported: a browser spells an origin one way only. */
    var LOOPBACK_ORIGIN = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?$/;
    function from_trusted_parent(event) {
        if (window.parent === window || event.source !== window.parent) return false;
        var origin = String(event.origin);
        return LOOPBACK_ORIGIN.test(origin) || trusted_parents.indexOf(origin) !== -1;
    }

    window.addEventListener('message', function (event) {
        if (!from_trusted_parent(event)) return;
        var data = event.data;
        if (data && data.type === 'voidcraft-terminal-key') {
            send_soft_key(data.key);
            return;
        }
        if (!data || data.type !== 'voidcraft-terminal-config') return;
        var font_size = Number(data.font_size);
        if (font_size >= 8 && font_size <= 120) {
            pending_font_size = Math.round(font_size);
            apply_font_size();
        }
    });

    /* Hide xterm's scrollbar. ttyd 1.7.7 ships
       `.xterm .xterm-viewport{overflow-y:scroll}` and no `color-scheme`, so
       every pane drew an always-on scrollbar in the LIGHT system theme — the
       white strip down the right edge of the agent rack. It can never scroll
       anything: tmux runs on the alternate screen, where xterm keeps no
       scrollback (tmux's own copy-mode is the history). Hiding it also zeroes
       xterm's measured scrollBarWidth, so the fit addon stops reserving 15 px
       for it. `color-scheme: dark` keeps any other native control dark. */
    (function inject_style() {
        var style = document.createElement('style');
        style.textContent = ':root { color-scheme: dark; }\n' +
                            '.xterm .xterm-viewport { scrollbar-width: none; }\n' +
                            '.xterm .xterm-viewport::-webkit-scrollbar { display: none; }\n';
        document.head.appendChild(style);
    })();

    /* Cmd/Ctrl + = / − / 0 → font-size step/reset, consumed here so the browser
       never sees its page-zoom shortcut while the terminal has focus. The parent
       owns the size: it clamps, persists, and pushes the new value back down via
       the config message above. */
    document.addEventListener('keydown', function (e) {
        if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
        var step;
        if (e.key === '=' || e.key === '+') step = 1;
        else if (e.key === '-' || e.key === '_') step = -1;
        else if (e.key === '0') step = 0;
        else return;
        e.preventDefault();
        e.stopImmediatePropagation();
        window.parent.postMessage({ type: 'voidcraft-terminal-font-step', step: step }, '*');
    }, true);

    /* ── Copy on select: tmux copy-mode → OSC 52 → the clipboard ─────────
       OSC 52 decode: a port of ui/src/engine/terminal/terminal-clipboard.ts — see the note there. */
    var OSC52_SELECTION_RE = /^[cpqs0-7]*$/;

    function decode_osc52_write(data) {
        var sep = data.indexOf(';');
        if (sep < 0) return null;
        var selection = data.slice(0, sep);
        var payload = data.slice(sep + 1);
        if (!OSC52_SELECTION_RE.test(selection)) return null;
        if (payload === '' || payload === '?') return null;  // '?' is a READ request: never answered
        var binary;
        try { binary = atob(payload); } catch (err) { return null; }
        var bytes = new Uint8Array(binary.length);
        for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
        return new TextDecoder('utf-8').decode(bytes);
    }

    function exec_command_copy(text, term) {
        var area = document.createElement('textarea');
        area.value = text;
        area.setAttribute('readonly', '');
        area.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none;';
        document.body.appendChild(area);
        area.select();
        var ok = false;
        try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
        document.body.removeChild(area);
        if (typeof term.focus === 'function') term.focus();
        return ok;
    }

    function write_clipboard(text, term, done) {
        var fallback = function () { done(exec_command_copy(text, term)); };
        try {
            navigator.clipboard.writeText(text).then(function () { done(true); }, fallback);
        } catch (err) {
            fallback();  // no navigator.clipboard at all (insecure origin)
        }
    }

    /* ttyd's own ✂ — the mark its native copy-on-select already flashes. */
    function show_copy_result(term, ok) {
        var overlay = find_addon(term, function (inst) {
            return typeof inst.showOverlay === 'function';
        });
        if (overlay) overlay.showOverlay(ok ? '✂' : '✂ blocked', ok ? 200 : 1200);
    }

    function install_osc52_copy(term) {
        if (!term.parser || typeof term.parser.registerOscHandler !== 'function') return;
        term.parser.registerOscHandler(52, function (data) {
            var text = decode_osc52_write(data);
            if (text !== null) {
                write_clipboard(text, term, function (ok) { show_copy_result(term, ok); });
            }
            return true;
        });
    }

    /* Clipboard images: the storage and delivery contract is owned by images.py.
       Capture at WINDOW before xterm's text-only paste listener consumes it.
       Only a real paste or file-picker change may read bytes; parent messages
       never trigger uploads or supply terminal input. */
    var image_busy = false;
    var image_button = null;
    var image_notice = null;

    function image_result(message) {
        if (image_notice) image_notice.textContent = message;
    }

    function attachment_url(path) {
        var url = new URL(window.location.href);
        // The tailnet mapping is owned by agent-rack/rack-remote.ts.
        // imageBridgePort supports nonstandard status-listener mappings.
        var override = Number(url.searchParams.get('imageBridgePort'));
        var loopback = ['localhost', '127.0.0.1', '[::1]'].indexOf(url.hostname) !== -1;
        url.port = String(override > 0 && override <= 65535 && override % 1 === 0
            ? override : loopback ? image_bridge_port : 8443);
        url.pathname = path;
        url.search = '';
        url.hash = '';
        return url.href;
    }

    function upload_image(file, term) {
        return new Promise(function (resolve, reject) {
            if (!file.size || file.size > 8 * 1024 * 1024) {
                reject(new Error('Image is empty or exceeds 8 MB'));
                return;
            }
            var reader = new FileReader();
            reader.onerror = function () { reject(new Error('Could not read image')); };
            reader.onload = function () {
                fetch(attachment_url('/images'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ data: String(reader.result).split(',')[1] }),
                    signal: AbortSignal.timeout(30000)
                }).then(function (response) {
                    return response.json().then(function (result) {
                        if (!response.ok) throw new Error(result.error || 'Image upload failed');
                        if (typeof result.paste !== 'string') throw new Error('Bridge returned no image path');
                        if (term !== window.term || term.options.disableStdin) {
                            throw new Error('Terminal changed; paste the image again');
                        }
                        term.paste(result.paste);
                        resolve();
                    });
                }).catch(reject);
            };
            reader.readAsDataURL(file);
        });
    }

    function attach_images(files) {
        var term = window.term;
        if (!term || typeof term.paste !== 'function' || term.options.disableStdin) {
            image_result('Terminal is not writable');
            return;
        }
        if (image_busy) { image_result('Wait for the current image upload'); return; }
        image_busy = true;
        image_button.disabled = true;
        var uploaded = 0;
        var chain = Promise.resolve();
        files.forEach(function (file) {
            chain = chain.then(function () {
                image_result('Uploading image ' + (uploaded + 1) + '/' + files.length + '…');
                return upload_image(file, term).then(function () { uploaded++; });
            });
        });
        chain.then(function () {
            image_result(uploaded + ' image path(s) added to prompt');
            term.focus();
        }, function (error) {
            image_result((uploaded ? uploaded + ' added; ' : '') + error.message);
        }).then(function () { image_busy = false; image_button.disabled = false; });
    }

    /* The tmux session this page shows, from ttyd's ?arg= (`attach:` is the
       attach-only grammar, owned by main.py ttyd_session_script). */
    function arg_session() {
        var arg = new URL(window.location.href).searchParams.get('arg') || '';
        if (arg.indexOf('attach:') === 0) arg = arg.slice(7);
        return /^[A-Za-z0-9_-]{1,64}$/.test(arg) ? arg : null;
    }

    /* ◧ DRAW: why it pastes, and what, is owned by plugins/agent-canvas/agent_canvas/draw.py. */
    function request_drawing(button) {
        var term = window.term;
        if (!term || typeof term.paste !== 'function' || term.options.disableStdin) {
            image_result('Terminal is not writable');
            return;
        }
        var session = arg_session();
        if (!session) { image_result('No session named in this pane'); return; }
        button.disabled = true;
        fetch(attachment_url('/plugin/agent-canvas/draw') + '?session=' + encodeURIComponent(session), {
            signal: AbortSignal.timeout(10000)
        }).then(function (response) {
            return response.json().then(function (result) {
                if (!response.ok) throw new Error(result.error || 'Canvas plugin not answering');
                if (typeof result.paste !== 'string') throw new Error('Bridge returned no canvas request');
                if (term !== window.term || term.options.disableStdin) {
                    throw new Error('Terminal changed; press DRAW again');
                }
                term.paste(result.paste);
                image_result('Canvas request added to prompt');
                term.focus();
            });
        }).catch(function (error) { image_result(error.message); })
            .finally(function () { button.disabled = false; });
    }

    /* ◇ SKILLS: flips THIS pane's agent canvas to the skills audit and back. Nothing is typed.
       Why the view lives on the bridge: plugins/agent-canvas/agent_canvas/desk.py `set_view`. */
    function toggle_skills(button) {
        var session = arg_session();
        if (!session) { image_result('No session named in this pane'); return; }
        button.disabled = true;
        fetch(attachment_url('/plugin/agent-canvas/view'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session: session, view: 'toggle' }),
            signal: AbortSignal.timeout(10000)
        }).then(function (response) {
            return response.json().then(function (result) {
                if (!response.ok) throw new Error(result.error || 'Canvas plugin not answering');
                image_result(result.view === 'skills' ? 'Skills on the canvas' : 'Canvas back');
            });
        }).catch(function (error) { image_result(error.message); })
            .finally(function () { button.disabled = false; });
    }

    /* Starts an allow-listed skill in its OWN rack session, never in this pane: nothing is typed.
       What may run, and the one-session rule: plugins/claude-skills/claude_skills/run.py. */
    function start_skill(button, skill) {
        button.disabled = true;
        image_result('Starting ' + skill + '…');
        fetch(attachment_url('/plugin/claude-skills/run'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ skill: skill }),
            signal: AbortSignal.timeout(20000)
        }).then(function (response) {
            return response.json().then(function (result) {
                if (!response.ok) throw new Error(result.error || 'Skills plugin not answering');
                image_result(skill + (result.created ? ' started: see its row in the rack'
                    : ' is already running: see its row in the rack'));
            });
        }).catch(function (error) { image_result(error.message); })
            .finally(function () { button.disabled = false; });
    }

    /* A button that starts a skill is drawn only when this rack may run that skill. The allow-list
       is the operator's and is empty by default (plugins/claude-skills/claude_skills/run.py; it is
       served as `runnable` on GET /state), and /run refuses anything else, so a button drawn
       without asking would be a control that can only fail. No plugin, or no answer: no buttons. */
    function draw_runnable(controls, row, before, buttons) {
        fetch(attachment_url('/plugin/claude-skills/state'), { signal: AbortSignal.timeout(10000) })
            .then(function (response) { return response.ok ? response.json() : {}; })
            .then(function (state) {
                var keys = {};
                (Array.isArray(state.runnable) ? state.runnable : []).forEach(function (entry) {
                    if (entry && typeof entry.key === 'string') keys[entry.key] = true;
                });
                var drawn = 0;
                buttons.forEach(function (pair) {
                    if (keys[pair[0]] === true) { row.appendChild(pair[1]); drawn++; }
                });
                if (drawn) controls.insertBefore(row, before);
            }).catch(function () {});
    }

    /* ⟳ SYNC: armed — the first press only asks, a second within SYNC_ARM_MS starts sync-main —
       because the skill merges on GitHub. It replaced ⇄ MERGE PRS (its first half): two buttons
       would start two agents on main. Deploy and push wait for a press inside the session. */
    var SYNC_ARM_MS = 4000;
    var SYNC_LABEL = '⟳ SYNC';
    var sync_armed = null;
    function press_sync(button) {
        if (sync_armed === null) {
            button.textContent = 'CONFIRM SYNC?';
            image_result('Press again: merges the open PRs, pulls main, cleans up merged branches + worktrees');
            sync_armed = setTimeout(function () {
                sync_armed = null;
                button.textContent = SYNC_LABEL;
                image_result('');
            }, SYNC_ARM_MS);
            return;
        }
        clearTimeout(sync_armed);
        sync_armed = null;
        button.textContent = SYNC_LABEL;
        start_skill(button, 'sync-main');
    }

    /* Work persona, top left: how hard THIS session's agent verifies. Nothing is typed —
       the bridge answers the agent's next UserPromptSubmit hook with it. The catalog and
       the why: bridge/tldrack/personas.py. Loaded once the terminal
       opens (and on focus), so a pick made from another surface shows here.

       Drawn as ONE mode selector, not as more buttons: the top-right row is actions,
       these are three settings of one dial. A shared rail, a radio dot, and a rigor
       meter whose pips count up the catalog order (fast → careful) is what ties the
       three together; the hue ramp is what tells them apart at a glance. */
    var PERSONA_HUES = ['#e0a650', '#5fc4b8', '#7fa8e8'];  // fast → careful; extra modes reuse the last
    var PERSONA_DIM = '#6d8889';
    var persona_row = null;
    var persona_buttons = [];
    var persona_active = null;
    var persona_busy = false;

    function paint_personas() {
        var n = persona_buttons.length;
        persona_buttons.forEach(function (entry) {
            var on = entry.id === persona_active;
            var hue = entry.hue;
            entry.button.setAttribute('aria-checked', on ? 'true' : 'false');
            entry.button.style.background = on ? 'rgba(255,255,255,0.06)' : 'transparent';
            entry.button.style.boxShadow = on ? 'inset 2px 0 0 ' + hue : 'none';
            entry.dot.textContent = on ? '●' : '○';
            entry.dot.style.color = on ? hue : PERSONA_DIM;
            entry.name.style.color = on ? '#eafcfc' : PERSONA_DIM;
            var pips = '';
            for (var i = 0; i < n; i++) pips += i <= entry.rank ? '▮' : '▯';
            entry.meter.textContent = pips;
            entry.meter.style.color = on ? hue : '#3d5557';
        });
        if (persona_row) persona_row.style.borderColor = persona_active ? '#527476' : '#2b4446';
    }

    function set_persona(id) {
        var session = arg_session();
        if (!session) { image_result('No session named in this pane'); return; }
        if (persona_busy) return;
        persona_busy = true;
        var next = id === persona_active ? null : id;
        fetch(attachment_url('/persona'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session: session, persona: next }),
            signal: AbortSignal.timeout(10000)
        }).then(function (response) {
            return response.json().then(function (result) {
                if (!response.ok) throw new Error(result.error || 'Persona not saved — restart the bridge');
                persona_active = result.persona || null;
                paint_personas();
                var picked = persona_buttons.filter(function (entry) { return entry.id === persona_active; })[0];
                image_result(picked ? picked.label + ' from your next prompt' : 'Persona off from your next prompt');
            });
        }).catch(function (error) { image_result(error.message); })
            .finally(function () { persona_busy = false; });
    }

    function render_personas(catalog) {
        if (persona_buttons.length || !Array.isArray(catalog)) return;
        var head = document.createElement('div');
        head.textContent = 'MODE';
        head.style.cssText = 'font-size:10px;letter-spacing:0.18em;color:' + PERSONA_DIM + ';padding:0 6px 2px;';
        persona_row.appendChild(head);
        catalog.forEach(function (card, rank) {
            var button = document.createElement('button');
            button.setAttribute('role', 'radio');
            button.setAttribute('aria-label', card.label + ' mode');
            button.title = card.title + '. Applies from the next prompt; press the lit one to turn it off';
            button.style.cssText = 'font:inherit;color:inherit;background:transparent;border:0;padding:3px 6px;cursor:pointer;white-space:nowrap;display:flex;align-items:center;gap:8px;width:100%;text-align:left;';
            var dot = document.createElement('span');
            var name = document.createElement('span');
            var meter = document.createElement('span');
            name.textContent = card.glyph + ' ' + card.label;
            name.style.cssText = 'flex:1;min-width:6.5em;letter-spacing:0.06em;';
            meter.style.cssText = 'font-size:10px;letter-spacing:1px;';
            meter.setAttribute('aria-hidden', 'true');
            button.appendChild(dot);
            button.appendChild(name);
            button.appendChild(meter);
            button.addEventListener('click', function () { set_persona(card.id); });
            persona_row.appendChild(button);
            persona_buttons.push({ id: card.id, label: card.label, rank: rank, button: button,
                                   dot: dot, name: name, meter: meter,
                                   hue: PERSONA_HUES[Math.min(rank, PERSONA_HUES.length - 1)] });
        });
        persona_row.style.display = 'flex';
    }

    function load_personas() {
        var session = arg_session();
        if (!session || !persona_row) return;
        fetch(attachment_url('/persona') + '?session=' + encodeURIComponent(session), {
            signal: AbortSignal.timeout(10000)
        }).then(function (response) {
            if (!response.ok) return;  // an older bridge: no chips, nothing else lost
            return response.json().then(function (result) {
                render_personas(result.personas);
                persona_active = result.persona || null;
                paint_personas();
            });
        }).catch(function () { /* bridge down: the chips wait for the next focus */ });
    }

    function install_personas() {
        persona_row = document.createElement('div');
        persona_row.setAttribute('aria-label', 'Work persona');
        persona_row.setAttribute('role', 'radiogroup');
        // Hidden until the catalog arrives: an older bridge draws no empty frame.
        persona_row.style.cssText = 'position:fixed;left:8px;top:4px;z-index:20;display:none;flex-direction:column;align-items:stretch;gap:1px;padding:4px 2px 4px 0;background:#091419;border:1px solid #2b4446;border-left-width:2px;font:12px monospace;color:#c6dddd;';
        document.body.appendChild(persona_row);
        window.addEventListener('focus', load_personas);
    }
    install_personas();

    function install_image_paste() {
        /* Two rows: this pane's tools on top; below, the buttons that start a repo agent in its own
           session, when the rack may run any (draw_runnable). */
        var controls = document.createElement('div');
        controls.style.cssText = 'position:fixed;right:8px;top:4px;z-index:20;display:flex;flex-direction:column;align-items:flex-end;gap:4px;max-width:90%;font:12px monospace;color:#c6dddd;';
        var pane_row = document.createElement('div');
        var agent_row = document.createElement('div');
        pane_row.style.cssText = 'display:flex;gap:8px;align-items:center;background:#091419;';
        agent_row.style.cssText = pane_row.style.cssText;
        image_notice = document.createElement('span');
        image_notice.setAttribute('role', 'status');
        image_notice.setAttribute('aria-live', 'polite');
        image_button = document.createElement('button');
        image_button.textContent = '+ IMAGE';
        image_button.title = 'Attach image — or paste an image here (up to 8 MB)';
        image_button.setAttribute('aria-label', 'Attach image');
        image_button.style.cssText = 'font:inherit;color:inherit;background:transparent;border:1px solid #527476;padding:4px 8px;cursor:pointer;white-space:nowrap;';
        var draw_button = document.createElement('button');
        draw_button.textContent = '◧ DRAW';
        draw_button.title = 'Ask this agent to draw what it just did on its canvas. Pastes the request into your prompt: add a focus, then Enter';
        draw_button.setAttribute('aria-label', 'Ask the agent to draw on its canvas');
        draw_button.style.cssText = image_button.style.cssText;
        draw_button.addEventListener('click', function () { request_drawing(draw_button); });
        var skills_button = document.createElement('button');
        skills_button.textContent = '◇ SKILLS';
        skills_button.title = "Show every skill an agent here can call on this pane's canvas: project, global, synced, plugin, built-in. Press again for the canvas";
        skills_button.setAttribute('aria-label', 'Show the skills on the canvas');
        skills_button.style.cssText = image_button.style.cssText;
        skills_button.addEventListener('click', function () { toggle_skills(skills_button); });
        var sync_button = document.createElement('button');
        sync_button.textContent = SYNC_LABEL;
        sync_button.title = 'Start an agent that merges every open PR that can merge (resolving conflicts), pulls main, then cleans up the merged branches and worktrees (the Branch Harbor CLEANUP). It asks once before it deploys dev, fixes small gate failures and pushes. Press twice';
        sync_button.setAttribute('aria-label', 'Merge the open pull requests, pull main and clean up merged branches');
        sync_button.style.cssText = image_button.style.cssText;
        sync_button.addEventListener('click', function () { press_sync(sync_button); });
        // ⇡ DEPS needs no arm: the skill changes nothing until the operator approves its plan.
        var deps_button = document.createElement('button');
        deps_button.textContent = '⇡ DEPS';
        deps_button.title = 'Start an agent that audits every npm and Python dependency in the repo, shows one update plan, and applies only what you approve. It never commits or deploys';
        deps_button.setAttribute('aria-label', 'Audit and update the third-party dependencies');
        deps_button.style.cssText = image_button.style.cssText;
        deps_button.addEventListener('click', function () { start_skill(deps_button, 'deps-update'); });
        // ✓ CHECKS needs no arm: it edits the working tree only, never commits, pushes or deploys.
        var checks_button = document.createElement('button');
        checks_button.textContent = '✓ CHECKS';
        checks_button.title = 'Start an agent that runs every deploy check (verify --env dev --all: lint, typecheck, unit tests, duplication, ratchets), then fixes what fails. Pairs with deploy --quick. It never commits or deploys';
        checks_button.setAttribute('aria-label', 'Run every deploy check, then fix what fails');
        checks_button.style.cssText = image_button.style.cssText;
        checks_button.addEventListener('click', function () { start_skill(checks_button, 'ci-sweep'); });
        var picker = document.createElement('input');
        picker.type = 'file';
        picker.accept = 'image/png,image/jpeg,image/gif,image/webp';
        picker.multiple = true;
        picker.hidden = true;
        image_button.addEventListener('click', function () { picker.click(); });
        picker.addEventListener('change', function () {
            var files = Array.prototype.slice.call(picker.files || []);
            if (files.length) attach_images(files);
            picker.value = '';
        });
        pane_row.appendChild(image_notice);
        pane_row.appendChild(image_button);
        pane_row.appendChild(draw_button);
        pane_row.appendChild(skills_button);
        controls.appendChild(pane_row);
        controls.appendChild(picker);
        document.body.appendChild(controls);
        draw_runnable(controls, agent_row, picker,
            [['sync-main', sync_button], ['deps-update', deps_button], ['ci-sweep', checks_button]]);
        window.addEventListener('paste', function (event) {
            if (!event.isTrusted || !event.clipboardData) return;
            var files = [];
            var items = event.clipboardData.items || [];
            for (var i = 0; i < items.length; i++) {
                if (items[i].kind !== 'file' || items[i].type.indexOf('image/') !== 0) continue;
                var file = items[i].getAsFile();
                if (file) files.push(file);
            }
            if (!files.length) return;
            event.preventDefault();
            event.stopImmediatePropagation();
            attach_images(files);
        }, true);
    }
    install_image_paste();

    /* ⤒ TOP, top middle: jump to the first prompt of the conversation, then ⤓ END to come back.
       The agent draws on the alternate screen, so there is no tmux or xterm history to scroll
       (inject_style's note): the jump is the agent's OWN scroll key, typed as the keyboard would
       type it. Ctrl+Home and Ctrl+End are Claude Code's scroll:top and scroll:bottom; an agent
       that binds neither ignores the key. Not a SOFT_KEYS token: the press is a click in this
       page, never a message from the page that frames it.

       The label follows the last press, not the scroll position, which this page cannot read.
       After a wheel scroll back down, ⤓ END is one press that does nothing, then ⤒ TOP again. */
    var SCROLL_JUMPS = {
        top: { key: '\x1b[1;5H', label: '⤒ TOP', next: 'end',
               title: 'Jump to the top of the conversation: the first prompt (Ctrl+Home)',
               aria: 'Jump to the top of the conversation' },
        end: { key: '\x1b[1;5F', label: '⤓ END', next: 'top',
               title: 'Back to the latest message (Ctrl+End)',
               aria: 'Jump back to the latest message' }
    };

    function install_scroll_jump() {
        var button = document.createElement('button');
        var pending = 'top';
        function paint() {
            var jump = SCROLL_JUMPS[pending];
            button.textContent = jump.label;
            button.title = jump.title;
            button.setAttribute('aria-label', jump.aria);
        }
        button.style.cssText = 'position:fixed;left:50%;top:4px;transform:translateX(-50%);z-index:21;font:12px monospace;color:#c6dddd;background:#091419;border:1px solid #527476;padding:4px 8px;cursor:pointer;white-space:nowrap;';
        button.addEventListener('click', function () {
            var term = window.term;
            if (!term || typeof term.input !== 'function' || term.options.disableStdin) {
                image_result('Terminal is not writable');
                return;
            }
            term.input(SCROLL_JUMPS[pending].key);
            pending = SCROLL_JUMPS[pending].next;
            paint();
            if (typeof term.focus === 'function') term.focus();
        });
        paint();
        document.body.appendChild(button);
    }
    install_scroll_jump();

    /* Shift+Enter → newline for Claude Code: send backslash+CR (its universal
       line continuation). window.term is set when ttyd's client opens the
       terminal (after this script parses) — poll until it exists. */
    var poll = setInterval(function () {
        var term = window.term;
        if (!term || typeof term.attachCustomKeyEventHandler !== 'function') return;
        clearInterval(poll);
        apply_font_size();  // config usually lands before the WebSocket opens
        load_personas();
        install_osc52_copy(term);
        term.attachCustomKeyEventHandler(function (ev) {
            if (ev.key === 'Enter' && ev.shiftKey && !ev.ctrlKey && !ev.altKey && !ev.metaKey) {
                if (ev.type === 'keydown') {
                    if (typeof term.input === 'function') term.input('\\\r');
                    else if (typeof term.paste === 'function') term.paste('\n');
                }
                return false;
            }
            return true;
        });
    }, 250);
})();
</script>
"""


def ttyd_version() -> str:
    """Parse `ttyd --version` (stdout looks like 'ttyd version 1.7.7-unknown')."""
    result = subprocess.run(["ttyd", "--version"], capture_output=True, text=True, check=False)
    match = re.search(r"(\d+\.\d+\.\d+\S*)", result.stdout + result.stderr)
    return match.group(1) if match else "unknown"


def patch_index_html(html: str, status_port: int = 7682, trusted_parents: tuple[str, ...] = ()) -> str | None:
    """Inject INJECTED_SCRIPT before the LAST </body>. None if the marker is missing."""
    head, sep, tail = html.rpartition("</body>")
    if not sep:
        return None
    return head + configured_script(status_port, trusted_parents) + sep + tail


def configured_script(status_port: int, trusted_parents: tuple[str, ...] = ()) -> str:
    """INJECTED_SCRIPT for one bridge: its status port, and the origins besides loopback whose
    frame may drive the page (`framing.framer_origins` of the operator's list)."""
    if not isinstance(status_port, int) or not 1 <= status_port <= 65535:
        raise ValueError("invalid image bridge port")
    if any(
        not isinstance(origin, str) or not re.fullmatch(r"https?://[a-z0-9.\-]+(:[0-9]+)?", origin) for origin in trusted_parents
    ):
        raise ValueError("a trusted parent must be a plain http(s) origin")
    script = INJECTED_SCRIPT
    for marker, value in (
        ("var image_bridge_port = 7682;", f"var image_bridge_port = {status_port};"),
        ("var trusted_parents = [];", f"var trusted_parents = {json.dumps(sorted(trusted_parents))};"),
    ):
        if script.count(marker) != 1:
            raise ValueError(f"INJECTED_SCRIPT lost its marker: {marker}")
        script = script.replace(marker, value)
    return script


def fetch_stock_index() -> str | None:
    """Launch a scratch ttyd on an OS-chosen free loopback port and fetch `/`."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        scratch_port = sock.getsockname()[1]
    # `sh -c true` never actually runs — ttyd spawns the command per WebSocket
    # connection and we only issue a plain HTTP GET.
    proc = subprocess.Popen(
        ["ttyd", "-p", str(scratch_port), "-i", "127.0.0.1", "sh", "-c", "true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + FETCH_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return None  # scratch ttyd died (port race lost, etc.)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{scratch_port}/", timeout=1) as response:
                    return response.read().decode("utf-8")
            except (OSError, ValueError):
                time.sleep(POLL_INTERVAL_SECONDS)
        return None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def get_patched_index_path(status_port: int = 7682, trusted_parents: tuple[str, ...] = ()) -> Path | None:
    """Best-effort path to the patched index, or None (caller then omits -I).

    Cache key includes the ttyd version and the configured script, so a script
    edit, a port change or a new allowed origin invalidates old caches automatically.
    """
    try:
        script_tag = hashlib.sha256(configured_script(status_port, trusted_parents).encode()).hexdigest()[:8]
        version = re.sub(r"[^A-Za-z0-9._-]", "_", ttyd_version())
        cache_path = CACHE_DIR / f"index-{version}-{script_tag}.html"
        if cache_path.is_file():
            return cache_path
        click.echo("tldrack: building patched ttyd index (first run for this ttyd/script version)…")
        stock = fetch_stock_index()
        if stock is None:
            click.echo(
                "tldrack: could not fetch ttyd's index page — serving the stock page (no key forwarding).",
                err=True,
            )
            return None
        patched = patch_index_html(stock, status_port, trusted_parents)
        if patched is None:
            click.echo("tldrack: unexpected ttyd index page (no </body>) — serving the stock page.", err=True)
            return None
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(patched)
        os.replace(tmp_name, cache_path)  # atomic — concurrent bridges never see a torn file
        return cache_path
    except Exception as err:  # noqa: BLE001 — never block the terminal over the nicety
        click.echo(f"tldrack: index patching failed ({err}) — serving the stock page.", err=True)
        return None
