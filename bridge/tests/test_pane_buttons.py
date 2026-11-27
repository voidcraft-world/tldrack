"""Pane buttons: what a plugin's declaration becomes on `GET /pane-buttons` (pane_buttons.py)."""

import pytest

from tldrack import pane_buttons
from test_session_routes import handler  # noqa: F401


class Plugin:
    """A mounted plugin as the collector sees it: its routes and, when given, its declaration."""

    def __init__(self, name, buttons=None, routes=("pick",)):
        self.name = name
        self.route_names = routes
        if buttons is not None:
            self.pane_buttons = buttons if callable(buttons) else lambda: buttons

    def routes(self):
        return [f"/plugin/{self.name}/{route}" for route in self.route_names]


def button(label="+ SAMPLE", route="/plugin/sample/pick", **extra):
    return {"label": label, "route": route, **extra}


def collect(*plugins):
    return pane_buttons.collect({plugin.name: plugin for plugin in plugins})


def test_a_declared_button_is_served_with_its_plugin_and_nothing_it_did_not_declare():
    declared = button(title="Attach a sample", colour="red")
    assert collect(Plugin("sample", [declared])) == {
        "buttons": [{"plugin": "sample", "label": "+ SAMPLE", "title": "Attach a sample", "route": "/plugin/sample/pick"}],
        "refused": [],
    }


def test_no_plugin_and_a_plugin_that_declares_nothing_are_both_an_empty_answer():
    empty = {"buttons": [], "refused": []}
    assert pane_buttons.collect(None) == pane_buttons.collect({}) == collect(Plugin("quiet")) == empty


@pytest.mark.parametrize(
    "entry,reason",
    [
        ("+ SAMPLE", "label must be"),
        (button(label=""), "label must be"),
        (button(label="   "), "label must be"),
        (button(label="x" * 25), "label must be"),
        (button(label="two\nlines"), "label must be"),
        (button(label=7), "label must be"),
        (button(title="t" * 201), "title must be"),
        (button(title="a\x1b[31m"), "title must be"),
        (button(title=None), "title must be"),
        (button(route="/plugin/other/pick"), "route must be"),
        (button(route="/shutdown"), "route must be"),
        (button(route="/plugin/sample/pick/../../shutdown"), "route must be"),
        (button(route=None), "route must be"),
    ],
)
def test_a_malformed_button_is_refused_with_its_reason_and_the_next_one_still_draws(entry, reason):
    result = collect(Plugin("sample", [entry, button(label="+ GOOD")]))
    assert [found["label"] for found in result["buttons"]] == ["+ GOOD"]
    assert len(result["refused"]) == 1 and result["refused"][0]["plugin"] == "sample"
    assert result["refused"][0]["reason"].startswith(reason)


def raising():
    raise RuntimeError("boom")


@pytest.mark.parametrize(
    "declaration,reason", [(raising, "pane_buttons() raised RuntimeError"), ({"label": "x"}, "must return a list")]
)
def test_a_plugin_whose_declaration_fails_costs_only_its_own_buttons(declaration, reason):
    result = collect(Plugin("broken", declaration), Plugin("sample", [button()]))
    assert [found["plugin"] for found in result["buttons"]] == ["sample"]
    assert [refusal["plugin"] for refusal in result["refused"]] == ["broken"]
    assert reason in result["refused"][0]["reason"]


def test_a_plugin_draws_at_most_its_share_and_a_pane_at_most_the_total():
    def crowd(name):
        return Plugin(name, [button(label=f"+ {n}", route=f"/plugin/{name}/pick") for n in range(pane_buttons.PER_PLUGIN + 2)])

    result = collect(crowd("one"), crowd("two"), crowd("three"))
    assert [found["plugin"] for found in result["buttons"]] == ["one"] * 4 + ["two"] * 4
    assert len(result["buttons"]) == pane_buttons.TOTAL
    assert [refusal["plugin"] for refusal in result["refused"]] == ["one", "two", "three"]
    assert "at most 8" in result["refused"][2]["reason"]


def test_the_route_is_in_the_guarded_table_and_serves_the_mounted_plugins(handler):  # noqa: F811
    # In GET_ROUTES, so the Host and Origin guards of do_GET run ahead of it like every other read.
    assert handler.GET_ROUTES["/pane-buttons"] == "_handle_get_pane_buttons"
    handler._handle_get_pane_buttons()
    assert handler.recorded.status == 200 and handler.recorded.body == {"buttons": [], "refused": []}
    type(handler).plugins = {"sample": Plugin("sample", [button()])}
    handler._handle_get_pane_buttons()
    assert [found["route"] for found in handler.recorded.body["buttons"]] == ["/plugin/sample/pick"]
