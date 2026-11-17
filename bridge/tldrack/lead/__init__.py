"""The LEAD launch: a rack session that runs a planned feature through worker sessions.

The rule this package follows: the bridge keeps the state and does the watching; a coordinator
never polls and never keeps state only in its conversation.

Who owns what here:

- `runs.py` — runs, steps, worker questions and events in rack.sqlite, the step state machine, NEXT,
  and the reaction to an operator's answer (reopen, unpark), which commits with the answer.
- `close.py` — the verified close: the truth table a worker session must pass before the bridge
  kills it. Pure; the facts are gathered by the service.
- `watcher.py` — the zero-token watcher: live facts in, events out, one wake line per batch.
- `brief.py` — every text an agent reads: the worker prompt, the launch prompt, `lead status`, the wake.
- `routing.py` (core, `tldrack/routing.py`) — which profile a worker step runs on (rung 7).
- `subscription.py` — subscription logins only.
- `models.py` — the model a lead runs on and the weaker one its workers and subagents run on.
- `service.py` — the routes' logic, wired to the bridge by main.py.
- `names.py` — a lead's tmux name, built and recognised in one place (it wears a topic label, never a rename).
- `cli.py` — `tldrack lead …`, the only door agents use.
"""
