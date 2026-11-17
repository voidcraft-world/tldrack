# Inspirations

tldrack learned from the work below. Each row names what it took from that work.

No code below was copied into tldrack. Works that tldrack does contain, and their licences, are in
[`NOTICE`](../NOTICE); what it downloads or calls at run time is in [`lab/NOTICE`](../lab/NOTICE).

## Agent orchestrators

| Work | What tldrack took from it |
|---|---|
| [firstmate](https://github.com/kunchenguid/firstmate) by kunchenguid (MIT) | The biggest one. **Supervision costs no tokens**: a plain watcher sleeps on the fleet and wakes a model only for an event that needs action. The bridge works this way: hooks feed its state, and the LEAD is woken by one `<rack-wake …>` line, never by polling. Also: the coordinator never edits code; "ship" and "scout" crews; "evidence is never authorization"; a decision queue that comes back until someone closes it. |
| [herdr](https://github.com/herdrdev/herdr) ([herdr.dev](https://herdr.dev), Apache-2.0) | **Hooks alone leak status.** A turn interrupted with Esc stays busy, and an answered prompt stays "needs you", so the rack reads the agent's own session index as well as its hooks. Also: a terminal workspace that restores itself after a restart and gets you back to work fast. |
| [GhosttyEXTREME](https://github.com/steventsvik/GhosttyEXTREME) by steventsvik (AGPL-3.0; ideas only) | **One overview of every agent**, and acting from it only when the agent's own prompt is on screen. A per-launch secret on status, so text an agent prints cannot fake a state. |

## The agent canvas

| Work | What tldrack took from it |
|---|---|
| [OpenUI](https://github.com/thesysdev/openui) by thesysdev (MIT) | The generative-UI move behind the canvas: register typed components, tell the model exactly those, and check every document it writes. An invalid action is skipped and the rest still run. |
| [Lavish](https://github.com/kunchenguid/lavish-axi) by kunchenguid (MIT) | **Agent-written HTML, reviewed and answered in a browser.** An agent draws a page beside its terminal, and the operator answers its questions on that page. |

## Evaluation

| Work | What tldrack took from it |
|---|---|
| [Langfuse](https://github.com/langfuse/langfuse) (MIT core; ideas from the core only) | **A dataset is named by when it took its content**, so every Laya report says which version it scored, and promoting labels adds a version. Also: every model decision is a record that later reactions attach to, each naming its source (the decision ledger); training labels are checked by hand in a review queue (Laya review). |
| [lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness) by EleutherAI (MIT) | **A cached score is keyed by everything that produced it.** A probability from the CPU is not the one from MPS, so each Laya report is keyed by checkpoint digest, device and case id, and the nightly run does not score a checkpoint again when its last report has the same key. |

## The model

| Work | Role |
|---|---|
| [Laya](https://huggingface.co/convaiinnovations/laya) by Convai Innovations (Apache-2.0) | The base decision model tldrack fine-tunes to judge whether a finished turn needs the operator. |
| [ModernBERT-large](https://huggingface.co/answerdotai/ModernBERT-large) by Answer.AI (Apache-2.0) | The encoder inside Laya. |

## The terminal

| Work | Role |
|---|---|
| [tmux](https://github.com/tmux/tmux) (ISC) | Every agent session is a tmux session; the rack attaches, never owns. |
| [ttyd](https://github.com/tsl0922/ttyd) by tsl0922 (MIT) | Serves each terminal to the browser; the bridge patches its page for the rack's controls. |
| [xterm.js](https://github.com/xtermjs/xterm.js) (MIT) | The terminal inside that page, and inside the rack. |

## The layout

| Work | Role |
|---|---|
| [lot-map](https://github.com/alexeyw/lot-map) by Aleksei Volodin (MIT) | The activity classification and data helpers behind the rack's activity view; vendored with their licence (see `NOTICE`). TLDRack draws its own board in `ui/src/agent-rack/activity-map-renderer.ts`. |
