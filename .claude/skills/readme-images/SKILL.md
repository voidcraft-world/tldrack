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

For the rack pair, edit `scripts/readme-images/scene.json`:

- `sessions`: public, invented names and states. `parent` and `worker` connect a worker to its
  LEAD; omit both for independent CLI sessions. `branch` gives a worktree; `merged: true` on a
  finished row shows landed work. Keep the worker's run equal to its parent's run.
- `focus`, `terminal`, and `canvas`: the selected session and its illustrative transcript,
  progress, worker table and file names. Keep these consistent with the session rows.
- `annotations`: the portions the operator wants explained, each with a Playwright `selector`,
  `title`, and `text`. Match existing UI elements. Missing targets fail the capture. Six short
  callouts fit the default layout. Change the copy and selectors rather than paint fictional
  controls over the screenshot.

The default includes one LEAD, three workers, independent sessions, working and waiting states,
active and merged worktrees, a terminal, the 3D office, files and a canvas. LEAD and coordinator
are different features; name the one actually shown.

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
The office uses WebGL: the scenario is repeatable, but animation/GPU rasterization is not promised
to be byte-identical across machines.

After visual review, copy the two WebPs and `manifest.json` into `docs/img/readme/`. Verify:

```bash
node scripts/readme-images/capture.mjs --out docs/img/readme --check
python3 tests/test_purity.py --files scripts/readme-images/capture.mjs scripts/readme-images/scene.json .claude/skills/readme-images/SKILL.md docs/img/readme/manifest.json docs/img/readme/rack-demo.webp docs/img/readme/rack-annotated.webp README.md
```

The hash check reports staleness after changes to the scene, capture code, UI, schemas or canvas
renderer. It does not regenerate images in CI or silently rewrite README links.

## Infographic or illustrated annotations

For an authored raster revision, use the imagegen skill and built-in image tool. Inspect the current
image first; preserve its visual style and exact copy. Save a new version and compare every small
label before updating a README link. Do not let generative editing alter UI text in a screenshot.
The deterministic callouts above are the default for screenshot annotations; an illustrated variant
is optional when requested.

## Deliver

Keep README edits limited to the requested gallery change. Preserve old assets unless their removal
was requested. Run the changed-file purity scan and capture check, then prepare the PR. Register
final image previews through the agent-canvas skill's artifact shelf in the starting workspace.
