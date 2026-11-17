#!/bin/sh
# Claude Code PostToolUse (Write): relay the write for HTML artifact registration
# and the structural-create reminder. The plugin owns both decisions.
# Owner: agent_canvas/nudge.py — see the note there. Dumb on purpose: rules
# change in the plugin, never here. Prints nothing and exits 0 on every other path.
input=$(cat)
[ -n "$TMUX" ] || exit 0
session=$(tmux display-message -p '#S' 2>/dev/null)
case "$session" in
    ''|*[!A-Za-z0-9_-]*) exit 0 ;;
esac
port="${TLDRACK_STATUS_PORT:-7682}"
printf '%s' "$input" | curl -s -f -m 2 --noproxy '*' -X POST -H 'Content-Type: application/json' --data-binary @- \
    "http://127.0.0.1:${port}/plugin/agent-canvas/nudge?session=${session}" 2>/dev/null
exit 0
