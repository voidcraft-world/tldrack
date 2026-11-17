/**
 * The pages' shared stylesheets, plus the runtime gate that makes them honest. Import this ONCE
 * from a page entry, before anything mounts (`agent-rack/mount.ts` and each `main.ts` do).
 *
 * WHY ONE MODULE. These imports have to travel together, and a requirement a module can
 * enforce should not be a paragraph every entry has to remember.
 *
 * WHAT WOULD BREAK WITHOUT THE GATE. `fui-panel.css` ships its motion with NO
 * `@media (prefers-reduced-motion)` block: it has one `.fui-motion-off` class instead of a
 * second media block restating every selector. So the stylesheet's whole reduced-motion story
 * is that class, and `config/fui-motion-flags.ts` is the only thing that writes it to `<html>`.
 * A page that took the CSS without the gate would run full motion at someone who asked the OS
 * for less.
 *
 * ORDER IS LOAD-BEARING. `tokens.css` precedes the sheet that reads its custom properties, and
 * the gate runs last because it publishes the `--fui-*-ms` values over the top of them at
 * module load, before Vue mounts anything. Do not reorder, and do not turn these into a
 * function call: a bare side-effect import cannot be forgotten, called twice, or called after
 * mount.
 *
 * NOT AN `@import`. These are imports from a `.ts` module, which keeps them clear of a Vite
 * trap: an `@import` inside a scoped `<style>` block is scoped too, and silently strips the
 * shared classes (`fui-panel.css` owns the full note).
 */
import './tokens.css';
import './fui-panel.css';
import '../config/fui-motion-flags.ts';
