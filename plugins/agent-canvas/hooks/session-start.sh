#!/bin/sh
# Claude Code SessionStart: tell an agent in the rack where its canvas is.
# Owner: agent_canvas/session_start.py (the text, the path and why the hook only
# relays) — see the note there. Prints only inside tmux AND when the plugin
# answers, so no session is told about a pane nobody draws. Always exits 0.
cat >/dev/null 2>&1
[ -n "$TMUX" ] || exit 0
session=$(tmux display-message -p '#S' 2>/dev/null)
case "$session" in
    ''|*[!A-Za-z0-9_-]*) exit 0 ;;
esac
port="${TLDRACK_STATUS_PORT:-7682}"
curl -s -f -m 2 --noproxy '*' "http://127.0.0.1:${port}/plugin/agent-canvas/session-start?session=${session}" 2>/dev/null
exit 0
