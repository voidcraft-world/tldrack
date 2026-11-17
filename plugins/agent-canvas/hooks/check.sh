#!/bin/sh
# Claude Code PostToolUse (Write|Edit): when an agent has just written its TYPED
# canvas, say what the plugin cut from it. Exit 2 puts stderr in front of the
# agent, which is how it learns a block was cut instead of hearing it from the user.
# Owner: agent_canvas/plugin.py `_check_written` — see the note there. Every write
# that is not a key-shaped .json leaves after one `case`; the plugin decides the
# rest. Exits 0 when the bridge is not answering: then nothing draws the canvas.
input=$(cat)
case "$input" in
    *'/'[0-9]*'-'[0-9]*'.json"'*) ;;
    *) exit 0 ;;
esac
port="${TLDRACK_STATUS_PORT:-7682}"
report=$(printf '%s' "$input" | curl -s -f -m 2 --noproxy '*' -X POST -H 'Content-Type: application/json' \
    --data-binary @- "http://127.0.0.1:${port}/plugin/agent-canvas/check" 2>/dev/null) || exit 0
[ -n "$report" ] || exit 0
printf '%s\n' "$report" >&2
exit 2
