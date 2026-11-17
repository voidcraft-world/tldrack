"""Execute the shipped clipboard transport in Node, with no browser/server."""

import json
import subprocess

import pytest

from tldrack.index_patch import configured_script, patch_index_html

HARNESS = r"""
const config = JSON.parse(process.argv[1]);
const listeners = {}, uploads = [], pasted = [], typed = [], elements = [];
function element() {
    const node = {style:{}, attrs:{}, listeners:{}, appendChild(child){ child.parent = this; },
        insertBefore(child){ child.parent = this; }, setAttribute(k, v){this.attrs[k] = v;},
        addEventListener(type, fn){this.listeners[type] = fn;}};
    elements.push(node); return node;
}
global.window = {location:{href:config.url || 'http://localhost:7681/?arg=codex'},
    term:{options:{disableStdin:!!config.readonly},paste(text){pasted.push(text);},
        input(text){typed.push(text);},focus(){},
        attachCustomKeyEventHandler(){}},
    addEventListener(type, fn){(listeners[type] ||= []).push(fn);}};
global.document = {createElement:element, head:element(), body:element(),
    addEventListener:window.addEventListener, documentElement:element()};
const intervals = [];
global.setInterval = (fn) => { intervals.push(fn); return 1; };
global.clearInterval = () => {};
global.FileReader = class {
    readAsDataURL(file){ this.result = 'data:image/png;base64,aW1hZ2U='; this.onload(); }
};
global.fetch = async (url, options) => {
    // The skills plugin's allow-list (index_patch.py `draw_runnable`): a read, never an upload.
    if (String(url).endsWith('/plugin/claude-skills/state')) {
        if (config.no_skills_plugin) throw new Error('connection refused');
        return {ok:true, json:async()=>({runnable:(config.runnable || []).map(key=>({key, skill:key, session:key}))})};
    }
    uploads.push({url, body:options.body === undefined ? null : JSON.parse(options.body)});
    if (config.changed) window.term = {};
    return {ok:!config.fail, json:async()=>config.fail ? {error:'Disk full'}
        : config.reply ? config.reply
        : {paste:config.draw ? 'Draw it ' : '/tmp/image.png '}};
};
"""

PROBE = r"""
let claimed = false, stopped = false;
const file = {type:'image/png', size:config.oversize ? 9000000 : 10};
const item = {kind:'file',type:'image/png',getAsFile:()=>file};
const event = {isTrusted:!config.synthetic,clipboardData:{items:config.text ? [] : [item, ...(config.multiple ? [item] : [])]},
    preventDefault(){claimed=true;},stopImmediatePropagation(){stopped=true;}};
if (config.draw) elements.find(e=>e.textContent === '◧ DRAW').listeners.click();
else if (config.skills) elements.find(e=>e.textContent === '◇ SKILLS').listeners.click();
else if (config.sync) {
    const button = elements.find(e=>e.textContent === '⟳ SYNC');
    for (let press = 0; press < config.sync; press++) button.listeners.click();
}
else if (config.top) {
    // One reference: the label changes with each press.
    const button = elements.find(e=>e.textContent === '⤒ TOP');
    for (let press = 0; press < config.top; press++) button.listeners.click();
}
else if (config.deps) elements.find(e=>e.textContent === '⇡ DEPS').listeners.click();
else if (config.checks) elements.find(e=>e.textContent === '✓ CHECKS').listeners.click();
else if (config.persona !== undefined) {
    // The terminal opens: the poll runs once, which loads the chips.
    for (const fn of intervals) fn();
    setTimeout(()=>{ if (config.persona) elements.find(e=>e.attrs['aria-label'] === config.persona + ' mode').listeners.click(); }, 5);
}
else if (config.messages) {
    // Who posts: the frame that holds this page (`parent`), or any other window, from `origin`.
    window.parent = config.top_level ? window : {};
    window.dispatchEvent = () => {};
    global.Event = class {};
    for (const message of config.messages) {
        const source = message.parent ? window.parent : {};
        for (const fn of listeners.message || []) fn({source, origin:message.origin, data:message.data});
    }
}
else for (const fn of listeners.paste || []) fn(event);
const on_page = (node) => { while (node.parent) node = node.parent; return node === document.body; };
setTimeout(()=>process.stdout.write(JSON.stringify({claimed,stopped,uploads,pasted,typed,
    font_size:window.term?.options?.fontSize ?? null,
    drawn:elements.filter(e=>e.textContent && on_page(e)).map(e=>e.textContent),
    notices:elements.map(e=>e.textContent).filter(Boolean),
    checked:elements.filter(e=>e.attrs['aria-checked'] === 'true').map(e=>e.attrs['aria-label'])})), 20);
"""


