---
name: readme-images
description: Refresh TLDRack README screenshots and annotations from configurable demo sessions, or revise its infographic. Use for README image upkeep and selected visual callouts; not for changing live session state.
---

# README images

Use this with an image-capable agent. The reusable part is the scene and capture
command; model selection stays with the operator. Work in the repository lane's own worktree.

## Choose the images

Honor the requested subset: the plain rack, annotated rack, infographic, or review-app images.
Do not regenerate an approved infographic just because the UI changed. The README's image links
are the inventory; follow them instead of guessing the newest filename.

When the operator supplies a screenshot and asks to use it as the base, follow
**Screenshot-based edits** below. Use the isolated capture for fresh standalone UI screenshots.

For an isolated rack pair, edit `scripts/readme-images/scene.json`:

- `sessions`: public, invented names and states. `parent` and `worker` connect a worker to its
  LEAD; omit both for independent CLI sessions. `branch` gives a worktree; `merged: true` on a
  finished row shows landed work. Keep the worker's run equal to its parent's run.
- `services` and `devices`: invented service readouts and local/remote resource meters. Every
  `svc-` session needs a matching service entry. Keep both sections visible in the capture.
- `focus`, `terminal`, and `canvas`: the selected session and its illustrative transcript,
  progress, worker table and file names. Keep these consistent with the session rows.
- `annotations`: the portions the operator wants explained, each with a Playwright `selector`,
  `title`, and `text`. Match existing UI elements. Missing targets fail the capture. Eight short
  callouts fit the default layout. Change the copy and selectors rather than paint fictional
  controls over the screenshot.

The default includes one LEAD, three workers, independent sessions, working and waiting states,
active and merged worktrees, Services, Devices, a terminal, the 3D office, files and a canvas. LEAD and coordinator
are different features; name the one actually shown. The example operator prompt gives the objective
and constraints; the LEAD response owns planning the waves and assigning workers. Keep any deferred
validation consistent across the transcript, worker states and canvas.

## Capture and inspect

Prerequisites: the UI's installed dependencies, Playwright Chromium, Python 3, and `cwebp`
(`brew install webp` on macOS). From the repository root:

```bash
node scripts/readme-images/capture.mjs
```

Output defaults to `output/playwright/readme/`: raw PNGs, lossless WebPs, a text inspection dump,
resolved annotation targets and a source/image manifest. `--scene <path>` selects another scene;
`--out <directory>` selects the output directory.

Capture isolation: owned by `scripts/readme-images/capture.mjs` — see the note there.
Do not seed the operator's live bridge or tmux for a documentation capture. The app is real;
the sessions, PR states, terminal transcript and deliverable names are illustrative fixtures.
Say that beside the README screenshots. A demo can illustrate implemented UI states, not claim
that an unimplemented feature exists.

Inspect both PNGs at full size. Check that session names are readable, the LEAD's workers are
visibly grouped, independent sessions are separate, both branch states appear, and the selected
terminal and canvas agree. Check every annotation against what it points to. Use the real UI's
layout controls and scene data to fix composition; don't edit application code for a screenshot.
If the operator authorizes a live capture, inspect their actual rack using available browser tools.
Open display-only controls to show the relevant buttons. Keep a raw capture in ignored output and
check all visible text for private names, paths and account details before publishing. If browser
access fails, report that limitation and use the demo capture; do not describe it as a live shot.
Never add host-only controls to the standalone rack merely for its documentation.

The office uses WebGL: the scenario is repeatable, but animation/GPU rasterization is not promised
to be byte-identical across machines.

After visual review, copy the two WebPs and `manifest.json` into `docs/img/readme/`. Verify:

```bash
node scripts/readme-images/capture.mjs --out docs/img/readme --check
python3 tests/test_purity.py --files scripts/readme-images/capture.mjs scripts/readme-images/scene.json .claude/skills/readme-images/SKILL.md docs/img/readme/manifest.json docs/img/readme/rack-demo.webp docs/img/readme/rack-annotated.webp README.md
```

The hash check reports staleness after changes to the scene, capture code, UI, schemas or canvas
renderer. It does not regenerate images in CI or silently rewrite README links.

## Screenshot-based edits

Treat the supplied screenshot as the edit target, not merely a style reference. Preserve its
column widths, toolbar, session controls, Services, Devices and detail pane. Add only the requested
example states, LEAD/worker group or canvas diagram. Use the imagegen skill for requested raster
edits. Replace private names and transcript content with public examples before publication.

Inspect all text and controls against the original. Save the approved plain and annotated variants
with an `-edited` suffix so the isolated captures and their manifest remain independently verifiable.
Store the exact edit prompt in ignored output; record image hashes and the edit method in
`docs/img/readme/edited-manifest.json`. Do not claim that a generated edit is a fresh live capture.
Label it beside the README images as an edited screenshot with illustrative content. Generative
editing is reviewed output, not a deterministic CI regeneration step.

## Infographic or illustrated annotations

For an authored raster revision, use the imagegen skill and built-in image tool. Inspect the current
image first; preserve its visual style and exact copy. Save a new version and compare every small
label before updating a README link. For screenshot edits explicitly requested by the operator, use the workflow above and check every
changed label. Otherwise preserve screenshot text.
The deterministic callouts above are the default for screenshot annotations; an illustrated variant
is optional when requested.

## Deliver

Keep README edits limited to the requested gallery change. Preserve old assets unless their removal
was requested. Run the changed-file purity scan and capture check, then prepare the PR. Register
final image previews through the agent-canvas skill's artifact shelf in the starting workspace.
