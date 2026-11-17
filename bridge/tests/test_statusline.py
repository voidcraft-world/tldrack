from tldrack.main import build_statusline_settings, statusline_command
from tldrack.statusline import DEFAULT_STATUS_PORT, context_left_percent, render_statusline, resolve_port


# ── render_statusline ───────────────────────────────────────────────────


def test_render_full_payload_leads_with_context_and_drops_the_model():
    # The model is the rack row's job; repeating it under every pane was the
    # "model shown twice" the operator flagged.
    payload = {
        "model": {"display_name": "Opus 4.8"},
        "context_window": {"remaining_percentage": 62.4, "used_percentage": 37.6},
        "rate_limits": {
            "five_hour": {"used_percentage": 24.4},
            "seven_day": {"used_percentage": 41.0},
        },
    }
    assert render_statusline(payload) == "62% left · 5h 24% · wk 41%"


def test_render_model_is_only_the_fallback_when_nothing_else_is_known():
    assert render_statusline({"model": {"display_name": "Fable 5"}}) == "Fable 5"


def test_render_limits_only_has_no_label():
    payload = {"model": {"display_name": "Opus 4.8"}, "rate_limits": {"seven_day": {"used_percentage": 80}}}
    assert render_statusline(payload) == "wk 80%"


def test_render_empty_payload():
    assert render_statusline({}) == "claude"


def test_render_rounds_percentages():
    payload = {"rate_limits": {"five_hour": {"used_percentage": 24.6}}}
    assert render_statusline(payload) == "5h 25%"


def test_render_skips_non_numeric_percentage():
    # Never raises, never renders garbage — the segment is simply dropped.
    payload = {"rate_limits": {"five_hour": {"used_percentage": "41%"}, "seven_day": {"used_percentage": 10}}}
    assert render_statusline(payload) == "wk 10%"


def test_render_malformed_shapes_fall_back():
    assert render_statusline({"model": "not a dict", "rate_limits": "nope", "context_window": 5}) == "claude"


# ── context_left_percent ────────────────────────────────────────────────


def test_context_prefers_remaining_percentage():
    assert context_left_percent({"context_window": {"remaining_percentage": 61.6, "used_percentage": 10}}) == 62


def test_context_falls_back_to_used_percentage():
    assert context_left_percent({"context_window": {"used_percentage": 92.2}}) == 8


def test_context_null_early_in_a_session_and_after_compact():
    assert context_left_percent({"context_window": {"remaining_percentage": None, "used_percentage": None}}) is None
    assert context_left_percent({}) is None


def test_context_rejects_bools_and_clamps():
    assert context_left_percent({"context_window": {"remaining_percentage": True}}) is None
    assert context_left_percent({"context_window": {"remaining_percentage": 140}}) == 100
    assert context_left_percent({"context_window": {"used_percentage": 103}}) == 0


# ── resolve_port ────────────────────────────────────────────────────────


def test_resolve_flag_wins():
    assert resolve_port(["--status-port", "9000"], {"TLDRACK_STATUS_PORT": "8000"}) == 9000
    assert resolve_port(["--status-port=9001"], {}) == 9001


def test_resolve_env_status_port():
    assert resolve_port([], {"TLDRACK_STATUS_PORT": "8000"}) == 8000


def test_resolve_env_ttyd_port_plus_one():
    assert resolve_port([], {"TLDRACK_PORT": "7700"}) == 7701


def test_resolve_default():
    assert resolve_port([], {}) == DEFAULT_STATUS_PORT == 7682


def test_resolve_garbage_falls_through():
    assert resolve_port(["--status-port", "banana"], {"TLDRACK_STATUS_PORT": "x", "TLDRACK_PORT": "7700"}) == 7701


# ── settings stanza (from main.py, mirrors --print-hooks) ───────────────


def test_statusline_command_shape():
    command = statusline_command(7682)
    assert command.startswith("python3 ")
    assert command.endswith("statusline.py --status-port 7682")
    assert "tldrack" in command


def test_statusline_command_quotes_a_path_with_a_space():
    import shlex
    from pathlib import Path

    script = Path("/work/my code/tldrack/statusline.py")
    assert shlex.split(statusline_command(7682, script)) == ["python3", str(script), "--status-port", "7682"]


def test_build_statusline_settings_envelope():
    settings = build_statusline_settings(7682)
    assert set(settings) == {"statusLine"}
    assert settings["statusLine"]["type"] == "command"
    assert settings["statusLine"]["command"] == statusline_command(7682)