def run_paste(trusted_parents=(), **config):
    script = configured_script(7792, tuple(trusted_parents)).split("<script>", 1)[1].split("</script>", 1)[0]
    result = subprocess.run(
        ["node", "-e", HARNESS + script + PROBE, json.dumps(config)], capture_output=True, text=True, check=True, timeout=10
    )
    return json.loads(result.stdout)


def test_image_paste_transports_bytes_and_inserts_path_without_enter():
    result = run_paste(multiple=True)
    assert result["claimed"] and result["stopped"]
    assert result["uploads"] == [{"url": "http://localhost:7792/images", "body": {"data": "aW1hZ2U="}}] * 2
    assert result["pasted"] == ["/tmp/image.png "] * 2


@pytest.mark.parametrize("config", [{"text": True}, {"synthetic": True}])
def test_text_paste_and_synthetic_events_are_untouched(config):
    result = run_paste(**config)
    assert not result["claimed"] and not result["stopped"]
    assert result["uploads"] == result["pasted"] == []


@pytest.mark.parametrize(
    "config,message",
    [
        ({"fail": True}, "Disk full"),
        ({"oversize": True}, "Image is empty or exceeds 8 MB"),
        ({"readonly": True}, "Terminal is not writable"),
    ],
)
def test_failure_is_visible_and_never_types_into_terminal(config, message):
    result = run_paste(**config)
    assert result["pasted"] == []
    assert message in result["notices"]


def test_remote_upload_uses_viewed_host_and_mapped_port():
    result = run_paste(url="https://mac.tail.example/?arg=codex")
    assert result["uploads"][0]["url"] == "https://mac.tail.example:8443/images"
    result = run_paste(url="https://mac.tail.example/?imageBridgePort=9443")
    assert result["uploads"][0]["url"] == "https://mac.tail.example:9443/images"


def test_patch_has_the_configured_status_port():
    assert "var image_bridge_port = 7792;" in patch_index_html("<body></body>", 7792)
    with pytest.raises(ValueError):
        configured_script("7792;alert(1)")


def test_patch_has_the_operators_origins_and_nothing_that_could_leave_the_script():
    patched = patch_index_html("<body></body>", 7792, ("https://rack.example", "https://mac.tail.example:8443"))
    assert 'var trusted_parents = ["https://mac.tail.example:8443", "https://rack.example"];' in patched
    for bad in ("https://x</script><script>alert(1)//", 'https://x";alert(1)//', "javascript:alert(1)", "https://[fd7a::1]"):
        with pytest.raises(ValueError):
            configured_script(7792, (bad,))


KEY = {"type": "voidcraft-terminal-key", "key": "ctrl_c"}


def test_soft_keys_come_only_from_the_frame_that_holds_the_page_on_a_trusted_origin():
    sent = [
        ("http://localhost:7682", True),  # the rack page
        ("http://127.0.0.1:5173", True),  # a dev page on loopback, any port
        ("http://[::1]:7682", True),
        ("https://rack.example", True),  # an origin the operator allowed
        ("https://evil.example", True),  # a hostile parent (its frame-ancestors refusal is the first lock)
        ("http://localhost:7682", False),  # a frame BESIDE the terminal, even on loopback
        ("http://localhost.evil.example", True),
        ("https://rack.example:8443", True),  # another port is another origin
        ("null", True),
    ]
    result = run_paste(
        trusted_parents=["https://rack.example"],
        messages=[{"origin": origin, "parent": parent, "data": KEY} for origin, parent in sent],
    )
    assert result["typed"] == ["\x03"] * 4


