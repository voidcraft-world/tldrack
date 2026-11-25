# @tldrack/ui

tldrack's pages, as one Vite + Vue app. The bridge serves the build at `/`; a host application
can import the same modules as a package.

| Page | What it is |
|---|---|
| `index.html` | The rack: every agent session as a row, beside one live terminal |
| `review.html` | Laya review: grade Laya's verdicts, one card or one conversation at a time |
| `diagram.html` | How the rack works, one diagram per tab (the rack's ⌗ HOW button) |
| `office.html` | Every agent at a desk, in 3D (the rack's office button) |
| `evals.html` | The eval results: one results file from `bench/results/`, eight panels |
| `settings.html` | The rack's settings: setup, features, the terminal's appearance, launch directories (the rack's ALL SETTINGS button) |

## Build and run

```bash
npm ci
npm run build        # writes dist/
npm test             # the unit tests, with node's own test runner
npm run typecheck    # vue-tsc
```

`tldrack up` runs the build when `dist/` is missing, and the bridge then serves it:
`http://127.0.0.1:7682/`. A page talks to the origin that served it, so there is nothing to
configure. Rebuild and reload the browser to see a change.

For hot reload, run `npm run dev` and point the page at a bridge. The dev server is not the
bridge, so the page has to be told where one is:

```
http://localhost:5173/?remote=http://127.0.0.1:7681&bridge=http://127.0.0.1:7682
```

`remote` is the ttyd origin and `bridge` the bridge's. The page remembers them, and the pages
it opens by name (settings, Laya review) talk to the same bridge; the rack's
⇄ SWITCH MACHINE forgets them. For a dev bridge use its ports (`7781` and `7791`).

## The office

The Canvas strip and `office.html` share one renderer. Wheel or pinch zooms; FIT floats over the scene.
Names are perspective-scaled 3D nameplates. They grow by at most 50% or shift a short distance only
when the projected desks, robots and nearer labels leave room. Farther overlapping names fade gently;
none are dropped. A short leader appears only when a label moves. The selected session keeps its bright
halo through furniture. Agent monitors are 50% larger in both dimensions.

The office keeps hardware antialiasing and supersamples the viewport, with an eight-million-pixel
budget for larger windows. Text uses double-resolution textures, mipmaps and anisotropic filtering;
floating names and floor inscriptions have a thin dark outline, and names have a dark translucent
backing. Rendering quality is owned by `src/agent-office/office-render-quality.ts`; texture painting
is owned by `src/engine/terminal/agent-office-scene.ts`.

Floor readouts reuse the open DEVICES section's cached session sample: each desk or service, each room,
and all sections combined. They include only sessions represented in the office; partial totals show
sample coverage. Missing CPU is an em dash, never zero. Closing DEVICES freezes these readings as stale.
The number definitions and sampling cost belong to `../plugins/devices/devices_plugin/sessions.py`.

Label placement runs only after camera, viewport, or layout changes, at most ten times a second, with
bounded local candidate searches and no minimum pixel size. Floor textures change only when the formatted numbers change. Neither
view starts an extra process sampler or network poller.

## The activity board

The selected session's activity view uses TLDRack's own rectangular SVG board: grouped tool tiles,
one current-activity connection, a context-use meter and a short trail of observed category changes.
The neutral-gray board measures its pane and redistributes the tiles when either dimension changes;
wide panes use a circuit layout, while taller panes use a grid. `src/agent-rack/activity-map-renderer.ts` owns drawing;
lot-map supplies the classification helpers and retains its MIT attribution in the view and `NOTICE`.
The input remains sampled last activity, so counts are observations, never tool-call totals or timings.

## The evals page

`evals.html` is static: it reads no bridge. The build bundles the results files in
`../bench/results/`, and the page shows the newest real run, or `fixture.json` (invented numbers,
marked on every panel) while there is none. `src/evals/evals-model.ts` owns the pick.

The public copy is `../docs/evals/index.html`, one self-contained file that opens from the file
system and from GitHub Pages, with the README's figures beside it:

```bash
npm run export:evals                 # docs/evals/index.html + docs/evals/figures/*.png (both themes)
npm run export:evals -- --no-figures # the page alone, on a machine with no Playwright browser
```

Run it after a change to the page or a new results file, and commit what it writes: the unit
tests fail on an export older than its sources. The figures need Playwright's browser once
(`npx playwright install chromium-headless-shell`). `?theme=light` or `?theme=dark` stamps a
theme over the OS setting.

## Another machine

A rack page can drive a bridge on another machine, typically a phone over a tailnet:
`…/index.html?remote=<host>`. The host must be named at build time, or the page refuses it:

```bash
VITE_ALLOWED_REMOTE_HOSTS=mymac.tail1234.ts.net npm run build
```

It is an exact-host list and it is empty by default. `src/agent-rack/rack-remote.ts` owns why
a suffix rule is not safe.

## As a package

A host application imports modules by path. `package.json` `exports` is the whole public
surface: one entry per module, each a TypeScript or Vue source file, so the host's bundler
compiles them (Vite with `@vitejs/plugin-vue` needs no extra setup).

```ts
import { mount_rack_window, mount_rack_frame } from '@tldrack/ui/agent-rack/mount.ts';

// The whole rack page, fed by the bridge that served it (what index.html does):
mount_rack_window('#app');

// The same page as a satellite of your application's main window, over the window bus:
mount_rack_window('#app', { feed: 'bus' });

// The list alone, inside a frame of your application, which owns the terminal:
mount_rack_frame('#app');
```

Both take `settings`, a render function for the rack's settings tray, where a host puts its
own controls.

What a host has to know:

- **Three transports.** `agent-rack/rack-http-transport.ts` (the page talks to a bridge),
  `agent-rack/rack-bus-transport.ts` (a satellite window: `companion/companion-bus.ts` lists
  the messages your main window must speak) and `agent-rack/rack-transport.ts`
  (`create_iframe_transport`: a framed rack posts to `window.parent`).
- **One network gate.** `engine/terminal/local-network-gate.ts` keeps its opt-in in module
  state. Import it from this package and nowhere else: a second copy is a second gate, and the
  rack's fetches consult this one.
- **Machinery sessions.** `config/service-session-flags.ts` lists the tmux name prefixes the
  rack files under SERVICES. Add your own before you mount.
- **Storage.** Every key is `tldrack:…` in `localStorage`. `storage/migrate-keys.ts` copies the
  older `voidcraft:…` names once per origin; the mounts import it first.
- **Styles.** A mount loads the rack's own stylesheets (`styles/boot.ts`). A component you
  mount yourself, such as `laya-review/LayaReviewDeck.vue`, expects them on the page too, plus
  `laya-review/laya-review.css`.
- **Wire strings do not change.** The window-bus channel and message names, the frame
  messages and the three terminal messages (`events/messages.ts`) are contracts with a host and
  with the bridge.

## Layout

| Directory | Holds |
|---|---|
| `src/agent-rack/` | The rack: the list, the page, the transports, and one client per bridge route |
| `src/engine/terminal/` | Pure logic the rack draws from: rows, cohorts, status, render options |
| `src/laya-review/`, `src/decide/` | Laya review and the Laya plugin's clients |
| `src/agent-rack-diagram/`, `src/agent-office/` | The two smaller pages |
| `src/config/` | Flags, as plain objects a page or a host may set before mount |
| `src/styles/` | The shared stylesheets and their one loader |
| `tests/unit/` | One file per module under test; `tests/register.mjs` is the loader hook |

Sources import each other by relative path with the extension written, so `node --test` loads
them with no bundler and no alias.