def test_a_page_nobody_frames_takes_no_posts_and_config_obeys_the_same_rule():
    assert run_paste(top_level=True, messages=[{"origin": "http://localhost:7682", "parent": True, "data": KEY}])["typed"] == []
    config = {"type": "voidcraft-terminal-config", "font_size": 30}
    assert run_paste(messages=[{"origin": "https://evil.example", "parent": True, "data": config}])["font_size"] is None
    assert run_paste(messages=[{"origin": "http://localhost:7682", "parent": True, "data": config}])["font_size"] == 30


@pytest.mark.parametrize("url", ["http://localhost:7681/?arg=codex", "http://localhost:7681/?arg=attach:codex"])
def test_draw_asks_the_canvas_plugin_for_this_sessions_request_and_pastes_it_without_enter(url):
    result = run_paste(draw=True, url=url)
    assert result["uploads"] == [{"url": "http://localhost:7792/plugin/agent-canvas/draw?session=codex", "body": None}]
    assert result["pasted"] == ["Draw it "]


@pytest.mark.parametrize(
    "config,message",
    [
        ({"url": "http://localhost:7681/"}, "No session named in this pane"),
        ({"url": "http://localhost:7681/?arg=a%20b"}, "No session named in this pane"),
        ({"readonly": True}, "Terminal is not writable"),
        ({"fail": True}, "Disk full"),
        ({"changed": True}, "Terminal changed; press DRAW again"),
    ],
)
def test_draw_never_types_without_a_session_a_plugin_answer_or_the_same_terminal(config, message):
    result = run_paste(draw=True, **config)
    assert result["pasted"] == []
    assert message in result["notices"]


@pytest.mark.parametrize("view,notice", [("skills", "Skills on the canvas"), ("canvas", "Canvas back")])
def test_skills_flips_this_panes_canvas_and_never_types(view, notice):
    result = run_paste(skills=True, url="http://localhost:7681/?arg=attach:codex", reply={"view": view})
    assert result["uploads"] == [
        {"url": "http://localhost:7792/plugin/agent-canvas/view", "body": {"session": "codex", "view": "toggle"}}
    ]
    assert result["pasted"] == []
    assert notice in result["notices"]


@pytest.mark.parametrize(
    "config,message",
    [
        ({"url": "http://localhost:7681/"}, "No session named in this pane"),
        ({"fail": True}, "Disk full"),
    ],
)
def test_skills_says_why_when_it_cannot_flip(config, message):
    result = run_paste(skills=True, **config)
    assert result["pasted"] == []
    assert message in result["notices"]


def test_sync_does_nothing_on_one_press():
    result = run_paste(sync=1)
    assert result["uploads"] == [] and result["pasted"] == []
    assert "CONFIRM SYNC?" in result["notices"]


@pytest.mark.parametrize(
    "created,notice",
    [
        (True, "sync-main started: see its row in the rack"),
        (False, "sync-main is already running: see its row in the rack"),
    ],
)
def test_sync_starts_its_own_session_on_the_second_press_and_never_types(created, notice):
    result = run_paste(sync=2, reply={"skill": "sync-main", "session": "sync-main", "created": created})
    assert result["uploads"] == [{"url": "http://localhost:7792/plugin/claude-skills/run", "body": {"skill": "sync-main"}}]
    assert result["pasted"] == []
    assert notice in result["notices"]


def test_sync_failure_is_visible():
    assert "Disk full" in run_paste(sync=2, fail=True)["notices"]


def test_merge_prs_has_no_button_of_its_own():
    # ⟳ SYNC runs merge-prs as its first half; a second button would race a second agent on main.
    assert "⇄ MERGE PRS" not in run_paste(sync=1)["notices"]


@pytest.mark.parametrize(
    "created,notice",
    [
        (True, "deps-update started: see its row in the rack"),
        (False, "deps-update is already running: see its row in the rack"),
    ],
)
def test_deps_starts_its_own_session_on_one_press_and_never_types(created, notice):
    result = run_paste(deps=True, reply={"skill": "deps-update", "session": "deps-update", "created": created})
    assert result["uploads"] == [{"url": "http://localhost:7792/plugin/claude-skills/run", "body": {"skill": "deps-update"}}]
    assert result["pasted"] == []
    assert notice in result["notices"]


def test_deps_failure_is_visible():
    assert "Disk full" in run_paste(deps=True, fail=True)["notices"]


def test_checks_starts_the_ci_sweep_on_one_press_and_never_types():
    result = run_paste(checks=True, reply={"skill": "ci-sweep", "session": "ci-sweep", "created": True})
    assert result["uploads"] == [{"url": "http://localhost:7792/plugin/claude-skills/run", "body": {"skill": "ci-sweep"}}]
    assert result["pasted"] == []
    assert "ci-sweep started: see its row in the rack" in result["notices"]


CATALOG = [
    {"id": "ship", "glyph": "⚡", "label": "SHIP", "title": "t"},
    {"id": "sprawl", "glyph": "⧉", "label": "SPRAWL", "title": "t"},
]


def test_persona_chips_load_from_the_bridge_catalog_once_the_terminal_opens():
    result = run_paste(
        persona="",
        url="http://localhost:7681/?arg=attach:codex",
        reply={"session": "codex", "persona": None, "personas": CATALOG},
    )
    assert result["uploads"] == [{"url": "http://localhost:7792/persona?session=codex", "body": None}]
    assert "⚡ SHIP" in result["notices"] and "⧉ SPRAWL" in result["notices"]
    assert "MODE" in result["notices"] and "▮▯" in result["notices"] and "▮▮" in result["notices"]
    assert result["checked"] == [] and result["pasted"] == []


def test_the_bridges_pick_is_the_one_checked_mode():
    result = run_paste(persona="", reply={"persona": "sprawl", "personas": CATALOG})
    assert result["checked"] == ["SPRAWL mode"]


def test_persona_chip_picks_for_this_session_and_never_types():
    result = run_paste(persona="SPRAWL", reply={"persona": None, "personas": CATALOG})
    assert result["uploads"][1] == {"url": "http://localhost:7792/persona", "body": {"session": "codex", "persona": "sprawl"}}
    assert result["pasted"] == []


def test_pressing_the_active_persona_turns_it_off():
    result = run_paste(persona="SHIP", reply={"persona": "ship", "personas": CATALOG})
    assert result["uploads"][1]["body"] == {"session": "codex", "persona": None}


def test_no_session_in_the_url_draws_no_chips():
    result = run_paste(persona="", url="http://localhost:7681/", reply={"personas": CATALOG})
    assert result["uploads"] == []
    assert "⚡ SHIP" not in result["notices"]


SKILL_BUTTONS = {"⟳ SYNC", "⇡ DEPS", "✓ CHECKS"}


def test_no_skill_button_is_drawn_until_the_operator_allows_a_skill():
    drawn = set(run_paste(text=True)["drawn"])
    assert not drawn & SKILL_BUTTONS and {"+ IMAGE", "◧ DRAW", "◇ SKILLS"} <= drawn


def test_only_the_runnable_skills_get_a_button():
    assert set(run_paste(text=True, runnable=["deps-update"])["drawn"]) & SKILL_BUTTONS == {"⇡ DEPS"}
    assert set(run_paste(text=True, runnable=["sync-main", "ci-sweep", "something-else"])["drawn"]) & SKILL_BUTTONS == {
        "⟳ SYNC",
        "✓ CHECKS",
    }


def test_no_skills_plugin_means_no_skill_buttons_and_the_pane_still_works():
    result = run_paste(no_skills_plugin=True)
    assert not set(result["drawn"]) & SKILL_BUTTONS and result["pasted"] == ["/tmp/image.png "]


CTRL_HOME, CTRL_END = "\x1b[1;5H", "\x1b[1;5F"


def test_top_types_the_agents_own_scroll_keys_and_flips_to_end():
    one = run_paste(top=1)
    assert one["typed"] == [CTRL_HOME] and "⤓ END" in one["drawn"] and "⤒ TOP" not in one["drawn"]
    two = run_paste(top=2)
    assert two["typed"] == [CTRL_HOME, CTRL_END] and "⤒ TOP" in two["drawn"]
    assert one["uploads"] == one["pasted"] == []


def test_top_on_a_read_only_pane_says_so_and_stays_top():
    result = run_paste(top=1, readonly=True)
    assert result["typed"] == [] and "⤒ TOP" in result["drawn"]
    assert "Terminal is not writable" in result["notices"]
